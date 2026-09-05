"""Deliver a message into the seat's running agent, by the runtime's own mechanism.

ADR-0009 §7b–§7g. Three rules shape everything here:

- **Deliver into the session already running** (§7c). A message from a seat's
  arch is not pollution to quarantine — it is the work.
- **Never start an agent** (§7e). No agent means the message waits. That is what
  closes the unattended-overnight case structurally rather than by a counter.
- **Never guess** (§7g, constitution §10). The runtime a seat drives is
  *declared* in `~/.seat/seat.yml`, and each runtime is reached by its own
  mechanism. Scanning tmux for it failed twice in the pilot: it missed a headless
  codex, and it missed a Claude launched through a wrapper.

The pane scan survives only where `model` is absent, and says so when it runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

#: Pane commands that mean "an agent is running here", used only by the
#: last-resort fallback when the seat declares no `model`.
DEFAULT_AGENT_COMMANDS = ("claude", "codex")

#: Markers of a pane sitting on a selection prompt rather than an input prompt.
#: Observed live 2026-09-05: a freshly started Claude session offering "Try the
#: new fullscreen renderer?" reported `pane_current_command=claude` while
#: consuming keystrokes as menu navigation. The orchestrator hit the same class
#: independently with codex's per-directory trust prompt.
BLOCKING_MARKERS = (
    "Enter to confirm",
    "Esc to cancel",
    "Do you trust the files in this folder?",
    "Is this a project you trust?",
    "Do you trust the contents of this directory?",
)


@dataclass
class Pane:
    target: str
    command: str
    path: str
    pid: int = 0


class WakeError(Exception):
    """A wake that could not be completed, and that the sender must be told about."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=20, check=False)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return _run(["tmux", *args])


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


# ---------------------------------------------------------------------------
# finding where a runtime actually is
# ---------------------------------------------------------------------------

