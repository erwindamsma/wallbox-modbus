#!/usr/bin/env python3
"""Bench tool: poll the emulator the way the charger does.

Point it at a second USB-RS485 adapter wired A-A / B-B to the emulator's
adapter and you can verify the whole thing on a desk before opening the
charger.

    python3 tools/test_master.py /dev/ttyUSB1 --baud 9600 --parity E
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

import serial

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from evse_meter import n1ct  # noqa: E402
from evse_meter.rtu import append_crc, crc_ok  # noqa: E402

IDENTITY = [
    (n1ct.REG_SERIAL_NUMBER, 2, "i32"),
    (n1ct.REG_METER_CODE, 1, "u16"),
    (n1ct.REG_MODBUS_ID, 1, "u16"),
    (n1ct.REG_BAUD_RATE, 1, "u16"),
    (n1ct.REG_PROTOCOL_VERSION, 2, "f32"),
    (n1ct.REG_SOFTWARE_VERSION, 2, "f32"),
    (n1ct.REG_HARDWARE_VERSION, 2, "f32"),
    (n1ct.REG_METER_AMPS, 2, "i32"),
    (n1ct.REG_COMBINATION_CODE, 1, "u16"),
    (n1ct.REG_PARITY, 1, "u16"),
]

MEASUREMENTS = [
    (n1ct.REG_VOLTAGE, "Voltage", "V"),
    (n1ct.REG_FREQUENCY, "Frequency", "Hz"),
    (n1ct.REG_CURRENT, "Current", "A"),
    (n1ct.REG_ACTIVE_POWER, "Active power", "kW"),
    (n1ct.REG_TOTAL_ENERGY, "Total energy", "kWh"),
    (n1ct.REG_FORWARD_ENERGY, "Forward energy", "kWh"),
    (n1ct.REG_REVERSE_ENERGY, "Reverse energy", "kWh"),
]


def decode(raw: bytes, kind: str):
    if kind == "f32":
        return struct.unpack(">f", raw)[0]
    if kind == "i32":
        return struct.unpack(">i", raw)[0]
    return struct.unpack(">H", raw)[0]


def read(ser: serial.Serial, unit: int, addr: int, count: int) -> bytes | None:
    req = append_crc(bytes([unit, 0x03]) + addr.to_bytes(2, "big") + count.to_bytes(2, "big"))
    ser.reset_input_buffer()
    ser.write(req)
    ser.flush()
    expected = 5 + 2 * count
    deadline = time.monotonic() + 1.0
    buf = b""
    while len(buf) < expected and time.monotonic() < deadline:
        buf += ser.read(expected - len(buf))
    if len(buf) < expected:
        print(f"  0x{addr:04X}: timeout (got {buf.hex(' ') or 'nothing'})")
        return None
    if not crc_ok(buf):
        print(f"  0x{addr:04X}: bad CRC in {buf.hex(' ')}")
        return None
    if buf[1] & 0x80:
        print(f"  0x{addr:04X}: exception {buf[2]}")
        return None
    return buf[3:3 + 2 * count]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", default="E", choices=["E", "N", "O"])
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, bytesize=8, parity=args.parity,
                        stopbits=1, timeout=0.2)

    print(f"identity block (unit {args.unit} @ {args.baud} 8{args.parity}1):")
    for addr, count, kind in IDENTITY:
        raw = read(ser, args.unit, addr, count)
        if raw is not None:
            name = n1ct.REGISTER_NAMES.get(addr, "?")
            value = decode(raw, kind)
            # float32 cannot hold 1.17 exactly, so %g hides the rounding noise --
            # but only for floats, or an 8-digit serial becomes 2.025e+07.
            text = f"{value:g}" if kind == "f32" else str(value)
            print(f"  0x{addr:04X} {name:<24} {text}")

    print("\npolling measurements (Ctrl-C to stop):")
    try:
        while True:
            parts = []
            for addr, name, unit in MEASUREMENTS:
                raw = read(ser, args.unit, addr, 2)
                if raw is not None:
                    parts.append(f"{name} {struct.unpack('>f', raw)[0]:.3f} {unit}")
            print("  " + " | ".join(parts))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
