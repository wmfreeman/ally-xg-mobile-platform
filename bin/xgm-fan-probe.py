#!/usr/bin/env python3
"""
READ-ONLY probe of the XG Mobile dock MCU's fan interface.

Target: the ITE 8910 at 0b05:1970, which exposes the ASUS ROG HID command
channel (usage page 0xFF31, usage 0x80, report ID 0x5E, 299-byte feature
report). Topology says this is the DOCK, not the Ally: the same USB controller
(0b:00.3) also carries the r8152 Ethernet and the card reader, while the Ally's
own N-KEY controller sits on 09:00.3 with a 63-byte report.

Commands from osy/XG_Mobile_Station Docs/Software.md:
    GetFanSpeed            5E C0 01 01 00 00 ...
    GetFanCurveTable       5E D0 00 .. 5E D0 03
    GetFanCurveLowerLimit  5E D0 04
    SetFanCurve            5E D1 01 <18 bytes>     <-- NOT SENT HERE
    DisableFanCurve        5E D1 02                <-- NOT SENT HERE

This script issues QUERIES ONLY. It never writes a fan curve and never
disables one. It is still talking to an undocumented MCU, so it is not
zero-risk — but a malformed query should at worst be ignored. The dock can be
power-cycled if it sulks.

    sudo python3 xgm-fan-probe.py            # auto-detect
    sudo python3 xgm-fan-probe.py /dev/hidraw3
"""
import fcntl, glob, os, re, sys, time

REPORT_ID = 0x5E
SIZES = (300, 64, 65)   # 299+ID first; fall back to the 63-byte laptop shape

def _ioc(d, t, nr, size): return (d << 30) | (size << 16) | (t << 8) | nr
def HIDIOCSFEATURE(n): return _ioc(3, ord('H'), 0x06, n)
def HIDIOCGFEATURE(n): return _ioc(3, ord('H'), 0x07, n)


def find_dock():
    """The 0xFF31/usage-0x80 node whose USB path is NOT the Ally's own."""
    cands = []
    for hr in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        dev = os.path.join(hr, 'device')
        try:
            rd = open(os.path.join(dev, 'report_descriptor'), 'rb').read().hex()
            ue = open(os.path.join(dev, 'uevent')).read()
        except OSError:
            continue
        if '0631ff0980' not in rd:          # usage page FF31 + usage 0x80
            continue
        phys = (re.search(r'HID_PHYS=(.*)', ue) or [None, ''])[1].strip()
        name = (re.search(r'HID_NAME=(.*)', ue) or [None, ''])[1].strip()
        hid_id = (re.search(r'HID_ID=(.*)', ue) or [None, ''])[1].strip()
        cands.append((f"/dev/{os.path.basename(hr)}", name, phys, hid_id))
    if not cands:
        return None, cands
    # The dock is the ITE 8910 (product 0x1970). Prefer it explicitly.
    for c in cands:
        if '1970' in c[3].lower():
            return c[0], cands
    return cands[0][0], cands


def feature(fd, payload, size):
    """SET_FEATURE the command, then GET_FEATURE the reply. Returns bytes."""
    buf = bytearray(size)
    buf[0] = REPORT_ID
    buf[1:1 + len(payload)] = bytes(payload)
    fcntl.ioctl(fd, HIDIOCSFEATURE(size), buf)
    time.sleep(0.02)
    rep = bytearray(size)
    rep[0] = REPORT_ID
    fcntl.ioctl(fd, HIDIOCGFEATURE(size), rep)
    return bytes(rep)


def show(label, data, width=32):
    body = data[:width]
    hexs = ' '.join(f'{b:02x}' for b in body)
    nz = 'all-zero' if not any(data[1:]) else ''
    print(f"  {label:<26} {hexs} {nz}")


def main():
    dev, cands = (sys.argv[1], None) if len(sys.argv) > 1 else find_dock()
    if cands is not None:
        print("Candidates exposing usage page 0xFF31 / usage 0x80:")
        for p, n, ph, i in cands:
            tag = '  <-- DOCK (ITE 8910)' if '1970' in i.lower() else '  (Ally internal)'
            print(f"  {p:<14} {i:<28} {n}{tag}")
            print(f"  {'':<14} phys={ph}")
        print()
    if not dev:
        sys.exit("No 0xFF31/0x80 HID node found. Is the dock attached?")
    print(f"Probing {dev}\n")

    try:
        fd = os.open(dev, os.O_RDWR)
    except PermissionError:
        sys.exit(f"Permission denied on {dev} — run with sudo.")

    QUERIES = [
        ('GetFanSpeed',           [0xC0, 0x01, 0x01, 0x00, 0x00]),
        ('GetFanCurveTable[0]',   [0xD0, 0x00]),
        ('GetFanCurveTable[1]',   [0xD0, 0x01]),
        ('GetFanCurveTable[2]',   [0xD0, 0x02]),
        ('GetFanCurveTable[3]',   [0xD0, 0x03]),
        ('GetFanCurveLowerLimit', [0xD0, 0x04]),
    ]

    for size in SIZES:
        print(f"--- feature report size {size} ---")
        ok = False
        for label, payload in QUERIES:
            try:
                show(label, feature(fd, payload, size))
                ok = True
            except OSError as e:
                print(f"  {label:<26} ioctl failed: {e}")
        print()
        if ok:
            break

    os.close(fd)
    print("Read-only. No fan curve was written and none was disabled.")
    print("If any line above came back with non-zero data, the dock's fan")
    print("interface is reachable and SetFanCurve (5E D1 01 ...) is the next step.")


if __name__ == '__main__':
    main()
