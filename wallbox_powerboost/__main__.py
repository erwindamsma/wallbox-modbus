"""Entry point: pretend to be an INEPRO N1-CT so a Wallbox charger will do
dynamic load management and solar charging without the Power Boost accessory.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import config as config_mod
from . import n1ct
from .model import MeterModel
from .n1ct import RegisterFile, build_register_map
from .rtu import ModbusRtuSlave
from .sources.homeassistant import HomeAssistantSource

log = logging.getLogger("wallbox_powerboost")


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="wallbox-powerboost", description=__doc__)
    p.add_argument("-c", "--config", default="config.yaml", help="path to the YAML config")
    p.add_argument("--passive", action="store_true",
                   help="decode and log bus traffic but never transmit; use this to "
                        "watch what the charger asks for before answering it")
    p.add_argument("--dump-map", action="store_true",
                   help="print the register map that would be served and exit")
    p.add_argument("--log-level", help="override log_level from the config")
    return p.parse_args(argv)


def dump_map(cfg) -> None:
    model = MeterModel(
        nominal_voltage=cfg.meter.nominal_voltage,
        current_sign=cfg.meter.current_sign,
        max_data_age_s=cfg.failsafe.max_data_age_s,
        failsafe_current_a=cfg.failsafe.current_a,
    )
    model.update(power_w=2300.0, voltage=230.0)
    regs = build_register_map(model.snapshot(), cfg.meter.identity)
    print(f"{'reg':>6}  {'words':>21}  name")
    starts = sorted(a for a in regs if a in n1ct.REGISTER_NAMES)
    for addr in starts:
        name = n1ct.REGISTER_NAMES[addr]
        width = 1 if addr + 1 not in regs or addr + 1 in n1ct.REGISTER_NAMES else 2
        words = " ".join(f"{regs[addr + i]:04X}" for i in range(width))
        print(f"0x{addr:04X}  {words:>21}  {name}")


async def status_loop(cfg, model, slave, source, regfile) -> None:
    while True:
        await asyncio.sleep(cfg.status_interval_s)
        m = model.snapshot()
        link = "locked" if slave.locked else "searching"
        if slave.locked:
            link = f"{slave.locked[0]} 8{slave.locked[1]}{cfg.serial.stopbits}"
        unmapped = sorted(regfile.unmapped)
        log.info(
            "grid %+.0f W / %.2f A%s | modbus: %s, %d requests, %d bad frames%s | HA: %s",
            m.active_power_kw * 1000, m.current, " [FAILSAFE]" if m.stale else "",
            link, slave.requests, slave.bad_frames,
            f", unmapped {[f'0x{a:04X}' for a in unmapped[:8]]}" if unmapped else "",
            "connected" if source.connected else "disconnected",
        )


async def amain(cfg, args) -> int:
    model = MeterModel(
        nominal_voltage=cfg.meter.nominal_voltage,
        current_sign=cfg.meter.current_sign,
        max_data_age_s=cfg.failsafe.max_data_age_s,
        failsafe_current_a=cfg.failsafe.current_a,
        energy_file=cfg.meter.energy_file,
    )

    def on_comm_change(unit_id=None, baud=None, parity=None) -> None:
        if unit_id is not None:
            cfg.serial.unit_id = unit_id
        if baud is not None:
            cfg.serial.baud = baud
        if parity is not None:
            cfg.serial.parity = parity
        slave.request_reopen()

    regfile = RegisterFile(
        model, cfg.meter.identity,
        policy=cfg.meter.unknown_register_policy,
        on_comm_change=on_comm_change,
    )
    slave = ModbusRtuSlave(cfg.serial, regfile, passive=args.passive)
    source = HomeAssistantSource(cfg.source, model)

    if args.passive:
        log.warning("passive mode: listening only, the charger will get no answers")

    slave.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    tasks = [
        asyncio.create_task(source.run(), name="source"),
        asyncio.create_task(status_loop(cfg, model, slave, source, regfile), name="status"),
    ]
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        slave.stop()
        slave.join(timeout=3.0)
        model.save()
        if regfile.read_counts:
            polled = sorted(regfile.read_counts.items(), key=lambda kv: -kv[1])[:16]
            log.info(
                "registers the charger read: %s",
                ", ".join(f"0x{a:04X}({n})" for a, n in polled),
            )
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
    except FileNotFoundError:
        print(f"config file not found: {args.config}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, (args.log_level or cfg.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.dump_map:
        dump_map(cfg)
        return 0

    try:
        return asyncio.run(amain(cfg, args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
