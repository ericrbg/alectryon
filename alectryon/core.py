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

from typing import Any, Dict, DefaultDict, Iterator, List, Tuple, Union, NamedTuple

from collections import namedtuple, defaultdict
from contextlib import contextmanager
from shlex import quote
from shutil import which
from subprocess import Popen, PIPE, check_output
from pathlib import Path
import textwrap
import re
import sys
import unicodedata

from . import sexp as sx

DEBUG = False
TRACEBACK = False

class UnexpectedError(ValueError):
    pass

def indent(text, prefix):
    if prefix.isspace():
        return textwrap.indent(text, prefix)
    text = re.sub("^(?!$)", prefix, text, flags=re.MULTILINE)
    return re.sub("^$", prefix.rstrip(), text, flags=re.MULTILINE)

def debug(text, prefix):
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")
    if DEBUG:
        print(indent(text.rstrip(), prefix), flush=True)

class GeneratorInfo(namedtuple("GeneratorInfo", "name version")):
    def fmt(self, include_version_info=True):
        return "{} v{}".format(self.name, self.version) if include_version_info else self.name

Hypothesis = namedtuple("Hypothesis", "names body type")
Goal = namedtuple("Goal", "name conclusion hypotheses")
Message = namedtuple("Message", "contents")
Sentence = namedtuple("Sentence", "contents messages goals")
Text = namedtuple("Text", "contents")

class Enriched():
    __slots__ = ()
    def __new__(cls, *args, **kwargs):
        if len(args) < len(getattr(super(), "_fields", ())):
            # Don't repeat fields given by position (it breaks pickle & deepcopy)
            kwargs = {"ids": [], "markers": [], "props": {}, **kwargs}
        return super().__new__(cls, *args, **kwargs)

def _enrich(nt):
    # LATER: Use dataclass + multiple inheritance; change `ids` and `markers` to
    # mutable `id` and `marker` fields.
    name = "Rich" + nt.__name__
    fields = nt._fields + ("ids", "markers", "props")
    # Using ``type`` this way ensures compatibility with pickling
    return type(name, (Enriched, namedtuple(name, fields)),
                {"__slots__": ()})

Goals = namedtuple("Goals", "goals")
Messages = namedtuple("Messages", "messages")

class Names(list): pass
RichHypothesis = _enrich(Hypothesis)
RichGoal = _enrich(Goal)
RichMessage = _enrich(Message)
RichCode = _enrich(namedtuple("Code", "contents"))
RichSentence = _enrich(namedtuple("Sentence", "input outputs annots prefixes suffixes"))

def b16(i):
    return hex(i)[len("0x"):]

class Gensym():
    # Having a global table of counters ensures that creating multiple Gensym
    # instances in the same session doesn't cause collisions
    GENSYM_COUNTERS: Dict[str, DefaultDict[str, int]] = {}

    def __init__(self, stem):
        self.stem = stem
        self.counters = self.GENSYM_COUNTERS.setdefault(stem, defaultdict(lambda: -1))

    def __call__(self, prefix):
        self.counters[prefix] += 1
        return self.stem + prefix + b16(self.counters[prefix])

@contextmanager
def nullctx():
    yield

class Backend:
    def __init__(self, highlighter):
        self.highlighter = highlighter

    def gen_fragment(self, fr): raise NotImplementedError()
    def gen_hyp(self, hyp): raise NotImplementedError()
    def gen_goal(self, goal): raise NotImplementedError()
    def gen_message(self, message): raise NotImplementedError()
    def highlight(self, s): raise NotImplementedError()
    def gen_names(self, names): raise NotImplementedError()
    def gen_code(self, code): raise NotImplementedError()
    def gen_txt(self, s): raise NotImplementedError()

    def highlight_enriched(self, obj):
        lang = obj.props.get("lang")
        with self.highlighter.override(lang=lang) if lang else nullctx():
            return self.highlight(obj.contents)

    def _gen_any(self, obj):
        if isinstance(obj, (Text, RichSentence)):
            self.gen_fragment(obj)
        elif isinstance(obj, RichHypothesis):
            self.gen_hyp(obj)
        elif isinstance(obj, RichGoal):
            self.gen_goal(obj)
        elif isinstance(obj, RichMessage):
            self.gen_message(obj)
        elif isinstance(obj, RichCode):
            self.gen_code(obj)
        elif isinstance(obj, Names):
            self.gen_names(obj)
        elif isinstance(obj, str):
            self.gen_txt(obj)
        else:
            raise TypeError("Unexpected object type: {}".format(type(obj)))

