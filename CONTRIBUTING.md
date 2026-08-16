# Contributing

The most useful thing you can send me is **what your charger did**. I worked all
of this out from one Pulsar Plus, so every other model either confirms the
handshake or shows me where I've overfitted to mine.

## Reporting a charger

Open an issue with the model, the firmware version, the meter model selected in
the installer settings, and whether Power Boost came on. If it didn't work, the
tail of a `--log-level DEBUG` run is what I need, especially the
`charger read unmapped register 0x....` lines. A register your charger insists
on that isn't in the N1-CT manual means it thinks it's talking to a different
meter, and that's the most useful thing anyone can tell me.

**Scrub it before you paste.** At INFO level the log prints your Home Assistant
address and your entity names, and `--list-entities` prints the lot. None of
that helps me: the lines I need are the Modbus ones, and those say nothing about
your house.

Serial settings found by `baud: auto` / `parity: auto` are worth reporting even
when everything works. The 19200 8N1 in the example config comes from exactly
one charger and contradicts the meter's own manual.

## Wanted

- **Anyone who has actually charged on solar.** Eco-Smart is the biggest
  untested claim in the repo. I know the meter reports export correctly; I don't
  know whether the charger reads the sign or just the magnitude. Enable it, watch
  the charge current while you're exporting, and tell me whether `current_sign`
  wants `magnitude` or `signed`.
- **Three-phase.** Needs per-phase data and emulation of an INEPRO N3 or a Carlo
  Gavazzi EM340. I have a single-phase supply and can't test any of it.
- **Other chargers.** Copper SB, Commander 2, Quasar should work unchanged.

## Tests

```bash
python tools/selftest.py
```

No hardware. Drives the real Modbus slave over a pty pair and checks framing,
CRC, resync, the register map, writes, the failsafe and config validation. CI
runs it on 3.10–3.13 plus a parse of `config.example.yaml`.

If you change what the charger sees on the wire, add a check. The interesting
bugs here are all in the bytes.

## Adding a data source

A source is one module in [evse_meter/sources/](evse_meter/sources/).
[base.py](evse_meter/sources/base.py) has the interface and
[homeassistant.py](evse_meter/sources/homeassistant.py) is the worked example.
You need a `SourceConfig` subclass with your options, an `async def run(self)`
that loops until cancelled calling `self.model.update(...)`, and `@register` on
the class.

Two rules that aren't negotiable, because a fuse depends on them:

1. **`power_w` is positive when importing.** If your source is the other way
   round, flip it in the source, not the model. Get this wrong and the charger
   accelerates exactly when it should back off, and it'll look fine until the
   first time the house is loaded.
2. **Never invent a reading.** If you can't get a fresh value, stop calling
   `update()` and let the failsafe trip. Repeating the last value or
   substituting zero defeats the whole safety mechanism.

Put optional dependencies behind an extra in `pyproject.toml`.

## Style

Match what's there. Comments explain *why*, especially where a number was
measured rather than reasoned, and where something is still a guess it says so.
Please keep that. It's the only reason anyone can tell the confirmed parts from
the rest.
