# wallbox-modbus

My Wallbox Pulsar Plus will only do Power Boost (dynamic load management) if it
sees an INEPRO N1-CT energy meter on its RS485 port. This program pretends to be
that meter, fed with live grid power from Home Assistant.

I didn't want to buy the accessory, and the meter turned out to be a stock
Modbus device, so this was mostly a weekend of staring at a serial port.

## Status

Everything here comes from one charger, mine.

- **Works.** Pulsar Plus, single phase, Home Assistant. I've watched it throttle
  the charge when the house load goes up.
- **Untested.** Eco-Smart (solar). See [Solar](#solar).
- **Refuses to run.** Three-phase supplies. See [Three-phase](#three-phase).
- **Needs.** Linux, Python 3.10+, a USB-RS485 adapter, and access to the
  charger's terminal block.

Other Wallbox models (Copper SB, Commander 2, Quasar) use the same EMS docs and
will probably work, but nobody has tried. If you do,
[tell me what happened](CONTRIBUTING.md).

## How it works

The Power Boost accessory is an off-the-shelf
[INEPRO N1-CT](https://www.ineprometering.com/product/n1-ct-electricity-meter),
and the charger polls it as a plain Modbus RTU master:

| Layer | |
|---|---|
| Wiring | RS485: D+, D−, GND |
| Serial | **19200 8N1** |
| Slave id | 1 (it also scans 2 and 12) |
| Function | 03 to read, 06 to write |
| Encoding | big-endian float32 over two registers |

The N1-CT manual says 9600 8E1. That's wrong for this bus, Wallbox reconfigures
the meter it ships. I lost a day to that, because at 9600 the frames still
decode far enough to look like a parity problem. Set `baud: auto` and
`parity: auto` if you want the emulator to find it for you.

So: read the house load, put it on the registers the charger asks for, and the
charger does its own balancing. That's the right place for it, since the charger
is the only thing that knows how much it's drawing.

## The handshake

I couldn't find this documented anywhere, so here's what a Pulsar Plus does.

**It only looks for a meter for about two minutes after it boots**, then gives
up until the next restart. This is why the app keeps saying no meter is
connected while your wiring is perfectly fine. Start this program first, then
reboot the charger from the app.

During those two minutes it cycles three probes, roughly every 0.6 s, against
unit ids 1, 2 and 12:

| Probe | Looking for |
|---|---|
| 1 register at `0x000B` | Carlo Gavazzi id code (EM340 answers 340) |
| 1 register at `0x4002` | INEPRO meter code |
| 8 registers at `0x0000` and `0x0008` | Carlo Gavazzi measurement block |

**`0x4002` is the gate.** Answer `0x0102` and it starts polling measurements
60 ms later. `0x0103` is rejected, even though that's the CT variant code and an
N1-CT is a CT meter. Everything else can stay zero.

After that it settles into about 7.5 requests/second: voltage at `0x5002`,
current at `0x500C`, power at `0x5014`, energy at `0x6000` and `0x6018`. It
reads all three phases even from a single-phase meter. Zeros for L2 and L3 are
fine.

## Safety

- Kill the breaker before you open the charger. The RS485 terminals are low
  voltage but the rest of the box isn't.
- Opening it may void your warranty. Your call.
- **This thing guards a fuse.** If it lies about the house load, the charger
  will happily pull its maximum on top of everything else. Read
  [Failsafe](#failsafe).
- Set the charger's rotary switch low (16 A) until you've seen it throttle.
- It's not a certified load-management device and the MIT licence means what it
  says. The rotary switch is the only limit that survives this crashing.

## Hardware and wiring

A USB-**RS485** adapter with **A, B and GND** broken out. Not a USB-TTL
programmer, they look alike and TTL levels on A/B can damage things. Automatic
direction control (CH340, CH343, CP2102, FTDI) saves you fiddling with RTS.
CH340 and FTDI show up as `/dev/ttyUSB0`; CH343 and CH9102 are CDC-ACM and show
up as `/dev/ttyACM0`. TX/RX LEDs are worth it, they tell you the charger is
transmitting before any software works.

Behind the front cover there's a four-pin block:

| Charger | N1-CT | Your adapter |
|---|---|---|
| `D+` | 20 (A) | A / D+ |
| `D-` | 21 (B) | B / D− |
| `GND` | 24 | GND |
| `12V` | 23 | leave it, power your box separately |

**Connect GND.** RS485 is differential but not ground-referenced. Skipping it
works on the bench and fails in a meter cupboard.

Running it over spare pairs in Cat-5? Put D+ and D− on the two wires of *one*
twisted pair. That twist is the whole noise-rejection mechanism. Use a second
pair, both wires together, for ground.

Also set the **RS485 switch to `T`** (you're the only slave, so terminate), and
the **rotary switch** to your maximum charging current
(`1=6A 2=10A 3=13A 4=16A 5=20A 6=25A 7=32A`).

Then install the udev rules. They drop the FTDI latency timer from 16 ms to 1 ms,
which otherwise lands right on the Modbus turnaround, and give you a stable
device name so a second adapter can't steal `/dev/ttyUSB0`:

```bash
sudo cp udev/99-wallbox-rs485.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

## Install

```bash
git clone https://github.com/erwindamsma/wallbox-modbus /opt/wallbox-modbus
cd /opt/wallbox-modbus
python3 -m venv .venv
./.venv/bin/pip install .
cp config.example.yaml config.yaml
```

Three settings have no sensible default:

- `limits.installation_current_a` — your main fuse rating. Required. Set the
  same number under Load Management in the app.
- `source.token` — a Home Assistant long-lived token (profile → Security).
- `source.power_entity` — the sensor with net grid power, positive when
  importing. `--list-entities` will find it.

`failsafe.current_a` is derived from your fuse rating unless you set it.

As a service:

```bash
sudo useradd -r -G dialout wallbox
sudo install -D -m 640 -o wallbox config.yaml /etc/evse-meter/config.yaml
sudo cp systemd/evse-meter.service /etc/systemd/system/
sudo systemctl enable --now evse-meter
```

`config.yaml` has a token in it. It's gitignored and the unit installs it 0640.

## Bring-up

Do these in order, each one fails differently.

**1. Test the logic.** `./.venv/bin/python tools/selftest.py` — pty pair, no
hardware.

**2. Test the data.** `--list-entities` to find your sensor, then
`--check-source` to watch it live without touching the serial port. **Switch on
a kettle. The number must go up.** If it goes down your sensor is inverted, and
Power Boost would speed up exactly when it should back off.

**3. Test the stack.** Three terminals, no hardware:

```bash
./.venv/bin/python tools/vlink.py
./.venv/bin/python -m evse_meter -c config.yaml --port /tmp/wallbox-a --parity none
./.venv/bin/python tools/test_master.py /tmp/wallbox-b --parity N
```

Use `parity none` here. A pty has no UART and can't emulate parity.

**4. Test the wiring** with a second adapter over real RS485, A–A, B–B, GND–GND.

**5. Listen first.** Wire it to the charger, enable Power Boost in the app, run
with `--passive --log-level DEBUG`. It decodes but never transmits, so you can
see what the charger asks for. Leave baud and parity on `auto` and it'll log
what it locked onto.

*Seeing nothing at all? Swap D+ and D−. It's always that.*

**6. Answer it.** Drop `--passive`, reboot the charger from the app. Success is
a burst of `0x4000` reads followed by steady polling of `0x500A` and `0x5012`.

**7. Prove it throttles.** Start a charge, confirm it sits at 16 A, then switch
on an oven. The charge current should drop within seconds. That's the whole
point, and until you've seen it you don't have load management.

## When the charger won't accept the meter

1. **Is it still hunting?** Two minutes after boot, that's it. Restart it.
2. **Read the log.** Unmapped reads get logged once each as
   `charger read unmapped register 0x40XX`. A register it insists on that isn't
   in the N1-CT manual is the best clue you can get, and I'd like to see it.
3. **Check for writes.** `charger wrote 0x4004` means it's reconfiguring baud or
   parity. The emulator applies it and reopens the port.
4. **Try other identity values.** `meter_code` first, `[0x0102, 0x0103]` tries
   both in one detection window. Then `software_version`, then `serial_number`.
5. **Try `unknown_register_policy: exception`.** Answering zeros to everything
   is permissive, and a charger ruling models out may expect a refusal.
6. **Check the app.** Installer settings must have the meter set to N1-CT.
   Picking EM340 or the P1 module gives you a completely different register map.

## Three-phase

Not supported. `meter.phases` must be 1 and the program exits if it isn't.

The N1-CT is single phase and I only have one number to report, so L2 and L3
would read 0 A. The charger takes that as "those phases are idle" and draws
freely on them. It would look like it was working while protecting nothing,
which is worse than not running.

Doing it properly needs per-phase data and emulation of a meter Wallbox actually
pairs with three-phase installs (INEPRO N3, or the Carlo Gavazzi EM340 the
charger already probes for). I can't test either. PRs welcome.

## Limits

Two separate limits, doing separate jobs:

- **The rotary switch** caps the charger in its own firmware. It holds if this
  crashes, if the cable falls out, if Home Assistant dies, and if everything I
  think I know about Power Boost is wrong. Nothing here can override it.
- **`limits.installation_current_a`** is your fuse, and what the charger
  balances against. Match it to the app.

With a 35 A fuse and the switch at 16 A: idle house, charger takes 16 A. House
at 25 A, Power Boost allows 10 A. This program dies, charger is still capped at
16 A.

The emulator has no limit of its own on purpose. A meter can't address the
charger and can't separate the charger's draw from the rest of the house, so
anything it did here would mean lying about the measurement, and it would fall
apart while exporting.

## Solar

**I have not tested this.** Power Boost I proved with a kettle. Eco-Smart I've
never even switched on, and no charge session has run on solar surplus.

What I do know is that the meter reports export correctly: `0x5012` goes
negative, current stays an unsigned magnitude, reverse energy accumulates. That
matches what a real CT does, direction lives in the sign of the power.

What I don't know is whether the charger reads that sign or just the magnitude.
If it throttles while you're exporting, it's ignoring the sign, and
`current_sign: signed` should fix it. That setting exists precisely because I
couldn't test this. If you find out which one is right,
[please tell me](CONTRIBUTING.md).

## Failsafe

If no fresh reading arrives within `failsafe.max_data_age_s` (15 s), the meter
reports `failsafe.current_a` instead of the last value it had. The charger sees
the installation over its limit and backs off.

That number has to be **above** `installation_current_a` and the config won't
start otherwise. Reporting exactly the limit is a fixed point of the charger's
control loop (`allowance = limit − (limit − own_current)`), so it would just sit
there instead of backing off.

This matters more than it looks. Home Assistant restarting, wifi dropping, the
P1 reader hanging: all of them leave a perfectly plausible last reading. A stale
"house is drawing 300 W" is exactly what lets the charger sit at maximum while
the oven is on. Better to keep the link up and lie pessimistically, because a
charger that loses its meter may fault instead of throttling.

Energy counters are integrated from power and persisted, so they stay monotonic
across restarts like a real meter's would.

## Register map

From the N1 CT manual V1.17 section 9. Two registers each, big-endian float32
unless noted. `--dump-map` prints it with live values.

| Register | Contents | Type |
|---|---|---|
| `0x4000` | Serial number | int32 |
| `0x4002` | Meter code | int16 |
| `0x4003` | Modbus id | int16 |
| `0x4004` | Baud rate | int16 |
| `0x4005` | Protocol version | |
| `0x4007` | Software version | |
| `0x4009` | Hardware version | |
| `0x400B` | Meter amps | int32 |
| `0x400F` | Combination code | int16 |
| `0x4011` | Parity (1=even, 2=none, 3=odd) | int16 |
| `0x401B` | Software CRC | int32 |
| `0x5000` `0x5002` | Voltage, L1 voltage | V |
| `0x5008` | Frequency | Hz |
| `0x500A` `0x500C` | **Current**, L1 current | A |
| `0x5012` `0x5014` | **Active power**, L1 | kW |
| `0x501A` `0x5022` | Reactive, apparent power | kvar, kVA |
| `0x502A` | Power factor | |
| `0x6000` `0x6006` | Total energy, L1 | kWh |
| `0x600C` `0x6018` | Forward, reverse energy | kWh |

Writable with 06: `0x4003`, `0x4004`, `0x4011`, `0x400F`. The emulator accepts
those and reopens the port with the new settings.

**Power is in kW, not W.** Send watts and the charger thinks your house is
pulling 3450 kW.

## Layout

| File | |
|---|---|
| [rtu.py](evse_meter/rtu.py) | Modbus RTU slave: framing, CRC, resync, baud/parity probing |
| [n1ct.py](evse_meter/n1ct.py) | Register map, and logging what the charger asks for |
| [model.py](evse_meter/model.py) | Meter state, energy integration, failsafe |
| [sources/base.py](evse_meter/sources/base.py) | What a data source is; `source.type` picks one |
| [sources/homeassistant.py](evse_meter/sources/homeassistant.py) | WebSocket with REST fallback |
| [tools/selftest.py](tools/selftest.py) | End-to-end over a pty pair |
| [tools/test_master.py](tools/test_master.py) | Polls the emulator like the charger does |

Frames are delimited by expected length and CRC, not the usual 3.5-character
idle gap, because USB serial adapters buffer with milliseconds of jitter.

Home Assistant isn't special, it's just the only source written so far.
`source.type` picks a class out of a registry and that class brings its own
config options. MQTT or a P1 reader would be one module, see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Alternatives

Any of these may suit you better, and two of them are supported products:

- **Wallbox's own Power Boost accessory.** A meter, an install guide, and
  someone to call.
- **Wallbox's P1 module** (P1MB) reads the Dutch smart meter's P1 port. Same
  idea, supported, in the official EMS guide.
- **A real N1-CT**, wired as the accessory would be. No software to maintain.
- **Drive the charger instead.** The myWallbox API can set the charging current
  from Home Assistant. Cloud-dependent and slow (tens of seconds), so it's a
  convenience feature rather than fuse protection.

## Sources

- [INEPRO N1-CT manual](https://www.ineprometering.com/product/n1-ct-electricity-meter) — register map in section 9
- [Wallbox EMS installation guide](https://support.wallbox.com/wp-content/uploads/ht_kb/2024/09/EN_EMS_Installation-Guide.pdf) — wiring, terminals, rotary switch, meter compatibility
- [relyd/modbussniffer](https://github.com/relyd/modbussniffer) — a real capture of Pulsar Plus ↔ N1-CT traffic. Cited, not reused: that repo carries no licence, so nothing here is copied from it
- [Inepro register reference](https://www.aggsoft.com/modbus-data-logging/inepro-metering.htm)

## Licence

[MIT](LICENSE). Not affiliated with Wallbox Chargers S.L. or inepro Metering B.V.
