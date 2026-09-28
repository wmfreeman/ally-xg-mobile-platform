# ROG Ally + XG Mobile on Linux — what we had to discover

Measured on an **ASUS ROG Ally (board RC71L, Ryzen Z1 Extreme)** with an
**ASUS XG Mobile** eGPU attached, Ubuntu, kernel 7.0.0-28, ROCm 7.2.4.

```
eGPU   Navi 22, gfx1031, PCI 1002:73df, subsys 1043:21cb, pci 0000:03:00.0
       12 GiB, "Display controller" (headless secondary), TGP locked 165 W
iGPU   Phoenix1, gfx1103, pci 0000:09:00.0, 4 GiB VRAM + 5.8 GiB GTT
dock   ITE 8910 MCU, USB 0b05:1970, exposes the ASUS ROG HID channel
```

None of the below is in vendor documentation. Each item cost real time.

---

## 1. A GPU thermal fault powers off the HOST — there is no throttle stage

```
amdgpu 0000:03:00.0: ERROR: GPU over temperature range(SW CTF) detected!
amdgpu 0000:03:00.0: ERROR: System is going to shutdown due to GPU SW CTF!
```

SW CTF debounces **50 ms** and then calls `orderly_poweroff(true)`. Not a GPU
reset, not a clock throttle — the whole machine goes down, taking unsynced work
with it. Trip point is **100 °C junction** (emergency 105 °C).

**Consequence for any long-running GPU workload on this hardware: you need a
software thermal guard, and anything it writes must be `fsync`'d.**

## 2. Both fans ship effectively disabled, and they are on different buses

This is the whole problem, and the two halves are unrelated to each other.

**Ally internal fans** — `asus_custom_fan_curve` hwmon, 8 `(temp, pwm)` points:

| | shipped | as % of 255 |
|---|---|---|
| `pwm1` cpu_fan | 10, 20, **66, 66, 66, 66, 66, 66** | 26 % ceiling |
| `pwm2` gpu_fan | **2** at every point | under 1 % |

The curve was never slow. It was **capped**. Flattening all points to 255 took
`cpu_fan` from 2 800 → **8 200 rpm**.

**XG Mobile dock fan** — not reachable by anything obvious:

- `amdgpu`'s `pwm1` **accepts writes and silently ignores them**. `pwm1` reads
  back 255 while `fan1_target` stays 0. `fan1_max` reports 3800, so the driver
  believes it has a fan it cannot drive.
- Mainline `asus-wmi` has **no XG Mobile fan device id at all**. The Linux
  bring-up path handles power and PCIe only.
- `/sys/devices/platform/asus-nb-wmi/throttle_thermal_policy` (what the
  Decky "XG Mobile Manager" plugin writes) does **not** reach it either.

Its stock curves, read off the dock:

```
table[1]  temp 60 65 68 72 75 79 85 85   duty  1 17 25 29 34 42 46 46
table[2]  temp 60 64 68 71 74 74 74 74   duty  1 17 25 29 34 34 34 34
table[3]  temp 60 65 68 72 75 80 98 98   duty 17 21 25 38 50 75 87 87
```

The most aggressive curve reaches **34 % duty at 98 °C** — two degrees under
the shutdown. That is why the card ran away.

## 3. The dock fan IS controllable — over USB HID

