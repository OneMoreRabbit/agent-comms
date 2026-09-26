"""Configuration and identity resolution.

Two ideas carry this module:

1. **A seat should not have to be told who it is.** `devagent-seat-contract`
   v0.3 already places `~/.seat/seat.yml` with `project` and `seat`, and the
   estate mints the bot as `<project>-<seat>` from the same two values. So
   identity is derived, not configured, and a consumer's only obligation is to
   turn comms on.
2. **Enabled-but-broken must never look like off.** `resolve()` returns a
   `Resolution` that names which of the two it found (contract §3).
"""

from __future__ import annotations

import configparser
import os
import stat
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from .errors import (
    CommsDisabled,
    CredentialMissing,
    CredentialUnreadable,
    InsecureTransportRefused,
)

DEFAULT_LIFESPAN_SECS = 3600
"""Contract §2. Client-set per queue; the estate configures nothing."""

#: Zulip added the register-response echo of the queue lifespan
#: (`idle_queue_timeout_secs`) at feature level 481 / Zulip 12.0. Below this the
#: value cannot be read back at all — see `hub.verify_lifespan`.
LIFESPAN_ECHO_FEATURE_LEVEL = 481

#: The feature level the estate verified **in the running server's source** —
#: `zerver/tornado/event_queue.py` on hub-1, reported in
#: `agent-comms-hub-response` 0.2: `lifespan_secs` is client-set per queue, with
#: no server-side cap in the handler. On this exact server the lifespan is
#: therefore honoured, and saying otherwise at every connect is noise rather than
#: diligence. The estate pins the Zulip image and commits to announcing upgrades
#: and re-verifying, which is what makes a pinned level safe to trust.
SOURCE_VERIFIED_FEATURE_LEVEL = 372


class Declared(BaseModel):
    """What the DIRECTORY says this seat is: its FQN, its bot, its channel.

    **All three are read, never derived** — the addressing model is
    channel↔seat, bot↔seat, FQN↔agent, and the directory holds all of it
    (`transports.comms` per agent, plus the agent's own FQN). This class exists
    because four things in this client used to assemble them from names
    instead:

    - the channel fell back to the PROJECT NAME. Measured 2026-09-25: both test
      seats believed they were on `agent-eco` while the directory said
      `seat-testing`. It worked only because both were subscribed — a check
      passing for the wrong reason.
    - the bot was `f"{project}-{seat}"`, giving `agent-eco-test-claude` where
      the directory says `test-claude`.
    - the credential filename came from that template, so the code had to try
      TWO spellings and take whichever existed.
    - `canonical_names(role)` existed to accept two spellings of the bot,
      because the derived one might be the wrong one — and `role` itself was
      guessed from the seat name's suffix, absent from 29/29 manifests.

    One read replaces all four. `role` and `canonical_names` are gone with them.
    """

    #: **No `fqn` here, deliberately.** A seat has no FQN: the model is
    #: channel↔seat, bot↔seat, FQN↔AGENT. A field holding "this seat's FQN"
    #: is a category error however it is filled, and it invites the next
    #: reader to treat a seat as addressable. The sending agent states itself
    #: with `--from`; see `operations.sending_agent()`.
    bot: str = ""
    channel: str = ""
    source: str = "unread"

    @property
    def known(self) -> bool:
        return bool(self.bot and self.channel)


def declared_identity(project: str, seat: str, state_dir: Path | None = None) -> Declared:
    """Read this seat's own FQN, bot and channel from the directory's answer.

    The source is the assignments cache this seat already refreshes on a timer
    (`~/.comms/routes.json`), which carries `transports.comms` per agent since
    the directory was extended on 2026-09-25.

    **One bot and one channel per seat** — measured across all 29 seats in the
    estate, and the model says so. If the cached agents disagree about either,
    that is not something to average: it is reported as unknown, so `doctor`
    fails loudly rather than this returning a guess.

    **Only the bot and the channel, because only those belong to a seat.** No
    FQN is read or kept here: an FQN names an agent session, so "this seat's
    FQN" is a category error however it is filled. This class held one briefly
    — first preferring "the agent whose last segment is the seat name", which
    is deriving an FQN from a seat name, and then "the one agent this seat
    serves". The operator rejected both, and correctly: the sending agent
    states itself with `--from`, and nothing else needs a seat-level FQN.
    """
    from . import config_sync

    try:
        agents = config_sync.agent_set(state_dir or (Path.home() / ".comms"))
    except Exception:  # noqa: BLE001 - a fresh seat has no cache yet
        return Declared(source="no cache")
    if not agents:
        return Declared(source="no cache")

    bots = {((r.get("transports") or {}).get("comms") or {}).get("bot")
            for r in agents.values()}
    chans = {((r.get("transports") or {}).get("comms") or {}).get("channel")
             for r in agents.values()}
    bots.discard(None)
    chans.discard(None)
    if len(bots) != 1 or len(chans) != 1:
        return Declared(source=f"disagreeing transports: bots={sorted(bots)} "
                               f"channels={sorted(chans)}")

    return Declared(bot=next(iter(bots)), channel=next(iter(chans)),
                    source=f"directory ({len(agents)} agent(s))")


