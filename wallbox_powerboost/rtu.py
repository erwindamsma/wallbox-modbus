"""A minimal Modbus RTU slave, shaped for pretending to be an energy meter.

Deliberately hand-rolled rather than built on pymodbus: the register map is
sparse and non-standard, we must answer *every* request the charger makes (and
log the ones we do not understand), and during bring-up the serial parameters
are not known with certainty.

Framing note: RTU normally delimits frames by a 3.5-character idle gap. USB
serial adapters buffer with millisecond-scale jitter, which makes gap timing
unreliable. Instead we derive the expected request length from the function
code and validate the CRC at that offset, resynchronising a byte at a time.
"""

from __future__ import annotations

import logging
import threading
import time

import serial

log = logging.getLogger(__name__)

FC_READ_COILS = 0x01
FC_READ_DISCRETE = 0x02
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_COIL = 0x05
FC_WRITE_SINGLE = 0x06
FC_WRITE_MULTIPLE_COILS = 0x0F
FC_WRITE_MULTIPLE = 0x10

EX_ILLEGAL_FUNCTION = 0x01
EX_ILLEGAL_ADDRESS = 0x02
EX_ILLEGAL_VALUE = 0x03
EX_SLAVE_FAILURE = 0x04

# Tried in order when serial settings are set to "auto". The N1-CT leaves the
# factory on 9600/even, but Wallbox may reconfigure the meter it ships with.
PROBE_CANDIDATES = (
    (9600, "E"),
    (9600, "N"),
    (19200, "E"),
    (19200, "N"),
    (9600, "O"),
    (19200, "O"),
)

PARITY_NAMES = {"E": "even", "N": "none", "O": "odd"}


class ModbusError(Exception):
    """Raised by a register file to request a Modbus exception response."""

    def __init__(self, code: int):
        super().__init__(f"modbus exception {code}")
        self.code = code


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_crc(frame: bytes) -> bytes:
    return frame + crc16(frame).to_bytes(2, "little")


def crc_ok(frame: bytes) -> bool:
    return len(frame) > 2 and crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def expected_request_len(buf: bytes) -> int | None:
    """Length of the request at the head of *buf*.

    Returns 0 when more bytes are needed to decide, and None when the function
    code is not one we can length-decode (caller should resynchronise).
    """
    if len(buf) < 2:
        return 0
    fc = buf[1]
    if fc in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06):
        return 8
    if fc in (0x0F, 0x10):
        if len(buf) < 7:
            return 0
        return 9 + buf[6]
    if fc in (0x07, 0x0B, 0x0C, 0x11):
        return 4
    return None