**The single most valuable finding here.** Protocol from
[osy/XG_Mobile_Station](https://github.com/osy/XG_Mobile_Station), confirmed
live on this dock.

```
device       usage page 0xFF31, usage 0x80, report ID 0x5E, 299-byte feature report
             = the ITE 8910 at 0b05:1970  (NOT the Ally's own N-KEY at 0b05:1abe)

GetFanSpeed            5E C0 01 01 00 ...      -> byte[9] = applied PWM, byte[6] = dock temp C
GetFanCurveTable       5E D0 00..03            -> 5E D0 XX <8 temps> <8 duties>
GetFanCurveLowerLimit  5E D0 04                -> firmware-enforced duty floor
SetFanCurve            5E D1 01 <8 temps> <8 duties>
DisableFanCurve        5E D1 02                -> back to firmware default
```

Find it by report descriptor, not by index — match `0631ff0980` in
`report_descriptor` **and** product `1970` in `HID_ID`. The Ally's own N-KEY
device exposes the same usage page with a 63-byte report, and writing a fan
curve to it does nothing useful.

**Two traps:**

- **`SetFanCurve` writes the ACTIVE curve, which `GetFanCurveTable` cannot
  read back.** The stored tables come back unchanged while the fan audibly
  responds. Do not trust a readback — trust `GetFanSpeed` byte[9], or your ears.
- **The active curve is volatile.** Firmware reclaims it. Re-assert on a timer;
  10 s works.

Measured duty response (requested → applied PWM):

```
   0  30  60  87 | 100 150 200 255
 175 175 175 175 | 195 245 255 255
```

Linear at `requested + 95` through the middle, floored at **175 (69 %)**,
saturating at 255 for requested ≥ ~200. So an active custom curve gives roughly
**three times** the fan authority the firmware ever used.

## 4. Junction is not a control signal — gate on edge

Sampled at 0.5 s during a real workload:

| | max jump in one 0.5 s sample | peak under load |
|---|---|---|
| `temp2_input` junction | **30 °C** | 94 °C |
| `temp1_input` edge | **4 °C** | 68 °C |

Junction is the **hotspot** — an instantaneous max across on-die sensors that
collapses in the gap between generation steps and spikes straight back. It read
66 °C and 94 °C two seconds apart with edge unchanged at 65 °C.

A guard gating on junction paused at 84 °C and "resumed at 66 °C after 0.5 s",
straight back into full load. **Use edge for hysteresis; keep junction only as
an emergency ceiling.** Under load the two differ by ~26–30 °C.

## 5. There is no power cap and no clock ceiling on this card

- `power1_cap` = `power1_cap_min` = `power1_cap_max` = **165 W**. Fixed.
- `amdgpu.ppfeaturemask=0xfff7ffff` (default `0xfff7bfff` clears
  `PP_OVERDRIVE_MASK`, bit 14) **does** make `pp_od_clk_voltage` appear — but on
  this pptable it contains **only** `OD_VDDGFX_OFFSET`. No `OD_SCLK`, no
  `OD_MCLK`, empty `OD_RANGE`.
- `pp_dpm_sclk` offers only `300 MHz` and `2700 MHz`. Nothing between.

Measured sweep of every remaining lever, identical load each time:

| config | mean W under load | prefill t/s | decode t/s |
|---|---|---|---|
| stock | 164 | 495.6 ±2.8 | 35.07 |
| `POWER_SAVING` profile | 154 | 409.5 **±101.4** | 34.87 |
| + `mclk` masked to 675 MHz | 151 | 478.3 | **25.19** |
| + `vo -80mV` | 147 | 410.2 ±105.9 | 35.11 |

**Peak stayed 165 W in every configuration.** `POWER_SAVING` makes prefill
erratic; the memory cap costs 28 % of decode for ~3 W. **None of it is worth
taking.** Duty cycle is the only real control — pace the workload in software.

*(`vo -80mV` did shed ~7 W at no throughput cost, contradicting the usual
expectation that a TDP-limited part converts undervolt into clocks. Free, but
not a solution.)*

## 6. Never set both `HIP_VISIBLE_DEVICES` and `ROCR_VISIBLE_DEVICES`

```
HIP_VISIBLE_DEVICES=1   alone   works
ROCR_VISIBLE_DEVICES=1  alone   works
both set to 1                   "no ROCm-capable device is detected"
```

They **compose**. ROCR filters first and reindexes the survivor to 0; HIP then
asks for index 1 of a one-device list. It works by accident whenever the target
is index 0, which is how this survives casual testing. **Set `HIP_VISIBLE_DEVICES`
only.**

## 7. The eGPU runtime-suspends to D3cold and takes its sensors with it

At idle the card drops to `D3cold`, and every hwmon attribute returns `EBUSY` —
which reads exactly like a broken sensor. `echo on > power/control` keeps it
resident so temperature is readable and the fan can be pre-spun before load
arrives. Re-plugging the dock resets this — originally handled by a udev rule
that restarted the service; the daemon has since grown a per-tick re-assert of
`power/control` (and per-tick dock re-detection), so the rule is retired and a
re-plug heals within one 10 s tick, with no curve gap from a unit bounce.

## 8. Both GPUs can serve concurrently, at a measurable cost

| | alone | concurrent |
|---|---|---|
| eGPU, Qwen2.5-14B Q4_K_M | 35.4 t/s decode | 27.7 t/s (**−22 %**) |
| iGPU, Qwen2.5-0.5B Q8_0 | 82.2 t/s decode | 79.9 t/s (−3 %) |

eGPU peak during the concurrent run: edge 54 °C, 162 W. They share PCIe and
memory bandwidth, so the big model pays for the split. Worth knowing before
designing anything that overlaps them.

`llama.cpp` built with `AMDGPU_TARGETS=gfx1031;gfx1103` covers both. The iGPU
runs Qwen2.5-3B Q4_K_M at ~30–33 t/s decode.

## 9. Thermal behaviour with the fans actually working

```
idle                              37-38 °C junction
continuous llama-bench, 165 W     70 °C from a 38 °C start (short load)
                                  87 °C from 37 °C (heavy load)
heating under load                ~0.76 °C/s junction, ~1.1 °C/s edge
recovery at idle                  86 -> 53 °C in 16 s
```

Recovery is fast, so **pacing works**: pause between turns and the card never
approaches the limit. Equilibrium under continuous 165 W sits near edge 65 /
junction 90 — survivable but not somewhere to live.

---

## What we never solved

- **No way to read the dock's fan RPM.** `GetFanSpeed` byte[9] is the applied
  PWM, not a tachometer. `fan1_input` on the amdgpu hwmon stays 0.
- **`ppt_*` under `asus-nb-wmi` all read `5`** with undocumented units. The APU
  was measured drawing 21 W, so `5` is not a 5 W cap. We left them alone;
  whether RC71L firmware clamps out-of-design values is unknown and untested.
- **`OD_RANGE` is empty**, so even the voltage offset has no advertised bounds.

---

## 10. The dock's duty floor is per-point and temperature-shaped (measured 2026-08-08)

*(2026-08-12: superseded by §13 on the narrow question of what the fan
actually DOES — the table below is an ACCEPTANCE limit, real and still used
for clamping, but the applied PWM turned out to depend on curve shape in a
way nobody has explained. Every measurement in this section stands.)*

Read off the hardware with `bin/xgm-fan-probe.py` against the ITE 8910 at
`/dev/hidraw6` (`0003:00000B05:00001970`), dock attached. Read-only.

```
GetFanCurveLowerLimit   5e d0 04 | 00 00 00 00 22 36 3e 57
```

Eight bytes after the header — **one duty floor per curve point**, ascending
with the temperature points:

| point | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| floor /255 | 0 | 0 | 0 | 0 | 34 | 54 | 62 | 87 |
| floor % | 0 | 0 | 0 | 0 | 13 | 21 | 24 | 34 |

**The first four points have no floor.** A silent dock at idle is reachable
with a custom curve still active. The floors that exist rise with temperature,
so the firmware refuses a dangerously low duty on a hot card — the protection
is real and it is the firmware's, not ours.

### This corrects an earlier reading

An earlier survey recorded "applied PWM is floored at 175/255 (69%) whenever
any custom curve is active." **That is wrong.** 175 appears nowhere in the
device's own limit table. It was a reading of the *daemon's own* max curve —
`GetFanSpeed` byte[9] reads `ff` (255) while the current daemon holds the card.

Consequence, and it matters: **quiet does NOT require `DisableFanCurve`.** The
shutdown sequence does not have to hand the dock back to the firmware curve —
the one implicated in the 2026-08-07 SW CTF host power-off. Set a low custom
curve instead and keep the guard active throughout.

### A flat quiet constant is the wrong shape

`QUIET_CURVE = 40/255` flat is silently clamped to 54/62/87 at points 6-8. The
clamp is in the safe direction, but the constant then describes something the
hardware is not doing. Shape the curve instead: near-zero on points 1-4, at or
above the floor on 5-8.

### The 34% cap, read directly rather than inferred

```
GetFanCurveTable[3]     5e d0 03 | 3c 41 44 48 4b 50 62 62 | 11 15 19 26 32 4b 57 57
                                   60 65 68 72 75 80 98 98 C  17 21 25 38 50 75 87 87
```

87/255 = **34.1% duty at 98 C** — the firmware performance curve's ceiling, and
the reason a userspace daemon exists at all. Note the top lower-limit byte is
the same `0x57`: the floor at the hottest point equals that ceiling.

### Still unmeasured

Whether `asus_custom_fan_curve` (the Ally side, a different interface) accepts a
low duty; whether any given quiet curve is *audibly* quiet; `ally_release()` on
SIGTERM; and how long a one-shot dock curve survives without re-assertion.
Those need a write, and nothing has written yet.

`GetFanSpeed` byte[6] read `0x20` (32) on this probe rather than the `0` seen on
57 of 60 earlier daemon ticks — so that field is intermittent rather than
uniformly dead. Still do not build a decision on it; there is no dock
tachometer. Use byte[9] applied PWM, and take temperature from the amdgpu hwmon
globbed by PCI path.

---

## 11. Idle thermal characterisation, and why "quiet" cannot mean releasing the dock (measured 2026-08-12)

Controlled test. Nothing running, eGPU at 10 W throughout, supplementary chassis
exhaust on **auto**. `mode quiet` requested via the daemon socket; sampled at
10 s from the PCI-globbed hwmon.

### Drift with the dock released to firmware

```
   0s  edge 34C   fans 5900 rpm      182s  edge 41C
  30s  edge 35C   fans 3600 rpm      242s  edge 44C
  60s  edge 37C                      272s  edge 45C
 121s  edge 39C                      303s  edge 46C  -> quiet REVOKED
```

**+12 C in five minutes, linear at ~2.4 C/min, still climbing at cutoff.** The
passive equilibrium was never reached. The firmware curve does duty 1 until
~65 C, so that is the neighbourhood an idle card would have settled in.

### Recovery once max resumed

```
 303s  46C  ->  333s  42C  ->  363s  38C  ->  454s  35C  ->  515s  35C
```

46 -> 35 C in ~150 s, then flat. **Equilibrium at full cooling is 35 C**, about
30 C below where the firmware curve engages.

### The conclusion, withdrawn once and now restored

An earlier reading attributed a 32->46 C drift to the released dock. It was
withdrawn when the operator noted a chassis exhaust fan had moved from full to
auto in the same window — a genuine confound. **This test reproduces the drift
with the exhaust held at auto**, so the release is the cause and the exhaust was
not. 34->46 C here versus 32->46 C then; the same slope.

**`mode quiet` must never mean `DisableFanCurve`.** Releasing the dock surrenders
the card at exactly the moment nothing is watching it, and the state is
self-terminating: the card always drifts past the threshold that permits quiet,
so quiet can never sustain. No threshold value fixes that — the equilibrium is
on the wrong side of any bound you would be willing to set.

### Numbers the fan ladder can be built on

Previously every threshold was derived, never measured. These are measured:

| quantity | value |
|---|---|
| passive drift, dock released, 10 W idle | **+2.4 C/min** |
| equilibrium at full cooling | **35 C** |
| recovery slope from 46 C | **~ -4.4 C/min** initially |
| headroom below firmware engagement | **~30 C** |

The bottom rung therefore needs very little: shedding roughly the idle 10 W is
enough, and section 10's zero-floor first four curve points allow it. A shaped
low curve should hold 38-42 C — quiet, stable, and still responsive on the way
up rather than asleep until 65 C.

### Also measured, and useful on its own

The Ally fans went 5900 -> 3600 rpm and held there for the whole quiet period.
**Most of the audible reduction is available on the Ally side alone**, with the
dock curve never released.

---

## 12. SETTLED: the dock's applied PWM floors at 175 with any custom curve (measured 2026-08-12)

*(Superseded by §13, same day: "SETTLED" was premature — the 175 floor is
real for the FLAT curve this test wrote and does not generalise to "any
custom curve"; a shaped curve was measured at applied 115, and at 0. The
decay measurement below stands.)*

**This supersedes section 10's conclusion. Read this one.** Section 10 stays on
the page per convention; its curve-table reading is correct and its inference
about what the fan would DO is wrong.

Direct measurement. `xgm-fan-hold.py --duty 40 --tick 2`, daemon stopped, dock
attached, card idle at 10 W:

```
  time  requested  applied_pwm
14:22:57    40        255      <- decay begins
14:22:59    40        235
14:23:01    40        215
14:23:03    40        195
14:23:05    40        175      <- floor reached in ~8s
14:23:07 ... 14:23:40           held 173-178 for the rest of the run
```

**Requested 40, applied 175.** The floor is real and it is a property of the
controller, not of the curve table.

### Why section 10 was wrong

`GetFanCurveLowerLimit` = `00 00 00 00 22 36 3e 57` describes what duty values
the firmware will ACCEPT INTO a curve. It says nothing about what the controller
then RUNS. Those are different stages, and the second one applies
`requested+95, floored at 175, saturating at 255 above ~200` — the transform
already recorded in `xgm-fan-hold.py`'s own header, measured long before this.

So the original survey reading ("applied PWM is floored at 175/255 (69%)
whenever any custom curve is active") was correct, and the correction in section
10 that it was "a reading of the daemon's own max curve" was mistaken.

### The dock has a usable range, just not a quiet one

| state | applied PWM | note |
|---|---|---|
| custom curve, any low request | **175** (69%) | hard floor |
| custom curve, high request | up to 255 | |
| `DisableFanCurve` | 0 | firmware curve; +2.4 C/min drift at idle (section 11) |

**255 -> 175 is a real 31% reduction and audibly quieter** — the operator
confirmed hearing the fan slow without stopping. So a dock ladder exists; it
just lives entirely in [175, 255] and cannot reach silence.

Near-silence on the dock is available ONLY by releasing the curve, which
section 11 shows is self-terminating. **That trade is genuine and unavoidable:
quiet dock or governed dock, not both.**

### Consequences for the fan ladder

- Dock rungs: [175, 255]. Roughly four to five meaningful steps.
- Ally rungs: 3600-6500 rpm measured, a proportionally larger range — and most
  of the audible win, per section 11.
- A resting state that "keeps our curve" is correct and remains the right
  design, but its floor is 69% on the dock, not near-zero. Any constant written
  on the assumption of a low dock rung needs re-checking.

### Provenance note

Three statements were made about this floor before this measurement: the survey
asserted it, section 10 overturned it from an adjacent register, and this test
restores it. The lesson is narrow and worth keeping: **the curve table and the
applied PWM are different registers, and only the second one moves air.**

---

## 13. The applied-PWM floor depends on curve SHAPE — mechanism unknown (measured 2026-08-12)

**Supersedes §10 and §12 on the narrow question of what the fan actually does
under a low custom curve. Their measurements stand; both conclusions were
premature — §12's "SETTLED" included, disproved as a general claim by the
run below.**

*(2026-08-13: §15 closes the applied-duty question for the committed curve
family, over its tested band — the "unknown mechanism" below was an affine
map plus a units confusion between requested, interpolated and applied.
Every measurement in this section stands; the closed loop it motivated
stays, because the map is band- and family-local.)*

Four measurements of the same dock, all on 2026-08-12, all with the card
idle (~10 W, edge ~35 °C):

| curve written | low-end request | applied PWM (byte[9]) | the fan |
|---|---|---|---|
| flat — all eight points at duty 40 (`xgm-fan-hold.py --duty 40`) | 40 | **175** | spinning |
| shaped — rising from a low idle point (deployed ladder) | 89 | **115** | spinning, audibly slow — the operator heard it "back, low speed" |
| shaped — the old bottom rung, zeros at the low end (deployed ladder) | 0 | **0** | **stopped** |
| shaped — the ladder daemon's floor curve, idle request 89 again, different shape (deployed 2026-08-12) | 89 | **179–180** | spinning — holding edge 35 °C, Ally fans 4600 rpm, stable |

The one variable separating 175 from 115 from 0 is the SHAPE of the curve. A
flat curve pinned applied at 175 no matter how low it asked. A shaped curve
walked straight through that "floor" to 115, and a shaped curve with zeros at
the low end stopped the fan outright.

### The fourth measurement — same request, different shape, 64 units apart

The strongest of the four, because it removes the last confound: the fourth
row's curve **requests the identical idle value, 89**, that the second row's
curve requested — only the shape around it differs — and the controller runs
**179–180 where it ran 115**. A 64-unit spread in applied duty from the same
requested number, predicted by nobody, in either direction. Had the spread
gone the other way, the fan would have stopped again — and nothing in the
requested bytes would have said so.

That pair of numbers is why the daemon now runs CLOSED LOOP
(`bin/system-thermal-daemon.py`, `verify_tick`): after applying, it reads
back every tick — the Ally fans' real tachometers, the dock's byte[9] — and
a fan that is not obeying (0 rpm, or applied below `FLOOR_DUTY`) escalates
to the top rung, is logged loudly, and blocks all descent until the readings
recover. No correction factor, no model of the mapping — the mechanism is
unknown, so the only honest number is the one read back.

Requested → applied across the shaped rungs of the same run:

```
178 -> 252     134 -> 178     89 -> 115     0 -> 0
```

Applied is not the number requested, the offset is not constant, and none of
it matches the `requested+95, floored at 175` transform that is real for flat
curves (§12's decay table, `xgm-fan-hold.py`'s header).

### What is NOT known

The mechanism. Nothing here explains *why* shape matters — point spread,
slope, which points the controller samples, something else. No fourth
confident answer is on offer, deliberately: **we cannot currently predict
what applied duty a given curve will produce.** Whoever needs to know what a
curve does must measure that curve, with `GetFanSpeed` byte[9] or their ears.

### Where that leaves §10 and §12

- §12's measurement (flat request 40 → applied decays to 175 and holds) is
  correct. The word "any" in its title was the error: it generalised one
  curve shape to all of them.
- §10's acceptance-floor table (`GetFanCurveLowerLimit` = 0/0/0/0/34/54/62/87)
  is correct as a statement of what a curve may ASK, and is still used to
  clamp every rung. It says nothing about what the controller then RUNS —
  on that §12 was right, even though its own answer didn't generalise.
- One sentence survives all three rounds: **the stored tables, the acceptance
  limits, and the applied PWM are three different registers, and only the
  last one moves air — per curve, by measurement.**

### The engineering conclusion (independent of mechanism)

Because applied duty is unpredictable from the requested curve, a daemon that
must never stop a fan cannot get there by choosing constants that look safe.
It has to enforce a nonzero floor STRUCTURALLY — every rung, every point, at
the point of hardware contact, proved by test over every rung the ladder will
ever hold — which is what `bin/system-thermal-daemon.py` now does
(`FLOOR_DUTY`, `never_stops()`). The floor is pinned to the measured
`89 → 115` pair (~45 % applied): the request that landed nearest 50 %
applied, and the level at which the card held its temperature with the
supplementary chassis exhaust running — the normal configuration. (The
fourth measurement above later showed the same request applying 179–180
under the deployed floor curve's shape, so the constant's job widened: it
is the request floor no rung may ask below, and the bar the daemon's
per-tick readback is verified against — not a predictor of what will run.)
The requirement driving it is the operator's: fan-off is not an acceptable
resting state on a host that is online 24×7.

---

## 14. The daemon verifies its fans, and reacts to load — not to a process name (2026-08-12)

Two conclusions from the same day, both now structural in
`bin/system-thermal-daemon.py`. Recorded here so neither gets quietly
reverted by someone to whom the old shape looks simpler.

### The daemon no longer assumes the fan obeys

§13 is the reason, and it is cited rather than re-derived: applied duty
depends on curve SHAPE by a mechanism nobody understands, and the fourth
measurement — the identical request value, 89, applying **115** under one
shape and **179–180** under another, a 64-unit spread predicted by nobody —
removed the last excuse for open loop. Had that spread gone the other way,
the fan would have stopped, and the requested bytes would have said nothing.

So the daemon runs CLOSED LOOP (`verify_tick`): every tick, after applying,
it reads back the Ally fans' real tachometers (`fan1`/`fan2_input`, `asus`
hwmon — rpm > 0 is directly observable) and the dock's `GetFanSpeed` byte[9]
(the only signal that fan emits; there is no dock tachometer, and byte[6] is
garbage — never key anything on it). A fan reading 0 rpm, or a dock applied
duty below `FLOOR_DUTY`, held for `VERIFY_FAIL_TICKS` (3) consecutive
verifiable ticks — one bad sample is a spin-up transient or a flaky read,
never a step — escalates to the top rung on that same tick, logs
`VERIFICATION FAILURE` loudly, and refuses all descent until the readings
recover. No correction factor, no model of requested→applied: measure,
verify, react.

### The trigger is load, because the process name failed in both directions

The daemon's only non-thermal input used to be `comm == "llama-server"` — a
fossil from its eGPU-inference-guard days. On 2026-08-12 alone:

- **False negative, the dangerous one.** A `cmake --build -j12` of
  llama.cpp, and a CPU inference benchmark run as
  `llama-bench --device none -ngl 0`, drove **Tctl to 95.0 °C** — the
  hottest reading of the day. Neither process is named `llama-server`. The
  daemon was blind to both and had only absolute temperature to go on,
  reactively.
- **False positive.** A 14B resident at 10 W doing nothing held every fan
  at 100 % indefinitely.

Maximum is now forced by what the box is DOING — eGPU `power1_average`
(10 W resident vs 165 W generating, measured repeatedly), eGPU
`gpu_busy_percent`, CPU package temperature (k10temp Tctl), and
**rate-of-rise** on edge or Tctl (+3 °C per 30 s: 2.5× the worst measured
idle drift, ~1/10 of measured load onset — anticipation was the only real
argument for watching a process, and a fast rise anticipates better than an
identity does). The `mode quiet` grant runs on the same evidence. A
recognisable engine comm (`llama*`/`ollama*`) appearing in `/proc` survives
only as a PRE-POSITION hint toward a middle rung, explicitly not
load-bearing. **Do not reintroduce a process-name trigger** — the two
counter-examples above are what it costs.

### What is NOT yet measured

The ladder's intermediate rungs were re-spaced (geometrically in duty,
178/150/126/106/89 at idle; thresholds 56/51/46/41 °C) after the
measurements above, so **every intermediate rung is a new curve shape whose
applied duty is unknown until the operator deploys and reads byte[9]** —
per this section and §13, that is not a formality: the same request has
measured 64 units apart across shapes. The endpoints are byte-for-byte the
measured curves. The per-tick verification is the net under the new rungs;
the journal prints `dock pwm=` on every tick, and `status` reports
`verify_failed` the moment a reading contradicts a rung.

