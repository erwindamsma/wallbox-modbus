# Contributing

The most valuable thing you can contribute is **what your charger did**. This
project is reverse-engineered from one Pulsar Plus and a handful of published
captures; every additional model either confirms the handshake or shows where
it is model-specific, and there is no way to find that out except from people
with different hardware.

## Reporting a charger

Open an issue with:

- the charger model, firmware version, and the meter model selected in the
  myWallbox installer settings;
- whether Power Boost activated, and how long it took;
- the tail of a `--log-level DEBUG` run, which lists every register the charger
  read and flags the ones outside the N1-CT map;
- if it did **not** work: the `charger read unmapped register 0x....` lines.
  A register the charger insists on that the N1-CT manual does not document is
  the single most useful clue there is — it means it is talking to a different
  meter model than we think.

Serial settings found by `baud: auto` / `parity: auto` are worth reporting even
when everything works. The 19200 8N1 in the example config comes from exactly
one charger and contradicts the meter's own manual.

## Running the tests

```bash
python tools/selftest.py
```

No hardware needed — it drives the real Modbus slave over a pty pair and checks
framing, CRC, resynchronisation, the register map, writes, the failsafe and
config validation. CI runs this on every push, plus a parse of
`config.example.yaml`. Both must pass.

If you change anything the charger sees on the wire, add a check. The whole
reason this file is a pty test rather than unit tests is that the interesting
bugs are in the bytes.

## Adding an energy source

`source.type` selects a class from a registry, so a new source is a module in
[wallbox_powerboost/sources/](wallbox_powerboost/sources/) and nothing else.
[base.py](wallbox_powerboost/sources/base.py) documents the interface and
[homeassistant.py](wallbox_powerboost/sources/homeassistant.py) is the worked
example. You need:

- a `SourceConfig` subclass holding your options — this is what makes them
  valid in `config.yaml`, and anything not on it is rejected as a typo;
- `async def run(self)`, looping until cancelled, calling
  `self.model.update(power_w=..., voltage=...)`;
- `@register` on the class, and an import in `sources/__init__.py`.

Two rules that are not negotiable, because a fuse depends on them:

1. **`power_w` is positive when importing.** If your source reports the
   opposite, negate it in the source, not in the model. A sign error makes the
   charger accelerate exactly when it should back off, and it will look fine
   until the first time the house is heavily loaded.
2. **Never invent a reading.** If you cannot get a fresh value, stop calling
   `update()` and let the failsafe trip. Repeating the last known value, or
   substituting zero, defeats the entire safety mechanism — a stale "the house
   is drawing 300 W" is precisely the input that lets a charger sit at maximum
   while the oven is on.

Dependencies that are not needed by everyone should be imported inside the
source module and declared as an optional extra in `pyproject.toml`.

## Things known to be missing

- **Three-phase installations.** See the README section; this needs a
  multi-phase model *and* emulation of a three-phase meter (INEPRO N3, or a
  Carlo Gavazzi EM340 — the charger probes for one at `0x000B`). Anyone with a
  three-phase supply and a spare afternoon would be doing everyone a favour.
- **Chargers other than the Pulsar Plus.** Copper SB, Commander 2 and Quasar
  use the same EMS documentation and are likely to work unchanged, but nobody
  has reported one.

## Style

Match what is there: comments explain *why*, especially where a value was
measured rather than reasoned. Where something is a guess, the code says so.
Please keep that — it is the difference between a reader trusting the confirmed
parts and trusting all of it equally.
