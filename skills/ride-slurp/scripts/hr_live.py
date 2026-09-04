#!/usr/bin/env python3
"""Live heart rate from any Bluetooth LE chest strap, in the terminal.

Dual-band straps (Garmin HRM-Dual and similar) broadcast ANT+ and Bluetooth LE
at the same time, so a computer can read the standard BLE Heart Rate service
(0x180D) without disturbing the strap's pairing to a bike computer or watch.

Most straps only wake up once they are actually worn - the electrodes need skin
contact. A scan with the strap sitting on a desk finds nothing.

  hr_live.py            # scan, connect, live display (Ctrl-C quits)
  hr_live.py --scan     # just list the visible heart rate senders
  hr_live.py --raw      # one line per reading (timestamp, bpm, rr) for logging
"""

import argparse
import asyncio
import sys
import time

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("bleak is not installed. Run the plugin's setup.sh, or: pip install bleak")

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"


def parse_hr(data: bytearray):
    """Heart Rate Measurement (0x2A37): flags, bpm, optional RR intervals."""
    flags = data[0]
    if flags & 0x01:  # uint16 bpm
        bpm = int.from_bytes(data[1:3], "little")
        i = 3
    else:
        bpm = data[1]
        i = 2
    contact = (flags >> 1) & 0x03  # 2 = worn but no contact, 3 = contact ok
    if flags & 0x08:  # energy expended present - skip it
        i += 2
    rr = []
    if flags & 0x10:
        while i + 1 < len(data):
            rr.append(int.from_bytes(data[i:i + 2], "little") / 1024.0)
            i += 2
    return bpm, contact, rr


async def find_strap(name_filter, timeout):
    devices = await BleakScanner.discover(timeout=timeout, service_uuids=[HR_SERVICE])
    if name_filter:
        devices = [d for d in devices if name_filter.lower() in (d.name or "").lower()]
    return devices


async def stream(args):
    while True:
        print("Scanning for a heart rate sender (put the strap on so it wakes up) ...",
              file=sys.stderr)
        devices = await find_strap(args.name, args.scan_timeout)
        if not devices:
            print("Nothing found - is the strap being worn? Retrying in 5s (Ctrl-C quits).",
                  file=sys.stderr)
            await asyncio.sleep(5)
            continue

        device = devices[0]
        print(f"Connecting to {device.name or device.address} ...", file=sys.stderr)

        disconnected = asyncio.Event()
        stats = {"n": 0, "sum": 0, "min": None, "max": None, "t0": time.monotonic()}

        def on_hr(_char, data: bytearray):
            bpm, contact, rr = parse_hr(data)
            if bpm == 0:
                return
            stats["n"] += 1
            stats["sum"] += bpm
            stats["min"] = bpm if stats["min"] is None else min(stats["min"], bpm)
            stats["max"] = bpm if stats["max"] is None else max(stats["max"], bpm)
            if args.raw:
                print(f"{time.strftime('%H:%M:%S')}\t{bpm}\t"
                      f"{','.join(f'{x:.3f}' for x in rr)}", flush=True)
            else:
                elapsed = int(time.monotonic() - stats["t0"])
                avg = stats["sum"] / stats["n"]
                warn = "" if contact != 2 else "  [no skin contact]"
                print(f"\r  HR {bpm:3d} bpm   min {stats['min']:3d} · "
                      f"avg {avg:5.1f} · max {stats['max']:3d}   "
                      f"{elapsed // 60:02d}:{elapsed % 60:02d}{warn}   ",
                      end="", flush=True)

        try:
            async with BleakClient(
                    device, disconnected_callback=lambda c: disconnected.set()) as client:
                try:
                    batt = await client.read_gatt_char(BATTERY_LEVEL)
                    print(f"Connected - battery {batt[0]} %", file=sys.stderr)
                except Exception:
                    print("Connected", file=sys.stderr)
                await client.start_notify(HR_MEASUREMENT, on_hr)
                await disconnected.wait()
            print("\nDisconnected - reconnecting ...", file=sys.stderr)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"\nError: {e} - retrying in 5s.", file=sys.stderr)
            await asyncio.sleep(5)


async def scan_only(args):
    devices = await find_strap(None, args.scan_timeout)
    if not devices:
        print("No BLE heart rate senders visible. "
              "(Most straps only wake up once worn.)")
    for d in devices:
        print(f"{d.address}  {d.name or '?'}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true", help="only scan, do not connect")
    ap.add_argument("--name", help="name filter, e.g. 'HRM-Dual' (default: first sender)")
    ap.add_argument("--raw", action="store_true", help="one line per reading")
    ap.add_argument("--scan-timeout", type=float, default=8.0,
                    help="scan duration in seconds (default 8)")
    args = ap.parse_args()
    try:
        asyncio.run(scan_only(args) if args.scan else stream(args))
    except KeyboardInterrupt:
        print("", file=sys.stderr)


if __name__ == "__main__":
    main()