*(2026-08-13: the floor sweep in §15 then measured the BOTTOM rung at three
floor values — and moved the committed floor to 55/65, re-spacing the walk
to 178/133/99/74/55. The intermediate rungs remain unmeasured; §15's affine
map now predicts them within its band, but a prediction is deployed as a
prediction: the verification stays the net.)*

---

## 15. CLOSED: the floor sweep — applied duty is affine in the temperature-interpolated request (measured 2026-08-13)

**This closes the open question §10, §12 and §13 each left standing.** Three
sections tried to characterise the dock's low-duty behaviour and each read a
different truth off a different surface: §10 from the acceptance-limit
register, §12 from one flat curve, §13 from unpaired shapes. The answer was
an **affine map plus a units confusion** — three different quantities wearing
the same 0–255 clothes, and a labelled constant being compared against a
number the controller computes. There was never a hidden offset.

### The sweep

Three floor values for the ladder's bottom rung, varying ONLY the
`QUIET_CURVE` 30 °C / 40 °C points (the rest of the curve byte-identical),
each deployed to the running daemon and held ~10 minutes at idle with the
supplementary chassis exhaust running — the normal configuration — sampling
the daemon's own status socket at 5 s:

| QUIET_CURVE (30 °C, 40 °C) | value @ 35 °C | dock applied | Ally rpm | edge |
|---|---|---|---|---|
| 89, 99 | 94 | 179 (70 %) | 4600 (71 %) | 35 °C |
| 70, 80 | 74 | 152 (60 %) | 4200 (65 %) | 35 °C |
| **55, 65** | 60 | **131 (51 %)** | **3900 (60 %)** | **35–36 °C** |