class Asset(str):
    def __new__(cls, fname, _gen):
        return super().__new__(cls, fname)

    def __init__(self, _fname, gen):
        super().__init__()
        self.gen = gen

class Position(namedtuple("Position", "fpath line col")):
    def as_header(self):
        return "{}:{}:{}:".format(self.fpath or "<unknown>", self.line, self.col)

class Range(namedtuple("Range", "beg end")):
    def as_header(self):
        assert self.end is None or self.beg.fpath == self.end.fpath
        beg = "{}:{}".format(self.beg.line, self.beg.col)
        end = "{}:{}".format(self.end.line, self.end.col) if self.end else ""
        pos = ("({})-({})" if end else "{}:{}").format(beg, end)
        return "{}:{}:".format(self.beg.fpath or "<unknown>", pos)

class PosStr(str):
    def __new__(cls, s, *_args):
        return super().__new__(cls, s)

    def __init__(self, _s, pos, col_offset):
        super().__init__()
        self.pos, self.col_offset = pos, col_offset

class View(bytes):
    def __getitem__(self, key):
        return memoryview(self).__getitem__(key)

    def __init__(self, s):
        super().__init__()
        self.s = s

class PosView(View):
    NL = b"\n"

    def __new__(cls, s):
        bs = s.encode("utf-8")
        # https://stackoverflow.com/questions/20221858/
        return super().__new__(cls, bs) if isinstance(s, PosStr) else View(bs)

    def __init__(self, s):
        super().__init__(s)
        self.pos, self.col_offset = s.pos, s.col_offset

    def __getitem__(self, key):
        return memoryview(self).__getitem__(key)

    def translate_offset(self, offset):
        r"""Translate a character-based `offset` into a (line, column) pair.
        Columns are 1-based.

        >>> text = "abc\ndef\nghi"
        >>> s = PosView(PosStr(text, Position("f", 3, 2), 5))
        >>> s.translate_offset(0)
        Position(fpath='f', line=3, col=2)
        >>> s.translate_offset(10) # col=3, + offset (5) = 8
        Position(fpath='f', line=5, col=8)
        """
        nl = self.rfind(self.NL, 0, offset)
        if nl == -1: # First line
            line, col = self.pos.line, self.pos.col + offset
        else:
            line = self.pos.line + self.count(self.NL, 0, offset)
            prefix = bytes(self[nl+1:offset]).decode("utf-8", 'ignore')
            col = 1 + self.col_offset + len(prefix)
        return Position(self.pos.fpath, line, col)

    def translate_span(self, beg, end):
        return Range(self.translate_offset(beg),
                     self.translate_offset(end))

class Notification(NamedTuple):
    obj: Any
    message: str
    location: Range
    level: int

class Observer:
    def _notify(self, n: Notification):
        raise NotImplementedError()

    def notify(self, obj, message, location, level):
        self._notify(Notification(obj, message, location, level))

class StderrObserver(Observer):
    def __init__(self):
        self.exit_code = 0

    def _notify(self, n: Notification):
        self.exit_code = max(self.exit_code, n.level)
        header = n.location.as_header() if n.location else "!!"
        message = n.message.rstrip().replace("\n", "\n   ")
        level_name = {2: "WARNING", 3: "ERROR"}.get(n.level, "??")
        sys.stderr.write("{} ({}/{}) {}\n".format(header, level_name, n.level, message))