class Identity(BaseModel):
    """Who this seat is.

    `project` and `seat` come from the seat manifest and name the SEAT. They are
    not an address and nothing is assembled from them — see `Declared`, which
    reads the FQN, bot and channel the directory holds.
    """

    project: str
    seat: str
    #: The bot and channel the directory declares for this seat. Read, never
    #: derived — and carrying no FQN, because a seat has no FQN.
    declared: Declared = Declared()

    @property
    def bot_name(self) -> str:
        """This seat's bot, as the directory states it.

        Falls back to `<project>-<seat>` ONLY on a seat that has never synced,
        because a fresh seat must still be able to find its credential and say
        what it is. The fallback is reported by `doctor`, never silent.
        """
        return self.declared.bot or f"{self.project}-{self.seat}"  # gate-exempt: the PRE-SYNC fallback only. A seat that has never read the directory must still name itself and find its credential or a fresh container cannot start. Declared.source records that it was not read, and doctor reports it.

    @property
    def credential_candidates(self) -> list[Path]:
        """`zuliprc-<bot>`, with the pre-sync fallbacks after it.

        The estate names the credential after the BOT — `zuliprc-test-claude`
        for bot `test-claude`, `zuliprc-agent-eco-arch` for bot
        `agent-eco-arch`. Reading the bot makes that one rule instead of two
        guessed spellings; the guesses stay only for a seat with no cache yet.
        """
        secrets = Path.home() / ".secrets"
        first = [secrets / f"zuliprc-{self.declared.bot}"] if self.declared.bot else []
        return first + [secrets / f"zuliprc-{self.seat}",
                        secrets / f"zuliprc-{self.project}-{self.seat}"]  # gate-exempt: the PRE-SYNC fallback only. A seat that has never read the directory must still name itself and find its credential or a fresh container cannot start. Declared.source records that it was not read, and doctor reports it.

    @property
    def credential_path(self) -> Path:
        return self.credential_candidates[0]

    def known_names(self) -> tuple[str, ...]:
        """Every name this seat is KNOWN by — for recognising itself only.

        Two distinct jobs, and conflating them is what produced
        `canonical_names(role)`:

        - **posting and credentials** need the ONE name the directory declares:
          `bot_name`. There is no set there and no choice to make.
        - **recognising ourselves** — is this topic prefix us, is this sender us,
          are we addressing ourselves — is asked about names other parties have
          already written down, in topics and mentions that predate any sync.

        This is the second. Every entry is a value we HOLD: the bot the
        directory declares, and the seat name from the manifest. Nothing is
        derived from a pattern, and the `<project>-<seat>` form appears only as
        the pre-sync fallback `bot_name` already returns.

        Being generous here is safe and being narrow is not: saying yes to a
        name that is ours costs nothing, while saying no to one makes the seat
        fail to recognise mail addressed to it and fail its own health check.
        """
        names = [self.bot_name, self.seat]
        if not self.declared.bot:
            names.append(f"{self.project}-{self.seat}")  # gate-exempt: the PRE-SYNC fallback only. A seat that has never read the directory must still name itself and find its credential or a fresh container cannot start. Declared.source records that it was not read, and doctor reports it.
        seen, out = set(), []
        for n in names:
            k = n.strip().casefold()
            if k and k not in seen:
                seen.add(k)
                out.append(n)
        return tuple(out)