**Zero verification failures at any floor** — the closed loop (§14) watched
every tick of all three runs. And **edge temperature was identical at all
three**: at idle, in this configuration, the card does not need the airflow.
The cooling is doing nothing the chassis exhaust was not already doing.

**The operator chose 55/65 as the committed floor.** `FLOOR_DUTY = 45`
accompanies it as **the clamp that permits it** — under the previous value
(89) `never_stops()` would have silently raised the chosen 55 back to 89 —
and it is *not* itself the floor: the floor is the curve.

### The map

Across all three points:

> **applied ≈ 1.37 × (curve value interpolated at the current temperature) + 50**

That reproduces 179, 152 and 131 to within one unit. It also retrodicts the
measurement that §13 called unexplainable: the old floor curve's points
(30 C: 89, 40 C: 99) interpolate to **94** at the idle 35 °C, and
1.37 × 94 + 50 = 178.8 — the measured **179–180**. The "64-unit spread from
shape alone" was the same map fed two different *interpolated* values: what
was labelled "the same request, 89" was the same 30 °C *point*, not the same
number reaching the controller at the live temperature.

### The three scales — record them, so nobody conflates them again

| scale | who produces it | at the committed floor, idle (35 °C) |
|---|---|---|
| **requested** | a curve POINT, 0–255, what `SetFanCurve` carries | 55 @ 30 °C, 65 @ 40 °C |
| **interpolated** | the controller, from the points, at the LIVE temperature | 60 |
| **applied** | `GetFanSpeed` byte[9] — the only number that moves air | 131 (51 %) |

