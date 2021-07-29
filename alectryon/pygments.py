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

import re
import sys
import warnings
from collections import deque
from textwrap import indent
from contextlib import contextmanager

import pygments
from pygments.token import Error, STANDARD_TYPES, Name, Operator
from pygments.filters import Filter, TokenMergeFilter, NameHighlightFilter
from pygments.formatters import HtmlFormatter, LatexFormatter # pylint: disable=no-name-in-module

from dominate.util import raw as dom_raw

from .pygments_lexer import CoqLexer
from .pygments_style import TangoSubtleStyle

LEXER = CoqLexer(ensurenl=False) # pylint: disable=no-member
LEXER.add_filter(TokenMergeFilter())
HTML_FORMATTER = HtmlFormatter(nobackground=True, nowrap=True, style=TangoSubtleStyle)
LATEX_FORMATTER = LatexFormatter(nobackground=True, nowrap=True, style=TangoSubtleStyle)
WHITESPACE_RE = re.compile(r"\A(\s*)(.*?)(\s*)\Z", re.DOTALL)

def add_tokens(tokens):
    """Register additional `tokens` to add custom syntax highlighting.

    `tokens` should be a dictionary, whose keys indicate a type of token and
    whose values are lists of strings to highlight with that token type.

    The return value is a list of Pygments filters.

    This is particularly useful to highlight custom tactics or symbols.  For
    example, if your code defines a tactic ``map_eq`` to decide map equalities,
    and two tactics ``map_simplify`` and ``map_subst`` to simplify map
    expressions, you might write the following:

    >>> filters = add_tokens({
    ...     'tacn-solve': ['map_eq'],
    ...     'tacn': ['map_simplify', 'map_subst']
    ... })
    """
    filters = []
    for kind, names in tokens.items():
        tokentype = LEXER.TOKEN_TYPES.get(kind)
        if not tokentype:
            raise ValueError("Unknown token kind: {}".format(kind))
        filters.append(NameHighlightFilter(names=names, tokentype=tokentype))
    for f in filters:
        LEXER.add_filter(f)
    return filters

@contextmanager
def added_tokens(tokens):
    """Temporarily register additional syntax-highlighting tokens.

    `tokens` should be as in ``add_tokens``.  This is intended to be used as a
    context manager.
    """
    added = add_tokens(tokens)
    try:
        yield
    finally:
        LEXER.filters[:] = [f for f in LEXER.filters if f not in added]

def _highlight(coqstr, lexer, formatter):
    # See https://bitbucket.org/birkenfeld/pygments-main/issues/1522/ to
    # understand why we munge the STANDARD_TYPES dictionary
    with munged_dict(STANDARD_TYPES, {Name: '', Operator: ''}):
        # Pygments' HTML formatter adds an unconditional newline, so we pass it only
        # the code, and we restore the spaces after highlighting.
        before, code, after = WHITESPACE_RE.match(coqstr).groups()
        return before, pygments.highlight(code, lexer, formatter).strip(), after

def highlight_html(coqstr):
    """Highlight a Coq string `coqstr`.

    Return a raw HTML string.  This function is just a convenience wrapper
    around Pygments' machinery, using a custom Coq lexer and a custom style.

    The generated code needs to be paired with a Pygments stylesheet, which can
    be generated by running the ``regen_tango_subtle_css.py`` script in the
    ``etc/`` folder of the Alectryon distribution.

    If you use Alectryon's command line interface directly, you won't have to
    jump through that last hoop: it renders and writes out the HTML for you,
    with the appropriate CSS inlined.  It might be instructive to consult the
    implementation of ``alectryon.cli.dump_html_standalone`` to see how the CLI
    does it.

    >>> str(highlight_html("Program Fixpoint a := 1."))
    '<span class="kn">Program Fixpoint</span> <span class="nf">a</span> := <span class="mi">1</span>.'
    """
    return dom_raw("".join(_highlight(coqstr, LEXER, HTML_FORMATTER)))

PYGMENTS_LATEX_PREFIX = r"\begin{Verbatim}[commandchars=\\\{\}]" + "\n"
PYGMENTS_LATEX_SUFFIX = r"\end{Verbatim}"

def highlight_latex(coqstr, prefix=PYGMENTS_LATEX_PREFIX, suffix=PYGMENTS_LATEX_SUFFIX):
    """Highlight a Coq string `coqstr`.

    Like ``highlight_html``, but return a plain LaTeX string.
    """
    before, tex, after = _highlight(coqstr, LEXER, LATEX_FORMATTER)
    assert tex.startswith(PYGMENTS_LATEX_PREFIX) and tex.endswith(PYGMENTS_LATEX_SUFFIX), tex
    body = tex[len(PYGMENTS_LATEX_PREFIX):-len(PYGMENTS_LATEX_SUFFIX)]
    return prefix + before + body + after + suffix

@contextmanager
def munged_dict(d, updates):
    saved = d.copy()
    d.update(updates)
    try:
        yield
    finally:
        d.update(saved)

class WarnOnErrorTokenFilter(Filter):
    """Print a warning when the lexer generates an error token."""

    def filter(self, _lexer, stream):
        history = deque(maxlen=80)
        for typ, val in stream:
            history.extend(val)
            if typ is Error:
                ell = '...' if len(history) == history.maxlen else ''
                context = ell + ''.join(history).lstrip()
                MSG = ("!! Warning: Unexpected token during syntax-highlighting: {!r}\n"
                       "!! Alectryon's lexer isn't perfect: please send us an example.\n"
                       "!! Context:\n{}")
                warnings.warn(MSG.format(val, indent(context, ' ' * 8)))
            yield typ, val

LEXER.add_filter(WarnOnErrorTokenFilter())

def replace_builtin_coq_lexer():
    """Monkey-patch pygments to replace the built-in Coq Lexer.

    https://stackoverflow.com/questions/40514205/ describes a way to register
    entry points dynamically, so we could use that to play nice with pygments
    architecture, but it wouldn't pick up our Lexer (it would stick with the
    built-in one).
    """ # FIXME replace the formatter too?
    from pygments.lexers import _lexer_cache
    from pygments.lexers._mapping import LEXERS
    (_mod, name, aliases, ext, mime) = LEXERS['CoqLexer']
    LEXERS['CoqLexer'] = ("alectryon.pygments_lexer", name, aliases, ext, mime)
    _lexer_cache.pop(name, None)