PrettyPrinted = namedtuple("PrettyPrinted", "sid pp")

def sexp_hd(sexp):
    if isinstance(sexp, list):
        return sexp[0]
    return sexp

def utf8(x):
    return str(x).encode('utf-8')

ApiAck = namedtuple("ApiAck", "")
ApiCompleted = namedtuple("ApiCompleted", "")
ApiAdded = namedtuple("ApiAdded", "sid loc")
ApiExn = namedtuple("ApiExn", "sids exn loc")
ApiMessage = namedtuple("ApiMessage", "sid level msg")
ApiString = namedtuple("ApiString", "string")

Pattern = type(re.compile("")) # LATER (3.7+): re.Pattern

class SerAPI():
    SERTOP_BIN = "sertop"
    DEFAULT_ARGS = ("--printer=sertop", "--implicit")

    # Whether to silently continue past unexpected output
    EXPECT_UNEXPECTED: bool = False

    MIN_PP_MARGIN = 20
    DEFAULT_PP_ARGS = {'pp_depth': 30, 'pp_margin': 55}

    @staticmethod
    def version_info(sertop_bin=SERTOP_BIN):
        bs = check_output([SerAPI.resolve_sertop(sertop_bin), "--version"])
        return GeneratorInfo("Coq+SerAPI", bs.decode('ascii', 'ignore').strip())

    def __init__(self, args=(), # pylint: disable=dangerous-default-value
                 fpath="-",
                 sertop_bin=SERTOP_BIN,
                 pp_args=DEFAULT_PP_ARGS):
        """Configure a ``sertop`` instance."""
        self.fpath = Path(fpath)
        self.args = [*args, *SerAPI.DEFAULT_ARGS, "--topfile={}".format(self.topfile)]
        self.sertop_bin = sertop_bin
        self.sertop = None
        self.next_qid = 0
        self.pp_args = {**SerAPI.DEFAULT_PP_ARGS, **pp_args}
        self.last_response = None
        self.observer : Observer = StderrObserver()

    def __enter__(self):
        self.reset()
        return self

    def __exit__(self, *_exn):
        self.kill()
        return False

    def kill(self):
        if self.sertop:
            self.sertop.kill()
            try:
                self.sertop.stdin.close()
                self.sertop.stdout.close()
            finally:
                self.sertop.wait()

    COQ_IDENT_START = (
        'lu', # Letter, uppercase
        'll', # Letter, lowercase
        'lt', # Letter, titlecase
        'lo', # Letter, others
        'lm', # Letter, modifier
        re.compile("""[
           \u1D00-\u1D7F # Phonetic Extensions
           \u1D80-\u1DBF # Phonetic Extensions Suppl
           \u1DC0-\u1DFF # Combining Diacritical Marks Suppl
           \u005F # Underscore
           \u00A0 # Non breaking space
         ]""", re.VERBOSE)
    )

    COQ_IDENT_PART = (
        *COQ_IDENT_START,
        'nd', # Number, decimal digits
        'nl', # Number, letter
        'no', # Number, other
        re.compile("\u0027") # Single quote
    )

    @staticmethod
    def valid_char(c, allowed):
        for pattern in allowed:
            if isinstance(pattern, str) and unicodedata.category(c).lower() == pattern:
                return True
            if isinstance(pattern, Pattern) and pattern.match(c):
                return True
        return False

    @classmethod
    def sub_chars(cls, chars, allowed):
        return "".join(c if cls.valid_char(c, allowed) else "_" for c in chars)

    @property
    def topfile(self):
        stem = self.fpath.stem
        if stem in ("-", ""):
            return "Top"
        stem = (self.sub_chars(stem[0], self.COQ_IDENT_START) +
                self.sub_chars(stem[1:], self.COQ_IDENT_PART))
        return stem + self.fpath.suffix

    @staticmethod
    def resolve_sertop(sertop_bin):
        path = which(sertop_bin)
        if path is None:
            msg = ("sertop not found (sertop_bin={});" +
                   " please run `opam install coq-serapi`")
            raise ValueError(msg.format(sertop_bin))
        return path

    def reset(self):
        self.kill()
        cmd = [self.resolve_sertop(self.sertop_bin), *self.args]
        debug(" ".join(quote(s) for s in cmd), '# ')
        # pylint: disable=consider-using-with
        self.sertop = Popen(cmd, stdin=PIPE, stderr=sys.stderr, stdout=PIPE)

    def next_sexp(self):
        """Wait for the next sertop prompt, and return the output preceding it."""
        response = self.sertop.stdout.readline()
        if not response: # pragma: no cover
            # https://github.com/ejgallego/coq-serapi/issues/212
            MSG = "SerTop printed an empty line.  Last response: {!r}."
            raise UnexpectedError(MSG.format(self.last_response))
        debug(response, '<< ')
        self.last_response = response
        try:
            return sx.load(response)
        except sx.ParseError: # pragma: no cover
            return response

    def _send(self, sexp):
        s = sx.dump([b'query%d' % self.next_qid, sexp])
        self.next_qid += 1
        debug(s, '>> ')
        self.sertop.stdin.write(s + b'\n') # type: ignore
        self.sertop.stdin.flush()

    @staticmethod
    def _deserialize_loc(loc):
        locd = dict(loc)
        return int(locd[b'bp']), int(locd[b'ep'])

    @staticmethod
    def _deserialize_hyp(sexp):
        meta, body, htype = sexp
        assert len(body) <= 1
        body = body[0] if body else None
        ids = [sx.tostr(p[1]) for p in meta if p[0] == b'Id']
        yield Hypothesis(ids, body, htype)

    @staticmethod
    def _deserialize_goal(sexp):
        name = dict(sexp[b'info'])[b'name']
        hyps = [h for hs in reversed(sexp[b'hyp'])
                for h in SerAPI._deserialize_hyp(hs)]
        return Goal(dict(name).get(b'Id'), sexp[b'ty'], hyps)

    @staticmethod
    def _deserialize_answer(sexp):
        tag = sexp_hd(sexp)
        if tag == b'Ack':
            yield ApiAck()
        elif tag == b'Completed':
            yield ApiCompleted()
        elif tag == b'Added':
            yield ApiAdded(sexp[1], SerAPI._deserialize_loc(sexp[2]))
        elif tag == b'ObjList':
            for tag, *obj in sexp[1]:
                if tag == b'CoqString':
                    yield ApiString(sx.tostr(obj[0]))
                elif tag == b'CoqExtGoal':
                    gobj = dict(obj[0])
                    for goal in gobj.get(b'goals', []):
                        yield SerAPI._deserialize_goal(dict(goal))
        elif tag == b'CoqExn':
            exndata = dict(sexp[1])
            opt_loc, opt_sids = exndata.get(b'loc'), exndata.get(b'stm_ids')
            loc = SerAPI._deserialize_loc(opt_loc[0]) if opt_loc else None
            sids = opt_sids[0] if opt_sids else None
            yield ApiExn(sids, exndata[b'str'], loc)
        else:
            raise UnexpectedError("Unexpected answer: {}".format(sexp))

    @staticmethod
    def _deserialize_feedback(sexp):
        meta = dict(sexp)
        contents = meta[b'contents']
        tag = sexp_hd(contents)
        if tag == b'Message':
            mdata = dict(contents[1:])
            # LATER: use the 'str' field directly instead of a Pp call
            yield ApiMessage(meta[b'span_id'], mdata[b'level'], mdata[b'pp'])
        elif tag in (b'FileLoaded', b'ProcessingIn',
                     b'Processed', b'AddedAxiom'):
            pass
        else:
            raise UnexpectedError("Unexpected feedback: {}".format(sexp))

    def _deserialize_response(self, sexp):
        tag = sexp_hd(sexp)
        if tag == b'Answer':
            yield from SerAPI._deserialize_answer(sexp[2])
        elif tag == b'Feedback':
            yield from SerAPI._deserialize_feedback(sexp[1])
        elif not self.EXPECT_UNEXPECTED: # pragma: no cover
            raise UnexpectedError("Unexpected response: {}".format(self.last_response))

    @staticmethod
    def highlight_substring(chunk, beg, end):
        prefix, substring, suffix = chunk[:beg], chunk[beg:end], chunk[end:]
        prefix = b"\n".join(bytes(prefix).splitlines()[-3:])
        suffix = b"\n".join(bytes(suffix).splitlines()[:3])
        return b"%b>>>%b<<<%b" % (prefix, substring, suffix)

    @staticmethod
    def _highlight_exn(span, chunk, prefix='    '):
        src = SerAPI.highlight_substring(chunk, *span)
        LOC_FMT = ("The offending chunk is delimited by >>>…<<< below:\n{}")
        return LOC_FMT.format(indent(src.decode('utf-8', 'ignore'), prefix))

    @staticmethod
    def _clip_span(loc, chunk):
        loc = loc or (0, len(chunk))
        return max(0, loc[0]), min(len(chunk), loc[1])

    @staticmethod
    def _range_of_span(span, chunk):
        return chunk.translate_span(*span) if isinstance(chunk, PosView) else None

    def _warn_on_exn(self, response, chunk):
        QUOTE = '  > '
        ERR_FMT = "Coq raised an exception:\n{}"
        msg = sx.tostr(response.exn)
        err = ERR_FMT.format(indent(msg, QUOTE))
        span = SerAPI._clip_span(response.loc, chunk)
        if chunk:
            err += "\n" + SerAPI._highlight_exn(span, chunk, prefix=QUOTE)
        err += "\n" + "Results past this point may be unreliable."
        self.observer.notify(chunk.s, err, SerAPI._range_of_span(span, chunk), level=3)

    def _collect_messages(self, typs: Tuple[type, ...], chunk, sid) -> Iterator[Any]:
        warn_on_exn = ApiExn not in typs
        while True:
            for response in self._deserialize_response(self.next_sexp()):
                if isinstance(response, ApiAck):
                    continue
                if isinstance(response, ApiCompleted):
                    return
                if warn_on_exn and isinstance(response, ApiExn):
                    if sid is None or response.sids is None or sid in response.sids:
                        self._warn_on_exn(response, chunk)
                if (not typs) or isinstance(response, typs): # type: ignore
                    yield response

    def _pprint(self, sexp, sid, kind, pp_depth, pp_margin):
        if sexp is None:
            return PrettyPrinted(sid, None)
        if kind is not None:
            sexp = [kind, sexp]
        meta = [[b'sid', sid],
                [b'pp',
                 [[b'pp_format', b'PpStr'],
                  [b'pp_depth', utf8(pp_depth)],
                  [b'pp_margin', utf8(pp_margin)]]]]
        self._send([b'Print', meta, sexp])
        strings: List[ApiString] = list(self._collect_messages((ApiString,), None, sid))
        if strings:
            assert len(strings) == 1
            return PrettyPrinted(sid, strings[0].string)
        raise UnexpectedError("No string found in Print answer")

    def _pprint_message(self, msg: ApiMessage):
        return self._pprint(msg.msg, msg.sid, b'CoqPp', **self.pp_args)

    def _exec(self, sid, chunk):
        self._send([b'Exec', sid])
        messages: List[ApiMessage] = list(self._collect_messages((ApiMessage,), chunk, sid))
        return [self._pprint_message(msg) for msg in messages]

    def _add(self, chunk):
        self._send([b'Add', [], sx.escape(chunk)])
        prev_end, spans, messages = 0, [], []
        responses: Iterator[Union[ApiAdded, ApiMessage]] = \
            self._collect_messages((ApiAdded, ApiMessage), chunk, None)
        for response in responses:
            if isinstance(response, ApiAdded):
                start, end = response.loc
                if start != prev_end:
                    spans.append((None, chunk[prev_end:start]))
                spans.append((response.sid, chunk[start:end]))
                prev_end = end
            elif isinstance(response, ApiMessage):
                messages.append(response)
        if prev_end != len(chunk):
            spans.append((None, chunk[prev_end:]))
        return spans, [self._pprint_message(msg) for msg in messages]

    def _pprint_hyp(self, hyp, sid):
        d = self.pp_args['pp_depth']
        name_w = max(len(n) for n in hyp.names)
        w = max(self.pp_args['pp_margin'] - name_w, SerAPI.MIN_PP_MARGIN)
        body = self._pprint(hyp.body, sid, b'CoqExpr', d, w - 2).pp
        htype = self._pprint(hyp.type, sid, b'CoqExpr', d, w - 3).pp
        return Hypothesis(hyp.names, body, htype)

    def _pprint_goal(self, goal, sid):
        ccl = self._pprint(goal.conclusion, sid, b'CoqExpr', **self.pp_args).pp
        hyps = [self._pprint_hyp(h, sid) for h in goal.hypotheses]
        return Goal(sx.tostr(goal.name) if goal.name else None, ccl, hyps)

    def _goals(self, sid, chunk):
        # LATER Goals instead and CoqGoal and CoqConstr?
        # LATER We'd like to retrieve the formatted version directly
        self._send([b'Query', [[b'sid', sid]], b'EGoals'])
        goals: List[Goal] = list(self._collect_messages((Goal,), chunk, sid))
        yield from (self._pprint_goal(g, sid) for g in goals)

    def _warn_orphaned(self, chunk, message):
        err = "Orphaned message for sid {}:".format(message.sid)
        err += "\n" + indent(message.pp, " >  ")
        err_range = SerAPI._range_of_span((0, len(chunk)), chunk)
        self.observer.notify(chunk.s, err, err_range, level=2)

    def run(self, chunk):
        """Send a `chunk` to sertop.

        A chunk is a string containing Coq sentences or comments.  The sentences
        are split, sent to Coq, and returned as a list of ``Text`` instances
        (for whitespace and comments) and ``Sentence`` instances (for code).
        """
        chunk = PosView(chunk)
        spans, messages = self._add(chunk)
        fragments, fragments_by_id = [], {}
        for span_id, contents in spans:
            contents = str(contents, encoding='utf-8')
            if span_id is None:
                fragments.append(Text(contents))
            else:
                messages.extend(self._exec(span_id, chunk))
                goals = list(self._goals(span_id, chunk))
                fragment = Sentence(contents, messages=[], goals=goals)
                fragments.append(fragment)
                fragments_by_id[span_id] = fragment
        # Messages for span n + δ can arrive during processing of span n or
        # during _add, so we delay message processing until the very end.
        for message in messages:
            fragment = fragments_by_id.get(message.sid)
            if fragment is None: # pragma: no cover
                self._warn_orphaned(chunk, message)
            else:
                fragment.messages.append(Message(message.pp))
        return fragments

    def annotate(self, chunks):
        with self as api:
            return [api.run(chunk) for chunk in chunks]

def annotate(chunks, sertop_args=()):
    r"""Annotate multiple `chunks` of Coq code.

    All fragments are executed in the same Coq instance, started with arguments
    `sertop_args`.  The return value is a list with as many elements as in
    `chunks`, but each element is a list of fragments: either ``Text``
    instances (whitespace and comments) or ``Sentence`` instances (code).

    >>> annotate(["Check 1."])
    [[Sentence(contents='Check 1.', messages=[Message(contents='1\n     : nat')], goals=[])]]
    """
    return SerAPI(args=sertop_args).annotate(chunks)
