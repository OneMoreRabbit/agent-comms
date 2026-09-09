"""Keeping a seat's one agent session alive.

The operator's requirement (2026-09-06): **one model instance per seat**, live
enough to receive a message and available to join from a remote-control session.

The distinction this module exists to preserve:

- **`comms wake` never starts anything.** ADR-0009 §7e, unchanged. A message
  arriving is not a reason for an agent to exist.
- **`comms session ensure` starts one if the seat has none** — because a
  supervisor said so, on a schedule, with no reference to any message. That is a
  standing policy, not a reaction, and it is the same shape as the estate already
  running a `comms daemon`.

Both statements have to stay true together. If waking ever gains the power to
start a session, §7e is gone whatever the code comments say.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .config import Settings
from .wake import (
    Pane,
    pane_blocked_reason,
    codex_daemon_running,
    codex_lock_threads,
    find_runtime_panes,
    list_panes,
    tmux_available,
)


class SessionError(Exception):
    """A seat's session could not be inspected or started."""


@dataclass
class SessionState:
    live: bool
    detail: str
    target: str | None = None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=30, check=False)


def _tmux_target_exists(target: str) -> bool:
    probe = _run(["tmux", "list-panes", "-t", target, "-F", "#{pane_id}"])
    return probe.returncode == 0 and bool(probe.stdout.strip())


def _seat_target(settings: Settings) -> str:
    """The tmux session name holding this seat's agent.

    Defaults to the seat name, so a seat that declares nothing still has one
    predictable place its agent lives — which is what "one instance per seat"
    needs in order to be checkable at all.
    """
    return settings.model_session or settings.identity.seat


def status(settings: Settings) -> SessionState:
    """Is this seat's one agent session live?"""
    model = (settings.model or "").strip().casefold()

    if model == "codex":
        if not codex_daemon_running():
            return SessionState(False, "no codex app-server daemon is running")
        threads = codex_lock_threads()
        wanted = settings.model_session
        if wanted:
            live = wanted in threads
            return SessionState(
                live,
                f"designated thread {wanted} is {'open' if live else 'not open'}",
                wanted,
            )
        if len(threads) == 1:
            return SessionState(True, f"one thread open ({threads[0]})", threads[0])
        if not threads:
            return SessionState(False, "no codex thread is open")
        return SessionState(
            False,
            f"{len(threads)} threads open and none designated — "
            "declare model_session so 'one instance per seat' is checkable",
        )

    if not tmux_available():
        return SessionState(False, "tmux is not installed, so no session can be held")

    target = _seat_target(settings)
    if settings.model_session and _tmux_target_exists(target):
        return SessionState(True, f"session {target} exists", target)

    panes: list[Pane] = list_panes()
    found = find_runtime_panes(panes, model or "claude")
    if len(found) == 1:
        target = found[0].target
        # "A session exists" is not "a message can reach it". A pane holding a
        # remote-control client has the process in its tree and no prompt, so
        # this used to report `deliverable` about a seat that was not — the same
        # false success `wake` was reporting one command away.
        blocked = pane_blocked_reason(target)
        if blocked is not None:
            return SessionState(False, f"{target} is not typeable: {blocked}", target)
        return SessionState(True, f"one {model or 'claude'} session ({target})", target)
    if not found:
        return SessionState(False, f"no {model or 'claude'} session is running")
    return SessionState(
        False,
        f"{len(found)} sessions running ({', '.join(p.target for p in found)}) — "
        "one instance per seat is the requirement, so this needs resolving by hand",
    )


def ensure(settings: Settings, workdir: str | None = None) -> str:
    """Start this seat's session if it has none. Idempotent.

    **Called by a supervisor, never by delivery.** Returns a description of what
    it did, so a supervisor's log says whether it acted or found things well.

    Refuses to start a second session: "one instance per seat" is the
    requirement, and a supervisor that quietly added a second would break the
    thing it was asked to maintain.
    """
    state = status(settings)
    if state.live:
        return f"already live: {state.detail}"

    if "threads open and none designated" in state.detail or "sessions running" in state.detail:
        raise SessionError(
            f"not starting anything: {state.detail}. More than one instance already "
            "exists, which is the condition this is supposed to prevent."
        )

    if not tmux_available():
        raise SessionError("tmux is not installed, so a session cannot be held open")

    model = (settings.model or "claude").strip().casefold()
    target = _seat_target(settings)
    workdir = workdir or os.path.expanduser("~/work")

    if model == "codex":
        if not codex_daemon_running():
            raise SessionError(
                "no codex app-server daemon is running, so a thread cannot be opened. "
                "The daemon is the estate's to run; starting one is not this client's job."
            )
        if not settings.model_session:
            raise SessionError(
                "no model_session is declared, so there is no designated thread to "
                "resume. Starting a fresh thread would create a second instance the "
                "operator has not designated — declare it in ~/.seat/seat.yml first."
            )
        if shutil.which("codex") is None:
            raise SessionError("the codex CLI is not on PATH")
        command = f"codex resume {settings.model_session}"
        target = f"agent-{settings.identity.seat}"
    else:
        if shutil.which("claude") is None:
            raise SessionError("the claude CLI is not on PATH")
        command = "claude"

    started = _run(["tmux", "new-session", "-d", "-s", target, "-c", workdir, command])
    if started.returncode != 0:
        raise SessionError(
            f"could not start the session in tmux: {(started.stderr or '').strip()}"
        )
    return f"started {model} session in tmux target {target} ({command})"
