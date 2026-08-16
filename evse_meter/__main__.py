"""Pretend to be an INEPRO N1-CT energy meter, so a Wallbox charger will do its
own dynamic load management from whatever data source you configure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import config as config_mod
from . import n1ct
from . import sources
from .model import MeterModel
from .n1ct import RegisterFile, build_register_map
from .rtu import ModbusRtuSlave

log = logging.getLogger("evse_meter")


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="evse-meter", description=__doc__)
    p.add_argument("-c", "--config", default="config.yaml", help="path to the YAML config")
    p.add_argument("--passive", action="store_true",
                   help="decode and log bus traffic but never transmit; use this to "
                        "watch what the charger asks for before answering it")
    p.add_argument("--dump-map", action="store_true",
                   help="print the register map that would be served and exit")
    p.add_argument("--list-entities", action="store_true",
                   help="list the readings the configured source can see, likeliest "
                        "grid sensors first, so you can pick one to measure")
    p.add_argument("--check-source", action="store_true",
                   help="connect to the energy source and print live readings without "
                        "touching the serial port; use this to verify entities, units "
                        "and the sign convention before wiring anything")
    p.add_argument("--log-level", help="override log_level from the config")
    p.add_argument("--port",
                   help="override serial.port, e.g. a virtual link from tools/vlink.py")
    p.add_argument("--baud", help="override serial.baud, e.g. 9600, or auto")
    p.add_argument("--parity", choices=["even", "none", "odd", "auto"],
                   help="override serial.parity; use none over a virtual link, which "
                        "has no UART and cannot emulate parity")
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


async def list_entities(cfg) -> int:
    source_cls = sources.get(cfg.source.type)
    try:
        rows = await source_cls.discover(cfg.source)
    except Exception as exc:
        print(f"could not reach the {cfg.source.type} source: {exc}", file=sys.stderr)
        return 1

    if rows is None:
        print(f"the {cfg.source.type} source has no list of readings to choose from; "
              "configure it by hand and check it with --check-source")
        return 1
    if not rows:
        print(f"the {cfg.source.type} source reports no power sensors at all. "
              "Is your P1 or energy integration set up?")
        return 1

    likely = [r for r in rows if r["likely"]]
    print(f"{len(rows)} power sensors, {len(likely)} look grid-related (listed first).")
    print("Pick the one holding NET grid power: positive importing, negative exporting.\n")
    print(f"{'id':<52} {'value':>12}  unit   name")
    for row in rows:
        mark = "*" if row["likely"] else " "
        unit = row["unit"] or "?"
        print(f"{mark}{row['id']:<51} {str(row['value']):>12}  {unit:<5}  {row['name']}")
    print("\nSet it as source.power_entity, then run --check-source.")
    print("No single signed sensor? Set source.import_entity and source.export_entity "
          "to a positive pair instead.")
    return 0


async def check_source(cfg, model, source) -> int:
    """Print what the meter would report, without serving Modbus."""
    print(f"connecting to {source.describe()} ...")
    print("\nWatch the sign: importing must be POSITIVE. Switch on a kettle — the")
    print("number should jump UP by a couple of kW. If it jumps down, your sensor is")
    print("inverted; swap import_entity/export_entity, or use a template sensor.\n")

    task = asyncio.create_task(source.run(), name="source")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    try:
        while not stop.is_set():
            m = model.snapshot()
            if m.stale:
                age = "never" if m.age_s < 0 else f"{m.age_s:.0f}s ago"
                print(f"  no usable reading ({age}) -- would report the "
                      f"{cfg.failsafe.current_a:.0f} A failsafe")
            else:
                print(f"  grid {m.active_power_kw * 1000:+8.0f} W   {m.current:6.2f} A   "
                      f"{m.voltage:5.1f} V   ({m.age_s:.1f}s old)")
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return 0


async def status_loop(cfg, model, slave, source, regfile) -> None:
    while True:
        await asyncio.sleep(cfg.status_interval_s)
        m = model.snapshot()
        link = "locked" if slave.locked else "searching"
        if slave.locked:
            link = f"{slave.locked[0]} 8{slave.locked[1]}{cfg.serial.stopbits}"
        unmapped = sorted(regfile.unmapped)
        log.info(
            "grid %+.0f W / %.2f A%s | modbus: %s, %d requests, %d bad frames%s | %s: %s",
            m.active_power_kw * 1000, m.current, " [FAILSAFE]" if m.stale else "",
            link, slave.requests, slave.bad_frames,
            f", unmapped {[f'0x{a:04X}' for a in unmapped[:8]]}" if unmapped else "",
            cfg.source.type, "connected" if source.connected else "disconnected",
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

    if args.list_entities:
        return await list_entities(cfg)

    if args.check_source:
        return await check_source(cfg, model, sources.create(cfg.source, model))

    regfile = RegisterFile(
        model, cfg.meter.identity,
        policy=cfg.meter.unknown_register_policy,
        on_comm_change=on_comm_change,
    )
    slave = ModbusRtuSlave(cfg.serial, regfile, passive=args.passive)
    source = sources.create(cfg.source, model)
    log.info("reading grid power from %s", source.describe())

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
        # Listing entities is how you find out what to put in power_entity, so
        # it must work before that setting is filled in.
        cfg = config_mod.load(args.config, require_source_entities=not args.list_entities)
        if args.port:
            cfg.serial.port = args.port
        if args.baud:
            cfg.serial.baud = args.baud if args.baud == "auto" else int(args.baud)
        if args.parity:
            cfg.serial.parity = config_mod.PARITY_ALIASES.get(args.parity, args.parity)
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