def list_panes() -> list[Pane]:
    """Every pane on this seat, with what is running in it and its pid."""
    if not tmux_available():
        raise WakeError(
            "tmux is not installed on this seat, so a message cannot be delivered to a "
            "Claude session. The estate runs Claude in tmux; without it there is no "
            "delivery path."
        )
    result = _tmux(
        "list-panes", "-a", "-F",
        "#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}\t"
        "#{pane_current_path}\t#{pane_pid}",
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "no server running" in stderr:
            return []
        raise WakeError(f"could not list tmux panes: {stderr or result.returncode}")
    panes = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            panes.append(
                Pane(target=parts[0], command=parts[1], path=parts[2],
                     pid=int(parts[3]) if parts[3].isdigit() else 0)
            )
    return panes


def _children(pid: int) -> list[int]:
    try:
        raw = open(f"/proc/{pid}/task/{pid}/children", encoding="utf-8").read()
    except OSError:
        return []
    return [int(p) for p in raw.split() if p.isdigit()]


def _descendants(pid: int, limit: int = 200) -> list[int]:
    seen, queue = [], [pid]
    while queue and len(seen) < limit:
        current = queue.pop()
        if current in seen:
            continue
        seen.append(current)
        queue.extend(_children(current))
    return seen


def _process_matches(pid: int, runtime: str) -> bool:
    """Is this process the named runtime?

    Checks the executable name first, then the command line. `comm` is truncated
    to 15 characters by the kernel, and a runtime launched via a wrapper shows up
    only in the arguments — which is exactly the case that broke the pilot.
    """
    token = runtime.casefold()
    try:
        comm = open(f"/proc/{pid}/comm", encoding="utf-8").read().strip().casefold()
    except OSError:
        return False
    if comm == token or os.path.basename(comm) == token:
        return True
    try:
        cmdline = open(f"/proc/{pid}/cmdline", encoding="utf-8").read().replace("\0", " ")
    except OSError:
        return False
    return any(os.path.basename(part).casefold() == token for part in cmdline.split())


def find_runtime_panes(panes: list[Pane], runtime: str) -> list[Pane]:
    """Panes running the named runtime anywhere in their process tree.

    **Not `pane_current_command`.** On `blocks/service` a live Claude session
    reported `bash`, because it was launched through a `bash -c '… claude …'`
    wrapper and remote-control backgrounds its server. The foreground command is
    a property of how the agent was launched; the process tree is a property of
    what is actually running (ADR-0009 §7g).
    """
    found = []
    for pane in panes:
        if pane.command.casefold() == runtime.casefold():
            found.append(pane)
        elif pane.pid and any(_process_matches(p, runtime) for p in _descendants(pane.pid)):
            found.append(pane)
    return found


def find_agent_panes(panes: list[Pane], agent_commands: tuple[str, ...]) -> list[Pane]:
    """The last-resort scan, used only when the seat declares no `model`."""
    wanted = {c.casefold() for c in agent_commands}
    return [p for p in panes if p.command.casefold() in wanted]


# ---------------------------------------------------------------------------
# the turn itself
# ---------------------------------------------------------------------------

def compose_turn(mention: dict) -> str:
    """The single line delivered to the agent.

    One line, deliberately: for the tmux path `send-keys` is a keyboard, and a
    newline is an Enter — each fragment would become its own turn. Long messages
    are pointed at rather than pasted.

    The sender is first and unmissable because §1a is only actionable if the
    agent knows who is asking: act only on your own arch seat; report, never
    comply, on an unexpected sender.
    """
    sender = mention.get("sender") or "unknown"
    topic = mention.get("topic") or "(no topic)"
    body = " ".join((mention.get("content") or "").split())
    permalink = mention.get("permalink") or ""
    mid = mention.get("id")

    limit = 1200
    if len(body) > limit:
        body = f"{body[:limit].rstrip()}… [truncated — full text: comms show {mid}]"

    return (
        f"[hub message from {sender} — topic '{topic}'] {body} "
        f"[cite {permalink} | reply: comms reply {mid} '<text>']"
    )


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------

def pane_blocked_reason(target: str) -> str | None:
    """Is this pane sitting on a prompt that would eat an injected message?

    A running agent is not necessarily an agent ready for a turn. This is a
    heuristic over markers seen in the wild, and it errs toward refusing: a
    refused wake is reported to the sender, while a swallowed one looks like a
    seat that read the message and ignored it.
    """
    captured = _tmux("capture-pane", "-t", target, "-p")
    if captured.returncode != 0:
        return (
            f"could not read the pane to check it is ready for input "
            f"({(captured.stderr or '').strip()})"
        )
    for marker in BLOCKING_MARKERS:
        if marker in captured.stdout:
            return (
                f"the session is showing a prompt that captures keystrokes "
                f"({marker!r}), so a message would be consumed as menu input rather "
                "than read as a turn. Someone needs to answer it in the session first."
            )
    return None


def deliver_to_pane(target: str, text: str) -> None:
    """Send one turn to a tmux pane.

    Text and Enter are separate calls. Sending the line with a trailing Enter in
    one call was unreliable on a live seat; separate calls were not — confirmed
    independently against both Claude and codex.
    """
    sent = _tmux("send-keys", "-t", target, "-l", text)
    if sent.returncode != 0:
        raise WakeError(f"send-keys failed for {target}: {(sent.stderr or '').strip()}")
    entered = _tmux("send-keys", "-t", target, "Enter")
    if entered.returncode != 0:
        raise WakeError(
            f"the message text reached {target} but Enter did not "
            f"({(entered.stderr or '').strip()}) — it is sitting unsent in the agent's "
            "input. Treating as a failed wake rather than assuming it will be noticed."
        )


def tmux_target_exists(target: str) -> bool:
    """Does the declared tmux target exist? A plain name is a session; `a:0.0` a pane."""
    probe = _tmux("list-panes", "-t", target, "-F", "#{pane_id}")
    return probe.returncode == 0 and bool(probe.stdout.strip())


def deliver_claude(mention: dict, session: str | None) -> str:
    """Deliver to Claude, at the declared target where there is one.

    **A declared target is authoritative and is never second-guessed.** If it is
    declared and not there, the agent is not running and the message queues —
    searching for some other Claude would be inferring past an answer the seat
    already gave, which is the §7g failure in a new place.

    `send-keys -t <session>` addresses that session's active pane, which is
    defined tmux behaviour rather than a guess about which pane is interesting.
    """
    if session:
        if not tmux_target_exists(session):
            return (
                f"queued: this seat declares Claude at tmux target {session!r} and no such "
                "target exists, so no session is running there. Per ADR-0009 §7e a message "
                "never starts an agent, and per §7g a declared target is not searched past "
                "— this waits in the inbox."
            )
        blocked = pane_blocked_reason(session)
        if blocked is not None:
            raise WakeError(f"Claude session {session} is not ready for a turn: {blocked}")
        deliver_to_pane(session, compose_turn(mention))
        return f"delivered to {session} (claude, declared target)"

    return _deliver_claude_by_search(mention, list_panes())


def _deliver_claude_by_search(mention: dict, panes: list[Pane]) -> str:
    """Fallback for a seat that declares `model` but not where the session is.

    Better than the pane-command scan it replaced — it finds a Claude hidden
    behind a launcher wrapper — but it is still a search, and a search is a guess
    that holds only while one answer is visible. Says so, so nobody mistakes it
    for the declared path.
    """
    agents = find_runtime_panes(panes, "claude")
    if not agents:
        running = ", ".join(sorted({p.command for p in panes})) or "nothing"
        return (
            f"queued: this seat declares model 'claude' but no Claude process was found "
            f"(panes: {running}). Per ADR-0009 §7e a message never starts an agent, so "
            "this waits in the inbox until a session next starts."
        )
    if len(agents) > 1:
        raise WakeError(
            "more than one Claude session is running on this seat "
            f"({', '.join(p.target for p in agents)}) and this seat declares no target, so "
            "there is no single answer to which one this message is for. Declare the "
            "session in ~/.seat/seat.yml rather than leaving it to be searched for."
        )
    pane = agents[0]
    blocked = pane_blocked_reason(pane.target)
    if blocked is not None:
        raise WakeError(f"Claude session {pane.target} is not ready for a turn: {blocked}")
    deliver_to_pane(pane.target, compose_turn(mention))
    return (
        f"delivered to {pane.target} (claude) BY SEARCH — no session declared for this "
        "seat; declare it in ~/.seat/seat.yml so the target is stated, not inferred"
    )


def codex_daemon_running() -> bool:
    """Is a codex app-server daemon alive on this seat?

    Codex under remote control has **no tmux pane** — its app-server persists on
    its own, which is precisely why the pane scan could not see it. The daemon's
    pid file is the signal instead.
    """
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    pid_file = os.path.join(home, "app-server-daemon", "app-server.pid")
    try:
        first = open(pid_file, encoding="utf-8").read().split()[0]
        pid = int(first)
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def deliver_codex(mention: dict, session: str | None) -> str:
    """Inject a turn through codex's own channel — no tmux involved.

    `codex queue --thread <session name or uuid> --message <text>` is codex's
    equivalent of `send-keys`, and it reaches the app-server directly.
    """
    if not codex_daemon_running():
        return (
            "queued: this seat declares model 'codex' but no codex app-server daemon is "
            "running. Per ADR-0009 §7e a message never starts an agent, so this waits in "
            "the inbox until a session next starts."
        )
    if not session:
        raise WakeError(
            "this seat declares model 'codex' and a codex daemon is running, but no "
            "session is declared, so there is no way to say which conversation the "
            "message is for. `codex queue` needs a session name or UUID. Declare it in "
            "~/.seat/seat.yml. Not guessing, and not enumerating sessions to pick one."
        )
    if shutil.which("codex") is None:
        raise WakeError("this seat declares model 'codex' but the codex CLI is not on PATH.")

    result = _run(["codex", "queue", "--thread", session, "--message", compose_turn(mention)])
    if result.returncode != 0:
        raise WakeError(
            f"codex queue failed for session {session!r} "
            f"({(result.stderr or result.stdout or '').strip()[:400]})"
        )
    return f"delivered to codex session {session} (declared target)"


def deliver_by_scan(mention: dict, panes: list[Pane], agent_commands: tuple[str, ...]) -> str:
    """The pre-§7g behaviour, kept only for a seat that declares no `model`.

    §7g allows this as a last resort and requires the path to be logged, because
    it is the mechanism that failed twice in the pilot.
    """
    agents = find_agent_panes(panes, agent_commands)
    if not agents:
        running = ", ".join(sorted({p.command for p in panes})) or "nothing"
        return (
            f"queued: no model declared for this seat and no agent pane found "
            f"(panes: {running}). Declare `model` in ~/.seat/seat.yml — the scan is a "
            "fallback and an unreliable one (ADR-0009 §7g)."
        )
    if len(agents) > 1:
        raise WakeError(
            "no model is declared for this seat and more than one agent pane is running "
            f"({', '.join(f'{p.target}={p.command}' for p in agents)}). This is exactly "
            "the case the scan gets wrong. Declare `model` in ~/.seat/seat.yml."
        )
    pane = agents[0]
    blocked = pane_blocked_reason(pane.target)
    if blocked is not None:
        raise WakeError(f"agent session {pane.target} is not ready for a turn: {blocked}")
    deliver_to_pane(pane.target, compose_turn(mention))
    return (
        f"delivered to {pane.target} ({pane.command}) VIA PANE SCAN — no model declared "
        "for this seat; declare `model` in ~/.seat/seat.yml (ADR-0009 §7g)"
    )


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def wake(
    mention: dict,
    model: str | None = None,
    session: str | None = None,
    agent_commands: tuple[str, ...] = DEFAULT_AGENT_COMMANDS,
) -> str:
    """Deliver a mention to this seat's declared runtime, at its declared target.

    Two facts, both the seat's to state (ADR-0009 §7g, constitution §10):
    **which runtime** it drives, and **where that runtime is** — a tmux target for
    Claude, a session name or UUID for codex. Neither is inferred when declared.

    `model` is a free string, so an unrecognised value is a real case rather than
    a defensive branch. It is reported, never scanned around: falling back would
    resurrect the guessing §7g removed, on a seat whose owner stated the answer.
    """
    declared = (model or "").strip()
    target = (session or "").strip() or None

    if declared.casefold() == "codex":
        return deliver_codex(mention, target)

    if declared.casefold() == "claude":
        return deliver_claude(mention, target)

    if declared:
        raise WakeError(
            f"this seat declares model {declared!r}, and there is no delivery adapter for "
            "it. `model` is a free string so a new runtime needs no estate change — but it "
            "does need an adapter here. Not falling back to scanning tmux: the seat stated "
            "its runtime, and guessing past that is the failure ADR-0009 §7g removed."
        )

    return deliver_by_scan(mention, list_panes(), agent_commands)
