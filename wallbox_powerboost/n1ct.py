"""INEPRO N1-CT register map.

Taken from the official "N1 CT user manual V1.17" (inepro Metering B.V.),
section 9 "Modbus register file". All measurement values are big-endian
IEEE-754 float32 (the manual calls this "Float - (ABCD)") spanning two
consecutive registers; identity values are 16- or 32-bit signed integers.

Only function code 03 is documented, but we answer 04 identically.
"""

from __future__ import annotations

import logging
import struct

from .rtu import EX_ILLEGAL_ADDRESS, ModbusError

log = logging.getLogger(__name__)

# Identity / configuration block
REG_SERIAL_NUMBER = 0x4000       # int32
REG_METER_CODE = 0x4002          # int16
REG_MODBUS_ID = 0x4003           # int16
REG_BAUD_RATE = 0x4004           # int16, the value *is* the baud rate
REG_PROTOCOL_VERSION = 0x4005    # float32
REG_SOFTWARE_VERSION = 0x4007    # float32
REG_HARDWARE_VERSION = 0x4009    # float32
REG_METER_AMPS = 0x400B          # int32, CT primary rating
REG_COMBINATION_CODE = 0x400F    # int16
REG_PARITY = 0x4011              # int16: 1=even, 2=none, 3=odd
REG_SOFTWARE_CRC = 0x401B        # int32

# Measurement block
REG_VOLTAGE = 0x5000
REG_L1_VOLTAGE = 0x5002
REG_FREQUENCY = 0x5008
REG_CURRENT = 0x500A
REG_L1_CURRENT = 0x500C
REG_ACTIVE_POWER = 0x5012        # kW, signed
REG_L1_ACTIVE_POWER = 0x5014
REG_REACTIVE_POWER = 0x501A      # kvar
REG_L1_REACTIVE_POWER = 0x501C
REG_APPARENT_POWER = 0x5022      # kVA
REG_L1_APPARENT_POWER = 0x5024
REG_POWER_FACTOR = 0x502A
REG_L1_POWER_FACTOR = 0x502C

# Energy block
REG_TOTAL_ENERGY = 0x6000        # kWh
REG_L1_ENERGY = 0x6006
REG_FORWARD_ENERGY = 0x600C
REG_REVERSE_ENERGY = 0x6018

PARITY_CODES = {1: "E", 2: "N", 3: "O"}

REGISTER_NAMES = {
    REG_SERIAL_NUMBER: "Serial number",
    REG_METER_CODE: "Meter code",
    REG_MODBUS_ID: "Meter ID (Modbus)",
    REG_BAUD_RATE: "Baud rate",
    REG_PROTOCOL_VERSION: "Protocol version",
    REG_SOFTWARE_VERSION: "Software version",
    REG_HARDWARE_VERSION: "Hardware version",
    REG_METER_AMPS: "Meter amps",
    REG_COMBINATION_CODE: "Combination code",
    REG_PARITY: "Parity setting",
    REG_SOFTWARE_CRC: "Software version (CRC)",
    REG_VOLTAGE: "Voltage",
    REG_L1_VOLTAGE: "L1 voltage",
    REG_FREQUENCY: "Grid frequency",
    REG_CURRENT: "Current",
    REG_L1_CURRENT: "L1 current",
    REG_ACTIVE_POWER: "Total active power",
    REG_L1_ACTIVE_POWER: "L1 active power",
    REG_REACTIVE_POWER: "Total reactive power",
    REG_L1_REACTIVE_POWER: "L1 reactive power",
    REG_APPARENT_POWER: "Total apparent power",
    REG_L1_APPARENT_POWER: "L1 apparent power",
    REG_POWER_FACTOR: "Power factor",
    REG_L1_POWER_FACTOR: "L1 power factor",
    REG_TOTAL_ENERGY: "Total active energy",
    REG_L1_ENERGY: "L1 active energy",
    REG_FORWARD_ENERGY: "Forward active energy",
    REG_REVERSE_ENERGY: "Reverse active energy",
}

# Address ranges a real N1-CT responds within. Reads outside these are almost
# certainly the charger probing for a different meter model.
KNOWN_BLOCKS = ((0x4000, 0x40FF), (0x5000, 0x50FF), (0x6000, 0x60FF))


def f32(value: float) -> tuple[int, int]:
    raw = struct.pack(">f", float(value))
    return int.from_bytes(raw[:2], "big"), int.from_bytes(raw[2:], "big")


def i32(value: int) -> tuple[int, int]:
    raw = struct.pack(">i", int(value))
    return int.from_bytes(raw[:2], "big"), int.from_bytes(raw[2:], "big")


def u16(value: int) -> tuple[int, ...]:
    return (int(value) & 0xFFFF,)


