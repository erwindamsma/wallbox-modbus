# wallbox-modbus — a Power Boost meter, without the Power Boost

Makes a Wallbox charger believe an **INEPRO N1-CT** energy meter is wired to its
RS485 port, so it will run **Power Boost** (dynamic load management) and
**Eco-Smart** (solar charging) using live data you already have — from Home
Assistant, or from anything else you care to plug in — instead of from the €200
accessory.

You supply one number, continuously: the power flowing through your grid
connection. The charger does the rest, in its own firmware, where the load
balancing belongs.

## Status

Reverse-engineered from one charger. The protocol findings below are measured,
not guessed, but they come from a sample of one — which is exactly why
[reports from other hardware](CONTRIBUTING.md) are the most useful thing you can
send.

| | |
|---|---|
| **Confirmed working** | Pulsar Plus, single-phase supply, Home Assistant as the data source |
| **Expected to work, unreported** | Copper SB, Commander 2, Quasar — same EMS documentation, same meter compatibility table |
| **Not supported** | Three-phase installations. The program refuses to start rather than half-protect one — [see below](#three-phase-installations) |
| **Requires** | Linux, Python 3.10+, a USB-RS485 adapter, and physical access to the charger's terminal block |

## Why this works

The EU Power Boost accessory is not a proprietary black box. It is an
[INEPRO Metering N1-CT](https://www.ineprometering.com/product/n1-ct-electricity-meter)
— an off-the-shelf single-phase DIN-rail meter — and the charger talks to it as
a plain **Modbus RTU master**:

| | |
|---|---|
| Physical layer | RS485, three wires (D+, D−, GND), plus 12 V if you want the charger to power your device |
| Serial | **19200 baud, 8 data bits, no parity, 1 stop bit** |
| Slave address | **1** (it also scans 2, and 12) |
| Function code | **03** (read holding registers); writes with 06 |
| Encoding | Big-endian IEEE-754 **float32** across two registers ("ABCD") |

Note the serial settings. The N1-CT manual documents 9600/even as the factory
default and that is wrong for this bus — Wallbox reconfigures the meter it
ships to **19200 8N1**. Trusting the manual costs you a day, because every
capture comes back as plausible-looking garbage rather than as silence. If your
charger disagrees, set `baud` and `parity` to `auto` and it will tell you what
it is actually using.

The register map is published in the N1-CT user manual and matches a
[real capture](https://github.com/relyd/modbussniffer) of Pulsar-Plus-to-meter
traffic (`01 03 50 0A 00 02` → `01 03 04 3F A1 68 73` = 1.25 A). Everything the
charger needs is in three blocks — identity at `0x4000`, measurements at
`0x5000`, energy at `0x6000`.

So: read the house load, present it on those registers, and the charger does the
rest. The load-balancing logic stays where it belongs — inside the charger,
which is the thing that knows how much current it is drawing.

```
  ┌──────────────┐   WebSocket   ┌──────────────┐   Modbus RTU    ┌─────────────┐
  │ energy       │──────────────▶│ this program │────────────────▶│   Wallbox   │
  │ source       │  grid power   │  (Linux box) │  RS485 @19200   │   charger   │
  └──────────────┘               └──────────────┘                 └─────────────┘
         ▲                                                               │
         │ P1 / smart meter                                              │ throttles
  ┌──────┴───────────────────────────────────────────────────────────────▼──────┐
  │                             main fuse                                       │
  └─────────────────────────────────────────────────────────────────────────────┘
```

The loop closes through the fuse: your grid measurement already includes the
charger's own draw, which is exactly what Power Boost expects.

## The handshake

Nobody had published a capture of the startup handshake, so this is what a
Pulsar Plus actually does. **It only looks for a meter for about two minutes
after booting**, then gives up until the next restart — which is why the app
insists no meter is connected even with everything correctly wired. Start this
program *before* the charger. Reboot the charger from the myWallbox app rather
than at the breaker: same effect, no switching transient on the bus to confuse
you.

During those two minutes it cycles three probes, roughly every 0.6 s, against
unit ids 1, 2 and 12:

| Probe | Looking for |
|---|---|
| read 1 register at `0x000B` | Carlo Gavazzi identification code (EM340 answers 340, EM330 answers 331) |
| read 1 register at `0x4002` | INEPRO meter code |
| read 8 registers at `0x0000` and at `0x0008` | a Carlo Gavazzi measurement block |

**`0x4002` is the gate.** Answer it with `0x0102` — INEPRO's direct-connect
variant code — and the charger stops hunting and starts polling measurements
within 60 ms. `0x0103`, the CT variant, is *not* accepted, despite the N1-CT
being a CT meter. Everything else can stay zero.

Once it accepts, the steady poll is about 7.5 requests per second:

| Register | Count | Contents |
|---|---|---|
| `0x5002` | 6 | voltage, L1 / L2 / L3 |
| `0x500C` | 6 | current, L1 / L2 / L3 |
| `0x5014` | 6 | active power, L1 / L2 / L3 |
| `0x6000` | 2 | total active energy |
| `0x6018` | 2 | reverse active energy |

It reads all three phases even from a meter that only has one. Answering zero
for L2 and L3 is correct for a single-phase installation and the charger is
happy with it — and is the reason a three-phase installation cannot use this.

If your charger wants something different, the emulator logs every register it
reads, flags the ones outside the N1-CT map, and logs every write — and
`meter_code` accepts a list of candidates to try within a single detection
window. See
[If the charger will not accept the meter](#if-the-charger-will-not-accept-the-meter).

## Safety

- **Isolate the charger** at the breaker before opening it. The RS485 terminals
  themselves are extra-low voltage, but you are working inside a unit that is
  otherwise on 230 V. If that sentence gives you pause, this is a job for an
  electrician.
- Opening the charger may affect your warranty. The wiring is exactly what a
  real Power Boost installation requires, but it is your call.
- **This protects a fuse.** Read [Failsafe](#failsafe) before leaving it
  unattended. If the emulator lies about the house load, the charger will
  cheerfully pull its maximum on top of whatever else is running.
- Set the charger's own limit conservatively at first (16 A), confirm the
  throttling actually happens, and only then raise it.
- Nothing here is certified as a load-management device, and the MIT licence
  means exactly what it says about warranty. The charger's internal rotary
  switch is the only limit that survives this program crashing — set it as if
  this program did not exist.

## Hardware

- A USB-RS485 adapter with **A, B and GND** broken out — a floating ground
  works on the bench and fails intermittently in a meter cupboard. Prefer one
  with automatic direction control (CH340, CH343, CP2102 or FTDI based); if
  yours needs RTS keying, set `serial.rts_direction_control: true`. Transient
  protection (TVS, resettable fuse) is worth having on a cable that runs to a
  charger. Status LEDs for TX and RX are worth more than they look — they tell
  you the charger is transmitting before any software works.
- Note the device name: CH340/FTDI appear as `/dev/ttyUSB0`, while CH343 and
  CH9102 are CDC-ACM devices and appear as `/dev/ttyACM0`.
- Three-core shielded cable from the box running this to the charger (the
  Wallbox guide specifies STP Cat-5e, up to 500 m). If the charger is outdoors
  and your Linux box is not, run the cable rather than putting a microcontroller
  in the weather.
- Optionally a second USB-RS485 adapter, to test on the bench with
  [tools/test_master.py](tools/test_master.py) before touching the charger.

## Wiring

The charger has a four-pin terminal block behind the front cover, labelled
`12V  GND  D+  D-`. A real N1-CT connects like this — your adapter takes the
meter's place:

| Charger | N1-CT terminal | Your USB-RS485 adapter |
|---|---|---|
| `D+` | 20 (A) | A / D+ |
| `D-` | 21 (B) | B / D− |
| `GND` | 24 (GND) | GND |
| `12V` | 23 (12V) | *leave unconnected* — power your box from its own supply |

**GND must be connected.** RS485 is differential but not ground-referenced;
skipping it works on the bench and fails intermittently in a meter cupboard.

If you are running this over spare pairs in a Cat-5 cable, put D+ and D− on the
two conductors of *one* twisted pair — that is what makes the differential
signalling work — and use a second pair, both conductors together, for ground.

Wallbox's own P1-port module is wired the same way and takes its power from the
P1 port rather than the charger, so leaving `12V` unused is a supported layout,
not a hack.

Inside the charger, also set:

- the **RS485 switch to position `T`** (bus termination — you are the only
  slave, so termination belongs at both ends);
- the **rotary switch to the maximum charging current**. Positions map to
  `1=6A  2=10A  3=13A  4=16A  5=20A  6=25A  7=32A`. Start at **position 4
  (16 A)** — see [Capping the charger](#capping-the-charger).

## Serial port setup

Plug the adapter in and find it with `dmesg | tail`. Then install the udev
rules, which do two things worth having:

```bash
sudo cp udev/99-wallbox-rs485.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

- **FTDI latency.** FTDI chips hold received bytes for up to 16 ms before
  passing them to userspace, which lands directly on our Modbus response
  turnaround. The rule drops it to 1 ms.
- **A stable device name.** `/dev/ttyUSB0` is a race: plug in a second adapter,
  or reboot with another USB serial device present, and the service may open
  the wrong one. Uncomment the `SYMLINK` line with your adapter's serial
  number and point `serial.port` at `/dev/wallbox-rs485`.

## Install

```bash
git clone https://github.com/erwindamsma/wallbox-modbus /opt/wallbox-modbus
cd /opt/wallbox-modbus
python3 -m venv .venv
./.venv/bin/pip install .

cp config.example.yaml config.yaml
$EDITOR config.yaml
```

That puts a `wallbox-powerboost` command in the venv; `python -m
wallbox_powerboost` works identically and is what the examples below use.

Three settings have no safe default and the program will tell you so:

| Setting | |
|---|---|
| `limits.installation_current_a` | **Required.** The rating of the fuse or main breaker the charger shares with the house. Set the same number as "maximum current per phase" in the myWallbox app. |
| `source.token` | A Home Assistant long-lived access token: profile → Security. |
| `source.power_entity` | The entity holding net grid power — positive importing, negative exporting. `--list-entities` finds it for you. |

If your integration gives separate import and export sensors instead, set
`import_entity` and `export_entity` and the emulator subtracts them.
`failsafe.current_a` is derived from your fuse rating unless you set it.

Once it works, install the service:

```bash
sudo useradd -r -G dialout wallbox
sudo install -D -m 640 -o wallbox config.yaml /etc/wallbox-powerboost/config.yaml
sudo cp systemd/wallbox-powerboost.service /etc/systemd/system/
sudo systemctl enable --now wallbox-powerboost
journalctl -fu wallbox-powerboost
```

`config.yaml` holds an access token to your home automation. It is gitignored
here, and the unit installs it `0640` — keep it that way.

## Bring-up

Work through this in order — each step fails in a way that tells you something.

**1. Test the logic — no hardware needed.**

```bash
python tools/selftest.py
```

Drives the real Modbus slave over a pty pair and checks framing, CRC,
resynchronisation, the register map, writes, the failsafe and config
validation.

**2. Test your data source — no hardware needed.** Fill in `url` and `token` in
`config.yaml`, then find the sensor to use:

```bash
./.venv/bin/python -m wallbox_powerboost -c config.yaml --list-entities
```

This lists every power sensor the source knows about, ones whose name looks
grid-related first, with their current values. Put the right one in
`source.power_entity` and check it:

```bash
./.venv/bin/python -m wallbox_powerboost -c config.yaml --check-source
```

Connects and prints what the meter would report, without touching the serial
port. This catches the mistakes that are painful to debug later: a wrong entity
id, a sensor in kW when you assumed W, and above all the **sign convention**.
Switch on a kettle — the number must jump *up*. If it drops, your sensor is
inverted and Power Boost would speed up exactly when it should back off.

**3. Test the whole stack — still no hardware.** In three terminals:

```bash
./.venv/bin/python tools/vlink.py     # links /tmp/wallbox-a <-> /tmp/wallbox-b
./.venv/bin/python -m wallbox_powerboost -c config.yaml --port /tmp/wallbox-a --parity none
./.venv/bin/python tools/test_master.py /tmp/wallbox-b --parity N
```

`--port` and `--parity` override the config, so the same `config.yaml` you will
run for real works here without edits.

`test_master.py` reads the identity block and then polls the same registers the
charger does, printing decoded values. If the current it reports tracks your
real house load, everything except the wiring is finished. Use `parity: none`
on both sides here — a pseudo-terminal has no UART and does not emulate parity.

**4. Test the wiring.** Same as above but over real RS485, with a second
USB-RS485 adapter wired A–A, B–B, GND–GND, and the real parity restored. This is
the first step that can tell you anything about cabling and termination.

**5. Listen to the charger before answering it.** Wire it up, enable Power Boost
in the app, and run:

```bash
./.venv/bin/python -m wallbox_powerboost -c config.yaml --passive --log-level DEBUG
```

Passive mode decodes traffic but never transmits. You will see the charger's
requests and, importantly, its retry pattern. This is also how you confirm the
serial settings: leave `baud` and `parity` on `auto` and the emulator probes
each combination until CRC-valid frames appear, then logs
`LOCKED: valid Modbus traffic at 19200 8N1`. Pin those values in the config
afterwards.

If you see nothing at all: swap D+ and D−. It is the usual answer.

**6. Answer it.** Drop `--passive`, and restart the charger from the app so it
starts hunting again. Success looks like a short burst of reads in the `0x4000`
range followed by **steady polling of `0x500A` and `0x5012`** about once a
second. That steady poll *is* the handshake succeeding.

**7. Prove the throttling.** With the rotary switch at 16 A, start a charge and
confirm it settles there. Then switch on an oven or a kettle and watch the
charging current drop below 16 A within a few seconds — that is Power Boost
working, as opposed to the charger merely sitting at its own limit. Verify in
the myWallbox app that it shows a reduced limit rather than an error.

## If the charger will not accept the meter

The symptom is unmistakable: the charger keeps re-reading the identity block and
never settles into polling measurements, or Power Boost switches itself off in
the app. Work down this list.

1. **Is it still hunting?** It stops looking about two minutes after it boots.
   Restart it from the app, with this program already running.
2. **Read the logs.** Every unanswered register is logged once as
   `charger read unmapped register 0x40XX`, and the status line lists them. A
   register the charger insists on that the N1-CT manual does not document is
   the single most valuable clue you can get — it means the charger is talking
   to a different meter model than we think. Please
   [report it](CONTRIBUTING.md).
3. **Check the writes.** `charger wrote 0x4004 …` means it is reconfiguring the
   meter's baud rate or parity. The emulator applies those and reopens the port
   automatically, but the log tells you it happened.
4. **Try the identity values.** In order of suspicion: `meter_code` (set it to
   `[0x0102, 0x0103]` to have both tried inside one detection window), then
   `software_version` / `protocol_version`, then `serial_number`.
5. **Try `unknown_register_policy: exception`.** Answering zeros to everything is
   permissive, but a charger probing for *which* meter it has may use an
   exception response to rule models out — and a meter that answers everything
   may look wrong.
6. **Check the app.** The meter model is selected in the installer settings; it
   must be set to the N1-CT (`N1CT` in Wallbox's compatibility table). Selecting
   an EM340 or a P1 module makes the charger speak a different register map
   entirely.

## Three-phase installations

**Not supported. `meter.phases` must be 1, and the program exits with an
explanation if it is not.**

This is a deliberate refusal rather than a missing feature. The emulated N1-CT
is a single-phase meter, and this program has exactly one grid power figure to
report, so it would answer **0 A for L2 and L3**. A charger reads that as "those
phases are idle" and will happily draw its full current on them no matter how
loaded your supply actually is — which is the opposite of what you installed
this for, while looking like it works. Silently under-protecting two thirds of a
connection is worse than not running at all.

Making it work properly needs two things, and
[contributions are welcome](CONTRIBUTING.md):

- a meter model that carries per-phase power, current and voltage rather than
  one aggregate;
- emulation of a meter that Wallbox actually pairs with three-phase
  installations — an INEPRO N3, or the Carlo Gavazzi EM340 that the charger
  already probes for at `0x000B` during the handshake.

Until then, a three-phase household wanting load management should look at the
[alternatives](#alternatives-if-this-stalls).

## Capping the charger

Set the charger's internal **rotary switch** to the highest current you are
willing to have drawn with no load management at all — **position 4 (16 A)** is
the right place to start. That is the cap, and the switch is the right place for
it: enforced by the charger's own firmware, so it holds if this service crashes,
if the RS485 cable falls out, if your data source is down, and if every
assumption in this repo about the Power Boost algorithm turns out to be wrong.
Nothing here can override it.

Leave load management aimed at the real fuse — "maximum current per phase" in
the app, and `limits.installation_current_a` here to match.

The two limits then do separate jobs, which is why this combination behaves
well in every case. Taking a 35 A fuse and a 16 A cap as the example:

| Situation | Result |
|---|---|
| House idle | Charger takes 16 A, its own maximum. Total is half the fuse. |
| House drawing 25 A | Power Boost allows 10 A, below the cap, so it throttles. |
| Exporting solar | Eco-Smart uses the real surplus, and the switch stops it at 16 A. |
| This service dies | Charger is still hard-limited to 16 A against a 35 A fuse. |

The meter reports measured values untouched, so what you see in the app is what
your house is actually doing.

The emulator deliberately has no current limit of its own. A meter cannot
address the charger and cannot tell the charger's draw apart from the rest of
the house — it only sees one number at the connection point — so anything it
did here would amount to lying about the load, and would come apart exactly
when you are exporting. The limit belongs in the charger.

## Solar charging

A real CT reports current as an unsigned magnitude and puts the direction in the
**sign of active power**. This emulator reproduces that faithfully
(`current_sign: magnitude`), so `0x5012` goes negative when you export.

If testing shows the charger throttling *while you are exporting* — meaning it
reads current and ignores the sign — switch to `current_sign: signed`. That
stops being a faithful N1-CT emulation, but it is the correct behaviour, and it
costs nothing to try.

## Failsafe

If no fresh reading arrives within `failsafe.max_data_age_s` (default 15 s), the
emulator reports `failsafe.current_a` instead of the last known value. The
charger sees the installation well past its limit and backs down to its minimum.

That value has to sit *above* `limits.installation_current_a`, and the config
refuses to start otherwise. Reporting exactly the limit is a fixed point of the
charger's control loop — `allowance = limit - (limit - own_current) = own_current`
— so it would hold whatever current it was already drawing rather than back off.
Overshooting the limit is what forces it down. Left unset, it is derived as 1.5×
your installation limit.

This matters more than it looks. Your home automation restarting, Wi-Fi
dropping, or the P1 reader hanging all leave the last reading looking perfectly
plausible — and a stale "house is drawing 300 W" is exactly the input that lets
the charger sit at maximum while the induction hob is on. Keeping the Modbus
link alive and reporting a *pessimistic* value is safer than going silent,
because a charger that loses its meter may fault rather than throttle.

Energy counters are integrated from power and persisted to
`meter.energy_file`, so they stay monotonic across restarts the way a real
meter's would.

## Register map

From the N1 CT user manual V1.17, section 9. Every value is 2 registers unless
noted; float32 is big-endian ABCD. `--dump-map` prints this with live values.

| Register | Content | Type | Unit |
|---|---|---|---|
| `0x4000` | Serial number | int32 | |
| `0x4002` | Meter code | int16 | |
| `0x4003` | Meter ID (Modbus) | int16 | |
| `0x4004` | Baud rate | int16 | |
| `0x4005` | Protocol version | float32 | |
| `0x4007` | Software version | float32 | |
| `0x4009` | Hardware version | float32 | |
| `0x400B` | Meter amps | int32 | A |
| `0x400F` | Combination code | int16 | |
| `0x4011` | Parity (1=even, 2=none, 3=odd) | int16 | |
| `0x401B` | Software version (CRC) | int32 | |
| `0x5000` | Voltage | float32 | V |
| `0x5002` | L1 voltage | float32 | V |
| `0x5008` | Grid frequency | float32 | Hz |
| `0x500A` | **Current** | float32 | A |
| `0x500C` | L1 current | float32 | A |
| `0x5012` | **Total active power** | float32 | kW |
| `0x5014` | L1 active power | float32 | kW |
| `0x501A` | Total reactive power | float32 | kvar |
| `0x5022` | Total apparent power | float32 | kVA |
| `0x502A` | Power factor | float32 | |
| `0x6000` | Total active energy | float32 | kWh |
| `0x6006` | L1 active energy | float32 | kWh |
| `0x600C` | Forward active energy | float32 | kWh |
| `0x6018` | Reverse active energy | float32 | kWh |

Writable with function 06: `0x4003` Modbus ID, `0x4004` baud rate,
`0x4011` parity, `0x400F` combination code. The emulator accepts these and
reopens the serial port with the new settings.

Note the units: **power is in kW, not W**. Reporting watts here would make the
charger think your house is drawing 3450 kW.

## Layout

| Path | |
|---|---|
| [wallbox_powerboost/rtu.py](wallbox_powerboost/rtu.py) | Modbus RTU slave: framing, CRC, resync, baud/parity probing |
| [wallbox_powerboost/n1ct.py](wallbox_powerboost/n1ct.py) | N1-CT register map and the register file that logs what the charger asks |
| [wallbox_powerboost/model.py](wallbox_powerboost/model.py) | Meter state, energy integration, failsafe |
| [wallbox_powerboost/sources/base.py](wallbox_powerboost/sources/base.py) | What an energy source is, and the registry `source.type` selects from |
| [wallbox_powerboost/sources/homeassistant.py](wallbox_powerboost/sources/homeassistant.py) | WebSocket subscription with REST fallback |
| [tools/selftest.py](tools/selftest.py) | End-to-end test over a pty pair, no hardware |
| [tools/test_master.py](tools/test_master.py) | Polls the emulator the way the charger does |

RTU frames are delimited by expected length and CRC rather than by the usual
3.5-character idle gap, because USB serial adapters buffer with millisecond
jitter and gap timing is unreliable through them.

Home Assistant is the only data source shipped, but it is not privileged:
`source.type` picks a class out of a registry and that class brings its own
config options with it. Adding MQTT, a Shelly EM, or a P1 reader read directly
over serial is one module — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Reports from chargers other than a Pulsar Plus are the most valuable thing this
project can receive, and three-phase support is the largest thing it is
missing. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Alternatives, if this stalls

- **Wallbox's own P1 module** (P1MB, ~€209) reads the Dutch smart meter's P1
  port and speaks Modbus to the charger — the same idea, supported, and it
  appears in the official EMS installation guide.
- **A real N1-CT** (~€80) wired as the accessory would be. Cheaper than the
  branded kit, and no software to maintain.
- **Control the charger instead of feeding it a meter.** The myWallbox API lets
  Home Assistant set the maximum charging current directly. It is cloud-
  dependent and slow to react (tens of seconds), so it is a decent convenience
  feature and a poor fuse protector. Power Boost reacts in the charger itself,
  which is why it is worth the effort.

## Sources

- [INEPRO N1-CT product page and user manual](https://www.ineprometering.com/product/n1-ct-electricity-meter) — the register map in section 9
- [Wallbox EMS installation guide, July 2024](https://support.wallbox.com/wp-content/uploads/ht_kb/2024/09/EN_EMS_Installation-Guide.pdf) — wiring, terminals, rotary switch, meter compatibility table
- [relyd/modbussniffer](https://github.com/relyd/modbussniffer) — a capture of real Pulsar Plus ↔ N1-CT traffic
- [Inepro register map reference](https://www.aggsoft.com/modbus-data-logging/inepro-metering.htm) — corroborates the same addresses across the PRO family

## Licence

[MIT](LICENSE). Not affiliated with, endorsed by, or supported by Wallbox
Chargers S.L. or inepro Metering B.V.
