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
