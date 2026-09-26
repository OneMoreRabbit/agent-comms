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
    receiving = daemon is not None and daemon.running and not daemon.stale
    # Receiving and waking are two halves, and a seat can have either without the
    # other. "ready" means both: a daemon with no wake trigger stores every
    # mention and answers none, which is the same reassuring green light over a
    # broken seat that the daemon check was added to stop.
    healthy = receiving and st.wake_trigger is not None
    if healthy:
        click.secho("comms: ready", fg="green", bold=True)
    elif not receiving:
        click.secho("comms: configured, but NOT RECEIVING", fg="red", bold=True)
    else:
        click.secho("comms: receiving, but NOTHING IS WOKEN", fg="red", bold=True)
    click.echo(f"  identity   {st.identity}")
    click.echo(f"  channel    {st.channel}")
    click.echo(f"  credential {st.credential}")
    if daemon is not None:
        click.secho(f"  daemon     {daemon.summary()}", fg=None if receiving else "red")
    if st.wake_trigger is not None:
        click.echo(f"  wake       {st.wake_trigger}")
    else:
        click.secho(
            "  wake       NONE — mentions are stored and no agent is woken. Set\n"
            '             notify_command = "comms wake" in ~/.comms/config.toml;\n'
            "             until then `comms inbox` is the only way they are seen.",
            fg="red",
        )
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
@click.option("--from", "from_fqn", required=True, help="The FQN of the AGENT sending this — estate.project.agent. Required: a message is from an agent to an agent, and the seat is only the delivery mechanism, so comms cannot supply it.")
def reply(message_id: int, content: str, from_fqn: str) -> None:
    """Reply in the mention's own topic."""
    posted = operations.reply(message_id, content, from_fqn=from_fqn)
    _say_sent(posted, "replied")


def _say_sent(posted, word: str = "sent") -> None:
    """Confirm the post, then say who will not see it.

    Warnings go to stderr and the word still goes to stdout: the message WAS
    posted, so a caller parsing stdout must still read success. What changes is
    that the sender now learns at the moment of sending — the only moment the
    information is worth anything — rather than from a third party days later.
    """
    click.echo(word)
    for warning in posted.warnings:
        click.secho(f"warning: {warning}", fg="yellow", err=True)


@main.command()
@click.option("--to", required=True,
              help="The seat this message is for, by plain name (agent-skeleton).")
@click.option("--subject", default=None, help="Subject; the topic becomes '<to>: <subject>'.")
@click.option("--topic", default=None,
              help="Continue an existing topic instead of starting one. Still needs --to.")
@click.option("--from", "from_fqn", required=True, help="The FQN of the AGENT sending this — estate.project.agent. Required: a message is from an agent to an agent, and the seat is only the delivery mechanism, so comms cannot supply it.")
@click.argument("content")
def send(to: str, subject: str | None, topic: str | None, from_fqn: str,
         content: str) -> None:
    """Post to this seat's project channel, addressed to a named seat.

    You name the seat; this client spells the address:

        comms send --to agent-skeleton --subject 'the ask' 'body text'

    gives the topic `agent-skeleton: the ask` and a real `@**agent-skeleton**`
    mention — both routes a recipient matches on, so it does not matter which.

    The body is never rewritten: a seat name typed in prose is prose, and
    addressing travels in the flag, not the text. It IS now read for one thing —
    an explicit `@**name**` is checked for reachability, and you get a warning on
    stderr if that name cannot see this channel. The message still posts.

    The name in --to is checked against the hub first, and a seat that does not
    exist or is not in this channel is refused rather than posted to.
    """
    posted = operations.send(content, to=to, subject=subject, topic=topic,
                             from_fqn=from_fqn)
    _say_sent(posted)


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
    # Exact match on a named outcome. This was `outcome.startswith("queued")`
    # until 2026-09-25: a prefix-match on a closed word set, choosing a
    # consumer-facing EXIT CODE by guesswork (write-time gate 1).
    if outcome.outcome == operations.Woken.QUEUED:
        sys.exit(EXIT_QUEUED)


@main.command()
@click.option("--once", is_flag=True, help="One poll cycle, then exit. For testing.")
@click.option("--detach", is_flag=True,
              help="Run in the background, surviving the shell that started it.")
@click.option("--supervise", is_flag=True,
              help="Run in the foreground and restart the daemon if it exits.")
@click.option("--stop", is_flag=True,
              help="Stop this seat's daemon and wait until its lock is free.")
@click.option("--restart", is_flag=True,
              help="Stop it if running, then start a fresh detached one.")
@click.option("--log", type=click.Path(), default=None,
              help="Where a detached daemon's stdout/stderr go (default ~/.comms/daemon.out).")
