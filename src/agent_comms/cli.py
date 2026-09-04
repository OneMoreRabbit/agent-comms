"""`comms` — the operator-facing CLI. Formatting only; logic lives in operations."""

from __future__ import annotations

import sys

import click

from . import __version__, operations
from .errors import CommsDisabled, CommsError

#: Exit codes, so a consumer's supervisor can tell these apart mechanically.
#: 0 success, 1 fault, 3 "comms disabled" — a state, not a failure.
EXIT_OK, EXIT_FAULT, EXIT_DISABLED = 0, 1, 3


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
    click.secho("comms: ready", fg="green", bold=True)
    click.echo(f"  identity   {st.identity}")
    click.echo(f"  channel    {st.channel}")
    click.echo(f"  credential {st.credential}")


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
        click.echo(f"{flag} {m.id:>8}  {m.when}  {m.sender}  [{m.topic}]")


@main.command()
@click.argument("message_id", type=int)
def show(message_id: int) -> None:
    """One mention in full, with the permalink to cite it by."""
    m = operations.show(message_id)
    if m is None:
        raise click.ClickException(f"no message {message_id} in the local store")
    click.echo(f"from    {m.sender}\nwhen    {m.when}\nchannel {m.channel}\ntopic   {m.topic}")
    click.echo(f"cite    {m.permalink}\n\n{m.content}")


@main.command()
@click.argument("message_id", type=int)
@click.argument("content")
def reply(message_id: int, content: str) -> None:
    """Reply in the mention's own topic."""
    operations.reply(message_id, content)
    click.echo("sent")


@main.command()
@click.option("--topic", required=True, help="Topic, named '<component>: <ask>'.")
@click.argument("content")
def send(topic: str, content: str) -> None:
    """Post to this seat's project channel."""
    operations.send(topic, content)
    click.echo("sent")


@main.command()
@click.option("--once", is_flag=True, help="One poll cycle, then exit. For testing.")
def daemon(once: bool) -> None:
    """Hold the outbound connection and record what arrives.

    One long-lived process per comms-enabled seat. It never writes into the
    agent's working session: mentions go to the local store, and the seat's
    designated comms conversation reads them from there.
    """
    stored = operations.run_daemon(max_iterations=1 if once else None)
    click.echo(f"stored {stored} mention(s)")


def run() -> None:
    try:
        main.main(standalone_mode=False)
    except CommsDisabled as exc:
        click.echo(f"comms: disabled\n\n{exc}")
        sys.exit(EXIT_DISABLED)
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
