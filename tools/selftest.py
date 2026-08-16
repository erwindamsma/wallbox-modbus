#!/usr/bin/env python3
"""End-to-end self test over a pty pair -- no hardware needed.

Drives the real Modbus slave through a pseudo-terminal and checks framing,
CRC handling, resynchronisation, the register map, writes and the failsafe.

    python3 tools/selftest.py
"""

from __future__ import annotations

import logging
import os
import pathlib
import struct
import sys
import time

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from evse_meter import n1ct  # noqa: E402
from evse_meter.config import IdentityConfig, SerialConfig  # noqa: E402
from evse_meter.model import MeterModel  # noqa: E402
from evse_meter.n1ct import RegisterFile  # noqa: E402
from evse_meter.rtu import ModbusRtuSlave, append_crc, crc_ok, crc16  # noqa: E402

failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}")
    if not condition:
        failures.append(name)


def request(master_fd: int, frame: bytes, expect: int, timeout: float = 2.0) -> bytes:
    os.write(master_fd, frame)
    buf = b""
    deadline = time.monotonic() + timeout
    while len(buf) < expect and time.monotonic() < deadline:
        try:
            buf += os.read(master_fd, expect - len(buf))
        except BlockingIOError:
            time.sleep(0.005)
    return buf


def read_req(unit: int, addr: int, count: int) -> bytes:
    return append_crc(bytes([unit, 0x03]) + addr.to_bytes(2, "big") + count.to_bytes(2, "big"))


