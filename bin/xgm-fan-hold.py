#!/usr/bin/env python3
"""
Hold the XG Mobile dock fan at a fixed duty and keep re-asserting it.

Established on this dock (ITE 8910, /dev/hidraw3, report 0x5E):
  * SetFanCurve  5E D1 01 <8 temps> <8 duties>   writes the ACTIVE curve.
    GetFanCurveTable cannot see it — the stored profiles read back unchanged
    while the fan audibly responds. Do not trust a readback here.
  * Telemetry    5E C0 01 01 ...  -> byte[9] is the APPLIED PWM, 0-255.
    Measured FOR THE FLAT CURVES THIS TOOL WRITES (all eight points at one
    duty): requested+95 through the middle, floored at 175, saturating at
    255 for requested >= ~200. That floor is flat-curve-specific, NOT a
    property of the controller: shaped curves on the same dock measured
    applied 115 and applied 0 (fan stopped). Curve shape changes what the
    controller runs and the mapping is unmodelled — FINDINGS s13. Do not
    use this tool's numbers to predict what a shaped curve will do.
  * Stock curves cap at 87/255 (34%) and table[3] only reaches it at 98 C —
    two degrees under the SW CTF that powers off the HOST. That is why this
    exists.

The active curve is volatile and the firmware may reclaim it, so this
re-asserts on an interval and holds until you stop it. Leaving it running is
the point: start it, run inference in another terminal, watch junction.

    sudo ./xgm-fan-hold.py                  duty 255, re-assert every 10 s
    sudo ./xgm-fan-hold.py --duty 200
    sudo ./xgm-fan-hold.py --quiet          no per-tick line
    sudo ./xgm-fan-hold.py --csv run.csv

Ctrl-C restores the firmware curve.
"""
import argparse, fcntl, glob, os, re, signal, sys, time

REPORT_ID, SIZE = 0x5E, 300
def _ioc(d, t, nr, n): return (d << 30) | (n << 16) | (t << 8) | nr
SET, GET = _ioc(3, ord('H'), 0x06, SIZE), _ioc(3, ord('H'), 0x07, SIZE)
EG = '/sys/bus/pci/devices/0000:03:00.0/hwmon'


def find_dock():
    for hr in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        d = os.path.join(hr, 'device')
        try:
            rd = open(os.path.join(d, 'report_descriptor'), 'rb').read().hex()
            ue = open(os.path.join(d, 'uevent')).read()
        except OSError:
            continue
        if '0631ff0980' in rd and '1970' in (re.search(r'HID_ID=(.*)', ue) or [None, ''])[1].lower():
            return f"/dev/{os.path.basename(hr)}"
    return None


def cmd(fd, payload):
    b = bytearray(SIZE); b[0] = REPORT_ID; b[1:1 + len(payload)] = bytes(payload)
    fcntl.ioctl(fd, SET, b); time.sleep(0.02)
    r = bytearray(SIZE); r[0] = REPORT_ID
    fcntl.ioctl(fd, GET, r); return bytes(r)


def gpu():
    try:
        h = glob.glob(EG + '/hwmon*')[0]
        rd = lambda f: int(open(h + '/' + f).read())
        return rd('temp1_input') // 1000, rd('temp2_input') // 1000, \
               rd('temp3_input') // 1000, rd('power1_average') // 1000000
    except Exception:
        return 0, 0, 0, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--duty', type=int, default=255)
    ap.add_argument('--interval', type=float, default=10.0)
    ap.add_argument('--tick', type=float, default=2.0)
    ap.add_argument('--abort-at', type=int, default=90,
                    help='junction C at which to shout (does not kill anything)')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--csv', default=None)
    a = ap.parse_args()

    dev = find_dock()
    if not dev:
        sys.exit('dock HID node not found — is it attached?')
    try:
        fd = os.open(dev, os.O_RDWR)
    except PermissionError:
        sys.exit('run with sudo')

    temps = [30] * 8                      # apply the duty from idle upward
    csv = open(a.csv, 'w') if a.csv else None
    if csv:
        csv.write('ts,requested,applied_pwm,dockC,edgeC,junctionC,memC,watts\n')

    def restore(*_):
        try:
            cmd(fd, [0xD1, 0x02])
            print('\nDisableFanCurve sent — firmware curve restored.')
        finally:
            if csv: csv.close()
            os._exit(0)

    signal.signal(signal.SIGINT, restore)
    signal.signal(signal.SIGTERM, restore)

    print(f'dock {dev} — holding duty {a.duty}, re-asserting every {a.interval:.0f}s')
    print('Ctrl-C restores the firmware curve.\n')
    if not a.quiet:
        print(f"{'time':>8} {'pwm':>4} {'dock':>5} {'edge':>5} {'junc':>5} {'mem':>5} {'W':>5}")
        print(f"{'-'*8} {'-'*4} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*5}")

    last = 0.0
    peak = 0
    while True:
        now = time.time()
        if now - last >= a.interval:
            cmd(fd, [0xD1, 0x01] + temps + [a.duty] * 8)
            last = now
        s = cmd(fd, [0xC0, 0x01, 0x01] + [0] * 12)
        pwm, dockc = s[9], s[6]
        edge, junc, mem, w = gpu()
        peak = max(peak, junc)
        flag = ''
        if junc >= a.abort_at:
            flag = f'   <-- {junc}C, ABORT THE RUN'
        elif junc >= a.abort_at - 10:
            flag = '   <-- climbing'
        if not a.quiet:
            print(f'{time.strftime("%H:%M:%S"):>8} {pwm:>4} {dockc:>4}C {edge:>4}C '
                  f'{junc:>4}C {mem:>4}C {w:>4}W{flag}', flush=True)
        if csv:
            csv.write(f'{now:.1f},{a.duty},{pwm},{dockc},{edge},{junc},{mem},{w}\n')
            csv.flush(); os.fsync(csv.fileno())     # a CTF takes the page cache
        time.sleep(a.tick)


if __name__ == '__main__':
    main()
