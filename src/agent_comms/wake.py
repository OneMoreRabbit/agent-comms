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


def deliver_claude(mention: dict, panes: list[Pane]) -> str:
    agents = find_runtime_panes(panes, "claude")
    if not agents:
        running = ", ".join(sorted({p.command for p in panes})) or "nothing"
        return (
            f"queued: this seat declares model 'claude' but no Claude session is running "
            f"(panes: {running}). Per ADR-0009 §7e a message never starts an agent, so "
            "this waits in the inbox until a session next starts."
        )
    if len(agents) > 1:
        raise WakeError(
            "more than one Claude session is running on this seat "
            f"({', '.join(p.target for p in agents)}), so there is no single answer to "
            "which one this message is for. Delivering to a guess is the best-effort "
            "send constitution §9 forbids. Not delivered."
        )
    pane = agents[0]
    blocked = pane_blocked_reason(pane.target)
    if blocked is not None:
        raise WakeError(f"Claude session {pane.target} is not ready for a turn: {blocked}")
    deliver_to_pane(pane.target, compose_turn(mention))
    return f"delivered to {pane.target} (claude)"


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


def deliver_codex(mention: dict, thread: str | None) -> str:
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
    if not thread:
        raise WakeError(
            "this seat declares model 'codex' and a codex daemon is running, but no "
            "codex_thread is configured, so there is no way to say which session the "
            "message is for. `codex queue` needs a session name or UUID. Set "
            "codex_thread in ~/.comms/config.toml. Not guessing at a session."
        )
    if shutil.which("codex") is None:
        raise WakeError("this seat declares model 'codex' but the codex CLI is not on PATH.")

    result = _run(["codex", "queue", "--thread", thread, "--message", compose_turn(mention)])
    if result.returncode != 0:
        raise WakeError(
            f"codex queue failed for thread {thread!r} "
            f"({(result.stderr or result.stdout or '').strip()[:400]})"
        )
    return f"delivered to codex thread {thread}"


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
    codex_thread: str | None = None,
    agent_commands: tuple[str, ...] = DEFAULT_AGENT_COMMANDS,
) -> str:
    """Deliver a mention to this seat's declared runtime.

    `model` is a **free string** by §7g, so a future runtime needs no code change
    in the estate. An unrecognised value is therefore a real possibility, and it
    is reported rather than quietly falling back to the pane scan — falling back
    would resurrect exactly the guessing §7g removed, on a seat whose owner had
    taken the trouble to declare the answer.
    """
    declared = (model or "").strip()

    if declared.casefold() == "codex":
        return deliver_codex(mention, codex_thread)

    if declared.casefold() == "claude":
        return deliver_claude(mention, list_panes())

    if declared:
        raise WakeError(
            f"this seat declares model {declared!r}, and there is no delivery adapter for "
            "it. `model` is a free string so a new runtime needs no estate change — but it "
            "does need an adapter here. Not falling back to scanning tmux: the seat stated "
            "its runtime, and guessing past that is the failure ADR-0009 §7g removed."
        )

    return deliver_by_scan(mention, list_panes(), agent_commands)