def as_float(payload: bytes) -> float:
    return struct.unpack(">f", payload)[0]


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    port = os.ttyname(slave_fd)

    model = MeterModel(nominal_voltage=230.0, current_sign="magnitude",
                       max_data_age_s=15.0, failsafe_current_a=35.0)
    identity = IdentityConfig()
    regfile = RegisterFile(model, identity, policy="zero")
    cfg = SerialConfig(port=port, unit_id=1, baud=9600, parity="E", probe_seconds=2.0)
    slave = ModbusRtuSlave(cfg, regfile)
    slave.start()
    time.sleep(0.3)

    print("crc")
    check("known-good frame validates",
          crc_ok(bytes.fromhex("01 03 04 3f a1 68 73 c9 e0".replace(" ", ""))),
          "captured N1-CT response, 1.25 A")
    check("crc16 matches the sniffed request",
          crc16(bytes.fromhex("0103500A0002")).to_bytes(2, "little").hex() == "f509",
          "01 03 50 0A 00 02 -> F5 09")

    print("\nmeasurements")
    model.update(power_w=3450.0, voltage=230.0)
    resp = request(master_fd, read_req(1, n1ct.REG_CURRENT, 2), 9)
    check("0x500A current is a valid response", len(resp) == 9 and crc_ok(resp), resp.hex(" "))
    if len(resp) == 9:
        check("current reads 15.0 A", abs(as_float(resp[3:7]) - 15.0) < 0.01,
              f"{as_float(resp[3:7]):.3f} A")

    resp = request(master_fd, read_req(1, n1ct.REG_ACTIVE_POWER, 2), 9)
    if len(resp) == 9:
        check("0x5012 active power reads 3.45 kW", abs(as_float(resp[3:7]) - 3.45) < 0.001,
              f"{as_float(resp[3:7]):.4f} kW")

    model.update(power_w=-2300.0, voltage=230.0)
    resp = request(master_fd, read_req(1, n1ct.REG_ACTIVE_POWER, 2), 9)
    if len(resp) == 9:
        check("export shows as negative power", as_float(resp[3:7]) < 0,
              f"{as_float(resp[3:7]):.4f} kW")
    resp = request(master_fd, read_req(1, n1ct.REG_CURRENT, 2), 9)
    if len(resp) == 9:
        check("current stays a magnitude while exporting", as_float(resp[3:7]) > 0,
              f"{as_float(resp[3:7]):.3f} A")

    print("\nidentity block")
    resp = request(master_fd, read_req(1, n1ct.REG_SERIAL_NUMBER, 2), 9)
    if len(resp) == 9:
        check("serial number round-trips",
              struct.unpack(">i", resp[3:7])[0] == identity.serial_number,
              str(struct.unpack(">i", resp[3:7])[0]))
    resp = request(master_fd, read_req(1, n1ct.REG_METER_AMPS, 2), 9)
    if len(resp) == 9:
        check("meter amps reads 100", struct.unpack(">i", resp[3:7])[0] == 100)

    # A charger sweeping the whole block in one go, as real masters tend to.
    resp = request(master_fd, read_req(1, 0x4000, 0x20), 5 + 0x40)
    check("a 32-register sweep of the identity block answers", len(resp) == 5 + 0x40 and crc_ok(resp),
          f"{len(resp)} bytes")

    print("\nrobustness")
    resp = request(master_fd, b"\x00\xff\x7e" + read_req(1, n1ct.REG_VOLTAGE, 2), 9)
    check("resynchronises after line garbage", len(resp) == 9 and crc_ok(resp))
    if len(resp) == 9:
        check("voltage still correct after resync", abs(as_float(resp[3:7]) - 230.0) < 0.01)

    back_to_back = read_req(1, n1ct.REG_VOLTAGE, 2) + read_req(1, n1ct.REG_CURRENT, 2)
    resp = request(master_fd, back_to_back, 18)
    check("two frames in one burst get two answers", len(resp) == 18 and crc_ok(resp[:9]) and crc_ok(resp[9:]))

    bad = bytearray(read_req(1, n1ct.REG_VOLTAGE, 2))
    bad[-1] ^= 0xFF
    resp = request(master_fd, bytes(bad), 9, timeout=0.5)
    check("a corrupt frame is not answered", len(resp) == 0, resp.hex(" "))

    resp = request(master_fd, read_req(2, n1ct.REG_VOLTAGE, 2), 9, timeout=0.5)
    check("a request for another unit id is ignored", len(resp) == 0)

    resp = request(master_fd, append_crc(bytes([1, 0x07])), 5, timeout=0.5)
    check("unsupported function returns exception 01",
          len(resp) == 5 and resp[1] == 0x87 and resp[2] == 0x01, resp.hex(" "))

    resp = request(master_fd, read_req(1, 0x7000, 2), 9)
    check("unmapped register answers zeros under the permissive policy",
          len(resp) == 9 and resp[3:7] == b"\x00\x00\x00\x00")
    check("unmapped read was recorded for later inspection", 0x7000 in regfile.unmapped)

    print("\nwrites")
    write = append_crc(bytes([1, 0x06]) + (0x400F).to_bytes(2, "big") + (5).to_bytes(2, "big"))
    resp = request(master_fd, write, 8)
    check("write single register is echoed", resp == write, resp.hex(" "))
    check("write was recorded", (0x400F, 5) in regfile.writes)

    print("\nfailsafe")
    strict = MeterModel(nominal_voltage=230.0, max_data_age_s=0.05, failsafe_current_a=35.0)
    strict.update(power_w=100.0, voltage=230.0)
    time.sleep(0.2)
    snap = strict.snapshot()
    check("stale data trips the failsafe", snap.stale)
    check("failsafe reports the installation limit", abs(snap.current - 35.0) < 0.01,
          f"{snap.current:.1f} A")
    check("failsafe power matches the failsafe current", abs(snap.active_power_kw - 8.05) < 0.01,
          f"{snap.active_power_kw:.2f} kW")
    strict.update(power_w=100.0, voltage=230.0)
    check("fresh data leaves the failsafe", not strict.snapshot().stale)

    print("\nthe meter reports measured values untouched")
    honest = MeterModel(nominal_voltage=230.0, max_data_age_s=60.0)
    honest.update(power_w=-10000.0, voltage=230.0)
    check("export is passed through as measured",
          abs(honest.snapshot().active_power_kw + 10.0) < 0.001)
    honest.update(power_w=2300.0, voltage=230.0)
    snap = honest.snapshot()
    check("import is passed through as measured",
          abs(snap.active_power_kw - 2.3) < 0.001 and abs(snap.current - 10.0) < 0.01,
          f"{snap.active_power_kw:.3f} kW / {snap.current:.2f} A")

    print("\nconfig validation")
    from evse_meter.config import Config, _validate  # noqa: E402

    def rejects(name, **kw):
        cfg = Config()
        cfg.source.token = "x"
        cfg.source.power_entity = "sensor.x"
        for key, value in kw.items():
            section, _, attr = key.partition("__")
            setattr(getattr(cfg, section), attr, value)
        try:
            _validate(cfg)
        except ValueError:
            check(name, True)
            return
        check(name, False, "was accepted")

    rejects("a failsafe at exactly the installation limit is rejected",
            failsafe__current_a=35.0)
    rejects("a failsafe below the installation limit is rejected",
            failsafe__current_a=20.0)
    rejects("a polyphase installation is rejected rather than half-protected",
            meter__phases=3)
    rejects("a nonsense current_sign is rejected", meter__current_sign="sideways")

    print("\nconfig file handling")
    import tempfile  # noqa: E402

    from evse_meter import config as config_mod  # noqa: E402

    base = {
        "limits": {"installation_current_a": 25.0},
        "source": {"type": "homeassistant", "token": "x", "power_entity": "sensor.x"},
    }

    def load_yaml(doc):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump(doc, fh)
            name = fh.name
        try:
            return config_mod.load(name)
        finally:
            os.unlink(name)

    def load_fails(name, doc, expect: str):
        try:
            load_yaml(doc)
        except ValueError as exc:
            check(name, expect in str(exc), f"raised: {str(exc).splitlines()[0]}")
            return
        check(name, False, "was accepted")

    loaded = load_yaml(base)
    check("a minimal config loads", loaded.limits.installation_current_a == 25.0)
    check("the failsafe is derived above the installation limit",
          loaded.failsafe.current_a > 25.0, f"{loaded.failsafe.current_a} A")
    check("the source config is the type's own dataclass",
          type(loaded.source).__name__ == "HomeAssistantConfig")

    load_fails("a missing fuse rating is refused, not defaulted",
               {k: v for k, v in base.items() if k != "limits"},
               "limits.installation_current_a is required")
    load_fails("an unknown source type names the ones that exist",
               {**base, "source": {**base["source"], "type": "carrier_pigeon"}},
               "unsupported source.type")
    load_fails("an option belonging to no source is a typo, not an extension",
               {**base, "source": {**base["source"], "urls": "http://x"}},
               "unknown option")
    load_fails("a misspelled top-level section is caught",
               {**base, "limit": {}},
               "unknown top-level option")

    print("\nenergy integration")
    acc = MeterModel(nominal_voltage=230.0, max_data_age_s=60.0)
    acc.update(power_w=3600.0)
    time.sleep(0.5)
    acc.update(power_w=3600.0)
    snap = acc.snapshot()
    check("forward energy accumulates", snap.forward_energy_kwh > 0,
          f"{snap.forward_energy_kwh * 1000:.4f} Wh in 0.5 s at 3.6 kW")
    check("reverse energy stays at zero", snap.reverse_energy_kwh == 0)
    acc.update(power_w=-3600.0)
    time.sleep(0.3)
    acc.update(power_w=-3600.0)
    check("export accumulates into reverse energy", acc.snapshot().reverse_energy_kwh > 0)

    print("\nshutdown")
    # Regression: the slave thread once held its stop flag in self._stop, which
    # shadows a private threading.Thread method that join() calls, so this line
    # raised "'Event' object is not callable" on 3.10 through 3.12. Assert it
    # rather than let a traceback escape and lose every result above.
    slave.stop()
    try:
        slave.join(timeout=2.0)
        check("the slave thread joins cleanly", not slave.is_alive())
    except Exception as exc:
        check("the slave thread joins cleanly", False, f"{type(exc).__name__}: {exc}")

    os.close(master_fd)
    os.close(slave_fd)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
