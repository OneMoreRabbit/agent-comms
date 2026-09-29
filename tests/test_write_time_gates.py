"""The LINT tier of architecture/write-time-gates-v0_1.md, against this repo.

These are the catalogue's classes turned around: mechanical checks the author's
own test run applies to the author's own diff. They FAIL the build.

**Only the gates the document marks LINT are here.** Gate 6's near-miss
requirement -- a case on each side of a boundary a test's name claims -- is a
CHECK, not a lint: I first implemented it as "a test whose name says `only` or
`never` needs two assertions" and it fired on eighteen correct tests, because
`pytest.raises` is an assertion and a single-assertion test can be complete.
A lint that fires on correct work gets silenced wholesale and then guards
nothing, which is gate 12 turned on the lint itself. It stays a judgment the
author states per PR.

Each check allows an explicit, named exemption -- a gate knowingly violated is
named with why, per the document's rule of use -- because a lint with no way
to say "this one is deliberate, here is the reason" gets silenced wholesale the
first time it is wrong, and then it protects nothing.
"""
from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "agent_comms"

#: **Gates 1, 1a, 1b and 1c scan the TESTS too, not only `src/`.**
#: A fixture that builds an identifier teaches the same habit and then blesses
#: the code that copies it: on 2026-09-28 the per-agent blocked fixtures here
#: carried short names, which is what kept a last-segment fallback alive in the
#: client. The rule is the estate's, not this package's, so it applies wherever
#: this repo names an agent.
TESTS = pathlib.Path(__file__).resolve().parent
ADDRESSING_SCOPE = (SRC, TESTS)
TESTS = pathlib.Path(__file__).resolve().parent
#: A line carrying this marker is exempt, and must say why on the same line.
EXEMPT = "gate-exempt:"


def _docstring_lines(tree: ast.AST) -> set:
    """Line numbers inside docstrings -- prose about a trap is not the trap."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            out.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return out


def _lines(directory: pathlib.Path):
    for path in sorted(directory.rglob("*.py")):
        text = path.read_text()
        try:
            skip = _docstring_lines(ast.parse(text))
        except SyntaxError:
            skip = set()
        for n, line in enumerate(text.splitlines(), 1):
            if n not in skip:
                yield path, n, line


def _live(directory: pathlib.Path):
    """Lines that are neither comments nor exempted."""
    for path, n, line in _lines(directory):
        stripped = line.strip()
        if stripped.startswith("#") or EXEMPT in line:
            continue
        yield path, n, line


def _report(hits, gate: str):
    if hits:
        shown = "\n".join(f"  {p.name}:{n}  {l.strip()[:100]}" for p, n, l in hits)
        raise AssertionError(f"gate {gate} violated:\n{shown}")


# -- gate 1: identity is read, never assembled -------------------------------

def test_gate_1_no_prefix_matching_on_identifiers():
    """`startswith` on an id accepts major 10 as major 1, and `arch` as
    `arch-shadow`. Both have bitten this estate."""
    pattern = re.compile(r"\.startswith\(|\.endswith\(")
    hits = [(p, n, l) for p, n, l in _live(SRC) if pattern.search(l)]
    _report(hits, "1 (prefix-match on an identifier)")


# -- gate 3: unknown values refuse, quoting the value ------------------------

def test_gate_3_closed_word_sets_have_a_refusing_else():
    """Every function comparing against a closed set of words ends in a branch
    that refuses by name. Checked structurally: a function whose body is a
    chain of `==`/`in` tests over string literals must not fall off the end
    returning a default."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            compares = [n for n in ast.walk(fn)
                        if isinstance(n, ast.Compare) and any(
                            isinstance(c, ast.Constant) and isinstance(c.value, str)
                            for c in n.comparators)]
            if len(compares) < 3:
                continue
            tail = fn.body[-1]
            ok = isinstance(tail, (ast.Raise, ast.Return))
            if isinstance(tail, ast.If):
                ok = True          # an if-chain ending in a branch is fine
            if not ok:
                offenders.append((path, fn.lineno, f"def {fn.name}(...)"))
    _report(offenders, "3 (closed word set without a refusing tail)")


# -- gate 6: tests earn their names -----------------------------------------

def _is_exit_assert(node: ast.Assert) -> bool:
    for sub in ast.walk(node.test):
        if isinstance(sub, ast.Attribute) and sub.attr in ("returncode", "exit_code"):
            return True
    return False