**55 is not 55 %.** A curve value is a request at a temperature point; the
controller interpolates it; the fan runs the applied number, which on this
dock is neither of the first two. (§13's provenance sentence survives intact:
the stored tables, the acceptance limits, and the applied PWM are different
registers, and only the last one moves air.)

### Where the map is valid — and where it is not

Established over the **tested band only**: interpolated values 60–94, on the
committed curve family (eight rising points, 255 tail, only the two low
points varying). Outside it, the map is known to FAIL:

- **At the bottom it is dangerously wrong.** The fit predicts applied 50 at
  curve 0; curve 0 was **observed to stop the fan** (applied 0, §13). Do not
  extrapolate downward, ever.
- §12's flat curve (all points 40) measured applied 175 where the map would
  say ~105 — a flat curve is outside this family, so **shape remains a
  variable** beyond the band.
- The pre-sweep "89 → 115" pair (§13, second row) cannot be checked against
  the map: that curve's points were not recorded, so its interpolated value
  at the live temperature is unknown. The map *plausibly* explains it; it
  does not *prove* it.

The engineering rule outside the band is unchanged from §13: measure the
curve you intend to run, with byte[9] or your ears. Inside the band the map
now predicts — and the daemon still verifies every tick, because a
prediction is deployed as a prediction.

