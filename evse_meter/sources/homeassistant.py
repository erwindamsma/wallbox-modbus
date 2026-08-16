"""Home Assistant energy source.

Prefers the WebSocket API (push, so we see a new value the moment the P1
reader publishes one) and falls back to REST polling if the WebSocket path
keeps failing. Either way, a broken connection simply stops feeding the model,
which trips the failsafe after `failsafe.max_data_age_s`.

This is also the reference implementation for `sources/base.py`: a config
dataclass, a `run()` that never returns, and a `register()` decorator.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import aiohttp

from .base import EnergySource, SourceConfig, register

log = logging.getLogger(__name__)

UNIT_TO_WATTS = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0}
UNAVAILABLE = ("unknown", "unavailable", "none", "")


@dataclass
class HomeAssistantConfig(SourceConfig):
    url: str = "http://homeassistant.local:8123"
    token: str = ""
    # Preferred: one sensor holding net grid power, + importing, - exporting.
    power_entity: str | None = None
    # Alternative: a positive pair, subtracted. Used only without power_entity.
    import_entity: str | None = None
    export_entity: str | None = None
    # Optional; falls back to meter.nominal_voltage when absent.
    voltage_entity: str | None = None


@register
class HomeAssistantSource(EnergySource):
    type = "homeassistant"
    config_class = HomeAssistantConfig

    def __init__(self, cfg, model):
        super().__init__(cfg, model)
        self.base = cfg.url.rstrip("/")
        self.mode = cfg.mode
        self._states: dict[str, float | None] = {}
        self._units: dict[str, str | None] = {}
        self._push_failures = 0
        self._backoff = 1.0

        self.entities = [
            e for e in (cfg.power_entity, cfg.import_entity, cfg.export_entity, cfg.voltage_entity)
            if e
        ]

    @classmethod
    def validate(cls, cfg, complete: bool = True) -> None:
        if not cfg.url:
            raise ValueError("source.url is required (your Home Assistant address)")
        if not cfg.token:
            raise ValueError(
                "source.token is required: a Home Assistant long-lived access token, "
                "created under your profile -> Security"
            )
        # --list-entities exists to find out what these should be, so it must
        # run before they are filled in.
        if complete and not cfg.power_entity and not (cfg.import_entity and cfg.export_entity):
            raise ValueError(
                "set source.power_entity (a signed net-grid sensor), or both "
                "source.import_entity and source.export_entity"
            )

    def describe(self) -> str:
        return f"Home Assistant at {self.base}"

    @classmethod
    async def discover(cls, cfg) -> list[dict]:
        return await fetch_power_entities(cfg)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.token}"}

    async def run(self) -> None:
        # self._backoff, not a local: neither _run_push nor _run_poll ever
        # returns normally, so a reset placed after them here is dead code and
        # the delay only ever grows. They reset it themselves once connected.
        async with aiohttp.ClientSession(headers=self._headers) as session:
            while True:
                try:
                    await self._bootstrap(session)
                    if self.mode == "push":
                        await self._run_push(session)
                    else:
                        await self._run_poll(session)
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientConnectorError, OSError) as exc:
                    # Home Assistant is simply unreachable. Polling would not
                    # help, so keep retrying without counting this against the
                    # WebSocket transport.
                    self.connected = False
                    log.warning("cannot reach Home Assistant at %s: %s", self.base, exc)
                    await self._sleep_backoff()
                except Exception as exc:
                    self.connected = False
                    log.warning("Home Assistant connection problem: %s", exc)
                    if self.mode == "push":
                        self._push_failures += 1
                        if self._push_failures >= 3:
                            log.warning(
                                "the WebSocket API keeps failing, falling back to REST polling"
                            )
                            self.mode = "poll"
                    await self._sleep_backoff()

    async def _sleep_backoff(self) -> None:
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, 30.0)

    # -- transports --------------------------------------------------------

    async def _bootstrap(self, session: aiohttp.ClientSession) -> None:
        for entity in self.entities:
            async with session.get(f"{self.base}/api/states/{entity}", timeout=_t(10)) as resp:
                if resp.status == 404:
                    raise RuntimeError(f"entity {entity} does not exist in Home Assistant")
                resp.raise_for_status()
                data = await resp.json()
            self._set(entity, data.get("state"),
                      (data.get("attributes") or {}).get("unit_of_measurement"))
        self._publish()

    async def _run_push(self, session: aiohttp.ClientSession) -> None:
        ws_url = self.base.replace("https://", "wss://").replace("http://", "ws://")
        async with session.ws_connect(f"{ws_url}/api/websocket", heartbeat=25) as ws:
            hello = await ws.receive_json(timeout=10)
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"unexpected greeting from Home Assistant: {hello}")
            await ws.send_json({"type": "auth", "access_token": self.cfg.token})
            auth = await ws.receive_json(timeout=10)
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"authentication rejected: {auth.get('message', auth)}")

            await ws.send_json({
                "id": 1,
                "type": "subscribe_trigger",
                "trigger": {"platform": "state", "entity_id": self.entities},
            })
            result = await ws.receive_json(timeout=10)
            if not result.get("success"):
                raise RuntimeError(f"could not subscribe: {result}")

            self.connected = True
            self._push_failures = 0
            self._backoff = 1.0  # we are up; the next blip retries promptly
            log.info("subscribed to %s via the Home Assistant WebSocket API",
                     ", ".join(self.entities))

            async for msg in ws:
                if msg.type is aiohttp.WSMsgType.TEXT:
                    self._on_event(json.loads(msg.data))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            raise RuntimeError("WebSocket closed")

    async def _run_poll(self, session: aiohttp.ClientSession) -> None:
        self.connected = True
        self._backoff = 1.0
        log.info("polling %s every %.1fs", ", ".join(self.entities), self.cfg.poll_interval_s)
        while True:
            await self._bootstrap(session)
            await asyncio.sleep(self.cfg.poll_interval_s)

    def _on_event(self, data: dict) -> None:
        if data.get("type") != "event":
            return
        try:
            trigger = data["event"]["variables"]["trigger"]
            new = trigger["to_state"]
            entity = new["entity_id"]
        except (KeyError, TypeError):
            return
        self._set(entity, new.get("state"),
                  (new.get("attributes") or {}).get("unit_of_measurement"))
        self._publish()

    # -- value handling ----------------------------------------------------

    def _set(self, entity: str, state, unit) -> None:
        if state is None or str(state).lower() in UNAVAILABLE:
            self._states[entity] = None
            return
        try:
            self._states[entity] = float(state)
        except (TypeError, ValueError):
            log.debug("ignoring non-numeric state %r for %s", state, entity)
            self._states[entity] = None
            return
        if unit:
            self._units[entity] = unit

    def _watts(self, entity: str | None) -> float | None:
        if not entity:
            return None
        value = self._states.get(entity)
        if value is None:
            return None
        unit = self.cfg.power_unit or self._units.get(entity) or "W"
        return value * UNIT_TO_WATTS.get(unit, 1.0)

    def _publish(self) -> None:
        cfg = self.cfg
        if cfg.power_entity:
            power = self._watts(cfg.power_entity)
        else:
            imported = self._watts(cfg.import_entity)
            exported = self._watts(cfg.export_entity)
            power = None if imported is None or exported is None else imported - exported

        if power is None:
            log.debug("no usable grid power value yet")
            return

        voltage = self._states.get(cfg.voltage_entity) if cfg.voltage_entity else None
        self.model.update(power_w=power, voltage=voltage)


def _t(seconds: float) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=seconds)


# Words that suggest a sensor measures the grid connection rather than one
# appliance. English first, then the Dutch a P1 reader tends to produce.
GRID_HINTS = ("grid", "net", "p1", "smart_meter", "smartmeter", "consumption",
              "production", "import", "export", "vermogen", "levering", "teruglevering")


async def fetch_power_entities(cfg) -> list[dict]:
    """Every power sensor Home Assistant knows about, for picking one."""
    base = cfg.url.rstrip("/")
    headers = {"Authorization": f"Bearer {cfg.token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"{base}/api/states", timeout=_t(20)) as resp:
            if resp.status == 401:
                raise RuntimeError(
                    "Home Assistant rejected the token. Create a new long-lived "
                    "access token under your profile -> Security."
                )
            resp.raise_for_status()
            states = await resp.json()

    rows = []
    for state in states:
        attrs = state.get("attributes") or {}
        unit = attrs.get("unit_of_measurement")
        if attrs.get("device_class") != "power" and unit not in UNIT_TO_WATTS:
            continue
        entity = state.get("entity_id", "")
        name = str(attrs.get("friendly_name", ""))
        rows.append({
            "id": entity,
            "value": state.get("state"),
            "unit": unit,
            "name": name,
            "likely": any(h in entity.lower() or h in name.lower() for h in GRID_HINTS),
        })
    rows.sort(key=lambda r: (not r["likely"], r["id"]))
    return rows
