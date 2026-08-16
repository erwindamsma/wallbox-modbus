"""YAML configuration loading, with typo detection."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from . import sources

PARITY_ALIASES = {
    "e": "E", "even": "E",
    "n": "N", "none": "N",
    "o": "O", "odd": "O",
}

# How far above the installation limit the failsafe sits when not given
# explicitly. It only has to be clearly above -- see FailsafeConfig.
FAILSAFE_HEADROOM = 1.5


@dataclass
class SerialConfig:
    port: str = "/dev/ttyUSB0"
    unit_id: int = 1
    baud: object = "auto"
    parity: object = "auto"
    stopbits: int = 1
    probe_seconds: float = 8.0
    rts_direction_control: bool = False
    rts_active_high: bool = True
    discard_echo: bool = False


@dataclass
class IdentityConfig:
    serial_number: int = 20250001
    # An int, or a list of candidates to cycle through while the charger hunts.
    # 0x0102 is INEPRO's direct-connect variant code and is the one a Pulsar
    # Plus was observed to accept; 0x0103, the CT variant, was rejected.
    meter_code: object = 0x0102
    modbus_id: int = 1
    baud_code: int = 9600
    protocol_version: float = 1.0
    software_version: float = 1.17
    hardware_version: float = 1.0
    meter_amps: int = 100
    combination_code: int = 5
    parity_code: int = 1
    software_crc: int = 0


@dataclass
class MeterConfig:
    # Phases at the grid connection point. Only 1 is supported: the N1-CT is a
    # single-phase meter and this emulator has one power figure to report, so
    # on a polyphase supply L2 and L3 would read zero and the charger would
    # believe those phases were idle. _validate() refuses anything else.
    phases: int = 1
    nominal_voltage: float = 230.0
    current_sign: str = "magnitude"
    unknown_register_policy: str = "zero"
    energy_file: str | None = None
    identity: IdentityConfig = field(default_factory=IdentityConfig)


@dataclass
class LimitsConfig:
    # Must match "maximum current per phase" in the myWallbox app's Load
    # Management settings -- the ceiling the charger balances against. The
    # emulator does not enforce this; it uses it to sanity-check the failsafe.
    # Required in the config file: there is no safe default for someone
    # else's main fuse.
    installation_current_a: float = 35.0


@dataclass
class FailsafeConfig:
    max_data_age_s: float = 15.0
    # Must sit above limits.installation_current_a: reporting exactly the
    # limit is a fixed point of the charger's control loop and leaves it
    # sitting wherever it already was, instead of backing off. Derived from
    # the installation limit when the config does not give it.
    current_a: float = 50.0


@dataclass
class Config:
    serial: SerialConfig = field(default_factory=SerialConfig)
    meter: MeterConfig = field(default_factory=MeterConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    failsafe: FailsafeConfig = field(default_factory=FailsafeConfig)
    source: sources.SourceConfig = field(
        default_factory=lambda: sources.get("homeassistant").config_class()
    )
    log_level: str = "INFO"
    status_interval_s: float = 10.0


def _build(cls, data, path: str):
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(
            f"{path}: unknown option(s) {sorted(unknown)}; valid options are {sorted(known)}"
        )
    return cls(**{k: v for k, v in data.items() if k in known})


def _section(raw: dict, name: str) -> dict:
    value = raw.pop(name, None) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{name}: expected a mapping, got {type(value).__name__}")
    return dict(value)


def load(path: str | Path, require_source_entities: bool = True) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("config file must contain a mapping at the top level")

    meter_raw = _section(raw, "meter")
    identity = _build(IdentityConfig, meter_raw.pop("identity", None), "meter.identity")
    meter = _build(MeterConfig, meter_raw, "meter")
    meter.identity = identity

    # No default is safe here: it is someone else's main fuse.
    limits_raw = _section(raw, "limits")
    if "installation_current_a" not in limits_raw:
        raise ValueError(
            "limits.installation_current_a is required. Set it to the rating of the "
            "fuse or main breaker the charger has to share, in amps, and set the same "
            "number as 'maximum current per phase' in the myWallbox app"
        )
    limits = _build(LimitsConfig, limits_raw, "limits")

    failsafe_raw = _section(raw, "failsafe")
    failsafe = _build(FailsafeConfig, failsafe_raw, "failsafe")
    if "current_a" not in failsafe_raw:
        failsafe.current_a = round(limits.installation_current_a * FAILSAFE_HEADROOM, 1)

    # The source type chooses which options are valid, so the class has to be
    # resolved before the section can be parsed.
    source_raw = _section(raw, "source")
    source_cls = sources.get(source_raw.get("type", "homeassistant"))
    source = _build(source_cls.config_class, source_raw, "source")

    cfg = Config(
        serial=_build(SerialConfig, raw.pop("serial", None), "serial"),
        meter=meter,
        limits=limits,
        failsafe=failsafe,
        source=source,
        log_level=raw.pop("log_level", "INFO"),
        status_interval_s=float(raw.pop("status_interval_s", 10.0)),
    )
    if raw:
        raise ValueError(f"unknown top-level option(s): {sorted(raw)}")

    _validate(cfg, require_source_entities)
    return cfg


def _validate(cfg: Config, require_source_entities: bool = True) -> None:
    s = cfg.serial
    if isinstance(s.baud, str) and s.baud != "auto":
        raise ValueError("serial.baud must be a number or 'auto'")
    if isinstance(s.parity, str) and s.parity != "auto":
        parity = PARITY_ALIASES.get(s.parity.lower())
        if not parity:
            raise ValueError("serial.parity must be one of even/none/odd or 'auto'")
        s.parity = parity
    if not 1 <= s.unit_id <= 247:
        raise ValueError("serial.unit_id must be between 1 and 247")

    if cfg.meter.phases != 1:
        raise ValueError(
            f"meter.phases is {cfg.meter.phases}, and only 1 is supported.\n"
            "  The emulated N1-CT is a single-phase meter and this program has a single\n"
            "  grid power figure to report, so it would answer 0 A for L2 and L3. A\n"
            "  charger reads that as 'those phases are idle' and would let itself pull\n"
            "  its full current on them however loaded your supply actually is -- the\n"
            "  opposite of what you installed this for. Refusing to start is the honest\n"
            "  outcome. See README, 'Three-phase installations'."
        )
    if cfg.meter.current_sign not in ("magnitude", "signed"):
        raise ValueError("meter.current_sign must be 'magnitude' or 'signed'")
    if cfg.meter.unknown_register_policy not in ("zero", "exception"):
        raise ValueError("meter.unknown_register_policy must be 'zero' or 'exception'")
    if cfg.meter.nominal_voltage <= 0:
        raise ValueError("meter.nominal_voltage must be positive")

    src = cfg.source
    if src.mode not in ("push", "poll"):
        raise ValueError("source.mode must be 'push' or 'poll'")
    if src.poll_interval_s <= 0:
        raise ValueError("source.poll_interval_s must be positive")
    sources.get(src.type).validate(src, complete=require_source_entities)

    lim = cfg.limits
    if lim.installation_current_a <= 0:
        raise ValueError("limits.installation_current_a must be positive")
    if cfg.failsafe.max_data_age_s <= 0:
        raise ValueError("failsafe.max_data_age_s must be positive")
    if cfg.failsafe.current_a <= 0:
        raise ValueError("failsafe.current_a must be positive")
    if cfg.failsafe.current_a <= lim.installation_current_a:
        raise ValueError(
            f"failsafe.current_a ({cfg.failsafe.current_a} A) must be above "
            f"limits.installation_current_a ({lim.installation_current_a} A), otherwise "
            "the charger reads it as 'exactly at the limit' and holds its current "
            "instead of backing off"
        )