def daemon(once: bool, detach: bool, supervise: bool, stop: bool, restart: bool,
           log: str | None) -> None:
    """Hold the outbound connection and record what arrives.

    One long-lived process per comms-enabled seat. It never writes into the
    agent's working session: mentions go to the local store, and the seat's
    designated comms conversation reads them from there.

    **--supervise restarts the daemon if it EXITS. That is all it does.** It does
    not survive being killed, the container restarting or the host rebooting —
    nothing inside the container can, because a devagent seat has no init to own
    it (no systemd, no cron, PID 1 is sshd). The durable answer is a host-side
    unit, asked for in `comms-daemon-supervision`. --supervise closes the part
    the client can close, and refuses to restart on a fault a restart cannot fix
    (bad credential, comms disabled, two wake triggers) rather than crash-looping
    over the reason.

    **--detach is not supervision, and neither is --restart.** Both survive the
    shell that started them; neither survives a container restart or a kill.
    --restart saves the operator a two-step by hand, no more.

    The daemon --restart starts is always **detached**, whatever the old one ran
    under. How a daemon is hosted is declared by whoever starts it, so this does
    not copy an arrangement it merely observed (constitution §10) — it says
    which it gave you instead.
    """
    chosen = [n for n, on in
              (("--once", once), ("--detach", detach), ("--supervise", supervise),
               ("--stop", stop), ("--restart", restart))
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

    if supervise:
        restarts = operations.supervise_daemon()
        click.echo(f"supervisor stopped after {restarts} restart(s)")
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


@main.group()
def config() -> None:
    """The local configuration — what this seat holds, and when it arrived."""


@config.command("show")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable.")
def config_show(as_json: bool) -> None:
    """What is held now, and how stale it is."""
    from . import config_sync
    from .config import load_settings

    settings = load_settings()
    held = config_sync.load(settings.state_dir)
    if as_json:
        click.echo(json.dumps(held or {"routes": []}, indent=2))
        return
    if not held:
        click.echo("no configuration fetched yet — run: comms config refresh")
        return
    click.echo(f"generation {held.get('generation', 0)}   "
               f"fetched {held.get('fetched_at') or 'never'}   "
               f"source {held.get('source') or '?'}")
    for record in held.get("routes") or []:
        click.echo(f"  {record.get('agent', '?'):44} "
                   f"{record.get('label', '-'):10} {record.get('delivery', '-')}")


@config.command("refresh")
def config_refresh() -> None:
    """Fetch now, rather than waiting for the timer."""
    from . import config_sync
    from .config import load_settings

    settings = load_settings()
    got = config_sync.fetch(settings.identity.project, settings.identity.seat,
                            settings.state_dir)
    click.echo(f"config: {got.line()}")
    if got.source != "directory":
        sys.exit(EXIT_FAULT)


@main.command()
@click.option("--last", default=20, help="How many.")
@click.option("--state", default="", help="Only this state.")
@click.option("--delivered", "only", flag_value="delivered", help="What reached a session.")
@click.option("--queued", "only", flag_value="queued", help="Waiting, with attempts.")
@click.option("--retired", "only", flag_value="retired", help="Abandoned and expired.")
@click.option("--refused", "only", flag_value="refused", help="Sender not permitted.")
@click.option("--json", "as_json", is_flag=True)
def log(last: int, state: str, only: str, as_json: bool) -> None:
    """Recent messages, any state, one line each."""
    rows = operations.recent(last=last, state=state or only or "")
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("nothing stored yet." if not state else f"nothing in state {state!r}.")
        return
    for r in rows:
        click.echo(f"  {r['id']:>6}  {r['when']}  {r['state']:<10} "
                   f"{r['sender']:<22} {r['topic'][:46]}")


@main.command()
@click.option("--json", "as_json", is_flag=True,
              help="For the estate dashboard to poll.")
def stats(as_json: bool) -> None:
    """Counts by state, and the health of the queue."""
    data = operations.stats()
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    click.echo(f"stored {data['stored']}   undelivered {data['undelivered']}   "
               f"retired {data['retired']}   refused {data['refused']}")
    if data["oldest_undelivered"]:
        click.echo(f"oldest undelivered: {data['oldest_undelivered']}")
    click.echo(f"daemon: {data['daemon']}")


@main.command()
@click.argument("message_id", type=int)
def trace(message_id: int) -> None:
    """End to end for one message: what happened to it, and when."""
    for line in operations.trace(message_id):
        click.echo(line)


@main.command("resolve")
@click.argument("name")
@click.option("--from", "from_fqn", required=True, help="The FQN of the AGENT asking — estate.project.agent. Required: the permission verdict depends on who is asking, so asking as the wrong agent prints the wrong answer.")
def resolve_cmd(name: str, from_fqn: str) -> None:
    """What would this address resolve to, and why. Sends nothing."""
    for line in operations.resolve_name(name, from_fqn=from_fqn):
        click.echo(line)


@main.command("queue")
@click.option("--json", "as_json", is_flag=True)
def queue_cmd(as_json: bool) -> None:
    """Exactly what the next pass would deliver, in order."""
    rows = operations.queued_now()
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("nothing would be delivered on the next pass.")
        return
    waiting = rows[0]["of"]
    click.echo(f"the next pass would deliver {len(rows)} of {waiting} waiting:")
    for r in rows:
        click.echo(f"  {r['id']:>6}  {r['when']}  {r['sender']:<22} "
                   f"{r['topic'][:40]}  attempts {r['attempts']}")
    if waiting > len(rows):
        click.echo(f"  ({waiting - len(rows)} more stay queued — the cap is per pass)")


@main.command("retire")
@click.argument("message_id", type=int)
@click.option("--reason", required=True, help="Why. Recorded with the retirement.")
def retire_cmd(message_id: int, reason: str) -> None:
    """Retire a message without delivering it. Logged, never silent."""
    click.echo(operations.retire(message_id, reason))


@main.command("requeue")
@click.argument("message_id", type=int)
@click.option("--reason", required=True, help="Why. Recorded with the resurrection.")
def requeue_cmd(message_id: int, reason: str) -> None:
    """Put a retired or abandoned message back in the queue. Logged."""
    click.echo(operations.requeue(message_id, reason))


if __name__ == "__main__":
    run()