class Settings(BaseModel):
    """Everything the client needs once comms is on."""

    identity: Identity
    channel: str = Field(
        description=(
            "The channel this seat posts and listens on, READ from the "
            "directory's `transports.comms.channel` for this seat's own agents. "
            "It is NOT the project name: measured 2026-09-25, both test seats "
            "are in project `agent-eco` and on channel `seat-testing`."
        )
    )
    # `model` and `model_session` used to live here. Deleted in 0.17: the seat
    # declares its runtime and target, and `seat status` reports both. Keeping a
    # second copy is the two-sources problem this contract exists to end.
    wake: bool = Field(
        default=False,
        description=(
            "Deliver each mention into the seat's running agent session (ADR-0009 §7b). "
            "Off by default: turning a seat from receiving to acting is a consumer's "
            "decision, not a package default."
        ),
    )
    lifespan_secs: int = DEFAULT_LIFESPAN_SECS
    state_dir: Path = Field(default_factory=lambda: Path.home() / ".comms")
    notify_command: str | None = Field(
        default=None,
        description=(
            "Optional command run once per mention, with the mention as JSON on stdin. "
            "This is the hand-off to the seat's comms conversation. The client does not "
            "decide how a seat surfaces a mention — only that it is never the working "
            "session (contract §3, 'Non-invasive')."
        ),
    )

    @field_validator("lifespan_secs")
    @classmethod
    def _sane_lifespan(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("lifespan_secs must be positive; 0 would mean the server default")
        return v


class Credential(BaseModel):
    """A parsed zuliprc, or its environment-variable equivalent."""

    email: str
    key: str
    site: str
    source: str = Field(description="Where it came from, for `comms doctor` output.")
    #: Non-fatal divergences found while loading. Surfaced, never swallowed.
    notices: list[str] = Field(default_factory=list)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Credential(email={self.email!r}, site={self.site!r}, source={self.source!r})"

    __str__ = __repr__

    @field_validator("site")
    @classmethod
    def _must_be_https(cls, v: str) -> str:
        if not v.startswith("https://"):  # gate-exempt: URL scheme, not an identifier
            raise ValueError(
                f"site must be https (got {v!r}). Estate traffic does not travel unverified."
            )
        return v.rstrip("/")


def _seat_manifest(path: Path | None = None) -> dict[str, str]:
    """Read `project`/`seat` out of the seat manifest, tolerating its absence.

    Parsed by hand rather than with PyYAML: the two keys we need are flat
    scalars, and the manifest is deployer-owned and documented as such. Adding a
    YAML dependency to read two strings would be the heavier choice.
    """
    path = path or Path.home() / ".seat" / "seat.yml"
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:  # gate-exempt: comment syntax in a file we parse, not an identifier
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in ("project", "seat", "codex_thread_selection"):
            out[key] = value.strip().strip("'\"")
    return out


def load_settings(state_dir: Path | None = None, seat_manifest: Path | None = None) -> Settings:
    """Resolve settings, or raise `CommsDisabled` if comms is not turned on.

    Enablement, in precedence order:

    - `AGENT_COMMS_ENABLED` in the environment (`1`/`true`/`yes`), or
    - `enabled = true` in `~/.comms/config.toml`.

    Absent both, comms is off and this raises `CommsDisabled`. That is the
    resting state of a seat, not a fault.
    """
    state_dir = state_dir or Path(os.environ.get("AGENT_COMMS_HOME", Path.home() / ".comms"))
    config_path = state_dir / "config.toml"

    file_cfg: dict = {}
    if config_path.exists():
        try:
            file_cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CredentialUnreadable(
                f"{config_path} exists but could not be parsed: {exc}. "
                "Comms is neither on nor cleanly off — fix or remove the file."
            ) from exc

    env_flag = os.environ.get("AGENT_COMMS_ENABLED", "").strip().lower()
    enabled = env_flag in ("1", "true", "yes") or bool(file_cfg.get("enabled"))
    if not enabled:
        raise CommsDisabled(
            "comms is not enabled on this seat. This is the normal resting state: "
            "enablement is per seat and optional (contract §2). To turn it on, set "
            f"enabled = true in {config_path}, or AGENT_COMMS_ENABLED=1."
        )

    manifest = _seat_manifest(seat_manifest)
    project = os.environ.get("AGENT_COMMS_PROJECT") or file_cfg.get("project") or manifest.get("project")
    seat = os.environ.get("AGENT_COMMS_SEAT") or file_cfg.get("seat") or manifest.get("seat")
    if not project or not seat:
        raise CredentialUnreadable(
            "cannot determine this seat's identity. Normally it is read from "
            "~/.seat/seat.yml (project + seat), which the deployer places. Set "
            "project/seat in config.toml, or AGENT_COMMS_PROJECT / AGENT_COMMS_SEAT."
        )

    # **Read this seat's own FQN, bot and channel from the directory.**
    declared = declared_identity(project, seat, state_dir)
    identity = Identity(project=project, seat=seat, declared=declared)
    return Settings(
        identity=identity,
        codex_thread_selection=manifest.get("codex_thread_selection"),
        # Declared first, then an explicit override, then the file. The
        # PROJECT NAME is no longer a fallback: it was wrong on both test seats
        # (project `agent-eco`, channel `seat-testing`) and worked only because
        # both channels happened to be subscribed.
        channel=(os.environ.get("AGENT_COMMS_CHANNEL") or declared.channel
                 or file_cfg.get("channel") or project),
        lifespan_secs=int(file_cfg.get("lifespan_secs", DEFAULT_LIFESPAN_SECS)),
        state_dir=state_dir,
        notify_command=os.environ.get("AGENT_COMMS_NOTIFY") or file_cfg.get("notify_command"),
        wake=_flag(os.environ.get("AGENT_COMMS_WAKE")) or bool(file_cfg.get("wake")),
        agent_commands=tuple(file_cfg.get("agent_commands", ("claude", "codex"))),
    )


def _flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes")


def _reject_insecure(values: dict, origin: str) -> None:
    """Contract §3: refuse; no insecure flag exists.

    The client does not offer this switch, and will not honour one it is handed.
    A delivered credential is not a trusted place to disable TLS verification —
    if it ever carries one, that is a fault at the source and we stop.
    """
    for key in ("insecure", "client_cert", "client_cert_key"):
        raw = values.get(key)
        if key == "insecure" and str(raw).strip().lower() in ("true", "1", "yes"):
            raise InsecureTransportRefused(
                f"{origin} sets insecure={raw!r}. This client has no insecure mode and "
                "will not honour one: TLS on the hub is publicly trusted, so an "
                "instruction to skip verification is a fault at the source, not a "
                "local workaround. Fix the credential."
            )
    if os.environ.get("ZULIP_ALLOW_INSECURE", "").strip().lower() in ("1", "true", "yes"):
        raise InsecureTransportRefused(
            "ZULIP_ALLOW_INSECURE is set in the environment. This client refuses to "
            "run with TLS verification disabled; unset it."
        )


def load_credential(identity: Identity) -> Credential:
    """Read the bot credential, distinguishing 'missing' from 'unreadable'.

    Environment variables win over the file, for consumers that inject rather
    than mount — `agent-image` is the expected case; seats use the file.
    """
    env_key, env_email = os.environ.get("ZULIP_API_KEY"), os.environ.get("ZULIP_EMAIL")
    if env_key and env_email:
        _reject_insecure({}, "the environment")
        site = os.environ.get("ZULIP_SITE", "")
        if not site:
            raise CredentialUnreadable(
                "ZULIP_API_KEY and ZULIP_EMAIL are set but ZULIP_SITE is not; "
                "there is no server to talk to."
            )
        return Credential(email=env_email, key=env_key, site=site, source="environment")

    candidates = identity.credential_candidates
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        raise CredentialMissing(
            f"comms is enabled but no credential exists at {candidates[0]}. The estate mints "
            f"the bot '{identity.bot_name}' and delivers this file; until it does, this seat "
            "cannot connect. This is reported as broken rather than quiet precisely "
            "because it is indistinguishable from 'comms disabled' on disk."
        )

    notices: list[str] = []

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialUnreadable(f"{path} exists but could not be read: {exc}") from exc

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise CredentialUnreadable(
            f"{path} is mode {mode:04o}; it holds a bot API key and must be 0600. "
            "Refusing to use a world- or group-readable credential."
        )

    parser = configparser.ConfigParser()
    try:
        parser.read_string(raw)
    except configparser.Error as exc:
        raise CredentialUnreadable(
            f"{path} is not valid INI: {exc}. Expected a stock zuliprc with an [api] section."
        ) from exc

    if not parser.has_section("api"):
        raise CredentialUnreadable(
            f"{path} has no [api] section. Expected a stock zuliprc "
            "([api] with email, key and site)."
        )

    values = dict(parser["api"])
    _reject_insecure(values, str(path))
    missing = [k for k in ("email", "key", "site") if not values.get(k)]
    if missing:
        raise CredentialUnreadable(
            f"{path} is missing required [api] key(s): {', '.join(missing)}."
        )

    return Credential(
        email=values["email"],
        key=values["key"],
        site=values["site"],
        source=str(path),
        notices=notices,
    )