### What is NOT established

**Every floor was tested with the chassis exhaust running.** 55/65 is
validated as a *resting* floor in the normal configuration only. Without the
exhaust it is a **starting state, not a resting one**: an idle card short of
airflow drifts (+2.4 °C/min measured on zero cooling, §11), and what
protects it is the ladder's ascent — designed, and observed in earlier
configurations, but **not measured at this floor**. Also still unmeasured:
every intermediate rung's applied duty (the sweep measured floors only), and
anything about this map under load.

### Where that leaves the code

`QUIET_CURVE` low points are 55/65 and `FLOOR_DUTY` is 45, committed, and
both now live in `/etc/system-thermal-daemon.conf` alongside this sweep
table and the three-scales warning. The closed loop keeps its job unchanged:
the map is band-local, the mechanism behind it is still unexplained, and the
only number the daemon ever trusts is the one it read back.

### s15 addendum — the committed ladder, measured end to end (2026-08-13)

Full descent on the committed config, sampled at 5s from the daemon status
socket. Chassis exhaust running. Raw capture:
`measurements-ladder-20260813.csv`.

| rung | idle duty | applied | Ally rpm | edge |
|---|---|---|---|---|
| 0 | 178 | 255 | 6500 | 34 C |
| 1 | 133 | 252 | 5600 | 34 C |
| 2 | 99 | 193 | 4800 | 34 C |
| 3 | 74 | 157 | 4300 | 35 C |
| 4 | 55 | 131 | 3900 | 35-36 C |

Monotonic on both surfaces; every rung turns every fan; **zero verification
failures**. Rungs 1-3 had no measured pair before this run.

The s15 affine map (applied ~ 1.37 x interpolated + 50) predicts rungs 0, 2, 3
and 4 within 3 units. Rung 1 measured 252 against ~235 predicted — most likely
an incomplete settle on a 60s dwell descending from max, not a break in the
relation. Not re-tested; recorded as the one loose end.

---

## 16. Two fans one package, one fan one card — the seam the fan ladder had to be split along (observed 2026-08-13)

### The observation

A CPU spike took **every fan on the desk** to maximum. From the journal of the
single-ladder daemon:

```
ladder: rung 4 -> 0 — cpu Tctl rising +12.6C in 20s (trip: +3.0C per 30s)
                      — heat is arriving; the fans go first
```

The load was a `claude` CLI job. **No inference engine was running anywhere.**
The eGPU was idle at 10 W, its edge temperature 32 °C, its fan resting at the
bottom of the ladder — and it went to 100 % along with everything else.

The rate trigger fired correctly. It is §14's anticipation signal doing exactly
what it was built to do: heat is arriving on the APU, spend the fans before a
threshold confirms what the slope already says. **Raising the Ally's fans was
right.** Raising the *dock* fan was not: it cools a physically separate card, in
its own enclosure, with its own airflow, that was doing nothing at all.

The defect was never the trigger. It was that one ladder drove three fans, so
*any* evidence about *any* part of the machine moved *all* of them.

### The seam is not "CPU fan vs GPU fan"

The obvious split — three fans, so three ladders, or at least CPU-versus-GPU —
is wrong, and wrong in a way worth recording because it looks right.

The Ally's `cpu_fan` and `gpu_fan` (`pwm1`/`pwm2` on `asus_custom_fan_curve`)
are **two fans cooling one package**: the Ryzen Z1 Extreme, whose GPU is on the
same die as its CPU. They are not independently addressable in any thermally
meaningful sense — there is no arrangement of this hardware in which cooling
the Ally's GPU but not its CPU means anything — and the package publishes **one
control temperature**, k10temp's `Tctl`. A CPU spike raising both of them is
not a coupling to be removed; it is one zone being cooled.

The genuinely independent surface is the **dock**. Different silicon, different
enclosure, different air, different controller (HID `0x5E` on the ITE 8910, not
hwmon), and its own sensors: `power1_average`, `gpu_busy_percent`, edge and
junction.

So the split is **two ladders, not three**:

| ladder | fans | driven by |
|---|---|---|
| **ally** | `pwm1` + `pwm2`, `asus_custom_fan_curve` | CPU `Tctl` (k10temp), and its rate of rise |
| **dock** | XG Mobile over HID `0x5E` | eGPU `power1_average`, `gpu_busy_percent`, edge, junction, and their rates |

