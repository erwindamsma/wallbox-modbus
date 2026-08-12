"""YAML configuration loading, with typo detection."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

PARITY_ALIASES = {
    "e": "E", "even": "E",
    "n": "N", "none": "N",
    "o": "O", "odd": "O",
}


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
    meter_code: int = 0
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
    nominal_voltage: float = 230.0
    current_sign: str = "magnitude"
    unknown_register_policy: str = "zero"
    energy_file: str | None = None
    identity: IdentityConfig = field(default_factory=IdentityConfig)


@dataclass
class FailsafeConfig:
    max_data_age_s: float = 15.0
    current_a: float = 35.0


@dataclass
class SourceConfig:
    type: str = "homeassistant"
    url: str = "http://homeassistant.local:8123"
    token: str = ""
    mode: str = "push"
    poll_interval_s: float = 1.0
    power_entity: str | None = None
    import_entity: str | None = None
    export_entity: str | None = None
    voltage_entity: str | None = None
    power_unit: str | None = None


@dataclass
class Config:
    serial: SerialConfig = field(default_factory=SerialConfig)
    meter: MeterConfig = field(default_factory=MeterConfig)
    failsafe: FailsafeConfig = field(default_factory=FailsafeConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
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


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("config file must contain a mapping at the top level")

    meter_raw = dict(raw.pop("meter", None) or {})
    identity = _build(IdentityConfig, meter_raw.pop("identity", None), "meter.identity")
    meter = _build(MeterConfig, meter_raw, "meter")
    meter.identity = identity

    cfg = Config(
        serial=_build(SerialConfig, raw.pop("serial", None), "serial"),
        meter=meter,
        failsafe=_build(FailsafeConfig, raw.pop("failsafe", None), "failsafe"),
        source=_build(SourceConfig, raw.pop("source", None), "source"),
        log_level=raw.pop("log_level", "INFO"),
        status_interval_s=float(raw.pop("status_interval_s", 10.0)),
    )
    if raw:
        raise ValueError(f"unknown top-level option(s): {sorted(raw)}")

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
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

    if cfg.meter.current_sign not in ("magnitude", "signed"):
        raise ValueError("meter.current_sign must be 'magnitude' or 'signed'")
    if cfg.meter.unknown_register_policy not in ("zero", "exception"):
        raise ValueError("meter.unknown_register_policy must be 'zero' or 'exception'")

    src = cfg.source
    if src.type != "homeassistant":
        raise ValueError(f"unsupported source.type {src.type!r}")
    if not src.token:
        raise ValueError("source.token is required (a Home Assistant long-lived token)")
    if not src.power_entity and not (src.import_entity and src.export_entity):
        raise ValueError(
            "set source.power_entity (a signed net-grid sensor), or both "
            "source.import_entity and source.export_entity"
        )
    if src.mode not in ("push", "poll"):
        raise ValueError("source.mode must be 'push' or 'poll'")

    if cfg.failsafe.current_a <= 0:
        raise ValueError("failsafe.current_a must be positive")
