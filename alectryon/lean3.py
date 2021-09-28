# Copyright © 2019 Clément Pit-Claudel
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import json
import re
import tempfile
from collections import deque
from pathlib import Path
from typing import Dict, List, Any, Tuple, Iterable

from .core import TextREPLDriver, Positioned, Document, Hypothesis, Goal, Message, Sentence,\
    Text, cwd, Position

AstNode = Any
AstData = List[Dict[str, AstNode]]

class ProtocolError(ValueError):
    pass

class Lean3(TextREPLDriver):
    BIN = "lean"
    NAME = "Lean3"

    REPL_ARGS = ("--server", "-M 4096", "-T 100000") # Same defaults as vscode
    CLI_ARGS = ("--ast", "-M 4096", "-T 100000")

    ID = "lean3_repl"
    LANGUAGE = "lean3"

    TACTIC_CONTAINERS = ["begin", "{"]
    TACTIC_NODES = ["tactic", "<|>", ";"]
    DONT_RECURSE_IN = ["by"] + TACTIC_NODES

    def __init__(self, args=(), fpath="-", binpath=None):
        super().__init__(args=args, fpath=fpath, binpath=binpath)
        self.ast: AstData = []
        self.seq_num = -1
        self.messages: List[Any] = []

    def _wait(self):
        while True:
            js = json.loads(self._read())
            kind = js["response"]
            if kind in ("ok", "error"):
                assert js["seq_num"] == self.seq_num
            if kind == "ok":
                return js
            if kind == "error":
                raise ProtocolError(js["message"])
            if kind == "current_tasks":
                pass
            elif kind == "all_messages":
                self.messages += js["msgs"]
            else:
                raise ProtocolError("Unexpected response {!r}".format(js))

    def _query(self, command, **kwargs):
        self.seq_num += 1
        query = {"seq_num": self.seq_num, "command": command,
                 "file_name": self.fpath.name, **kwargs}
        self._write(json.dumps(query, indent=None), "\n")
        # the seq_num is irrelevant here; the c++ ignores it, and this basically just forces it to sync up all asyncs
        self._write(json.dumps({"seq_num": -1, "command": "sync_output"}, indent=None), "\n")
        return self._wait()

    # maybe worth memoising?
    def _get_descendants(self, idx: int) -> Iterable[int]:
        node = self.ast[idx]
        if node and "children" in node and node["kind"] not in self.DONT_RECURSE_IN:
            yield from node["children"]
            for cidx in node["children"]:
                yield from self._get_descendants(cidx)

    def _assign_parenting(self):
        for idx, node in enumerate(self.ast):
            if node and "children" in node:
                for cidx in node["children"]:
                    if self.ast[cidx]:
                        # There may be non-unique parenting (`;`s come to mind); drop at the earliest sign of trouble
                        assert ("parent" not in self.ast[cidx] or self.ast[cidx]["parent"] == idx)
                        self.ast[cidx]["parent"] = idx

    def _pos(self, line, col):
        assert col >= 0
        return Position(self.fpath, line, col)

    KIND_ENDER = {'begin': 'end', '{': '}'}

    def _find_sentence_ranges(self) -> Iterable[Tuple[Position, Position, int]]:
        """Get the ranges covering individual sentences of a Lean3 file.

        For tactic containers return two ranges (beginning and end).
        The extra variable indicates how deep in the stack we are.
        """
        indices = set(idx for n in range(len(self.ast)) for idx in self._get_descendants(n))
        for idx in indices:
            node = self.ast[idx]
            if not node or "start" not in node or "end" not in node or self._by_in_parents(node):
                continue
            kind = node["kind"]
            start, end = self._pos(*node["start"]), self._pos(*node["end"])
            if kind in self.TACTIC_NODES:
                yield start, end, 0
            elif kind in self.TACTIC_CONTAINERS:
                # Yield two spans corresponding to the delimiters of the container
                yield start, self._pos(start.line, start.col + len(kind)), 1
                yield self._pos(end.line, end.col - len(self.KIND_ENDER[kind])), end, -1

    def _by_in_parents(self, node: AstNode) -> bool:
        return node["kind"] == "by" or ("parent" in node and self._by_in_parents(self.ast[node["parent"]]))

    def _get_state_at(self, pos: Position):
        # future improvement: use widget stuff. may be unviable.
        info = self._query("info", line=pos.line, column=pos.col)
        record = info.get("record", {})
        return record.get("state")

    def _collect_sentences_and_states(self, doc: Document) \
        -> Iterable[Tuple[Tuple[int, int], Any]]:
        prev = self._pos(0, 0)
        last_span = None
        last_span_ender = False
        stack = 0
        for start, end, stack_mod in sorted(self._find_sentence_ranges()):
            stack += stack_mod
            assert 0 <= stack
            if end <= prev: # Skip overlapping ranges or those with `by` in their parents
                continue
            prev = end
            if stack == 0 and stack_mod == -1:
                if last_span:
                    yield (last_span, None)
                last_span_ender = True
            else:
                if last_span:
                    yield (last_span, None if last_span_ender else self._get_state_at(start))
                last_span_ender = False
            last_span = (doc.pos2offset(start), doc.pos2offset(end))

        if last_span:
            yield (last_span, None)

    # FIXME: this does not handle hypotheses with a body
    HYP_RE = re.compile(r"(?P<names>.*?)\s*:\s*(?P<type>(?:.*|\n )+)(?:,\n|\Z)")

    def _parse_hyps(self, hyps):
        for m in self.HYP_RE.finditer(hyps.strip()):
            names = m.group("names").split()
            typ = m.group("type").replace("\n  ", "\n")
            yield Hypothesis(names, None, typ)

    # [⊢|] vs ⊢ is for `conv` mode - currently unused but makes less brittle
    CCL_SEP_RE = re.compile("(?P<hyps>.*?)^[⊢|](?P<ccl>.*)", re.DOTALL | re.MULTILINE)
    CASES_RE = re.compile(r"^\s*case\s+([^:].*)")

    def _parse_goals(self, state):
        if not state or state == "no goals":
            return
        goals = state.split("\n\n")
        if len(goals) > 1:
            goals[0] = goals[0][goals[0].find('\n'):]  # Strip "`n` goals"
        for goal in goals:
            name = self.CASES_RE.match(goal)
            if name:
                name = name.group(0)
            m = self.CCL_SEP_RE.match(goal)
            yield Goal(name, m.group("ccl").replace("\n  ", "\n").strip(),
                       list(self._parse_hyps(m.group("hyps"))))

    def _find_sentences(self, doc: Document):
        for (beg, end), st in self._collect_sentences_and_states(doc):
            sentence = Sentence(doc[beg:end], [], list(self._parse_goals(st)))
            yield Positioned(beg, end, sentence)

    def partition(self, doc: Document):
        return Document.intersperse_text_fragments(doc.contents, self._find_sentences(doc))

    NON_WHITESPACE_RE = re.compile(r"[^\s]+")

    def _collect_message_span(self, msg, doc: Document):
        if "end_pos_line" in msg and "end_pos_col" in msg:
            return (doc.pos2offset(self._pos(msg["pos_line"], msg["pos_col"])),
                    doc.pos2offset(self._pos(msg["end_pos_line"], msg["end_pos_col"])),
                    msg)
        msg_loc = doc.pos2offset(self._pos(msg["pos_line"], msg["pos_col"]))
        # this is a heuristic; Lean3 doesn't give end poses
        probable_command = self.NON_WHITESPACE_RE.search(str(doc), msg_loc)
        if probable_command:
            return msg_loc, probable_command.end(), msg

    def _add_messages(self, segments, messages, doc):
        segments = deque(Document.with_boundaries(segments))
        messages = deque(sorted(filter(None, (self._collect_message_span(m, doc) for m in messages))))

        if not segments:
            return

        fr_beg, fr_end, fr = segments.popleft()

        while messages:
            beg, end, msg = messages[0]
            assert fr_beg <= beg <= end
            if beg < fr_end: # Message overlaps current fragment
                end = min(end, fr_end) # Truncate to current fragment
                if isinstance(fr, Text): # Split current fragment if it's text
                    if fr_beg < beg:
                        # print(f"prefix: {(fr_beg, beg, Text(fr.contents[:beg - fr_beg]))=}")
                        yield Text(fr.contents[:beg - fr_beg])
                        fr_beg, fr = beg, fr._replace(contents=fr.contents[beg - fr_beg:])
                    if end < fr_end:
                        # print(f"suffix: {(end, fr_end, Text(fr.contents[end - fr_beg:]))=}")
                        segments.appendleft(Positioned(end, fr_end, Text(fr.contents[end-fr_beg:])))
                        fr_end, fr = end, fr._replace(contents=fr.contents[:end - fr_beg])
                    fr = Sentence(contents=fr.contents, messages=[], goals=[])
                fr.messages.append(Message(msg["text"])) # Don't truncate existing sentences
                messages.popleft()
            else: # msg starts past fr; move to next fragment
                yield fr
                fr_beg, fr_end, fr = segments.popleft()

        yield fr
        for _, _, fr in segments:
            yield fr

    def _annotate(self, document: Document):
        self._query("sync", content=document.contents)
        fragments = self._add_messages(self.partition(document), self.messages, document)
        return list(document.recover_chunks(fragments))

    def annotate(self, chunks):
        """Annotate multiple ``chunks`` of Lean 3 code.

        >>> lean3 = Lean3()
        >>> lean3.annotate(["#eval 1 + 1", "#check nat"])
        [[Sentence(contents='#eval 1 + 1',
                   messages=[Message(contents='2')], goals=[])],
         [Sentence(contents='#check nat',
                   messages=[Message(contents='ℕ : Type')], goals=[])]]
        """
        document = Document(chunks, "\n")
        with cwd(self.fpath.parent.resolve()):
            # We use this instead of the ``NamedTemporaryFile`` API
            # because it works with Windows file locking.
            (fdescriptor, tmpname) = tempfile.mkstemp(suffix=".lean")
            try:
                tmpname = Path(tmpname).resolve()
                with open(fdescriptor, "w", encoding="utf-8") as tmp:
                    tmp.write(document.contents)
                try:
                    self.run_cli([str(tmpname)])
                except ValueError:
                    print("""The Lean compiler returned an error code. This likely means there is an error in your code
                          (intentional or unintentional). Please check that the output is as you'd expect.""")
                self.ast = json.loads(tmpname.with_suffix(".ast.json").read_text("utf8"))["ast"]
                self._assign_parenting()
            finally:
                tmpname.unlink(missing_ok=True)
                tmpname.with_suffix(".ast.json").unlink(missing_ok=True)
        with self as api:
            return api._annotate(document)
