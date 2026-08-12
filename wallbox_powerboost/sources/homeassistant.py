"""Home Assistant energy source.

Prefers the WebSocket API (push, so we see a new value the moment the P1
reader publishes one) and falls back to REST polling if the WebSocket path
keeps failing. Either way, a broken connection simply stops feeding the model,
which trips the failsafe after `failsafe.max_data_age_s`.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

log = logging.getLogger(__name__)

UNIT_TO_WATTS = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0}
UNAVAILABLE = ("unknown", "unavailable", "none", "")


class HomeAssistantSource:
    def __init__(self, cfg, model):
        self.cfg = cfg
        self.model = model
        self.base = cfg.url.rstrip("/")
        self.mode = cfg.mode
        self._states: dict[str, float | None] = {}
        self._units: dict[str, str | None] = {}
        self._push_failures = 0
        self.connected = False

        self.entities = [
            e for e in (cfg.power_entity, cfg.import_entity, cfg.export_entity, cfg.voltage_entity)
            if e
        ]

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.token}"}

    async def run(self) -> None:
        backoff = 1.0
        async with aiohttp.ClientSession(headers=self._headers) as session:
            while True:
                try:
                    await self._bootstrap(session)
                    if self.mode == "push":
                        await self._run_push(session)
                    else:
                        await self._run_poll(session)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientConnectorError, OSError) as exc:
                    # Home Assistant is simply unreachable. Polling would not
                    # help, so keep retrying without counting this against the
                    # WebSocket transport.
                    self.connected = False
                    log.warning("cannot reach Home Assistant at %s: %s", self.base, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
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
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

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
