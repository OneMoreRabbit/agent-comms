"""`comms` — the operator-facing CLI. Formatting only; logic lives in operations."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

import click

from . import __version__, operations
from .errors import CommsDisabled, CommsError, DaemonAlreadyRunning
from .wake import WakeError

#: Exit codes, so a consumer's supervisor can tell these apart mechanically.
#: 0 success, 1 fault, 3 "comms disabled", 4 "a daemon is already running".
#: 3 and 4 are states, not failures: an idempotent installer that starts the
#: daemon should treat 4 as success, and must not read it as a broken install.
#: 5 = the message was queued because no agent is running. Normal, not a fault.
EXIT_OK, EXIT_FAULT, EXIT_DISABLED, EXIT_ALREADY_RUNNING, EXIT_QUEUED = 0, 1, 3, 4, 5


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
def main() -> None:
    """Agent-to-agent comms for this seat.

    Delivery is outbound from the seat: this client opens the connection to the
    hub and holds it. Nothing listens here (ADR-0009 §7).
    """


@main.command()
def status() -> None:
    """Is comms on, off, or broken — and which."""
    st = operations.status()
    if not st.enabled:
        click.echo(f"comms: disabled\n\n{st.detail}")
        sys.exit(EXIT_DISABLED)
    if not st.ready:
        click.secho(f"comms: enabled but not usable ({st.tag})", fg="red", bold=True)
        click.echo(f"\n{st.detail}")
        sys.exit(EXIT_FAULT)
    daemon = st.daemon
    healthy = daemon is not None and daemon.running and not daemon.stale
    if healthy:
        click.secho("comms: ready", fg="green", bold=True)
    else:
        # Configured and not receiving is not "ready". Saying ready here is the
        # reassuring green light over a seat that is losing its messages.
        click.secho("comms: configured, but NOT RECEIVING", fg="red", bold=True)
    click.echo(f"  identity   {st.identity}")
    click.echo(f"  channel    {st.channel}")
    click.echo(f"  credential {st.credential}")
    if daemon is not None:
        click.secho(f"  daemon     {daemon.summary()}", fg=None if healthy else "red")
    if not healthy:
        sys.exit(EXIT_FAULT)


@main.command()
def doctor() -> None:
    """Run every connect-time check and report all of them.

    These are the commitments in contract §3 that a consumer cannot verify for
    itself, so this command exists to let one verify them anyway.
    """
    report = operations.preflight()
    if report.disabled:
        click.echo("comms: disabled — nothing to check.\n")
        click.echo(report.checks[0][2])
        sys.exit(EXIT_DISABLED)
    for name, passed, detail in report.checks:
        mark = click.style("PASS", fg="green") if passed else click.style("FAIL", fg="red", bold=True)
        click.echo(f"  {mark}  {name}")
        if detail:
            click.echo(f"        {detail}")
    for note in report.notes:
        click.echo(f"  note  {note}")
    for warning in report.warnings:
        click.secho(f"  WARN  {warning}", fg="yellow")
    if not report.ok:
        sys.exit(EXIT_FAULT)
    click.secho("\nall connect-time checks passed", fg="green")


@main.command()
@click.option("--all", "show_all", is_flag=True, help="Include messages already read.")
def inbox(show_all: bool) -> None:
    """Mentions addressed to this seat."""
    rows = operations.inbox(unread_only=not show_all)
    if not rows:
        click.echo("nothing pending")
        return
    for m in rows:
        flag = " " if m.read else "*"
        click.echo(f"{flag} {m.id:>8}  {m.when}  {m.sender}  [{m.topic}]  ({m.reason})")


@main.command()
@click.argument("message_id", type=int)
def show(message_id: int) -> None:
    """One mention in full, with the permalink to cite it by."""
    m = operations.show(message_id)
    if m is None:
        raise click.ClickException(f"no message {message_id} in the local store")
    click.echo(f"from    {m.sender}\nwhen    {m.when}\nchannel {m.channel}\ntopic   {m.topic}")
    click.echo(f"reached me by  {m.reason}")
    click.echo(f"cite    {m.permalink}\n\n{m.content}")


@main.command()
@click.argument("message_id", type=int)
@click.argument("content")
def reply(message_id: int, content: str) -> None:
    """Reply in the mention's own topic."""
    operations.reply(message_id, content)
    click.echo("sent")


@main.command()
@click.option("--to", required=True,
              help="The seat this message is for, by plain name (agent-skeleton).")
@click.option("--subject", default=None, help="Subject; the topic becomes '<to>: <subject>'.")
@click.option("--topic", default=None,
              help="Continue an existing topic instead of starting one. Still needs --to.")
