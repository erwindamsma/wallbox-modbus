#!/usr/bin/env python3
"""Create two linked virtual serial ports, so the emulator and a test master
can talk with no RS485 hardware at all.

    python3 tools/vlink.py
    # then, in other terminals:
    python3 -m wallbox_powerboost -c config.yaml     # with serial.port: /tmp/wallbox-a
    python3 tools/test_master.py /tmp/wallbox-b

Bytes written to one end appear at the other, which is enough to exercise
everything above the transceivers: framing, CRC, the register map, and live
data coming from Home Assistant. It cannot tell you anything about wiring,
termination or the charger's own expectations.

Set `parity: none` on both sides for this test. A pseudo-terminal has no UART,
so it does not emulate parity at all, and asking for it can fail outright with
"Invalid argument". Real USB-RS485 adapters handle 8E1 the way the charger
expects.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import sys
import tty


def make_port(link: str) -> tuple[int, int]:
    master, slave = os.openpty()
    tty.setraw(slave)
    if os.path.islink(link) or os.path.exists(link):
        os.unlink(link)
    os.symlink(os.ttyname(slave), link)
    return master, slave


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="/tmp/wallbox-a", help="path for the emulator side")
    ap.add_argument("--b", default="/tmp/wallbox-b", help="path for the master side")
    args = ap.parse_args()

    master_a, slave_a = make_port(args.a)
    master_b, slave_b = make_port(args.b)
    print(f"linked {args.a}  <-->  {args.b}")
    print("point the emulator at one and tools/test_master.py at the other; Ctrl-C to stop")

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while running:
            readable, _, _ = select.select([master_a, master_b], [], [], 0.2)
            for src in readable:
                dst = master_b if src is master_a else master_a
                try:
                    data = os.read(src, 1024)
                except OSError:
                    continue
                if data:
                    os.write(dst, data)
    finally:
        for fd in (master_a, slave_a, master_b, slave_b):
            try:
                os.close(fd)
            except OSError:
                pass
        for link in (args.a, args.b):
            try:
                os.unlink(link)
            except OSError:
                pass
        print("\nlinks removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
