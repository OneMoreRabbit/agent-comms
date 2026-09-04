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
#: value cannot be read back at all — see `hub.register_queue`.
LIFESPAN_ECHO_FEATURE_LEVEL = 481


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

    @property
    def credential_candidates(self) -> list[Path]:
        """The contracted path first, then the shapes the estate has actually used.

        The estate delivered `zuliprc-<seat>` rather than the contracted
        `zuliprc-<project>-<seat>` (observed 2026-09-04). Refusing to start over
        a filename would block the critical path for a naming difference, and
        silently accepting it would let the divergence rot. So: accept, and say
        so loudly — `load_credential` records a notice naming both paths.
        """
        secrets = Path.home() / ".secrets"
        return [self.credential_path, secrets / f"zuliprc-{self.seat}"]


class Settings(BaseModel):
    """Everything the client needs once comms is on."""

    identity: Identity
    channel: str = Field(description="The project channel this seat watches.")
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
        if key in ("project", "seat"):
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
        channel=os.environ.get("AGENT_COMMS_CHANNEL") or file_cfg.get("channel") or project,
        lifespan_secs=int(file_cfg.get("lifespan_secs", DEFAULT_LIFESPAN_SECS)),
        state_dir=state_dir,
        notify_command=os.environ.get("AGENT_COMMS_NOTIFY") or file_cfg.get("notify_command"),
    )


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
    if path != identity.credential_path:
        notices.append(
            f"credential found at {path}, not the contracted "
            f"{identity.credential_path}. Accepted so the seat can work, but the paths "
            f"should converge: the contract derives the path from <project>-<seat>, and a "
            f"seat that guesses filenames is one rename away from a silent outage."
        )

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