@click.argument("content")
def send(to: str, subject: str | None, topic: str | None, content: str) -> None:
    """Post to this seat's project channel, addressed to a named seat.

    You name the seat; this client spells the address:

        comms send --to agent-skeleton --subject 'the ask' 'body text'

    gives the topic `agent-skeleton: the ask` and a real `@**agent-skeleton**`
    mention — both routes a recipient matches on, so it does not matter which.

    The body is never read or rewritten: a seat name typed in prose is prose.
    The name in --to is checked against the hub first, and a seat that does not
    exist or is not in this channel is refused rather than posted to.
    """
    operations.send(content, to=to, subject=subject, topic=topic)
    click.echo("sent")


@main.command()
@click.option("--message-id", type=int, help="Wake with a message already in the store.")
def wake(message_id: int | None) -> None:
    """Deliver a message into this seat's running agent session.

    Reads the mention as JSON on stdin, which is what `notify_command` provides,
    or takes --message-id to replay one from the store.

    Exit codes: 0 delivered, 5 queued (no agent running — normal, per ADR-0009
    §7e, which says a message never starts an agent), 1 could not be delivered
    and the sender was told.
    """
    if message_id is not None:
        m = operations.show(message_id)
        if m is None:
            raise click.ClickException(f"no message {message_id} in the local store")
        payload = asdict(m)
    else:
        raw = sys.stdin.read().strip()
        if not raw:
            raise click.ClickException("no mention on stdin (notify_command sends JSON)")
        payload = json.loads(raw)

    outcome = operations.wake_agent(payload)
    click.echo(outcome)
    if outcome.startswith("queued"):
        sys.exit(EXIT_QUEUED)


@main.command()
@click.option("--once", is_flag=True, help="One poll cycle, then exit. For testing.")
@click.option("--detach", is_flag=True,
              help="Run in the background, surviving the shell that started it.")
@click.option("--stop", is_flag=True,
              help="Stop this seat's daemon and wait until its lock is free.")
@click.option("--restart", is_flag=True,
              help="Stop it if running, then start a fresh detached one.")
@click.option("--log", type=click.Path(), default=None,
              help="Where a detached daemon's stdout/stderr go (default ~/.comms/daemon.out).")
def daemon(once: bool, detach: bool, stop: bool, restart: bool, log: str | None) -> None:
    """Hold the outbound connection and record what arrives.

    One long-lived process per comms-enabled seat. It never writes into the
    agent's working session: mentions go to the local store, and the seat's
    designated comms conversation reads them from there.

    **--detach is not supervision, and neither is --restart.** Both survive the
    shell that started them; neither survives a container restart or a kill, and
    nothing here notices a dead daemon or brings it back. --restart saves the
    operator a two-step by hand, no more. Real supervision belongs to the
    deployer — see the daemon-supervision need raised with ansible-platform.

    The daemon --restart starts is always **detached**, whatever the old one ran
    under. How a daemon is hosted is declared by whoever starts it, so this does
    not copy an arrangement it merely observed (constitution §10) — it says
    which it gave you instead.
    """
    chosen = [n for n, on in
              (("--once", once), ("--detach", detach), ("--stop", stop), ("--restart", restart))
              if on]
    if len(chosen) > 1:
        raise click.UsageError(
            f"{' and '.join(chosen)} ask for different things. Pick one."
        )

    if stop:
        stopped, pid = operations.stop_daemon()
        if stopped:
            click.echo(f"daemon stopped, was pid {pid}.\n\n"
                       "Nothing is watching the hub now, so messages to this seat are "
                       "lost rather than queued. Start one with: comms daemon --restart")
        else:
            click.echo("no daemon was running for this seat — nothing to stop.")
        return

    if restart:
        replaced, pid = operations.restart_daemon(log)
        was = "restarted" if replaced else "started (nothing was running)"
        click.echo(f"daemon {was}, pid {pid}, detached. Check it with: comms status")
        return

    if detach:
        pid = operations.detach_daemon(log)
        click.echo(f"daemon detached, pid {pid}. Check it with: comms status")
        return
    stored = operations.run_daemon(max_iterations=1 if once else None)
    click.echo(f"stored {stored} mention(s)")


def run() -> None:
    try:
        main.main(standalone_mode=False)
    except CommsDisabled as exc:
        click.echo(f"comms: disabled\n\n{exc}")
        sys.exit(EXIT_DISABLED)
    except DaemonAlreadyRunning as exc:
        click.echo(f"comms: already running\n\n{exc}")
        sys.exit(EXIT_ALREADY_RUNNING)
    except WakeError as exc:
        click.secho("comms: wake failed", fg="red", bold=True, err=True)
        click.echo(str(exc), err=True)
        sys.exit(EXIT_FAULT)
    except CommsError as exc:
        click.secho(f"comms: {exc.tag}", fg="red", bold=True, err=True)
        click.echo(str(exc), err=True)
        sys.exit(EXIT_FAULT)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        sys.exit(EXIT_FAULT)


if __name__ == "__main__":
    run()