**That the Ally pair is one thermal zone is a property of the Z1 Extreme, not a
configuration choice.** It is why the shipped config has no per-fan section for
them and why one is not a reasonable feature request on this hardware. On a
machine with a discrete CPU and GPU it would be two zones and two ladders, and
the daemon's machinery would take that without change — the ladder count
follows the surfaces, not the fan count.

### What the split does and does not change

Both ladders are two **instances of one piece of machinery**. Descent gating,
hysteresis, the stability window, dwell, backoff, `never_stops`/`clamp_to_floor`,
the closed-loop verification of §13/§14 and the ascent-is-total rule are shared
code, parameterised — only the signals and the applied surface differ. A second
copy of the descent gate would be the bug, not the feature.

The curves did **not** change, and that is deliberate: both surfaces ship the
same `max_curve`, the same floor curve and the same `floor_duty`, so §15's
sweep and the committed ladder walk stay applicable to each. What changed is
the *condition* under which a rung is reached. Under a split, §15's Ally rpm
column still holds (same curve, same fans) and belongs to the ally ladder; the
applied-PWM column still describes what the dock's rungs apply and is now
independent of Ally state. **A future capture showing the two ladders at
different rungs is the change working, not a discrepancy.**

An **unreadable sensor escalates only the surface it belongs to.** This is the
part most likely to be got wrong, because the wrong answer is the comfortable
one: escalating both "to be safe" silently rebuilds the coupling the split
removed, and does it invisibly. It also reversed a rule: an unreadable `Tctl`
used to be no escalation at all (a reused bundle without k10temp must not be
condemned to permanent maximum). Under one ladder Tctl was an anticipation
extra on top of edge; it is now the ally ladder's *only* control signal, and a
ladder with no reading has no evidence for descending. The cost is named where
it is paid: on a host with no k10temp the Ally pair runs loud, visibly, and the
honest fix is to bind that surface to a sensor the host has.

### The one coupling that is real, and what it is built on

An engine loading the eGPU **also heats the APU that feeds it**: the host
process doing the feeding runs on the Z1 Extreme, and the 165 W the card draws
while generating is dissipated into a chassis the Ally shares. If the dock
ladder is at maximum for sustained eGPU load, the Ally should not be resting at
its floor.

That is expressed as the weakest mechanism that does anything: a **floor**
(`couple_floor_rung`, the middle rung, after `couple_hold_s` of sustained
load), never an escalation. "The dock is at maximum" is not evidence that the
APU is hot, and treating it as evidence would rebuild the coupling with the
arrow reversed. If the APU really does heat, the Ally's own signals take it to
maximum on their own evidence and remain the load-bearing path.

**Stated plainly: this rests on the physical argument and no measurement.**
Every Tctl figure on record comes from CPU-side work (95.0 °C under `cmake
--build -j12` and a CPU `llama-bench`, §14) or from idle (32.6 °C, read live
2026-08-13 at load average 0.35). **Tctl under eGPU inference has never been
measured on this box.** The first observation will be the next live run — a 14B
on the eGPU with the APU relatively idle, which is also the first genuinely
independent load the dock ladder has ever seen. If that run shows the APU stays
cool, `couple_floor_rung` set to the bottom rung retires the coupling honestly,
and the journal's own `ally=` field is the measurement to do it with.

### The Ally ladder's thresholds are derived, not measured

The dock ladder kept its measured-range thresholds (56/51/46/41 °C edge). The
ally ladder needed thresholds in a channel that had never gated anything:
**75/65/55/45 °C Tctl**, spaced 10 °C rather than the dock's 5 °C because Tctl
is a far coarser signal — the event above moved it 12.6 °C in 20 s, and it
swings several degrees at idle from background work, where edge reports in flat
1 °C steps. Anchors: idle 32.6 °C (measured), light load mid-50s (§14), 95.0 °C
under CPU inference (§14), and `cpu_load_c` 80 °C forcing maximum by itself, so
the whole ladder lives below it. **DERIVED, all four**, and never walked against
a real sustained CPU load. If the Ally pair proves twitchy in the journal, the
rate trip is the number to re-derive — and it is now safe to re-derive it
without touching the dock, which is the point of the whole exercise.

---

## 17. The guard and the daemon do NOT disagree about this card — the contradiction was a clock, not a sensor (diagnosed 2026-08-13)

### The observation that prompted this

The seeded self-replicate ran `artifact-2026-08-07` twice. Turns 1-6 came back
byte-identical; turn 7 of run 1 was truncated by the pipeline's mid-stream
thermal guard:

```
"stopped generating at edge 68°C — the answer below is INCOMPLETE."
stop_reason = thermal_abort          -> scored UNSCORED
```

Against that, the daemon's journal tail showed both ladders walking *down* to
their floors on edge 36/35/34 °C and Tctl 34.6/34.2/33.2 °C. One component
aborted generation at edge 68; the other, reading the same card, saw 34-36 °C.
**Both cannot describe the same card at the same time** — so one of them had to
be reading the wrong device, or the wrong channel, or lying about which.

### Neither. They were reading the same device, by the same method, and they agree

**The run window is 19:14:50 - 19:27:19** (selfrep-1 19:14:50-19:21:27,
selfrep-2 19:21:38-19:27:19; `header.json` in both run directories).

**The quoted descent lines are all AFTER it:**

| journal line | timestamp | vs run end (19:27:19) |
|---|---|---|
| `ladder[ally]: rung 1 -> 2 — cpu Tctl 34.6C` | 19:28:46 | +87 s |
| `ladder[dock]: rung 1 -> 2 — edge 36.0C` | 19:28:56 | +97 s |
| `ladder[ally]: rung 2 -> 3 — cpu Tctl 34.2C` | 19:29:46 | +2m27s |
| `ladder[dock]: rung 2 -> 3 — edge 35.0C` | 19:29:56 | +2m37s |
| `ladder[ally]: rung 3 -> 4 — cpu Tctl 33.2C` | 19:30:47 | +3m28s |
| `ladder[dock]: rung 3 -> 4 — edge 34.0C` | 19:30:57 | +3m38s |