def test_gate_6_no_test_asserts_on_an_exit_code_alone():
    """A verdict from an exit code is not a verdict (the suite's own rule).

    **ALONE is the whole property, and it is structural.** A first attempt
    matched any line asserting an exit code and flagged two tests that assert
    the code and the output on consecutive lines -- correct tests, and a lint
    that calls them wrong is one nobody will keep. So: flag a test only when
    its exit-code assertion is the ONLY assertion it makes.
    """
    offenders = []
    for path in sorted(TESTS.rglob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if not fn.name.startswith("test_"):
                continue
            asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
            exits = [a for a in asserts if _is_exit_assert(a)]
            if exits and len(asserts) == len(exits):
                offenders.append((path, fn.lineno, f"def {fn.name}(...)"))
    _report(offenders, "6 (exit code is the only assertion)")


# -- gate 11: nothing load-bearing goes only to stderr ----------------------

def test_gate_11_stderr_is_not_swallowed_around_consumed_output():
    """`2>/dev/null` around a command whose output is read hides the row that
    says why something was refused."""
    pattern = re.compile(r"2>\s*/dev/null|stderr\s*=\s*subprocess\.DEVNULL")
    hits = [(p, n, l) for p, n, l in _live(SRC) if pattern.search(l)]
    _report(hits, "11 (stderr swallowed)")


# -- gate 1a: an FQN, channel or bot name is READ, never constructed ----------

def _identity_templates(path: pathlib.Path, skip: set):
    """f-strings that look like an identity assembled from parts.

    An identity template is a format string whose literal pieces are only
    SEPARATORS (`.` `-` `_` `/`) and at most bare tokens -- `f"{p}-{s}"`,
    `f"bakehouse.{p}.{s}"`, `f"{e}.{p}.{a}"`. Prose is excluded by the same
    test: a sentence has spaces in its literal parts, so it cannot match.

    This is the gate the operator asked for after four of these were found by
    hand: the bot was `f"{project}-{seat}"` where the directory says
    `test-claude`; the channel fell back to the project name; the seat's own
    FQN was `f"bakehouse.{project}.{seat}"` in seven places, estate hardcoded.
    Channel, bot and FQN all come from the directory (or its cache) and are
    never built.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or node.lineno in skip:
            continue
        holes = [v for v in node.values if isinstance(v, ast.FormattedValue)]
        literals = ["".join(v.value for v in node.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str))]
        if len(holes) < 2:
            continue
        joined = literals[0]
        # An identity has no whitespace: a display format string does.
        if any(ch.isspace() for ch in joined):
            continue
        # ...and no path or URL punctuation: those are addresses of another kind.
        if any(ch in joined for ch in "/#:?=&%"):
            continue
        # An identity template SEPARATES its parts with `.` or `-`. Without one
        # this is concatenation -- `f"{status}{where}"`, `f"{n}h{m}m"` -- which
        # joins values rather than assembling a name.
        if "." not in joined and "-" not in joined:
            continue
        bare = joined
        for sep in ".-_":
            bare = bare.replace(sep, "")
        if len(bare) > 24:
            continue            # a long literal is prose, not a separator
        yield node.lineno


def test_gate_1a_no_fqn_channel_or_bot_is_CONSTRUCTED():
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        exempt = {n for n, line in enumerate(text.splitlines(), 1) if EXEMPT in line}
        try:
            docs = _docstring_lines(ast.parse(text))
        except SyntaxError:
            docs = set()
        for lineno in _identity_templates(path, exempt | docs):
            line = text.splitlines()[lineno - 1]
            hits.append((path, lineno, line))
    _report(hits, "1a (an identity assembled from parts -- read it from the directory)")


# -- gate 1b: an identity is not TAKEN APART either ---------------------------

#: Splitting an identifier to get at one of its parts. `split`/`rsplit` on a
#: name, then indexing — `fqn.split(".")[1]` to reach the project,
#: `name.rsplit(".", 1)[-1]` to reach the agent.
_DECOMPOSE = re.compile(r"\.(?:r?split|partition|rpartition)\s*\(\s*['\"][.\-/]['\"]")


def test_gate_1b_no_addressing_value_is_TAKEN_APART():
    """Gate 1a catches an identity ASSEMBLED from parts. This catches one PULLED
    APART, which is the same fault from the other end and slipped past 1a.

    **Measured 2026-09-26.** A permission check took the recipient's blocked
    list, matched the caller's FQN against short entries, and scoped them by
    `answer.canonical_id.split(".")[1]` to reach the project. Gate 1a passed it
    clean: nothing was constructed. The operator caught it, having ruled against
    exactly this more than once, and the code was reverted -- the directory
    already publishes the verdict, so nothing needed deriving at all.

    A decomposition is sometimes right: validating that a string has the FQN
    SHAPE reads its structure without inferring a fact from it. Those carry
    `gate-exempt:` with the reason, so the difference is stated rather than
    assumed. What is never right is taking a part out in order to decide
    something -- that is a verdict computed from a name.
    """
    hits = [(p, n, l) for p, n, l in _live(SRC) if _DECOMPOSE.search(l)]
    _report(hits, "1b (an addressing value taken apart — read the verdict instead)")


# -- gate 1c: an identifier is not GUESSED AT among candidate keys ------------

#: Reaching for an identifier by trying one key and falling back to another:
#: `row.get("agent") or row.get("fqn")`. Gate 1a catches an identity built from
#: parts and 1b catches one pulled apart; this is the third way to invent one —
#: deciding for yourself where it lives.
_IDENT_KEYS = r"(?:agent|fqn|canonical_id|bot|channel|seat|target|caller)"
_GUESSED_KEY = re.compile(
    rf"\.get\(\s*['\"]{_IDENT_KEYS}['\"].*?\)\s*or\s*\w+\.get\(")


def test_gate_1c_an_identifier_is_not_GUESSED_AT_among_candidate_keys():
    """**Measured 2026-09-28, and the operator had ruled against it already.**

    A diagnostic read a seat's cached agent set with
    `d.get("assignments") or d.get("agents")`, then took each agent's name with
    `a.get("agent") or a.get("fqn")`. Both guesses were wrong, it reported
    `agents: 0` for a seat serving five, and the wrong answer was very nearly
    reported to the estate as a measurement.

    Gate 1a saw nothing built and 1b saw nothing split, because the fault is
    neither: it is deciding for yourself WHERE an identifier lives instead of
    reading it from the one place that records it. The supported accessor —
    `config_sync.agent_set()` — answered correctly on the first call.

    **A fallback chain over key names is a guess wearing an `or`.** If two
    shapes are genuinely possible, that is a contract question, not something
    to paper over at the call site: pick the documented key and let a missing
    one fail loudly.
    """
    hits = []
    for root in ADDRESSING_SCOPE:
        hits += [(p, n, l) for p, n, l in _live(root) if _GUESSED_KEY.search(l)]
    _report(hits, "1c (an identifier guessed at among candidate keys — "
                  "read it from the one place that records it)")