class ModbusRtuSlave(threading.Thread):
    def __init__(self, cfg, regfile, passive: bool = False):
        super().__init__(name="modbus-rtu", daemon=True)
        self.cfg = cfg
        self.regfile = regfile
        self.passive = passive
        self._stop = threading.Event()
        self._reopen = threading.Event()

        self.locked: tuple[int, str] | None = None
        self.requests = 0
        self.bad_frames = 0
        self.last_request_at: float | None = None

        fixed_baud = cfg.baud if isinstance(cfg.baud, int) else None
        fixed_parity = cfg.parity if cfg.parity in PARITY_NAMES else None
        if fixed_baud and fixed_parity:
            self._candidates = [(fixed_baud, fixed_parity)]
            self.locked = (fixed_baud, fixed_parity)
        elif fixed_baud:
            self._candidates = [(fixed_baud, p) for _, p in PROBE_CANDIDATES]
        elif fixed_parity:
            self._candidates = [(b, fixed_parity) for b, _ in PROBE_CANDIDATES]
        else:
            self._candidates = list(PROBE_CANDIDATES)
        # De-duplicate while keeping order.
        seen: set[tuple[int, str]] = set()
        self._candidates = [c for c in self._candidates if not (c in seen or seen.add(c))]

    def stop(self) -> None:
        self._stop.set()

    def request_reopen(self) -> None:
        """Ask the serial port to be re-opened (after a comm-parameter write)."""
        self._reopen.set()

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            for baud, parity in self._candidates:
                if self._stop.is_set():
                    return
                try:
                    self._serve(baud, parity)
                except serial.SerialException as exc:
                    log.error("serial error on %s: %s", self.cfg.port, exc)
                    time.sleep(2.0)
                except Exception:  # pragma: no cover - keep the bus alive
                    log.exception("unexpected error in Modbus slave")
                    time.sleep(1.0)
                if self.locked:
                    break  # _serve only returns on stop/reopen once locked

    def _open(self, baud: int, parity: str) -> serial.Serial:
        ser = serial.Serial(
            port=self.cfg.port,
            baudrate=baud,
            bytesize=8,
            parity=parity,
            stopbits=self.cfg.stopbits,
            timeout=0.005,
            write_timeout=1.0,
        )
        if self.cfg.rts_direction_control:
            ser.rts = not self.cfg.rts_active_high
        return ser

    def _serve(self, baud: int, parity: str) -> None:
        ser = self._open(baud, parity)
        mode = "passive" if self.passive else "active"
        if self.locked == (baud, parity):
            log.info(
                "listening on %s at %d 8%s%d (%s, unit id %d)",
                self.cfg.port, baud, parity, self.cfg.stopbits, mode, self.cfg.unit_id,
            )
        else:
            log.info(
                "probing %s at %d 8%s%d for %.0fs",
                self.cfg.port, baud, parity, self.cfg.stopbits, self.cfg.probe_seconds,
            )

        probe_deadline = None if self.locked else time.monotonic() + self.cfg.probe_seconds
        buf = bytearray()
        try:
            while not self._stop.is_set():
                if self._reopen.is_set():
                    self._reopen.clear()
                    log.info("re-opening serial port with new communication settings")
                    return
                chunk = ser.read(256)
                if chunk:
                    buf += chunk
                    self._consume(ser, buf, baud, parity)
                    if len(buf) > 512:
                        del buf[:-256]
                elif probe_deadline and time.monotonic() > probe_deadline and not self.locked:
                    log.info("no valid frames at %d 8%s%d, trying next setting",
                             baud, parity, self.cfg.stopbits)
                    return
        finally:
            try:
                ser.close()
            except Exception:
                pass

    def _consume(self, ser: serial.Serial, buf: bytearray, baud: int, parity: str) -> None:
        while len(buf) >= 4:
            want = expected_request_len(bytes(buf))
            if want == 0:
                return  # need more bytes to decide
            if want is None or (want and len(buf) >= want and not crc_ok(bytes(buf[:want]))):
                del buf[0]  # resynchronise
                self.bad_frames += 1
                continue
            if len(buf) < want:
                return
            frame = bytes(buf[:want])
            del buf[:want]
            self._on_frame(ser, frame, baud, parity)

    def _on_frame(self, ser: serial.Serial, frame: bytes, baud: int, parity: str) -> None:
        if not self.locked:
            self.locked = (baud, parity)
            log.warning(
                "LOCKED: valid Modbus traffic at %d 8%s%d -- the charger is talking to us",
                baud, parity, self.cfg.stopbits,
            )
        self.requests += 1
        self.last_request_at = time.monotonic()

        unit = frame[0]
        if unit not in (0, self.cfg.unit_id):
            log.debug("ignoring frame for unit %d: %s", unit, frame.hex(" "))
            return

        response = self._build_response(frame)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("rx %s  tx %s", frame.hex(" "),
                      response.hex(" ") if response else "(none)")
        if response is None or unit == 0 or self.passive:
            return
        self._send(ser, response)

    def _send(self, ser: serial.Serial, response: bytes) -> None:
        if self.cfg.rts_direction_control:
            ser.rts = self.cfg.rts_active_high
        try:
            ser.write(response)
            ser.flush()
        finally:
            if self.cfg.rts_direction_control:
                ser.rts = not self.cfg.rts_active_high
        if self.cfg.discard_echo:
            deadline = time.monotonic() + 0.05
            remaining = len(response)
            while remaining > 0 and time.monotonic() < deadline:
                got = ser.read(remaining)
                if not got:
                    break
                remaining -= len(got)

    # -- PDU handling ------------------------------------------------------

    def _build_response(self, frame: bytes) -> bytes | None:
        unit, fc = frame[0], frame[1]
        try:
            if fc in (FC_READ_HOLDING, FC_READ_INPUT):
                start = int.from_bytes(frame[2:4], "big")
                count = int.from_bytes(frame[4:6], "big")
                if not 1 <= count <= 125:
                    raise ModbusError(EX_ILLEGAL_VALUE)
                values = self.regfile.read(start, count)
                body = b"".join(v.to_bytes(2, "big") for v in values)
                return append_crc(bytes([unit, fc, len(body)]) + body)

            if fc == FC_WRITE_SINGLE:
                addr = int.from_bytes(frame[2:4], "big")
                value = int.from_bytes(frame[4:6], "big")
                self.regfile.write(addr, value)
                return append_crc(frame[:6])  # echo

            if fc == FC_WRITE_MULTIPLE:
                start = int.from_bytes(frame[2:4], "big")
                count = int.from_bytes(frame[4:6], "big")
                data = frame[7:-2]
                if len(data) != count * 2:
                    raise ModbusError(EX_ILLEGAL_VALUE)
                for i in range(count):
                    self.regfile.write(start + i, int.from_bytes(data[2 * i:2 * i + 2], "big"))
                return append_crc(frame[:6])

            log.warning("unsupported function code 0x%02X: %s", fc, frame.hex(" "))
            raise ModbusError(EX_ILLEGAL_FUNCTION)

        except ModbusError as exc:
            return append_crc(bytes([unit, fc | 0x80, exc.code]))
        except Exception:
            log.exception("failed to handle frame %s", frame.hex(" "))
            return append_crc(bytes([unit, fc | 0x80, EX_SLAVE_FAILURE]))
