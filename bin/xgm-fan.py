#!/usr/bin/env python3
"""
XG Mobile dock fan control over the ASUS ROG HID channel.

Protocol from osy/XG_Mobile_Station Docs/Software.md, confirmed live on this
dock (ITE 8910, 0b05:1970, /dev/hidraw3, usage page 0xFF31 / usage 0x80,
report ID 0x5E, 299-byte feature report):

    GetFanSpeed            5E C0 01 01 00 ...
    GetFanCurveTable       5E D0 00..03      -> 5E D0 XX <8 temps> <8 duties>
    GetFanCurveLowerLimit  5E D0 04          -> firmware-enforced duty floor
    SetFanCurve            5E D1 01 <8 temps> <8 duties>
    DisableFanCurve        5E D1 02          -> back to firmware default

WHY: this dock hit SW CTF (100 C junction -> orderly_poweroff of the HOST) in
~90 s of inference. Its built-in curves are the reason — the most aggressive
one does not reach duty 87 until 98 C, two degrees under the shutdown, and one
of them caps at 34 forever above 74 C. amdgpu's pwm1 never reached this fan;
nothing in asus-nb-wmi does either. This HID channel is the only thing that does.

The duty SCALE is undocumented. This tool determines it empirically by writing
and reading back, and never assumes.

    sudo ./xgm-fan.py                    read everything, write nothing
    sudo ./xgm-fan.py --probe-scale      find the max accepted duty
    sudo ./xgm-fan.py --apply            back up, write max curve, verify
    sudo ./xgm-fan.py --apply --duty 80  same, at a chosen duty
    sudo ./xgm-fan.py --restore          DisableFanCurve (firmware default)
"""
import argparse, fcntl, glob, json, os, re, sys, time

REPORT_ID, SIZE, BACKUP = 0x5E, 300, '/var/lib/xgm-fan-backup.json'

def _ioc(d, t, nr, n): return (d << 30) | (n << 16) | (t << 8) | nr
SET = _ioc(3, ord('H'), 0x06, SIZE)
GET = _ioc(3, ord('H'), 0x07, SIZE)


def find_dock():
    for hr in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        dev = os.path.join(hr, 'device')
        try:
            rd = open(os.path.join(dev, 'report_descriptor'), 'rb').read().hex()
            ue = open(os.path.join(dev, 'uevent')).read()
        except OSError:
            continue
        if '0631ff0980' in rd and '1970' in (re.search(r'HID_ID=(.*)', ue) or [None, ''])[1].lower():
            return f"/dev/{os.path.basename(hr)}"
    return None


def cmd(fd, payload):
    buf = bytearray(SIZE); buf[0] = REPORT_ID; buf[1:1 + len(payload)] = bytes(payload)
    fcntl.ioctl(fd, SET, buf)
    time.sleep(0.03)
    rep = bytearray(SIZE); rep[0] = REPORT_ID
    fcntl.ioctl(fd, GET, rep)
    return bytes(rep)


def curve(fd, idx):
    r = cmd(fd, [0xD0, idx])
    return list(r[3:11]), list(r[11:19])          # temps, duties


def limits(fd):
    return list(cmd(fd, [0xD0, 0x04])[3:11])


def speed(fd):
    return cmd(fd, [0xC0, 0x01, 0x01] + [0] * 12)[4:8]


def show(label, temps, duties):
    print(f"  {label}")
    print(f"    temp  {' '.join(f'{t:>4}' for t in temps)}   C")
    print(f"    duty  {' '.join(f'{d:>4}' for d in duties)}")


def read_all(fd):
    print("Fan speed / telemetry:")
    s = speed(fd)
    print(f"    raw {' '.join(f'{b:02x}' for b in s)}   "
          f"(bytes 3-4 track junction temp: {s[2]} / {s[3]} C)\n")
    print("Curve tables:")
    tables = {}
    for i in range(4):
        t, d = curve(fd, i)
        tables[i] = (t, d)
        if any(t) or any(d):
            show(f"table[{i}]", t, d)
        else:
            print(f"  table[{i}]  (empty)")
    lo = limits(fd)
    print(f"\n  duty floor (firmware-enforced minimum per point)")
    print(f"    {' '.join(f'{v:>4}' for v in lo)}")
    return tables, lo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--restore', action='store_true')
    ap.add_argument('--probe-scale', action='store_true')
    ap.add_argument('--duty', type=int, default=None,
                    help='duty for every point; default = max the device accepts')
    ap.add_argument('--dev', default=None)
    a = ap.parse_args()

    dev = a.dev or find_dock()
    if not dev:
        sys.exit("XG Mobile HID node not found — is the dock attached?")
    try:
        fd = os.open(dev, os.O_RDWR)
    except PermissionError:
        sys.exit(f"Permission denied on {dev} — run with sudo.")
    print(f"dock: {dev}\n")

    tables, lo = read_all(fd)

    if a.restore:
        cmd(fd, [0xD1, 0x02])
        print("\nDisableFanCurve sent — firmware default restored.")
        time.sleep(1); read_all(fd); return

    if a.probe_scale or a.apply:
        # Determine the accepted maximum empirically. Write, read back, observe
        # clamping. Never assume 0-100 vs 0-255.
        if not os.path.exists(BACKUP):
            os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
            json.dump({str(k): v for k, v in tables.items()}, open(BACKUP, 'w'))
            print(f"\nbacked up original tables -> {BACKUP}")

        probe_temps = [30, 35, 40, 45, 50, 55, 60, 65]
        accepted = None
        print("\nprobing duty scale (write, then read back):")
        for trial in (0xFF, 0x64, 0x57):
            cmd(fd, [0xD1, 0x01] + probe_temps + [trial] * 8)
            time.sleep(0.2)
            _, back = curve(fd, 1)
            print(f"    wrote {trial:>3} -> read back {back}")
            if back and max(back) > 0:
                accepted = max(back)
                if accepted >= trial:
                    break
        if accepted is None:
            print("    device did not accept a written curve — stopping.")
            return
        print(f"\n  maximum accepted duty: {accepted}")

    if a.apply:
        duty = a.duty if a.duty is not None else accepted
        duty = max(duty, max(lo))            # never below the firmware floor
        temps = [30, 35, 40, 45, 50, 55, 60, 65]
        print(f"\napplying: duty {duty} at every point from {temps[0]} C")
        cmd(fd, [0xD1, 0x01] + temps + [duty] * 8)
        time.sleep(2)
        t, d = curve(fd, 1)
        show("  readback", t, d)
        if d != [duty] * 8:
            print("  NOTE: device clamped or reordered the values — see above.")
        s = speed(fd)
        print(f"\n  telemetry now: {' '.join(f'{b:02x}' for b in s)}")
        print("\n  Listen to the dock. If it did not get louder, the curve was")
        print("  accepted but is not driving the fan, and this path is exhausted.")
        print(f"  Revert at any time:  sudo {sys.argv[0]} --restore")

    os.close(fd)


if __name__ == '__main__':
    main()