def build_register_map(m, ident) -> dict[int, int]:
    """Render a measurement snapshot into {register: 16-bit word}."""
    regs: dict[int, int] = {}

    def put(addr: int, words) -> None:
        for offset, word in enumerate(words):
            regs[addr + offset] = word

    put(REG_SERIAL_NUMBER, i32(ident.serial_number))
    put(REG_METER_CODE, u16(ident.meter_code))
    put(REG_MODBUS_ID, u16(ident.modbus_id))
    put(REG_BAUD_RATE, u16(ident.baud_code))
    put(REG_PROTOCOL_VERSION, f32(ident.protocol_version))
    put(REG_SOFTWARE_VERSION, f32(ident.software_version))
    put(REG_HARDWARE_VERSION, f32(ident.hardware_version))
    put(REG_METER_AMPS, i32(ident.meter_amps))
    put(REG_COMBINATION_CODE, u16(ident.combination_code))
    put(REG_PARITY, u16(ident.parity_code))
    put(REG_SOFTWARE_CRC, i32(ident.software_crc))

    # Single-phase meter: the "total" and "L1" figures are the same thing.
    put(REG_VOLTAGE, f32(m.voltage))
    put(REG_L1_VOLTAGE, f32(m.voltage))
    put(REG_FREQUENCY, f32(m.frequency))
    put(REG_CURRENT, f32(m.current))
    put(REG_L1_CURRENT, f32(m.current))
    put(REG_ACTIVE_POWER, f32(m.active_power_kw))
    put(REG_L1_ACTIVE_POWER, f32(m.active_power_kw))
    put(REG_REACTIVE_POWER, f32(m.reactive_power_kvar))
    put(REG_L1_REACTIVE_POWER, f32(m.reactive_power_kvar))
    put(REG_APPARENT_POWER, f32(m.apparent_power_kva))
    put(REG_L1_APPARENT_POWER, f32(m.apparent_power_kva))
    put(REG_POWER_FACTOR, f32(m.power_factor))
    put(REG_L1_POWER_FACTOR, f32(m.power_factor))

    put(REG_TOTAL_ENERGY, f32(m.total_energy_kwh))
    put(REG_L1_ENERGY, f32(m.total_energy_kwh))
    put(REG_FORWARD_ENERGY, f32(m.forward_energy_kwh))
    put(REG_REVERSE_ENERGY, f32(m.reverse_energy_kwh))
    return regs


def in_known_block(addr: int) -> bool:
    return any(lo <= addr <= hi for lo, hi in KNOWN_BLOCKS)


class RegisterFile:
    """Serves the N1-CT map and records everything the charger asks for.

    The read/write logs are the whole point during bring-up: nobody has
    published what the Pulsar Plus checks before it accepts a meter, so we
    watch what it reads and whether it moves on to polling measurements.
    """

    def __init__(self, model, identity, policy: str = "zero", on_comm_change=None):
        self.model = model
        self.identity = identity
        self.policy = policy
        self.on_comm_change = on_comm_change
        self.read_counts: dict[int, int] = {}
        self.unmapped: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []

    def read(self, start: int, count: int) -> list[int]:
        regs = build_register_map(self.model.snapshot(), self.identity)
        out: list[int] = []
        for addr in range(start, start + count):
            self.read_counts[addr] = self.read_counts.get(addr, 0) + 1
            if addr in regs:
                out.append(regs[addr])
                continue
            if self.unmapped.get(addr) is None:
                log.info(
                    "charger read unmapped register 0x%04X%s",
                    addr, "" if in_known_block(addr) else " (outside any N1-CT block)",
                )
            self.unmapped[addr] = self.unmapped.get(addr, 0) + 1
            if self.policy == "exception":
                raise ModbusError(EX_ILLEGAL_ADDRESS)
            out.append(0)
        return out

    def write(self, addr: int, value: int) -> None:
        name = REGISTER_NAMES.get(addr, "unknown register")
        log.warning("charger wrote 0x%04X (%s) = 0x%04X (%d)", addr, name, value, value)
        self.writes.append((addr, value))
        ident = self.identity

        if addr == REG_MODBUS_ID and 1 <= value <= 247:
            ident.modbus_id = value
            self._comm_change(unit_id=value)
        elif addr == REG_BAUD_RATE:
            ident.baud_code = value
            self._comm_change(baud=value)
        elif addr == REG_PARITY and value in PARITY_CODES:
            ident.parity_code = value
            self._comm_change(parity=PARITY_CODES[value])
        elif addr == REG_COMBINATION_CODE:
            ident.combination_code = value
        # Anything else is accepted and ignored: refusing a write the real
        # meter would accept is a good way to fail the handshake.

    def _comm_change(self, **kwargs) -> None:
        if self.on_comm_change:
            self.on_comm_change(**kwargs)
