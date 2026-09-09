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


class Identity(BaseModel):
    """Who this seat is. Derived from the seat manifest wherever possible."""

    project: str
    seat: str

    @property
    def bot_name(self) -> str:
        """`<project>-<seat>`, per ADR-0009 §1a. The estate mints under this name."""
        return f"{self.project}-{self.seat}"

    @property
    def credential_path(self) -> Path:
        """`~/.secrets/zuliprc-<project>-<seat>`, per the hub interface response."""
        return Path.home() / ".secrets" / f"zuliprc-{self.bot_name}"

    def canonical_names(self, role: str = "component") -> tuple[str, ...]:
        """Names this seat's bot may carry, per ADR-0009 §7a — by ROLE, not pattern.

        §7a's requirement is **unambiguity in every channel the bot appears in**.
        The canonical form follows from where a bot appears, so it is conditional
        on role rather than a flat pattern — and mistaking the pattern for the
        requirement produces `blocks-blocks-service`, which is worse at the job.

        - **component** — appears only in its own project's channel, where the
          project is implied, so `<seat>` is unambiguous. `<project>-<seat>` is
          also unambiguous, just verbose, so it is accepted rather than warned on.
        - **arch** — appears in several channels, so it **must** carry its
          project. A bare `<seat>` genuinely is ambiguous there, and is warned on.
        """
        if role == "arch":
            return (self.bot_name,)
        return (self.seat, self.bot_name)

    @property
    def bot_names(self) -> tuple[str, ...]:
        return self.canonical_names()

    @property
    def credential_candidates(self) -> list[Path]:
        """Credential paths, in the order the estate actually delivers them.

        `zuliprc-<seat>` first: that is what a component seat gets under §7a.
        `zuliprc-<project>-<seat>` second, which is what an arch seat gets. This
        client warned about the first as a divergence until §7a ruled it correct
        — a warning that always fires is one nobody reads.
        """
        secrets = Path.home() / ".secrets"
        return [secrets / f"zuliprc-{self.seat}", self.credential_path]


class Settings(BaseModel):
    """Everything the client needs once comms is on."""

    identity: Identity
    channel: str = Field(description="The project channel this seat watches.")
    role: str = Field(
        default="component",
        description=(
            "component | arch | estate. Decides the canonical bot name under "
            "ADR-0009 §7a: a component bot appears only in its own channel so the "
            "seat name alone is unambiguous; an arch bot appears in several so it "
            "must carry its project."
        ),
    )
    # `model` and `model_session` used to live here. Deleted in 0.17: the seat
    # declares its runtime and target, and `seat status` reports both. Keeping a
    # second copy is the two-sources problem this contract exists to end.
    authority: tuple[str, ...] = Field(
        default=(),
        description=(
            "Bots this seat accepts DIRECTION from (ADR-0009 §9). Declared by the "
            "estate, never by the seat: a seat widening its own accepted-sender list "
            "is the one edit no boundary should permit. Read from ~/.seat/seat.yml, "
            "which is deployer-owned and never hand-edited on the seat — not from "
            "comms config, which is closer to the seat's own hand. Empty means the "
            "default: this seat's own arch bot."
        ),
    )
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
        if not v.startswith("https://"):
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
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in ("project", "seat",
                   "codex_thread_selection", "role", "comms_authority"):
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

    identity = Identity(project=project, seat=seat)
    return Settings(
        identity=identity,
        codex_thread_selection=manifest.get("codex_thread_selection"),
        authority=_authority(manifest.get("comms_authority"), project),
        role=manifest.get("role") or ("arch" if seat == "arch" or seat.endswith("-arch") else "component"),
        channel=os.environ.get("AGENT_COMMS_CHANNEL") or file_cfg.get("channel") or project,
        lifespan_secs=int(file_cfg.get("lifespan_secs", DEFAULT_LIFESPAN_SECS)),
        state_dir=state_dir,
        notify_command=os.environ.get("AGENT_COMMS_NOTIFY") or file_cfg.get("notify_command"),
        wake=_flag(os.environ.get("AGENT_COMMS_WAKE")) or bool(file_cfg.get("wake")),
        agent_commands=tuple(file_cfg.get("agent_commands", ("claude", "codex"))),
    )


def _authority(raw: str | None, project: str) -> tuple[str, ...]:
    """Who this seat accepts direction from, defaulting to its own arch bot.

    ADR-0009 §9: the default is unchanged from §1a — a seat accepts direction
    from its own arch seat, and anything else is one line per link added
    deliberately by the estate.

    The field name and site are **provisional**: §9 leaves "whether the mesh
    table exists in the deployed `09devagents` today, and what the delivery
    mechanism to seats is" open and owned by `ansible-platform`. `seat.yml`
    alongside `model` is the leading candidate and the one this reads, because
    it is deployer-owned and never hand-edited on the seat — which is the
    property §9 actually requires. If the estate delivers it elsewhere, only
    this function changes.
    """
    if raw:
        names = tuple(n.strip() for n in raw.replace(",", " ").split() if n.strip())
        if names:
            return names
    return (f"{project}-arch",)


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
