"""Deliver a message into the seat's running agent session.

The mechanism ADR-0009 §7b–§7f rules, and the piece that turns a mailbox into
coordination. Three rules shape everything here:

- **Deliver into the session already running** (§7c). An incoming message from a
  seat's arch is not pollution to be quarantined — it is the work. It arrives as
  a turn in the conversation that already holds the repo state and the task.
- **Never start an agent** (§7e). No session means the message waits for one.
  This is what keeps the overnight-runaway case closed structurally rather than
  by a counter: an agent runs only because a human started it.
- **Never guess** (§7f, constitution §9). A message sent to a shell prompt is
  typed at a shell — the "text on a terminal nobody reads" failure. If we cannot
  confirm we are talking to a live agent, that is a failure to report, not a
  best-effort send.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

#: Pane commands that mean "an agent is running here". Verified on a live seat:
#: an idle shell reports `bash`, the comms daemon reports `python`, and a running
#: Claude Code session reports `claude` (§7f). Configurable because the set is
#: empirical, not a law.
DEFAULT_AGENT_COMMANDS = ("claude", "codex")


@dataclass
class Pane:
    target: str
    command: str
    path: str


class WakeError(Exception):
    """A wake that could not be completed, and that the sender must be told about."""


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args], text=True, capture_output=True, timeout=15, check=False
    )


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def list_panes() -> list[Pane]:
    """Every pane on this seat, with what is actually running in it."""
    if not tmux_available():
        raise WakeError(
            "tmux is not installed on this seat, so a message cannot be delivered to a "
            "running agent. The estate runs agent sessions in tmux; without it there is "
            "no delivery path."
        )
    result = _tmux(
        "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}\t"
        "#{pane_current_command}\t#{pane_current_path}"
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "no server running" in stderr:
            return []
        raise WakeError(f"could not list tmux panes: {stderr or result.returncode}")
    panes = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            panes.append(Pane(target=parts[0], command=parts[1], path=parts[2]))
    return panes


def find_agent_panes(panes: list[Pane], agent_commands: tuple[str, ...]) -> list[Pane]:
    wanted = {c.casefold() for c in agent_commands}
    return [p for p in panes if p.command.casefold() in wanted]


#: Markers of a pane sitting on a selection prompt rather than an input prompt.
#: Observed live on 2026-09-05: a freshly started Claude session offering
#: "Try the new fullscreen renderer?" reported `pane_current_command=claude`
#: while consuming keystrokes as menu navigation.
BLOCKING_MARKERS = (
    "Enter to confirm",
    "Esc to cancel",
    "Do you trust the files in this folder?",
    "Is this a project you trust?",
)


def pane_blocked_reason(target: str) -> str | None:
    """Is this pane sitting on a prompt that would eat an injected message?

    **`pane_current_command` confirms an agent process, not an agent ready for a
    turn.** ADR-0009 §7f names the first-run trust prompt as this trap; a
    fullscreen-renderer prompt on a freshly started session does the same thing,
    which suggests the general case is *any* interstitial rather than one known
    dialog. The command guard alone is not enough.

    This is a heuristic over observed markers, and it is honest about that: it
    catches the cases seen, and cannot promise to catch a dialog nobody has met
    yet. It errs toward refusing, because a refused wake is reported to the
    sender while a swallowed one looks like a seat that read the message and
    ignored it.
    """
    captured = _tmux("capture-pane", "-t", target, "-p")
    if captured.returncode != 0:
        return (
            f"could not read the pane to check it is ready for input "
            f"({(captured.stderr or '').strip()})"
        )
    text = captured.stdout
    for marker in BLOCKING_MARKERS:
        if marker in text:
            return (
                f"the session is showing a prompt that captures keystrokes "
                f"({marker!r}), so a message would be consumed as menu input rather "
                "than read as a turn. Someone needs to answer it in the session first."
            )
    return None


def compose_turn(mention: dict) -> str:
    """The single line injected into the agent's session.

    One line, deliberately. `send-keys` is a keyboard, and a multi-line payload
    is multiple Enters — each fragment becoming its own turn, most of them
    meaningless. Long messages are pointed at rather than pasted.

    The sender is first and unmissable because §1a is only actionable if the
    agent knows who is asking: *act only on your own arch seat; report, never
    comply, on an unexpected sender.*
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


def deliver(target: str, text: str) -> None:
    """Send one turn to a pane.

    Text and Enter are separate calls. Sending the line with a trailing Enter in
    one call was unreliable on a live seat (§7f); sending the text, then Enter,
    was not. `-l` sends the payload literally, so a message containing something
    that looks like a key name is not interpreted as one — the message is
    untrusted text arriving at a terminal.
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


def wake(mention: dict, agent_commands: tuple[str, ...] = DEFAULT_AGENT_COMMANDS) -> str:
    """Deliver a mention to this seat's running agent.

    Returns a short description of what happened. Raises `WakeError` if the
    message could neither be delivered nor honestly queued — the sender is told,
    because otherwise they wait forever on a seat that never woke (§7b).
    """
    panes = list_panes()
    agents = find_agent_panes(panes, agent_commands)

    if not agents:
        running = ", ".join(sorted({p.command for p in panes})) or "nothing"
        return (
            f"queued: no agent session running on this seat (panes running: {running}). "
            "Per ADR-0009 §7e a message never starts an agent, so this waits in the inbox "
            "and is taken up when a session next starts."
        )

    if len(agents) > 1:
        raise WakeError(
            "more than one agent session is running on this seat "
            f"({', '.join(f'{p.target}={p.command}' for p in agents)}), so there is no "
            "single answer to which one this message is for. ADR-0009 §7d says never two "
            "agents in one seat; delivering to a guess would be exactly the best-effort "
            "send constitution §9 forbids. Not delivered."
        )

    pane = agents[0]
    blocked = pane_blocked_reason(pane.target)
    if blocked is not None:
        raise WakeError(f"agent session {pane.target} is not ready for a turn: {blocked}")

    deliver(pane.target, compose_turn(mention))
    return f"delivered to {pane.target} ({pane.command})"
