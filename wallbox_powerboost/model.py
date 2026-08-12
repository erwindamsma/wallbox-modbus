"""Meter state: what the emulated N1-CT currently reports.

Fed by an energy source (Home Assistant), read by the Modbus thread. The two
run in different threads, so updates are taken under a lock and readers get an
immutable snapshot.

The failsafe is the important part. This device sits between the charger and a
35 A main fuse: if our data goes stale we must not keep reporting the last
known -- possibly very low -- house load, or the charger will happily keep
pulling 32 A while the house adds another 20 A on top. When data is stale we
report a current at or above the installation limit, which drives the charger
down to its minimum and eventually to a stop.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Measurement:
    # What the grid actually did, before the charger cap is applied. Reported
    # values are the other fields; this one exists so logs can show both.
    measured_power_w: float
    voltage: float
    frequency: float
    current: float
    active_power_kw: float
    reactive_power_kvar: float
    apparent_power_kva: float
    power_factor: float
    total_energy_kwh: float
    forward_energy_kwh: float
    reverse_energy_kwh: float
    stale: bool
    age_s: float


class MeterModel:
    def __init__(
        self,
        *,
        nominal_voltage: float = 230.0,
        current_sign: str = "magnitude",
        max_data_age_s: float = 15.0,
        failsafe_current_a: float = 50.0,
        installation_current_a: float = 35.0,
        charger_max_current_a: float | None = None,
        energy_file: str | None = None,
        save_interval_s: float = 60.0,
    ):
        self.nominal_voltage = nominal_voltage
        self.current_sign = current_sign
        self.max_data_age_s = max_data_age_s
        self.failsafe_current_a = failsafe_current_a
        self.installation_current_a = installation_current_a
        self.charger_max_current_a = charger_max_current_a
        self.energy_file = Path(energy_file) if energy_file else None
        self.save_interval_s = save_interval_s

        self._lock = threading.Lock()
        self._power_w = 0.0
        self._voltage = nominal_voltage
        self._last_update: float | None = None
        self._last_integrate: float | None = None
        self._forward_kwh = 0.0
        self._reverse_kwh = 0.0
        self._last_save = 0.0
        self._warned_stale = False

        self._load_energy()

    # -- input -------------------------------------------------------------

    def update(self, power_w: float, voltage: float | None = None) -> None:
        """Report grid power at the connection point: + = import, - = export."""
        if not math.isfinite(power_w):
            log.warning("ignoring non-finite power value %r", power_w)
            return
        now = time.monotonic()
        with self._lock:
            if self._last_integrate is not None:
                dt = min(now - self._last_integrate, 60.0)
                kwh = abs(self._power_w) * dt / 3_600_000.0
                if self._power_w >= 0:
                    self._forward_kwh += kwh
                else:
                    self._reverse_kwh += kwh
            self._last_integrate = now
            self._power_w = float(power_w)
            if voltage and math.isfinite(voltage) and 100.0 < voltage < 300.0:
                self._voltage = float(voltage)
            self._last_update = now
            if self._warned_stale:
                log.info("energy data is flowing again, leaving failsafe")
                self._warned_stale = False
        self._maybe_save()

    # -- output ------------------------------------------------------------

    def snapshot(self) -> Measurement:
        with self._lock:
            now = time.monotonic()
            age = math.inf if self._last_update is None else now - self._last_update
            stale = age > self.max_data_age_s
            voltage = self._voltage or self.nominal_voltage
            measured_w = self._power_w

            if stale:
                if not self._warned_stale:
                    log.warning(
                        "%s -- reporting failsafe current %.1f A so the charger throttles back",
                        f"no energy data for {age:.0f}s" if math.isfinite(age)
                        else "no energy data received yet",
                        self.failsafe_current_a,
                    )
                    self._warned_stale = True
                current = self.failsafe_current_a
                power_w = self.failsafe_current_a * voltage
            else:
                measured_w = self._power_w
                power_w = self._apply_charger_cap(measured_w, voltage)
                current = abs(power_w) / voltage if voltage else 0.0
                if self.current_sign == "signed" and power_w < 0:
                    current = -current

            forward, reverse = self._forward_kwh, self._reverse_kwh

        apparent = abs(power_w) / 1000.0
        return Measurement(
            measured_power_w=measured_w,
            voltage=voltage,
            frequency=50.0,
            current=current,
            active_power_kw=power_w / 1000.0,
            reactive_power_kvar=0.0,
            apparent_power_kva=apparent,
            power_factor=1.0 if apparent else 0.0,
            # Combination code 05: total = forward + reverse.
            total_energy_kwh=forward + reverse,
            forward_energy_kwh=forward,
            reverse_energy_kwh=reverse,
            stale=stale,
            age_s=age if math.isfinite(age) else -1.0,
        )

    def _apply_charger_cap(self, power_w: float, voltage: float) -> float:
        """Bend the reported load so Power Boost settles at the charger cap.

        A meter cannot address the charger directly, and it cannot tell the
        charger's own draw apart from the rest of the house -- it sees one
        number at the connection point. What it can do is describe a house in
        which the charger's allowance works out to the cap.

        The charger's rule is roughly
            allowance = installation_limit - (meter_current - own_current)
        so adding a constant `installation_limit - cap` of phantom load moves
        the equilibrium from the fuse rating down to the cap, while real house
        load still subtracts from it exactly as before.

        The floor is the other half: never advertise more solar surplus than
        the cap, or Eco-Smart will happily ask for all of it.

        Both halves are one expression, which keeps it continuous and monotone
        across the import/export boundary -- a step there would make the
        charger hunt.
        """
        if self.charger_max_current_a is None:
            return power_w
        headroom_w = max(0.0, self.installation_current_a - self.charger_max_current_a) * voltage
        surplus_floor_w = -self.charger_max_current_a * voltage
        return max(power_w + headroom_w, surplus_floor_w)

    # -- energy counter persistence ---------------------------------------

    def _load_energy(self) -> None:
        if not self.energy_file or not self.energy_file.exists():
            return
        try:
            data = json.loads(self.energy_file.read_text())
            self._forward_kwh = float(data.get("forward_kwh", 0.0))
            self._reverse_kwh = float(data.get("reverse_kwh", 0.0))
            log.info(
                "restored energy counters: %.3f kWh forward, %.3f kWh reverse",
                self._forward_kwh, self._reverse_kwh,
            )
        except Exception as exc:
            log.warning("could not read %s: %s", self.energy_file, exc)

    def _maybe_save(self) -> None:
        if not self.energy_file:
            return
        now = time.monotonic()
        if now - self._last_save < self.save_interval_s:
            return
        self._last_save = now
        self.save()

    def save(self) -> None:
        if not self.energy_file:
            return
        try:
            self.energy_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.energy_file.with_suffix(".tmp")
            with self._lock:
                payload = {"forward_kwh": self._forward_kwh, "reverse_kwh": self._reverse_kwh}
            tmp.write_text(json.dumps(payload))
            tmp.replace(self.energy_file)
        except Exception as exc:
            log.warning("could not persist energy counters: %s", exc)