That is the **post-run cooldown**, which is exactly what a descent to the floor
on 34-36 °C is. The daemon logs a ladder step only on change, so a tail of the
journal shows the last thing that happened — and the last thing that happened
was the card going cold after the run finished. The tail was read as
concurrent. It was not.

### What the daemon actually reported DURING the run

It escalated, immediately, and then stayed at maximum:

```
19:14:27  ladder[dock]: rung 4 -> 2 — engine hint: pid(s) 520053 appeared
19:14:27  ladder[ally]: rung 4 -> 0 — cpu Tctl rising +29.5C in 20s
19:14:57  ladder[dock]: rung 2 -> 0 — significant load: eGPU drawing 154W
```

Nine ladder moves in the window, four of them dock ascents. `pwm=255` on the
dock essentially continuously from 19:14:57 to the end. Its own edge readings
across those thirteen minutes run **49-63 °C**, its junction gate fired twice
(`junction 59.0C above the 55C ceiling`), and six seconds before the abort:

```
19:21:21  dock=rung0/4 gate=load pwm=255 eGPU drawing 164W
19:21:27  [guard] stopped generating at edge 68°C  (junction 82°C)
19:21:31  dock=rung0/4 gate=rise pwm=255 edge rising +11.0C in 20s
```

The daemon saw edge **rising** on the tick after the abort. The two components
corroborate each other to within the ~1 °C the edge channel reports in and the
few seconds separating their samples.

### The four questions, answered

**1. Which sensor does the pipeline guard read — PCI path or index?**
**PCI path.** `ThermalSource.discover` (`core/thermal.py`) walks
`/sys/bus/pci/devices/<pci>/hwmon/` and checks each node's `name` file against
`HWMON_NAME = "amdgpu"`; `/sys/class/hwmon/hwmonN` is never used. The role ->
PCI mapping is `core/gpu_roles.ROLES` (`egpu` = `0000:03:00.0`). The archived
`readiness.txt` records the resolution it actually made:

```
thermal guard: active — amdgpu @ pci 0000:03:00.0
  (/sys/bus/pci/devices/0000:03:00.0/hwmon/hwmon8); channels edge, junction, mem
```

The daemon resolves identically (`EGPU_PCI` + `PCI_ROOT` glob, `system-thermal-
daemon.py`). **Both readers are PCI-globbed; neither is index-pinned.** The §2
hazard — hwmon8 is the eGPU and hwmon9 the iGPU under the same driver name —
is real and is what both were written against. It did not fire here.

**2. Is "edge" in that message actually `temp1_input`?**
**Yes, and the label is not inherited from anything.** `_map_channels` builds
`{label: input_path}` by reading each `tempN_label` and pairing it with the
matching `tempN_input`; the string in the message is that key, carried from the
reading through `reading.control()` into `abort_text(temp_c, sensor=by)`. The
guard cannot print "edge" while reading junction — the label and the path come
out of the same map entry. Corroborating: the same log line reports
`(junction 82°C)` alongside, from the *other* map entry, and 68/82 is a
plausible edge/junction pair. A mislabelled junction reading of 68 would have
had to print an edge value it did not have.

**3. Did the card genuinely reach 68?**
**Yes.** Three independent records say so:
- the guard's own per-turn traces (`results.jsonl`), every turn of both runs:
  peak junction 72-83 °C, peak edge 58-68 °C, pauses on 11 of 12 scored turns;
- the daemon's independent 10 s reads, 49-63 °C edge, at `pwm=255`;
- the daemon's escalation on eGPU **power** — 146-165 W, against a 45 W work
  threshold — which is not a temperature reading at all and cannot be wrong
  about whether the card was working.

The card was running a 14B at 165 W for thirteen minutes. It was hot.

**4. Could a brief spike be real, invisible to the daemon's log, and not move
its rung?** **In general yes — but that is not what happened, and the mechanism
is worth recording separately.** The guard samples inside the generation frame
loop at `poll_s` = 0.5 s; the daemon samples every ~10 s (75 tick lines across
the 13-minute window) and its rungs are evaluated only at those instants. An
edge excursion shorter than ~10 s can therefore be real, be caught by the
guard, and leave no trace in the daemon's log *and* no ladder movement — the
rungs vacate to the top on a sampled reading above threshold, and a spike
between two samples is not a sampled reading. **That is a genuine blind spot in
the daemon and it is inherent to a 10 s tick.** It is not the explanation here
only because the daemon's log for this window is neither silent nor cool: it
had already escalated to maximum on the load gate 30 s into the run, and there
was no higher rung for a spike to move it to.

### Verdict

**The guard was right, its message was right, its sensor was right, and the
daemon agrees with it.** No defect exists in the sensor plane. The abort at
edge 68 °C was a correct report of a real reading from the correct channel of
the correct device.

What *is* wrong is the **calibration**, and it is a threshold problem, not a
sensor problem — see §18.

### A second finding, unlooked-for: Tctl under eGPU inference

§16 closed by recording that **Tctl under eGPU inference had never been
measured on this box**, that the Ally ladder's coupling floor rested on a
physical argument and no measurement, and that the next live 14B run would be
the first observation. This was that run. The APU does **not** stay cool while
the eGPU carries the model:

```
19:15:07  ally=rung0/4 gate=load  CPU Tctl 86.5C at or above the 80C heavy-work threshold
19:16:38  ally=rung0/4 gate=load  CPU Tctl 85.0C
19:20:21  ally=rung0/4 gate=load  CPU Tctl 86.8C
          (peaks of 87.1 / 87.9 C inside the stability windows)
```

Tctl ran **80-88 °C sustained**, above `CPU_LOAD_C` (80), with the Ally ladder
pinned at rung 0 for essentially the whole run on its own gates — the coupling
floor never had to carry it. The physical argument in §16 is **confirmed by
measurement**: the APU feeds that card and heats up doing it. `couple_floor_rung`
should NOT be retired to the bottom rung; the case §16 raised for doing so is
now closed against.
