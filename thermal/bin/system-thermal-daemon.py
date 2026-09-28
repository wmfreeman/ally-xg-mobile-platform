#!/usr/bin/env python3
"""
SystemThermalDaemon — the system's thermal authority.

One owner for every thermal knob this host has: the Ally's own CPU and
GPU fans (asus_custom_fan_curve), the XG Mobile dock fan (USB HID), the
platform profile, and the eGPU's runtime PM. The name reads as the
system's authority rather than an eGPU accessory because that is the
scope — every fan on this desk answers to this process.

TWO COOLING SURFACES, TWO LADDERS (2026-08-13). One ladder drove every
fan until a CPU spike — a `claude` CLI job, no inference engine
anywhere, the eGPU idle at 10 W — fired the rate trigger and took ALL
of them to maximum:

  ladder: rung 4 -> 0 — cpu Tctl rising +12.6C in 20s (trip: +3.0C per
                        30s) — heat is arriving; the fans go first

Raising the Ally fans was correct. Raising the DOCK fan was not: it
cools a physically separate card that was doing nothing.

THE SEAM IS NOT "CPU FAN VS GPU FAN". The Ally's cpu_fan and gpu_fan are
two fans cooling ONE PACKAGE — the Z1 Extreme, whose GPU is on the same
die — so they are not independently addressable in any thermally
meaningful sense and a CPU spike raising both is right. They are never
split from each other. The genuinely independent surface is the DOCK,
cooling the eGPU in its own enclosure with its own airflow:

  ladder  fans                        driven by
  ------  --------------------------  ------------------------------
  ally    pwm1 + pwm2 on              CPU Tctl (k10temp), and its rate
          asus_custom_fan_curve       of rise
  dock    XG Mobile over HID 0x5E     eGPU power1_average,
                                      gpu_busy_percent, edge, junction,
                                      and their rates

They are two INSTANCES of one piece of machinery (SURFACES, built by
_build_surface): the descent gating, hysteresis, stability window,
dwell, backoff, never_stops/clamp_to_floor, the closed-loop
verification and the ascent-is-total rule are shared code, parameterised
— only the SIGNALS and the APPLIED SURFACE differ. Each ladder descends
on its own gates, ascends to its own top on its own evidence, verifies
its own fans and reports its own rung. If an edit ever produces two
copies of the descent gate, it is wrong: parameterise instead.

DEPLOYED NAMES, deliberately unchanged: the systemd unit stays
`glados-thermal.service` (ExecStart=/usr/local/sbin/glados-thermal.py)
and the control socket stays /run/glados-thermal.sock. Both are load-
bearing — the running unit on this host, core/lifecycle/fan_client.py,
and every journalctl habit built against them — and renaming either
would break a deployed host for a purely cosmetic win. If they are ever
renamed, the deploy instructions must migrate the unit file, the socket
consumers, and the operator's muscle memory in one step.

WHY THIS EXISTS. On 2026-08-07 the host hard-powered-off mid-inference:
  amdgpu 0000:03:00.0: ERROR: GPU over temperature range(SW CTF) detected!
SW CTF debounces 50 ms then calls orderly_poweroff() on the HOST — no throttle
stage, no GPU-only reset. Two capped fan curves caused it:

  Ally  asus_custom_fan_curve : cpu_fan capped 66/255 (26%), gpu_fan 2/255
  Dock  stock curves          : table[2] 34/255 flat; table[3] reaches 87/255
                                (34%) only at 98 C — two degrees under the CTF

Neither was slow. Both were CAPPED. (What the dock ACTUALLY runs for a low
custom curve went through three confident answers — floored at 175, floored
per-point at 0/0/0/0/34/54/62/87, floored at 175 again — before FINDINGS s13
recorded what is measured and no more: a FLAT curve pinned applied PWM at
175; a SHAPED curve reached 115 with the fan audibly turning; and this
ladder's old bottom rung reached 0 and STOPPED THE FAN. Curve shape is the
operative variable and the mechanism is NOT KNOWN — which is why every rung
is now floored structurally (FLOOR_DUTY) instead of by trusting any model
of what the controller will do with a request.)

THE TWO FANS TAKE DIFFERENT PATHS, which is why nothing worked before:
  * Ally  -> asus_custom_fan_curve hwmon, 8 (temp,pwm) points. Persistent
             until reboot.
  * Dock  -> USB HID report 0x5E on the ITE 8910 (usage page 0xFF31, usage
             0x80). amdgpu's pwm1 is ACCEPTED AND IGNORED on this card, and
             asus-nb-wmi has no XG Mobile fan device id at all. This HID
             channel is the only thing that reaches it.
             Protocol: osy/XG_Mobile_Station Docs/Software.md.
             SetFanCurve writes the ACTIVE curve, which GetFanCurveTable
             cannot read back — do not trust a readback, trust the fan.
             It is VOLATILE, hence the re-assert loop.

NOT DONE, deliberately: ppt_* is left alone. Those read 5 while the APU was
measured drawing 21 W, so 5 is not a 5 W cap, the units are undocumented
(sysfs-platform-asus-wmi states none), and whether RC71L firmware clamps an
out-of-design value is unknown. platform_profile=performance is the supported
lever and is applied instead.

================================ THE CONTROL SOCKET ==========================

Added for the operator's "shutdown system" button. The web UI runs as `glados`
and can do ZERO fan control: every knob that matters is root-owned (the
asus_custom_fan_curve hwmon, /dev/hidraw* for the dock, platform_profile, the
eGPU's runtime PM). polkit is not an option — there is no graphical session on
this host, so there is no agent to ask — and putting sudo inside an
unauthenticated LAN-facing web process is the wrong shape. So this daemon,
which already owns every one of those knobs, grows a control interface, and
one owner per knob is preserved.

  socket   /run/glados-thermal.sock, root:glados, 0660. The socket GROUP is
           the authorization boundary: filesystem-checkable, no sudo, no
           setuid, and revocable with a chgrp.

  protocol VERB-ONLY. Three verbs, no parameters, ever:
               status        what the fans are actually doing
               mode max      the cooling curve
               mode quiet    the passive curve — SUBJECT TO THE INTERLOCK
           It never accepts a duty value, a curve, a device path, a pid or a
           temperature from the caller. There is therefore no input to
           validate and no way for a caller to ask for a state the daemon
           does not already believe in. That is deliberate: the endpoint that
           reaches this socket is itself unauthenticated on the LAN by
           operator decision, so the smallest possible vocabulary is the
           whole defence.

  interlock `mode quiet` is REFUSED unless this daemon confirms, ITSELF, all
           three of:
               * NO SIGNIFICANT LOAD is observable — eGPU power draw, eGPU
                 utilisation, CPU package temperature: the same evidence
                 that re-arms the ladder. A resident-but-idle engine is NOT
                 load, and no process name is consulted (see THE LOAD
                 SIGNALS for why that check was retired);
               * the eGPU edge temperature is at or below QUIET_EDGE_C, and
                 junction at or below QUIET_JUNCTION_C;
               * the sensor is READABLE — an unreadable sensor is a refusal,
                 never an assumption of cool;
           and that all three have held continuously for QUIET_HOLD_S.

           It does not take the caller's word for any of it. The caller is a
           systemd service with Restart=on-failure and RestartSec=2: it can
           die between deciding the engines are down and saying so, and come
           back two seconds later with no memory of what it was doing. A
           claim that arrives over a socket is a claim about a moment that
           has already passed.

           Quiet is also CONDITIONAL, not a latch: every tick re-evaluates
           the interlock, and the first tick that fails it moves the ladder
           back up and logs why. Somebody starting a model from the CLI
           while the fans are passive is the case that has to be survivable.

  watchdog A `mode max` that arrived over the SOCKET starts a timer. If no
           significant load appears within SPINUP_WATCHDOG_S, the daemon
           releases the operator pin — descent then resumes on the ladder's
           own gates.
           This covers "fans pinned at 6500 rpm forever because the GUI
           died mid-sequence". It applies ONLY to a socket-requested max:
           the boot default is the top of the ladder UNPINNED, and a
           watchdog on that would be redundant — the ladder already walks
           down by itself when the gates pass.

================================ THE LADDER ==================================

On 2026-08-10/11 the daemon re-armed (correctly — the card had warmed past
QUIET_EDGE_C) and then held maximum for TEN HOURS while logging "no engine
running, card cool, sensor readable" on every tick. Revocation was terminal
because the mode was binary and only a socket verb could lower it, and
nothing sends one. The binary mode is gone; in its place a LADDER of curves
the daemon walks by itself.

The design is EMPIRICAL by intent: the thermal environment includes at
least one variable this daemon cannot see or control (a supplementary
chassis exhaust fan the operator switches between full and auto), so
nothing here models cooling capacity. The daemon steps, OBSERVES the
response, and reacts — and every constant that shapes that walk is marked
measured or derived where it is defined.

ASYMMETRIC BY DESIGN:

  * UP is immediate, unconditional, and TOTAL — for THE SURFACE WHOSE
    EVIDENCE IT IS. Any tick that justifies any upward movement on a
    ladder takes that ladder straight to its very top: significant load
    on its own signals, a fast rise on its own control temperature, its
    own sensor unreadable, its own ceiling excursion, or its own rung's
    ceiling exceeded, all alike — with no dwell, no stability
    requirement, no hysteresis, and no intermediate stops. The other
    ladder does not move: that coupling is what the 2026-08-13 split
    removed.
    The operator, verbatim:
    "we step up to 100% from there quickly - since load is going to be
    exponential - and use the intermediate rungs not as gating the climb
    but the descent." Thermal load from inference is not gradual, and a
    card that climbs one rung at a time while heating is behind the
    curve at every step. Overshooting cooling costs noise; undershooting
    costs the host. Cooling is never something to be deliberate about.

  * DOWN is one rung at a time, and every step is gated: no significant
    load on that surface's signals, its control temperature readable and
    below the entered rung's threshold less a hysteresis margin, that
    temperature STABLE over a dwell window (below the bar is a level test
    and will hunt; see stability_ok), and a minimum dwell since the
    previous step in EITHER direction. A step down that gets thrown back
    extends the dwell before the next attempt — a capped backoff, so a
    surface that keeps warming stops being asked. Each ladder keeps its
    own dwell clock, its own history and its own backoff: a throw on one
    never spends the other's patience.

The bottom of the ladder keeps every fan TURNING — the committed floor
curve measured dock applied PWM 131 (~51%) and Ally fans 3900 rpm on the
2026-08-13 three-point floor sweep (FINDINGS s15), holding edge 35-36C,
identical to floors asking nearly twice as much: the applied duty is
curve-shape-dependent, which is why it is VERIFIED rather than believed
— and the top is the measured max curve, byte-for-byte. Two
floors shape every rung. The device's per-point ACCEPTANCE floors
(FINDINGS s10): a request below one is silently clamped by the firmware,
and a daemon that logs a duty the hardware is not honouring is a daemon
lying to the journal. And FLOOR_DUTY, the operator's: the 2026-08-12 run
proved a shaped curve can drive the dock's applied PWM all the way to ZERO
(FINDINGS s13), the old bottom rung did exactly that, and a card with no
cooling always warms out of that rung's own ceiling — so "fan off" was
never a resting state, only a sawtooth with extra steps. No rung may stop
a fan. The only stop is bye()'s SIGTERM handoff to firmware.

REACT TO LOAD, NOT TO A NAME. The daemon's only non-thermal trigger used
to be comm == "llama-server" — a fossil from its eGPU-inference-guard
days, when a process name was a fair proxy for the one failure it was
written against — and as the system's thermal authority it was wrong in
both directions on the same day (2026-08-12). False negative, the
dangerous one: a `cmake --build -j12` of llama.cpp and a CPU inference
benchmark run as `llama-bench --device none -ngl 0` drove Tctl to 95.0C,
the hottest reading of the day, and neither process is named
llama-server — the daemon was blind to both and had only temperature to
go on, reactively. False positive: a 14B resident at 10 W doing nothing
held every fan at 100% indefinitely. Maximum is now forced by OBSERVABLE
LOAD — eGPU power, eGPU utilisation, CPU package temperature, and
rate-of-rise on both machines' thermals (anticipation was the only real
argument for watching a process, and a fast rise anticipates better than
an identity does). A recognisable engine appearing in /proc survives
only as a PRE-POSITION hint toward a middle rung, so air is moving
before the load lands — never a jump to maximum, and never load-bearing.
Do not reintroduce a process-name trigger.

CLOSED LOOP: THE DAEMON NEVER TRUSTS ITS OWN CURVE. FINDINGS s13 measured
the same idle request, 89, applying 115 under one curve shape and 179-180
under another — a 64-unit spread nobody predicted in either direction —
and a third shape applying 0 with the fan STOPPED. Requested duty does not
determine applied duty, so after applying, every tick READS BACK: the Ally
fans' real tachometers (fan1/fan2_input under the asus hwmon; rpm > 0 is
directly observable) and the dock's byte[9] applied PWM (the only signal
that fan has). A reading that says a fan is not obeying — 0 rpm, or
applied below that surface's floor duty — held for VERIFY_FAIL_TICKS
consecutive verifiable ticks escalates ITS OWN LADDER to the top rung on
that same tick, is logged LOUDLY, and blocks descent ON THAT LADDER
until the readings recover. A dead Ally tachometer is no evidence at all
about the dock fan, and vice versa: the escalation is scoped to the
surface whose reading failed. No attempt
is made to model or correct the requested->applied mapping: the mechanism
is unknown, so a correction factor would be a guess wearing a number's
clothing. Measure, verify, react.

THE TUNING LIVES IN A CONFIG FILE. Every threshold, duty, dwell and floor
below is a compiled-in DEFAULT; /etc/system-thermal-daemon.conf (or
$SYSTEM_THERMAL_CONF) may override them, and the shipped config carries
these same committed values with the operator-facing explanation beside
each. The file is VALIDATED on load and refused LOUDLY, as a whole, on
any bad value — no duty below the floor, monotonic curves, ordered and
spaced thresholds, positive dwells and windows — falling back to these
defaults: a daemon that must never stop a fan does not get to start on a
curve that could. The structural guards are NOT reachable from the
config: never_stops() and clamp_to_floor() stay exactly where they are,
the apply paths included, and install_config() re-proves them on the
BUILT ladder and refuses rather than installs, so a config can MOVE the
floor but cannot remove it. A missing file means defaults, logged; the
daemon never fails to start for want of a config.
"""
import configparser
import errno, fcntl, glob, grp, json, os, re, select, signal, socket, sys, time

# ---------------------------------------------------------------------------
# CURVES. (temp C, duty 0-255).
# ---------------------------------------------------------------------------

#: The cooling curve. 70% floor, 100% by 60 C. MEASURED — this is the curve
#: that has been running on this host since 2026-08-07 and is the reason it
#: has not powered off again.
MAX_CURVE = [(30, 178), (40, 178), (50, 204), (55, 230),
             (60, 255), (65, 255), (70, 255), (75, 255)]

#: THE FLOOR THAT NEVER STOPS: the lowest duty any point of any rung may
#: request, on the dock AND on the Ally fans, and the bar verify_tick
#: holds the dock's read-back byte[9] against every tick. The operator's
#: standing requirement, verbatim: "fan off is dangerous for prolonged
#: use on a device that stays online 24x7 - I'd rather err on the side
#: of keeping it cool."
#:
#: 45 is THE CLAMP THAT PERMITS THE CHOSEN FLOOR CURVE — it is not
#: itself the floor, and it is not a resting duty (FINDINGS s15). The
#: 2026-08-13 three-point floor sweep held QUIET_CURVE low points of
#: 89/99, 70/80 and 55/65 for ~10 minutes each, and the eGPU edge sat at
#: an IDENTICAL 35-36C at all three, with zero verification failures.
#: The operator chose 55/65. Under the previous FLOOR_DUTY of 89 (the
#: old bottom curve's own idle request, from the days the two numbers
#: coincided) never_stops() would have silently raised that choice back
#: to 89 — so the clamp moved BELOW the chosen curve: 45 sits under
#: every committed point (the curves' minimum is 55) and above the one
#: measured stopped state (a zeros-at-the-low-end curve applied 0; s13).
#: What the floor curve actually RUNS is measured, not predicted: dock
#: applied 131 (~51%), Ally 3900 rpm.
#:
#: Do not raise this above the floor curve's minimum point (never_stops
#: would silently reshape the bottom rung); do not derive a "better"
#: value from s15's affine fit (valid only over the tested band — at
#: the bottom it predicts applied 50 where a stopped fan was MEASURED).
#: Lowering it lowers the dock verify bar with it; applied 0, the
#: measured stopped state, stays below any legal value, so the
#: escalation survives, but the buffer shrinks. Re-measure, then edit.
#:
#: The floor is asserted STRUCTURALLY: never_stops() clamps every rung at
#: construction and both apply paths clamp again at the hardware boundary,
#: so no future rung, no edit to any curve constant, and no config file
#: (validated or not — install_config() re-proves the built ladder) can
#: reach zero. Editing FLOOR_DUTY itself to zero trips the assert below;
#: editing it at all trips the pinned test in
#: tests/test_thermal_daemon_ladder.py.
FLOOR_DUTY = 45
assert FLOOR_DUTY > 0, "the floor exists so no rung can ever stop a fan"

#: The floor curve — the BOTTOM of the ladder, and still a CUSTOM curve
#: that keeps all three fans TURNING. The dock is never handed back to the
#: firmware curve in normal operation (that curve powered this host off
#: once; DisableFanCurve is the STOP handoff in bye(), nothing else):
#: resting on our own curve keeps our governor in control at exactly the
#: moment nobody is watching the card.
#:
#: MEASURED, 2026-08-13, as the chosen point of a three-point floor
#: sweep (FINDINGS s15): low pairs 89/99, 70/80 and 55/65 were each
#: deployed and held ~10 minutes with the chassis exhaust running,
#: sampling the daemon's own status socket at 5 s. The eGPU edge sat at
#: 35-36C AT ALL THREE — the card does not need the idle airflow in
#: this configuration — so the operator chose the quietest: THIS curve
#: runs the dock at applied PWM 131 (~51%) with the Ally fans at
#: 3900 rpm (~60%), zero verification failures over the held run. Only
#: the 30C and 40C points were the sweep's variable; every later point
#: is unchanged from the previously measured floor shape, so the
#: protective tail keeps its measured-here bytes.
#:
#: VALIDATED AS A RESTING FLOOR IN THE NORMAL CONFIGURATION ONLY: every
#: sweep point ran with the supplementary chassis exhaust on. Without
#: the exhaust this is a starting state, not a resting one — an idle
#: card on too little airflow drifts (+2.4C/min was measured on ZERO
#: cooling, s11) and the ladder's total ascent is what protects it:
#: designed and observed, but not measured at this floor.
#:
#: What replaced the old zeros: the previous bottom rung asked 0/20 at the
#: low points (legal per s10's acceptance floors) and the dock measured
#: applied 0 — ALL FANS STOPPED, card drifting 35 -> 41 C on no cooling,
#: bound to warm out of the rung's own 45 C ceiling. Self-terminating rest
#: is not rest. See FLOOR_DUTY for the requirement and the measurement.
#:
#: What is NOT an assumption: this curve is strictly safer than the factory
#: one under load. Factory capped cpu_fan at 66/255 and never rose; this
#: idles at a 55/255 (~22%) request and reaches 255 by 65 C. The failure
#: mode of the factory curve was never that it was slow to start — it was
#: that it never finished.
QUIET_CURVE = [(30, 55), (40, 65), (50, 147), (55, 185),
               (60, 228), (65, 255), (70, 255), (75, 255)]

# Status labels. Not a state variable any more — the state is ctx['rung']
# plus ctx['pinned'], and the label is derived (mode_label). 'max' and
# 'quiet' keep their exact old meanings at the ladder's endpoints so that
# every existing consumer (core/lifecycle/sequences.py greps mode=="max";
# the journal greps mode=quiet) reads on unchanged.
MODE_MAX, MODE_QUIET, MODE_STEPPING = 'max', 'quiet', 'stepping'

REASSERT = 10.0
EGPU_PCI = "0000:03:00.0"

# Filesystem roots, named rather than inlined so the offline tests can point
# the interlock at a synthetic tree. Nothing in this daemon resolves an hwmon
# BY INDEX: the eGPU is reached through its PCI path and the Ally fans through
# the hwmon `name` file, because hwmon8 is the eGPU and hwmon9 the iGPU on the
# same driver name TODAY and an index-pinned reader eventually drives the
# wrong device.
PROC_ROOT = "/proc"
PCI_ROOT = "/sys/bus/pci/devices"
HWMON_ROOT = "/sys/class/hwmon"

# ---------------------------------------------------------------------------
# THE INTERLOCK. Every one of these is DERIVED, not measured — validate on
# hardware before trusting the numbers rather than the shape.
# ---------------------------------------------------------------------------

#: Edge ceiling for quiet. EDGE, not junction: junction was measured falling
#: 84 -> 66 C in 0.5 s, so gating on it means gating on a number that is true
#: for a fraction of a second. Junction is an emergency ceiling only.
QUIET_EDGE_C = 45.0

#: Junction ceiling for quiet — a second, looser gate, so a hotspot excursion
#: that edge has not caught up with still blocks. It is also the DOCK
#: LADDER's emergency ceiling (SURFACE_SIGNALS[DOCK]['ceiling']): one
#: number, because "the hotspot is above 55C" is the same fact whether it
#: is vacating a rung or refusing a verb, and two copies would drift.
QUIET_JUNCTION_C = 55.0

#: The ALLY surface's quiet ceiling, in CPU Tctl — the same permission
#: bound QUIET_EDGE_C is for the dock, against the other machine's
#: sensor. Since the split, "is it cool enough to be passive?" is two
#: questions, and this is the Ally's half.
#:
#: DERIVED from the measured Tctl anchors (idle 32.6C read live
#: 2026-08-13; light load mid-50s; 95.0C under cmake -j12 and CPU
#: llama-bench, s14): 60 sits above the light-load band, so ordinary
#: desk work does not make the grant impossible, and well under
#: CPU_LOAD_C (80) so a grant can never be issued on an APU that is
#: already doing heavy work. Like every quiet bound it is a PERMISSION
#: bound, not a resting point — the Ally ladder's own bottom threshold
#: (ALLY_LADDER_C) decides where the fans actually rest, and a grant
#: only releases the operator's pin.
QUIET_TCTL_C = 60.0

#: How long all three interlock conditions must hold CONTINUOUSLY. A
#: hold-down and not a timer: the 86 -> 53 C in 16 s recovery this host can
#: manage was measured WITH THE FANS RUNNING, i.e. it is only true while the
#: curve being dropped is still being held. Anything shorter than a couple of
#: re-assert ticks is measuring one sample and calling it a trend.
QUIET_HOLD_S = 30.0

#: How long a socket-requested `mode max` waits for significant load to
#: appear before releasing the operator pin. Ten minutes: a 14B warm-up is
#: minutes, and a cold page cache makes it several — and the warm-up is
#: exactly the window where the engine HINT has fired but power and
#: utilisation have not yet.
SPINUP_WATCHDOG_S = 600.0

# ---------------------------------------------------------------------------
# THE LOAD SIGNALS. What forces maximum, in place of the retired
# comm == "llama-server" trigger — that name was a fossil from this
# daemon's eGPU-inference-guard days, and on 2026-08-12 it was wrong in
# both directions in one day: blind to a cmake -j12 and to a CPU
# llama-bench that drove Tctl to 95.0C (no process by that name), while a
# resident idle 14B at 10 W held every fan at 100%. Load is what the box
# is DOING, not what a process is CALLED. Thresholds are DERIVED from the
# measured endpoints noted beside each — validate on hardware.
# ---------------------------------------------------------------------------

#: eGPU power that counts as work. MEASURED endpoints, repeatedly, today:
#: a resident-but-idle 14B draws 10 W; generation draws 165 W. 45 sits
#: more than 4x above one and more than 3x below the other, so it
#: separates the operator's false positive (resident model, fans pinned)
#: from real work with a wide margin on both sides. DERIVED.
LOAD_POWER_W = 45.0

#: eGPU utilisation that counts as work — work regardless of WHICH binary
#: submitted it: a renamed server, a compute job, anything. Resident-idle
#: reads 0; generation reads ~100. DERIVED.
LOAD_BUSY_PCT = 50

#: CPU package temperature (k10temp Tctl) that counts as heavy work — the
#: signal that catches builds, test suites and CPU inference, everything a
#: GPU-shaped trigger misses. Measured 2026-08-12: cmake --build -j12 and
#: `llama-bench --device none -ngl 0` drove Tctl to 95.0C, the hottest
#: reading of the day; light load sits mid-50s. 80 is well above the light
#: band and safely under that measured peak. The Ally EC still evaluates
#: every rung's protective tail against its own sensors, so this gates
#: ANTICIPATION, not survival. DERIVED.
CPU_LOAD_C = 80.0

#: The rate-of-rise trip: a climb of RISE_TRIP_C across RISE_WINDOW_S on
#: eGPU edge or CPU Tctl forces maximum BEFORE any absolute threshold is
#: crossed. This is what makes retiring the name check safe: the reason to
#: look at a process at all was ANTICIPATION, and a fast rise anticipates
#: better than an identity does — it fires on anything that heats the box
#: and never on something merely resident. 3C per 30s = 6C/min: 2.5x the
#: worst measured idle drift (+2.4C/min on ZERO cooling, FINDINGS s11), so
#: settling into a lower rung cannot trip it; ~1/10 of measured load onset
#: (~1.1C/s edge, s9), so real load trips it within 2-3 ticks; and 3x the
#: edge channel's 1C reporting granularity, so flicker cannot sum to it.
#: Junction NEVER rate-triggers: it moves 30C in half a second at steady
#: state (s4). DERIVED from the measured slopes.
RISE_TRIP_C = 3.0
RISE_WINDOW_S = 30.0

#: Comm PREFIXES that read as an inference engine — A HINT, NOTHING MORE.
#: A fresh match pre-positions the ladder at PREPOSITION_RUNG so the fans
#: are already moving air when the load lands; it never jumps to maximum,
#: never blocks descent, and never gates quiet. Its correctness is NOT
#: load-bearing — the load signals above decide everything that matters —
#: so it is deliberately loose where the old check was exact: exact-comm
#: missed llama-bench and would miss any renamed or alternative server.
#: Matched as prefixes against /proc/<pid>/comm (15-char truncation makes
#: prefixes the right shape; a python-hosted engine's comm is "python3"
#: and is invisible here, which costs a few spin-up seconds, never
#: safety).
ENGINE_COMM_HINTS = ('llama', 'ollama')

#: Where a fresh engine hint pre-positions the ladder: the middle rung —
#: meaningful air on every surface without maximum's noise, because a
#: hint is a guess about the future, not evidence of heat, and a guess
#: must not cost what evidence costs. DERIVED.
PREPOSITION_RUNG = 2

# ---------------------------------------------------------------------------
# THE ONE COUPLING THAT SURVIVES THE SPLIT. The 2026-08-13 split severed
# every path by which one surface's evidence could move the other's fans,
# with a single deliberate exception in the other direction: AN INFERENCE
# ENGINE LOADING THE eGPU ALSO HEATS THE APU THAT FEEDS IT. The host
# process doing the feeding — PCIe transfers, sampling, tokenisation —
# runs on the Z1 Extreme, and the 165 W the card draws while generating
# (measured, repeatedly) is dissipated into a chassis the Ally shares.
# If the dock ladder is at maximum for sustained eGPU load, the Ally
# should not be resting at its floor.
#
# WHAT THIS IS BUILT ON, stated plainly because it is thin: the physical
# argument above and nothing else. Tctl UNDER eGPU INFERENCE HAS NEVER
# BEEN MEASURED on this box — every measured Tctl number on record comes
# from CPU-side work (95.0C under cmake -j12 and a CPU llama-bench, s14)
# or idle (32.6C, read live 2026-08-13). The live 14B run is the first
# time this coupling will be observed at all.
#
# SO IT IS EXPRESSED AS THE WEAKEST MECHANISM THAT DOES ANYTHING: a
# FLOOR, not an escalation. The Ally may not rest below COUPLE_FLOOR_RUNG
# while the coupling holds, and if it is already below it steps UP to it
# — one middle rung of air, on a guess about the near future, which is
# exactly what the engine hint costs and for a better reason. A full
# escalation was rejected outright: "the dock is at maximum" is not
# evidence that the APU is hot, and treating it as evidence rebuilds the
# coupling this brief removes with the arrow reversed. If the APU really
# does heat, the Ally's OWN signals — the rate trip, CPU_LOAD_C, its
# rung ceilings — take it to maximum on their own evidence, and they
# remain the load-bearing path.
#
# The cost of being wrong is bounded and one-directional: the Ally pair
# runs at rung 2 (idle ask 99, measured 4800 rpm) instead of rung 4
# (3900 rpm) while a model generates. The cost of the opposite mistake is
# a hot APU cooled at the floor because its fans were told to ignore the
# card bolted to it.
# ---------------------------------------------------------------------------

#: The rung the Ally ladder may not descend below while the coupling
#: holds — the same middle rung the engine hint pre-positions to, for the
#: same reason: it is the most that may be spent on an expectation rather
#: than a reading. DERIVED.
COUPLE_FLOOR_RUNG = PREPOSITION_RUNG

#: How long the dock must have been at its top rung UNDER LOAD before the
#: Ally's floor rises — "sustained", made a number. Six re-assert ticks:
#: long enough that a single spike or one tick of a warm-up cannot raise
#: the Ally's floor, short enough that it is in place well inside a
#: generation run (a 14B's warm-up alone is minutes). The same
#: MIN_STEP_DWELL_S spacing the ladder already thinks in. DERIVED.
COUPLE_HOLD_S = 60.0

# ---------------------------------------------------------------------------
# THE LADDER. Descent is gated per rung; ascent is free. Every constant in
# this block is DERIVED unless it says measured — validate the numbers on
# hardware before trusting them; the SHAPE is the design.
# ---------------------------------------------------------------------------

#: The dock's own per-point duty ACCEPTANCE floors, one byte per curve
#: point — the lowest values SetFanCurve may carry, NOT what the
#: controller then runs (those are different stages; FINDINGS s13).
#: MEASURED 2026-08-08 with bin/xgm-fan-probe.py (GetFanCurveLowerLimit,
#: reply `5e d0 04 | 00 00 00 00 22 36 3e 57`) and RECORDED here — read
#: from FINDINGS §10, not from the device at startup, because the daemon
#: must build its ladder before a dock is necessarily attached. The first
#: four points have no acceptance floor; the upper four floors are
#: protective and rise with temperature. A curve that asks below a floor
#: is silently clamped by the firmware, so every rung is clamped HERE
#: first — the duty the daemon logs must be a duty the hardware is
#: honouring. (FLOOR_DUTY sits above every one of these, so in practice
#: the operator's floor is the binding one; this table still guards a
#: device that reports HIGHER floors at attach.)
DOCK_DUTY_FLOOR = (0, 0, 0, 0, 34, 54, 62, 87)

# HOW MANY RUNGS, counting both ends: five on each ladder today, and the
# count is not a constant — it FOLLOWS each surface's own threshold list
# (surf['rungs'] = len(ladder_c)), so the two need not agree and a config
# that lists three thresholds gets a four-rung ladder on that surface.
#: Five: the idle-point request walks
#: 178 -> 133 -> 99 -> 74 -> 55 (see each surface's `fractions`) —
#: GEOMETRIC from the floor curve's idle point, ratio (178/55)^(1/4)
#: ~ 1.34, so the steps are ~45/34/25/19: FINER NEAR THE FLOOR. Ascent is total (the
#: rungs gate only the descent), so rung spacing is purely a question of
#: descent resolution, and resolution belongs where the card is settling:
#: noise perception is roughly logarithmic and cooling capacity goes
#: roughly as airflow^0.8, so the old equal steps were unequal
#: acoustically at the bottom and near-redundant thermally at the top.
#: A full descent is still four gated steps, ~5 minutes at
#: MIN_STEP_DWELL_S spacing. Only the endpoints sit on measured curves;
#: EVERY intermediate rung is a new shape whose applied duty is UNKNOWN
#: until the operator deploys and reads byte[9] (FINDINGS s13 — the same
#: request has measured 115 and 179-180 applied under different shapes).
#: verify_tick is the net under this arithmetic, not the arithmetic
#: itself. DERIVED.
#:
#: The top rung, on every ladder. Index 0 is maximum cooling — the one
#: rung index that means the same thing on both surfaces, which is why it
#: is shared and every other rung bound is per surface (surf['bottom']).
RUNG_TOP = 0

#: THE DOCK LADDER'S entry thresholds, in degrees of eGPU EDGE.
#: Edge temperature a rung demands, indexed by rung; a rung may be STEPPED
#: INTO (descending) only when edge has held below its threshold (less
#: STEP_HYSTERESIS_C — see descent_gate), and is vacated STRAIGHT TO THE
#: TOP the moment edge exceeds it — the rungs gate the descent, never the
#: climb. Rung 0 is unconditional, which is what None means.
#:
#: SPACED 5C ACROSS THE MEASURED OPERATING RANGE. The old 3C spacing
#: under the 2C hysteresis left a 1C usable band per rung — tight enough
#: to dither against a sensor that reports in 1C steps. Measured
#: 2026-08-12: idle sits at 35-39C with the chassis exhaust on (35C is
#: the full-cooling equilibrium), and the workload guard pauses work at
#: edge 58 (core/thermal.py) — roughly 19C of range for four thresholds.
#: 41 puts the bottom threshold just above the idle band (2C over its
#: top, 6C over equilibrium), so the card genuinely RESTS at the floor
#: rather than hovering on a boundary; 56 keeps the top threshold under
#: the guard's 58C pause, so no rung below maximum is tenable on a card
#: a workload guard is actively duty-cycling. Usable band per rung: 3C.
#:
#: The bottom is deliberately DECOUPLED from QUIET_EDGE_C (45), which it
#: used to alias: where the card may REST tracks the measured idle band,
#: while where passivity may be REQUESTED (the interlock's grant
#: ceiling) is a permission bound and did not move in this change. A
#: quiet grant at edge 44 releases the pin and the ladder settles
#: wherever its gates allow — exactly what the verb has meant since the
#: ladder existed. DERIVED from the measured range; what each rung's
#: curve APPLIES is unknown until read back (FINDINGS s13).
DOCK_LADDER_C = (None, 56.0, 51.0, 46.0, 41.0)

#: THE ALLY LADDER'S entry thresholds, in degrees of CPU package Tctl —
#: the same rule (enter below threshold-minus-hysteresis, vacate above
#: threshold, straight to the top) against a different sensor on a
#: different machine. It is NOT a re-scaling of the dock's numbers: the
#: two channels share no band and no dynamics.
#:
#: MEASURED ANCHORS on this box: idle Tctl 32.6C (read live 2026-08-13
#: at load average 0.35, eGPU idle at 10 W); light load sits mid-50s;
#: `cmake --build -j12` and `llama-bench --device none -ngl 0` reached
#: 95.0C (FINDINGS s14). CPU_LOAD_C (80) forces maximum by itself, so
#: the whole ladder lives below it.
#:
#: SPACED 10C, not the dock's 5C, because Tctl is a far coarser signal
#: than eGPU edge: the event that motivated the split moved it +12.6C in
#: 20 s, and it swings several degrees at idle from background work,
#: where edge reports in flat 1C steps. Wide bands (8C usable after the
#: 2C hysteresis) are what keeps a spiky channel from walking the ladder
#: for nothing. 45 sits 12C above the measured idle so a genuinely idle
#: APU RESTS at the floor rung; 75 sits under CPU_LOAD_C so there is a
#: rung between "warm" and "maximum" rather than a cliff.
#:
#: DERIVED, all four, from those anchors — this ladder has never been
#: walked against a real sustained CPU load, and the live 14B run is the
#: first chance to check the resting point against reality. What each
#: rung APPLIES is as unknown here as on the dock (FINDINGS s13); the
#: tachometers are the net.
ALLY_LADDER_C = (None, 75.0, 65.0, 55.0, 45.0)

#: Entry margin under a rung's threshold, so entry and exit are not the
#: same level, on either ladder. Enter rung r at that surface's
#: ladder_c[r] - 2; leave it above ladder_c[r]. A reading sitting
#: exactly ON a threshold therefore
#: neither enters nor leaves, and a reading dithering across one cannot
#: qualify for entry at all. 2C: wider than the 1C granularity the edge
#: channel reports in, by one more degree. DERIVED.
STEP_HYSTERESIS_C = 2.0

#: The stability dwell window for one step down, and the test applied to
#: it — see stability_ok, which is the docstring of record. ~3 re-assert
#: ticks: enough samples to tell a trend from a sample, short enough that
#: a genuine cool-down is not made to queue behind bureaucracy. DERIVED.
STABILITY_WINDOW_S = 30.0

#: A window is credited only if its oldest sample is at least
#: STABILITY_WINDOW_S - STABILITY_SLACK_S old and it holds at least
#: STABILITY_MIN_SAMPLES. Slack of one re-assert tick, so a single late
#: tick does not void the window. DERIVED.
STABILITY_SLACK_S = REASSERT
STABILITY_MIN_SAMPLES = 3

#: Net rise tolerated across the window (newest minus oldest) before the
#: reading counts as trending up. 0.5C: below the sensor's 1C reporting
#: granularity, so any real movement blocks and a flat signal passes.
#: Blocking is cheap — the window slides and requalifies a tick later.
#: DERIVED.
STABILITY_RISE_C = 0.5

#: Minimum spacing between steps, in EITHER direction, before another step
#: DOWN may be taken (steps up never wait). Six re-assert ticks: long
#: enough to see the response to the previous step before committing the
#: next, short enough that a full four-step descent completes in ~5
#: minutes. DERIVED.
MIN_STEP_DWELL_S = 60.0

#: A step down that gets thrown back (edge rose past the threshold of the
#: rung just entered) doubles the dwell before the next attempt, capped.
#: At the cap a card that keeps warming is asked once per
#: MIN_STEP_DWELL_S * BACKOFF_MAX_MULT = 480s — rare enough to stop
#: hunting, frequent enough to notice the room's airflow coming back
#: (the chassis exhaust fan this daemon cannot see). Capping is LOGGED.
#: DERIVED, both.
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_MULT = 8.0

#: How many CONSECUTIVE verifiable ticks a fan must read as not obeying
#: (0 rpm on a real tachometer, or dock applied PWM below FLOOR_DUTY)
#: before the daemon escalates. One bad sample must never cause a step:
#: a fan spinning up from rest reads 0 rpm briefly (well inside one 10 s
#: tick — the measured 46->35C recovery implies fans at speed within
#: seconds, so even a boot-time cold start never legitimately shows two
#: zeros in a row), and the dock's telemetry is known intermittent. Three:
#: one for the transient, one for a flaky sample, and the same "a sample
#: is not a trend" count STABILITY_MIN_SAMPLES uses. The cost of waiting
#: is bounded and small — 30 s of a genuinely stopped fan at idle is
#: ~+1.2C (the measured +2.4C/min zero-cooling drift, FINDINGS s11) —
#: while a spurious escalation costs a full-ladder walk of noise. DERIVED.
VERIFY_FAIL_TICKS = 3


def clamp_to_floor(curve, floors=DOCK_DUTY_FLOOR):
    """The curve, with no point's duty below the firmware's floor for that
    point. The floors are the DEVICE's (FINDINGS §10): asking below one is
    accepted and ignored, which turns the daemon's own log into fiction.
    Applied when the ladder is built, so what is asserted, logged and
    reported in `status` is the curve the hardware can actually run.

    The Ally side (asus_custom_fan_curve) has no published floor table and
    has never been probed for one — clamping both fans to the dock's
    floors is the recorded knowledge applied in the safe direction."""
    return [(t, max(d, f)) for (t, d), f in zip(curve, floors)]


def never_stops(curve, floor=None):
    """The curve, with no point's duty below FLOOR_DUTY — the structural
    guarantee that no rung, present or future, can stop a fan.

    Distinct from clamp_to_floor: that one honours the DEVICE's acceptance
    floors so the log matches the hardware; this one honours the
    OPERATOR's requirement that a host online 24x7 is never left with zero
    cooling. It is applied when the ladder is built AND again inside both
    apply paths at the hardware boundary, so an edited curve constant, a
    future rung, or a caller that bypassed the ladder entirely still
    cannot ask a fan to stop. The only code that may stop these fans is
    bye()'s handoff to the firmware curve at exit, when a dead process
    could no longer re-assert anything anyway.

    ``floor`` defaults to the RUNNING FLOOR_DUTY, read at call time so
    the apply paths always clamp at whatever floor is installed;
    _build_ladder passes a candidate floor explicitly so a proposed
    config can be built and proved before it is installed."""
    floor = FLOOR_DUTY if floor is None else floor
    return [(t, max(d, floor)) for (t, d) in curve]


def _blend(top, bottom, fraction):
    """Point-wise linear blend of two curves' duties, temps from `top`.
    Blending two monotonic curves is monotonic, so every rung inherits the
    shape property the endpoint tests pin."""
    return [(t, round(d_top + (d_bot - d_top) * fraction))
            for (t, d_top), (_, d_bot) in zip(top, bottom)]


def _ladder_fractions(max_curve, quiet_curve, rungs):
    """Blend fraction per rung, 0.0 (pure max) to 1.0 (pure floor curve),
    chosen so the IDLE-POINT REQUEST walks GEOMETRICALLY from the top
    idle to the floor curve's idle:
        idle_r = TOP_IDLE * (BOTTOM_IDLE / TOP_IDLE)^(r/(rungs-1)),
    solved for the blend fraction. The base is the floor CURVE's idle
    point, not FLOOR_DUTY — since the 2026-08-13 floor the clamp sits
    BELOW the curve it permits (45 under an idle ask of 55), and the
    walk must land on the curve the ladder actually rests on. The two
    coincided at 89 before that, which is why the old formula read
    FLOOR_DUTY. Endpoints land at exactly 0.0 and 1.0, so both measured
    curves stay byte-for-byte. DERIVED."""
    top, bottom = max_curve[0][1], quiet_curve[0][1]
    if top == bottom:                       # degenerate but legal: linear
        return tuple(r / (rungs - 1) for r in range(rungs))
    return tuple(
        (top - top * (bottom / top) ** (r / (rungs - 1))) / (top - bottom)
        for r in range(rungs))


def _build_ladder(max_curve, quiet_curve, fractions, floor_duty):
    """The rungs, top (0, the measured max curve, byte-for-byte) to
    bottom (the measured floor curve, byte-for-byte), intermediates
    blended at ``fractions`` and every rung passed through BOTH floors:
    never_stops (the operator's — no fan may stop) and clamp_to_floor
    (the device's — the log must match the hardware). Pure, so the
    config path can build a CANDIDATE ladder and prove it before
    anything is installed."""
    return [clamp_to_floor(never_stops(_blend(max_curve, quiet_curve, f),
                                       floor_duty))
            for f in fractions]


def _assert_ladder(ladder, max_curve, quiet_curve, edge_c, floor_duty):
    """The structural invariants, proved on a BUILT ladder — at import
    on the compiled-in defaults, and again on every candidate a config
    file proposes, so no path can install a ladder these do not hold
    over. Raises AssertionError explicitly (not via ``assert``, which
    ``python -O`` strips — a structural guarantee must not depend on an
    interpreter flag): at import that is a crash-before-touching-
    hardware; from install_config it is a refusal, and the defaults
    stay."""
    checks = (
        (floor_duty > 0,
         "the floor exists so no rung can ever stop a fan"),
        (ladder[RUNG_TOP] == list(max_curve),
         "rung 0 must BE the measured max curve"),
        (ladder[len(ladder) - 1] == list(quiet_curve),
         "the bottom rung must BE the measured floor curve, byte for byte"),
        (all(d >= floor_duty for c in ladder for _, d in c),
         "no rung may ask any fan for a duty below the floor that keeps "
         "it turning"),
        (len(edge_c) == len(ladder),
         "every rung needs its entry threshold (rung 0's is None)"),
    )
    for ok, message in checks:
        if not ok:
            raise AssertionError(message)


# ---------------------------------------------------------------------------
# THE TWO SURFACES. Everything above this line is machinery or a number;
# THIS is where a number is bound to a sensor and a fan, and it is the
# only place in the file that knows which is which.
#
# ALLY is the pair pwm1+pwm2 on asus_custom_fan_curve. They are TWO FANS
# COOLING ONE PACKAGE — the Z1 Extreme, CPU and GPU on the same die — so
# they are one thermal zone, driven by one signal, and are never split
# from each other. That is a property of the silicon, not a configuration
# choice: there is no arrangement of this hardware in which cooling the
# Ally's GPU without its CPU means anything.
#
# DOCK is the XG Mobile fan over HID 0x5E, cooling a different card in a
# different enclosure with its own airflow. It is the only genuinely
# independent cooling surface on this desk, which is why the split is
# two ladders and not three.
# ---------------------------------------------------------------------------
ALLY, DOCK = 'ally', 'dock'
SURFACE_KEYS = (ALLY, DOCK)

#: What each surface WATCHES and what it BLOWS ON — the identity of the
#: signals, as opposed to their thresholds (which are tunable, below).
#: Read this table to answer "what can move this fan?"; nothing else in
#: the file gets to have an opinion about that.
#:
#: `temp_key` is the surface's CONTROL temperature: the one the rungs are
#: thresholded against, the one the stability window and the rate trigger
#: read, and the one whose UNREADABILITY escalates — that surface and no
#: other. `load` lists the observable-work signals that force this
#: surface's maximum. `verify` lists what is read back to prove this
#: surface's fans are obeying.
SURFACE_SIGNALS = {
    ALLY: {
        'label': 'Ally cpu_fan+gpu_fan (one package: Z1 Extreme)',
        'temp_key': 'cpu_c',
        'temp_name': 'cpu Tctl',
        'load': (('cpu_c', 'CPU_LOAD_C',
                  lambda v, th: (f'CPU Tctl {v:.1f}C at or above the '
                                 f'{th:.0f}C heavy-work threshold')),),
        # No second ceiling: the APU publishes one package temperature and
        # this ladder is thresholded against it. The dock's junction gate
        # exists because the eGPU publishes a hotspot channel that moves
        # faster than its control channel; nothing on the Ally does.
        'ceiling': None,
        'verify': (('cpu_fan_rpm', 'rpm', 'cpu_fan'),
                   ('gpu_fan_rpm', 'rpm', 'gpu_fan')),
    },
    DOCK: {
        'label': 'XG Mobile dock fan (eGPU enclosure)',
        'temp_key': 'edge_c',
        'temp_name': 'edge',
        'load': (('power_w', 'LOAD_POWER_W',
                  lambda v, th: (f'eGPU drawing {v:.0f}W (work threshold '
                                 f'{th:.0f}W)')),
                 ('busy_pct', 'LOAD_BUSY_PCT',
                  lambda v, th: f'eGPU {v}% busy (work threshold {th}%)')),
        'ceiling': ('junction_c', 'junction'),
        'verify': (('dock_pwm', 'applied', 'dock'),),
    },
}

#: The curves are deliberately IDENTICAL on the two surfaces: the split
#: changed which SIGNALS walk each ladder, not what a rung asks for. The
#: 2026-08-13 measurements (the floor sweep and the full ladder walk)
#: were taken with one curve driving both, so keeping the numbers
#: byte-for-byte is what keeps those measurements applicable — see the
#: config file for which measured figure still attaches to which surface
#: now that they move independently.
def _build_surface(key, v):
    """One ladder instance: the shared machinery bound to ONE surface's
    signals, curves and thresholds. Pure — the config path builds a
    CANDIDATE surface and proves it before anything is installed.

    Everything a governing decision needs about a surface is in the dict
    this returns, which is why every gate below takes ``surf`` and none
    of them reads a global: two instances, one code path."""
    sig = SURFACE_SIGNALS[key]
    edges = tuple(v['LADDER_C'])
    rungs = len(edges)
    fractions = _ladder_fractions(v['MAX_CURVE'], v['QUIET_CURVE'], rungs)
    ladder = _build_ladder(v['MAX_CURVE'], v['QUIET_CURVE'], fractions,
                           v['FLOOR_DUTY'])
    return {
        'key': key,
        'label': sig['label'],
        'temp_key': sig['temp_key'],
        'temp_name': sig['temp_name'],
        'load': tuple((fact, v[tune], fmt) for fact, tune, fmt in sig['load']),
        'ceiling': (None if sig['ceiling'] is None else
                    (sig['ceiling'][0], v['CEILING_C'], sig['ceiling'][1])),
        'verify': sig['verify'],
        'quiet_c': v['QUIET_C'],
        'rise_trip_c': v['RISE_TRIP_C'],
        'rise_window_s': v['RISE_WINDOW_S'],
        'max_curve': v['MAX_CURVE'],
        'quiet_curve': v['QUIET_CURVE'],
        'floor_duty': v['FLOOR_DUTY'],
        'ladder_c': edges,
        'ladder': ladder,
        'fractions': fractions,
        'rungs': rungs,
        'bottom': rungs - 1,
    }


def _assert_surface(surf):
    """The structural invariants, per surface — _assert_ladder plus the
    ladder/threshold agreement, so a candidate surface cannot be
    installed on a ladder these do not hold over."""
    _assert_ladder(surf['ladder'], surf['max_curve'], surf['quiet_curve'],
                   surf['ladder_c'], surf['floor_duty'])


#: THE TWO LADDERS, built once at import from the compiled-in defaults
#: above and rebuilt by install_config() when a validated config file
#: replaces them (a [ally]/[dock] section IS a surface's tuning). The
#: tests pin the endpoints, the monotonicity, the geometric idle walk,
#: and both floors, over every rung either ladder will ever hold.
#:
#: This dict IS the daemon's tuning at runtime: every gate below reads
#: its numbers from the surface it was handed, never from a global, so
#: there is exactly one way for a surface to be tuned and no way for one
#: surface's tuning to reach the other.
SURFACES = {}

# ---------------------------------------------------------------------------
# THE CONFIG FILE. Every tunable above is a compiled-in DEFAULT this block
# can replace at startup — never weaken: validation refuses any file whose
# values could stop a fan or make the ladder dither, the WHOLE file falls
# back to the defaults on any problem (a config is a coherent tuning, not
# a grab bag), and install_config() re-proves the structural floor on the
# ladder it built before a single global moves. What is deliberately NOT
# configurable: the re-assert cadence (REASSERT — the dock curve is
# volatile, FINDINGS s3, and the verify/stability arithmetic is counted
# in its ticks), the dock acceptance floors (DOCK_DUTY_FLOOR — a recorded
# DEVICE property, re-learned live at every attach and merged upward),
# the socket path/group/verbs (deployed names, load-bearing), and the
# structural guards themselves.
# ---------------------------------------------------------------------------

#: Where the config lives, overridable for tests and odd deployments via
#: the environment. The name follows the daemon, not the host: the same
#: file works under glados-thermal and ally-thermal alike.
CONFIG_PATH = "/etc/system-thermal-daemon.conf"
CONFIG_ENV = "SYSTEM_THERMAL_CONF"


def _parse_curve(text):
    """'30:55 40:65 ...' -> [(30, 55), (40, 65), ...] — the same
    (temp C, duty 0-255) pairs the module constants carry."""
    pts = []
    for tok in text.split():
        t, _, d = tok.partition(':')
        pts.append((int(t), int(d)))
    if not pts:
        raise ValueError('empty curve')
    return pts


def _parse_thresholds(text):
    """'56 51 46 41' -> (None, 56.0, 51.0, 46.0, 41.0): the file lists
    only the REAL entry thresholds, warmest first; rung 0 has none by
    design (the top is unconditional), so the None is prepended here
    rather than asked of the operator. The rung count follows from how
    many thresholds are listed."""
    vals = tuple(float(x) for x in text.split())
    if not vals:
        raise ValueError('no thresholds')
    return (None,) + vals


def _parse_hints(text):
    """Whitespace-separated comm prefixes; empty is legal (no hint)."""
    return tuple(text.split())


#: THE SHARED MACHINERY: (global name, section, key, parser) for every
#: value that is NOT a signal — the gates both ladders walk the same way.
#: One copy, because "share everything that is not a signal" is the whole
#: design: a hysteresis that differed per surface would be two descent
#: policies wearing one name.
_SHARED_SPEC = (
    ('STEP_HYSTERESIS_C',     'ladder',    'step_hysteresis_c',     float),
    ('STABILITY_WINDOW_S',    'ladder',    'stability_window_s',    float),
    ('STABILITY_MIN_SAMPLES', 'ladder',    'stability_min_samples', int),
    ('STABILITY_RISE_C',      'ladder',    'stability_rise_c',      float),
    ('MIN_STEP_DWELL_S',      'ladder',    'min_step_dwell_s',      float),
    ('BACKOFF_FACTOR',        'ladder',    'backoff_factor',        float),
    ('BACKOFF_MAX_MULT',      'ladder',    'backoff_max_mult',      float),
    ('VERIFY_FAIL_TICKS',     'ladder',    'verify_fail_ticks',     int),
    ('PREPOSITION_RUNG',      'ladder',    'preposition_rung',      int),
    ('QUIET_HOLD_S',          'interlock', 'quiet_hold_s',          float),
    ('SPINUP_WATCHDOG_S',     'interlock', 'spinup_watchdog_s',     float),
    ('COUPLE_FLOOR_RUNG',     'coupling',  'couple_floor_rung',     int),
    ('COUPLE_HOLD_S',         'coupling',  'couple_hold_s',         float),
    ('ENGINE_COMM_HINTS',     'load',      'engine_comm_hints',     _parse_hints),
)

#: THE PER-SURFACE TUNING: (tuning name, key, parser), in a section named
#: for the surface — [ally] and [dock]. These are exactly the values a
#: ladder's SIGNALS need, and the names match _build_surface's, so a
#: section IS a surface's tuning with no translation step in between.
#:
#: A surface the file omits entirely keeps its compiled-in defaults; that
#: is a merge, not a failure. Editing [dock] cannot reach the Ally's fans
#: and editing [ally] cannot reach the dock's — which is the config-file
#: half of the same guarantee the governor makes.
_SURFACE_SPEC = {
    ALLY: (('MAX_CURVE',     'max_curve',     _parse_curve),
           ('QUIET_CURVE',   'quiet_curve',   _parse_curve),
           ('FLOOR_DUTY',    'floor_duty',    int),
           ('LADDER_C',      'ladder_c',      _parse_thresholds),
           ('QUIET_C',       'quiet_c',       float),
           ('RISE_TRIP_C',   'rise_trip_c',   float),
           ('RISE_WINDOW_S', 'rise_window_s', float),
           ('CPU_LOAD_C',    'cpu_load_c',    float)),
    DOCK: (('MAX_CURVE',     'max_curve',     _parse_curve),
           ('QUIET_CURVE',   'quiet_curve',   _parse_curve),
           ('FLOOR_DUTY',    'floor_duty',    int),
           ('LADDER_C',      'ladder_c',      _parse_thresholds),
           ('QUIET_C',       'quiet_c',       float),
           ('RISE_TRIP_C',   'rise_trip_c',   float),
           ('RISE_WINDOW_S', 'rise_window_s', float),
           ('CEILING_C',     'junction_c',    float),
           ('LOAD_POWER_W',  'load_power_w',  float),
           ('LOAD_BUSY_PCT', 'load_busy_pct', int)),
}

#: The compiled-in defaults — the committed, measured values above,
#: snapshotted before any config can touch them, in the same shape a
#: parsed file takes: one 'shared' block and one block per surface. This
#: is what a missing, unreadable or refused config runs, and what the
#: shipped default config file states (a pinned test holds the two
#: identical).
#:
#: Both surfaces carry the SAME curves and the SAME floor, and that is
#: not an accident of the split: the 2026-08-13 sweep and ladder walk
#: were measured with one curve driving both, so shipping them identical
#: is what keeps those measurements applicable to each. What has changed
#: is which measured figure attaches to which surface — see the config
#: file, which says it value by value.
_DEFAULTS = {
    'shared': {name: (list(globals()[name])
                      if isinstance(globals()[name], list)
                      else globals()[name])
               for name, _, _, _ in _SHARED_SPEC},
    ALLY: {'MAX_CURVE': list(MAX_CURVE), 'QUIET_CURVE': list(QUIET_CURVE),
           'FLOOR_DUTY': FLOOR_DUTY, 'LADDER_C': ALLY_LADDER_C,
           'QUIET_C': QUIET_TCTL_C, 'RISE_TRIP_C': RISE_TRIP_C,
           'RISE_WINDOW_S': RISE_WINDOW_S, 'CPU_LOAD_C': CPU_LOAD_C},
    DOCK: {'MAX_CURVE': list(MAX_CURVE), 'QUIET_CURVE': list(QUIET_CURVE),
           'FLOOR_DUTY': FLOOR_DUTY, 'LADDER_C': DOCK_LADDER_C,
           'QUIET_C': QUIET_EDGE_C, 'RISE_TRIP_C': RISE_TRIP_C,
           'RISE_WINDOW_S': RISE_WINDOW_S, 'CEILING_C': QUIET_JUNCTION_C,
           'LOAD_POWER_W': LOAD_POWER_W, 'LOAD_BUSY_PCT': LOAD_BUSY_PCT},
}

# Every spec'd key must have a default, or a config that omits a surface
# would build an incomplete one. Checked at import, where it is a crash
# before any hardware is touched rather than a KeyError mid-tick.
for _k, _spec in _SURFACE_SPEC.items():
    if {n for n, _, _ in _spec} != set(_DEFAULTS[_k]):
        raise AssertionError(f'{_k}: config spec and defaults disagree')


def config_scopes():
    """The scope every section belongs to: 'shared' for the machinery
    sections, the surface key for a surface's own section."""
    scopes = {sect: 'shared' for _, sect, _, _ in _SHARED_SPEC}
    scopes.update({k: k for k in SURFACE_KEYS})
    return scopes


def read_config(path):
    """(values, problems): {scope: {NAME: parsed}} for every key present
    in the file, plus one problem line per unparseable or unknown entry.
    An unknown key is a PROBLEM, not a shrug — a typo'd name silently
    ignored means the operator believes a value is applied that is not,
    which is exactly the fiction this daemon exists to refuse. Since the
    split that includes a key in the WRONG SECTION: `rise_trip_c` under
    [ladder] is not a shared value any more, and quietly tuning nothing
    would be the same fiction with a subtler cause."""
    cp = configparser.ConfigParser(interpolation=None,
                                   inline_comment_prefixes=('#',))
    empty = {scope: {} for scope in ('shared',) + SURFACE_KEYS}
    try:
        with open(path) as f:
            cp.read_file(f)
    except OSError as e:
        return empty, [f'unreadable: {e}']
    except (configparser.Error, UnicodeDecodeError) as e:
        return empty, ['not parseable as INI: ' + str(e).replace('\n', ' ')]
    known = {(sect, key): ('shared', name, parse)
             for name, sect, key, parse in _SHARED_SPEC}
    for skey, spec in _SURFACE_SPEC.items():
        for name, key, parse in spec:
            known[(skey, key)] = (skey, name, parse)
    values, problems = empty, []
    for sect in cp.sections():
        for key, raw in cp[sect].items():
            hit = known.get((sect, key))
            if hit is None:
                elsewhere = [s for (s, k) in known if k == key]
                hint = (f' — it belongs in [{"] or [".join(elsewhere)}]'
                        if elsewhere else ' (typo?)')
                problems.append(f'[{sect}] {key}: unknown key — nothing '
                                f'by that name exists to set here{hint}')
                continue
            scope, name, parse = hit
            try:
                values[scope][name] = parse(raw)
            except (ValueError, TypeError) as e:
                problems.append(f'[{sect}] {key}: cannot parse {raw!r} ({e})')
    return values, problems


def merge_config(values):
    """Defaults, with the file's values laid over them, scope by scope —
    so a file that names one surface, or one value, changes exactly
    that."""
    merged = {scope: dict(d) for scope, d in _DEFAULTS.items()}
    for scope, d in values.items():
        merged[scope].update(d)
    return merged


def config_problems(v):
    """Every reason the candidate ``v`` (a full merged tuning, defaults
    plus file) may not run, as loggable lines; [] means valid. The rules
    are the incident history restated as arithmetic: no duty below the
    floor and no zero floor (a stopped fan, s13), curves that finish
    (the factory curve's defect was never finishing, s2), thresholds
    ordered and spaced wider than the hysteresis (a 1C usable band
    dithers), dwells and windows positive (a zero dwell is a ladder
    with no gates). Validation is a courtesy to the operator —
    install_config() re-proves the floor structurally either way.

    Every surface is checked SEPARATELY and every complaint names the
    surface it came from: since the split there are two of most of these
    numbers, and "quiet_curve: point(s) ask below floor_duty" without a
    surface in front of it would send the operator to the wrong
    section."""
    p = list(_shared_problems(v['shared']))
    for key in SURFACE_KEYS:
        p += [f'[{key}] {line}'
              for line in _surface_problems(v[key], v['shared'])]
    return p


def _shared_problems(v):
    """The machinery both ladders walk: dwells, windows, streaks, the
    backoff, the hint rung and the coupling."""
    p = []
    for key, label in (('STABILITY_WINDOW_S', 'stability_window_s'),
                       ('MIN_STEP_DWELL_S', 'min_step_dwell_s'),
                       ('QUIET_HOLD_S', 'quiet_hold_s'),
                       ('SPINUP_WATCHDOG_S', 'spinup_watchdog_s'),
                       ('COUPLE_HOLD_S', 'couple_hold_s')):
        if not v[key] > 0:
            p.append(f'{label}: must be positive, got {v[key]!r}')
    if v['STEP_HYSTERESIS_C'] < 0:
        p.append('step_hysteresis_c: must be at least 0')
    if v['STABILITY_RISE_C'] < 0:
        p.append(f'stability_rise_c: must be at least 0, got '
                 f'{v["STABILITY_RISE_C"]!r}')
    for key, label in (('STABILITY_MIN_SAMPLES', 'stability_min_samples'),
                       ('VERIFY_FAIL_TICKS', 'verify_fail_ticks')):
        if v[key] < 1:
            p.append(f'{label}: must be at least 1, got {v[key]!r} — zero '
                     f'would gate on no evidence at all')
    for key, label in (('BACKOFF_FACTOR', 'backoff_factor'),
                       ('BACKOFF_MAX_MULT', 'backoff_max_mult')):
        if v[key] < 1:
            p.append(f'{label}: must be at least 1, got {v[key]!r} — below '
                     f'1 a thrown-back step would wait LESS')
    return p


def _surface_problems(v, shared):
    """One surface's own numbers: its curves, its floor, its ladder."""
    p = []
    floor = v['FLOOR_DUTY']
    if not isinstance(floor, int) or not 1 <= floor <= 255:
        p.append(f'floor_duty {floor!r}: must be an integer in 1..255 — '
                 f'0 would permit a stopped fan (FINDINGS s13)')
        # Keep checking the curves against SOMETHING sane rather than
        # bailing, so one bad value yields the whole list of problems.
        floor = max(1, FLOOR_DUTY)
    curves = (('max_curve', v['MAX_CURVE']),
              ('quiet_curve', v['QUIET_CURVE']))
    for label, curve in curves:
        if len(curve) != len(DOCK_DUTY_FLOOR):
            p.append(f'{label}: {len(curve)} point(s); the hardware takes '
                     f'exactly {len(DOCK_DUTY_FLOOR)}')
            continue
        temps = [t for t, _ in curve]
        duties = [d for _, d in curve]
        if temps != sorted(set(temps)):
            p.append(f'{label}: temperatures must be strictly increasing')
        bad = [d for d in duties if not 0 <= d <= 255]
        if bad:
            p.append(f'{label}: duty value(s) {bad} outside 0..255')
            continue
        if duties != sorted(duties):
            p.append(f'{label}: duties must be monotonic — never falling '
                     f'as temperature rises')
        low = [f'{t}C:{d}' for t, d in curve if d < floor]
        if low:
            p.append(f'{label}: point(s) {" ".join(low)} ask below '
                     f'floor_duty {floor} — no curve may ask a fan below '
                     f'the floor that keeps it turning')
        clamped = [f'{t}C:{d}' for (t, d), fl in zip(curve, DOCK_DUTY_FLOOR)
                   if d < fl]
        if clamped:
            p.append(f'{label}: point(s) {" ".join(clamped)} ask below the '
                     f'recorded per-point acceptance floors '
                     f'{DOCK_DUTY_FLOOR} (FINDINGS s10) — the firmware '
                     f'would silently run different numbers than this '
                     f'file states. (Those floors are the DOCK\'s; the '
                     f'Ally publishes none and is held to them too, '
                     f'because recorded knowledge applied in the safe '
                     f'direction beats an unmeasured guess — see '
                     f'clamp_to_floor.)')
        if duties[-1] != 255:
            p.append(f'{label}: must end at duty 255 — the protective tail '
                     f'is why this daemon exists (the factory curve\'s '
                     f'defect was never finishing, FINDINGS s2)')
        elif min(t for t, d in curve if d == 255) > 65:
            p.append(f'{label}: must reach duty 255 by 65C — every '
                     f'committed rung does, and the 2026-08-07 power-off '
                     f'came from a curve that never got there')
    mx, qt = v['MAX_CURVE'], v['QUIET_CURVE']
    if len(mx) == len(qt) == len(DOCK_DUTY_FLOOR):
        if [t for t, _ in mx] != [t for t, _ in qt]:
            p.append('max_curve and quiet_curve must share the same '
                     'temperature points — rungs blend the duties '
                     'point-wise')
        elif any(dm < dq for (_, dm), (_, dq) in zip(mx, qt)):
            p.append('max_curve must sit at or above quiet_curve at every '
                     'point — the ladder descends from max toward quiet')
    thresholds = list(v['LADDER_C'][1:])
    hyst = shared['STEP_HYSTERESIS_C']
    if not thresholds:
        p.append('ladder_c: at least one threshold — a ladder is at '
                 'least two rungs')
    else:
        if any(x <= 0 for x in thresholds):
            p.append('ladder_c: thresholds must be positive degrees C')
        if thresholds != sorted(thresholds, reverse=True) or \
                len(set(thresholds)) != len(thresholds):
            p.append('ladder_c: thresholds must be strictly '
                     'decreasing — rung 1 is the warmest, the bottom the '
                     'coolest')
        elif hyst >= 0:
            narrow = [f'{a:g}->{b:g}' for a, b in
                      zip(thresholds, thresholds[1:]) if a - b <= hyst]
            if narrow:
                p.append(f'ladder_c: gap(s) {", ".join(narrow)} not '
                         f'wider than the {hyst:g}C hysteresis — those '
                         f'rungs would have no usable band and the ladder '
                         f'would dither (the defect the 5C spacing fixed)')
    # This surface's own signal thresholds. Which of them exist depends on
    # which signals the surface HAS (_SURFACE_SPEC), so the list is built
    # from what is present rather than assumed.
    for key, label in (('RISE_TRIP_C', 'rise_trip_c'),
                       ('RISE_WINDOW_S', 'rise_window_s'),
                       ('QUIET_C', 'quiet_c'),
                       ('CEILING_C', 'junction_c'),
                       ('LOAD_POWER_W', 'load_power_w'),
                       ('CPU_LOAD_C', 'cpu_load_c')):
        if key in v and not v[key] > 0:
            p.append(f'{label}: must be positive, got {v[key]!r}')
    if 'LOAD_BUSY_PCT' in v and not 1 <= v['LOAD_BUSY_PCT'] <= 100:
        p.append(f'load_busy_pct: must be 1..100, got {v["LOAD_BUSY_PCT"]!r}')
    top = len(thresholds)
    if not 0 <= shared['PREPOSITION_RUNG'] <= top:
        p.append(f'[ladder] preposition_rung: must be a rung index 0..{top} '
                 f'on every surface, got {shared["PREPOSITION_RUNG"]!r}')
    if not 0 <= shared['COUPLE_FLOOR_RUNG'] <= top:
        p.append(f'[coupling] couple_floor_rung: must be a rung index '
                 f'0..{top} on every surface, got '
                 f'{shared["COUPLE_FLOOR_RUNG"]!r}')
    return p


def install_config(v):
    """Build BOTH candidate ladders, re-prove the structural floor on
    each, and only then swap every global at once. Returns [] on success
    or the refusal as loggable lines — NEVER installs partially: the
    governor must not run one tick on half a tuning, and it must not run
    one surface's new tuning against the other's old one. The assert pass
    is deliberately redundant with config_problems(): validation is the
    courtesy, the structure is the guarantee, and a bug in the courtesy
    must not become a stopped fan."""
    candidates = {}
    for key in SURFACE_KEYS:
        surf = _build_surface(key, v[key])
        try:
            _assert_surface(surf)
        except AssertionError as e:
            return [f'structural refusal [{key}]: {e}']
        candidates[key] = surf
    g = globals()
    for name, _, _, _ in _SHARED_SPEC:
        g[name] = v['shared'][name]
    g['SURFACES'] = candidates
    return []


#: The two ladders, built AT IMPORT from the compiled-in defaults — by
#: the same function a config file goes through, so the committed values
#: are proved by the same structural assertions before a single fan is
#: touched. A failure here is a crash before any hardware is reached,
#: which is the only safe way to fail at this point.
if install_config(_DEFAULTS):
    raise AssertionError('the compiled-in defaults do not build a legal '
                         'ladder on every surface')


def init_config(path=None):
    """Load the config at ``path`` (default: $SYSTEM_THERMAL_CONF, then
    CONFIG_PATH), or run the compiled-in defaults — NEVER raises and
    never leaves the module unconfigured, because a daemon that exists
    to keep fans turning must not be stoppable by its own config file.
    Logs exactly which source is in force; on any problem the whole
    file is refused LOUDLY, one line per reason, and the defaults are
    (re)installed. Returns the source line it logged."""
    path = path or os.environ.get(CONFIG_ENV) or CONFIG_PATH
    if not os.path.exists(path):
        install_config(_DEFAULTS)         # always passes: they built at import
        line = (f'config: no file at {path} — running compiled-in defaults '
                f'(the committed, measured values)')
        log(line)
        return line
    values, problems = read_config(path)
    if not problems:
        merged = merge_config(values)
        problems = config_problems(merged) or install_config(merged)
        if not problems:
            stated = sum(len(d) for d in values.values())
            total = sum(len(d) for d in _DEFAULTS.values())
            per_surface = ', '.join(
                f'{k} {len(values[k])}/{len(_DEFAULTS[k])}'
                for k in SURFACE_KEYS)
            line = (f'config: loaded {path} — {stated} value(s) from '
                    f'the file, {total - stated} compiled-in '
                    f'default(s) (per surface: {per_surface})')
            log(line)
            return line
    install_config(_DEFAULTS)             # always passes: they built at import
    line = (f'CONFIG REFUSED: {path} — {len(problems)} problem(s); running '
            f'the compiled-in defaults instead. A bad config must never '
            f'run a curve that could stop a fan (FINDINGS s13,s15):')
    log(line)
    for pr in problems:
        log(f'CONFIG REFUSED:   {pr}')
    return line


def now():
    """Monotonic seconds. The ONE clock every decision in this file reads,
    so the offline tests can replace time itself in one place."""
    return time.monotonic()


def surface_mode(surf, st):
    """'max' at the top of THIS ladder, 'quiet' at its bottom, 'stepping'
    between."""
    if st['rung'] == RUNG_TOP:
        return MODE_MAX
    if st['rung'] >= surf['bottom']:
        return MODE_QUIET
    return MODE_STEPPING


def mode_label(ctx):
    """The MACHINE's label, aggregated over both ladders: 'max' if either
    surface is at its top, 'quiet' only when both are at their bottom,
    'stepping' otherwise.

    The endpoint labels keep their exact pre-split meanings — the journal
    greps mode=quiet and ten months of habit read mode=max — and the
    aggregation is deliberately the LOUD direction: a reader who learns
    only one word about this machine should learn the loudest true one.
    Which surface is where is in `status`'s per-surface block and in the
    tick line's `ally=`/`dock=` fields."""
    labels = [surface_mode(SURFACES[k], ctx['surfaces'][k])
              for k in SURFACE_KEYS]
    if MODE_MAX in labels:
        return MODE_MAX
    if all(x == MODE_QUIET for x in labels):
        return MODE_QUIET
    return MODE_STEPPING


def rung_summary(ctx):
    """'ally=rung4 dock=rung0' — where both ladders are, in the one shape
    the journal and the socket log both use. Short on purpose: it goes
    inside other lines."""
    return ' '.join(f'{k}=rung{ctx["surfaces"][k]["rung"]}'
                    for k in SURFACE_KEYS)


def fresh_surface_state(surf, t):
    """One ladder's whole mutable state, seeded at time ``t``. Two of
    these exist and they share nothing: a dwell served on the dock is not
    a dwell served on the Ally, and a backoff earned by a warming eGPU is
    not the Ally's to pay.

    ``last_step_at`` starts at boot deliberately: the first step down
    waits out a full dwell from process start, so a crash-looping daemon
    (Restart=on-failure, RestartSec=2) can never ratchet downward faster
    than a healthy one walks."""
    return {
        # The boot default is the TOP of the ladder, UNPINNED. This daemon
        # exists because the firmware curves powered the host off, so it
        # starts at full cooling — but the ladder walks down by itself
        # once the gates pass, because "loud until someone asks" is the
        # exact shape of the ten-hour pin this design replaces.
        'rung': RUNG_TOP,
        'last_step_at': t,
        'temp_history': [],        # (monotonic_s, control temperature C)
        'backoff_k': 0,            # consecutive thrown-back descents
        'backoff_capped_logged': False,
        'probation_until': None,   # a down-step surviving to here clears k
        'logged_gate': '',         # last descent category the journal saw
        # Closed-loop verification (FINDINGS s13): consecutive ticks each
        # of THIS surface's channels has read as NOT OBEYING, and the
        # latched failure description ('' = healthy). verify_tick owns both.
        'verify_streaks': {name: 0 for _, _, name in surf['verify']},
        'verify_failed': '',
    }


def fresh_ctx(t):
    """The daemon's whole mutable state, in one place, seeded at time
    ``t``: one state block per ladder, plus the few things that are
    genuinely about the MACHINE rather than a cooling surface — the
    operator's pin, its watchdog, the quiet hold-down, and the engine
    hint."""
    return {
        'surfaces': {k: fresh_surface_state(SURFACES[k], t)
                     for k in SURFACE_KEYS},
        # Whole-machine state. The pin is the OPERATOR's intent about this
        # box (see handle('mode quiet') for why it did not become
        # per-surface); the rest is bookkeeping for the socket.
        'pinned': False,
        'interlock_ok_since': None,
        'watchdog_armed': False,
        'max_requested_at': None,
        'hint_pids': set(),        # engine-hint pids seen last tick
        # Since when the dock has been at maximum under sustained eGPU
        # load — the one cross-surface fact this daemon keeps, and it
        # raises the Ally's FLOOR and nothing else (see COUPLE_HOLD_S).
        'coupled_since': None,
        'dirty': False,
        # The last readings the apply paths produced, kept for `status`
        # and for the tick line. verify_tick judges them per surface.
        'ally_ok': False,
        'dock_pwm': None,
    }


def dwell_required_s(st):
    """Seconds that must separate the previous step from the next step
    DOWN on THIS ladder: the base dwell, doubled per thrown-back attempt,
    capped."""
    return MIN_STEP_DWELL_S * min(BACKOFF_FACTOR ** st['backoff_k'],
                                  BACKOFF_MAX_MULT)


def stability_ok(st, t, need_c):
    """(ok, why-not) — is this surface's control temperature STABLE at or
    below ``need_c``?

    THE TEST, stated once for both ladders: over the trailing
    STABILITY_WINDOW_S, (a) at
    least STABILITY_MIN_SAMPLES samples spanning at least
    STABILITY_WINDOW_S - STABILITY_SLACK_S, (b) EVERY sample at or below
    ``need_c``, and (c) the newest sample no more than STABILITY_RISE_C
    above the oldest — below-the-bar alone is a level test and hunts; a
    reading that is under the bar but climbing toward it fails (c) and is
    made to wait. Max-versus-min was considered and rejected as stricter
    for no gain: (b) already bounds the excursion, and first-to-last is
    the trend."""
    window = [s for s in st['temp_history']
              if s[0] >= t - STABILITY_WINDOW_S]
    if len(window) < STABILITY_MIN_SAMPLES or \
            (t - window[0][0]) < STABILITY_WINDOW_S - STABILITY_SLACK_S:
        span = 0.0 if not window else t - window[0][0]
        return False, (f'stability: {len(window)} sample(s) spanning '
                       f'{span:.0f}s of the {STABILITY_WINDOW_S:.0f}s window')
    peak = max(e for _, e in window)
    if peak > need_c:
        return False, (f'stability: peaked {peak:.1f}C above {need_c:.1f}C '
                       f'inside the window')
    rise = window[-1][1] - window[0][1]
    if rise > STABILITY_RISE_C:
        return False, (f'stability: rising ({rise:+.1f}C across the '
                       f'window, limit {STABILITY_RISE_C:+.1f}C)')
    return True, ''


#: Gate names, stable tokens for the log and for `status`. The countdown
#: inside a gate's text changes every tick; the NAME changes only when the
#: blocking condition itself does — which is exactly when a line in the
#: journal earns its place.
GATE_LOAD = 'load'
GATE_RISE = 'rise'
GATE_SENSOR = 'sensor'
GATE_JUNCTION = 'junction'
GATE_EDGE = 'edge'
GATE_STABILITY = 'stability'
GATE_DWELL = 'dwell'
GATE_VERIFY = 'verify'
GATE_COUPLED = 'coupled'


def surface_temp(surf, facts):
    """This surface's CONTROL temperature from ``facts``, or None. The one
    place a ladder learns which sensor is its own."""
    return facts.get(surf['temp_key'])


def load_now(surf, facts):
    """(loaded, why) — is significant work happening RIGHT NOW ON THIS
    SURFACE's signals, judged only from what the box is observably DOING?

    Any single one of that surface's signals suffices. For the dock:
    power (the cleanest separator — 10 W resident vs 165 W generating,
    measured repeatedly) and utilisation (work regardless of which binary
    submitted it). For the Ally: CPU package heat, which is everything a
    GPU-shaped view misses — builds, test suites, CPU inference.

    THE SIGNALS ARE NOT SHARED. eGPU power says nothing about the APU's
    fans and Tctl says nothing about the eGPU's, so each surface reads
    only its own list (SURFACE_SIGNALS): that is the whole point of the
    2026-08-13 split, and the single most likely place to reintroduce the
    coupling it removed. An unreadable signal contributes nothing —
    absence of evidence is not load, and the sensor whose absence IS
    dangerous is the surface's control temperature, which has its own
    escalation."""
    for key, threshold, phrase in surf['load']:
        v = facts.get(key)
        if v is not None and v >= threshold:
            return True, phrase(v, threshold)
    return False, ''


def rising_fast(surf, st, t):
    """The anticipation trigger for ONE surface: '' when calm, else why.
    A climb of the surface's rise_trip_c inside its rise_window_s on ITS
    OWN control temperature means heat is arriving NOW — fire before any
    absolute threshold is crossed, on anything that heats that surface,
    never on something merely resident.

    This is the trigger the 2026-08-13 event fired: a CPU spike
    (+12.6C/20s on Tctl) with the eGPU idle at 10 W. It was right about
    the Ally and wrong about the dock, and it is now read from the Ally's
    history alone. Junction is deliberately absent from every surface's
    rate trigger: 30C in half a second at steady state (FINDINGS s4) is a
    hotspot flickering, not a trend."""
    window = [s for s in st['temp_history']
              if s[0] >= t - surf['rise_window_s']]
    if len(window) < 2:
        return ''
    rise = window[-1][1] - window[0][1]
    if rise >= surf['rise_trip_c']:
        return (f'{surf["temp_name"]} rising {rise:+.1f}C in '
                f'{window[-1][0] - window[0][0]:.0f}s (trip: '
                f'+{surf["rise_trip_c"]:.1f}C per '
                f'{surf["rise_window_s"]:.0f}s)')
    return ''


def ceiling_excursion(surf, facts):
    """'' unless this surface publishes a second, emergency ceiling and
    the reading is above it. Only the dock has one — the eGPU's junction
    channel, a hotspot that moves 30C in half a second (FINDINGS s4), too
    fast to control on and too dangerous to ignore. The Ally publishes
    one package temperature and it is already the control signal."""
    if surf['ceiling'] is None:
        return ''
    key, limit, name = surf['ceiling']
    v = facts.get(key)
    if v is not None and v > limit:
        return f'{name} {v:.1f}C above the {limit:.0f}C ceiling'
    return ''


def update_coupling(ctx, facts, t):
    """Track how long the DOCK has been at its top rung under sustained
    eGPU load — the one cross-surface fact this daemon keeps. Mutates
    ctx; called exactly once per tick, from govern(). Reading it is
    coupled_floor(), which is pure, so `status` can report the same
    answer the next tick will act on without moving the clock."""
    loaded, why = load_now(SURFACES[DOCK], facts)
    at_top = ctx['surfaces'][DOCK]['rung'] == RUNG_TOP
    if loaded and at_top:
        if ctx['coupled_since'] is None:
            ctx['coupled_since'] = t
            log(f'coupling: dock at maximum under eGPU load ({why}) — the '
                f'APU feeds that card, so the Ally floor rises to rung '
                f'{COUPLE_FLOOR_RUNG} if this holds {COUPLE_HOLD_S:.0f}s')
    elif ctx['coupled_since'] is not None:
        ctx['coupled_since'] = None
        log('coupling: the dock is no longer at maximum under eGPU load — '
            'the Ally floor is released to its own gates')


def coupled_floor(ctx, t):
    """The rung the ALLY ladder may not go below right now because of the
    eGPU-load coupling, or None. Pure."""
    since = ctx['coupled_since']
    if since is not None and t - since >= COUPLE_HOLD_S:
        return COUPLE_FLOOR_RUNG
    return None


def descent_gate(surf, st, facts, t, floor_rung=None):
    """(allowed, gate, why-not, dwell_remaining_s) — may THIS ladder take
    ONE step down RIGHT NOW? One implementation, two instances: every
    number it reads comes from ``surf`` and every clock from ``st``, so
    the Ally and the dock are gated by the same code against different
    evidence. All the gates, first refusal wins the report; ``gate`` is
    the stable token, ``why`` carries the numbers:

      1. interlock: no significant load on THIS surface's signals
         (load_now — a process NAME is deliberately not consulted), no
         fast rise on its control temperature (rising_fast), that sensor
         readable, and no excursion past its emergency ceiling if it has
         one — the dock's junction is an emergency signal and only ever
         an emergency signal;
      2. the control temperature at or below the entered rung's threshold
         minus hysteresis;
      3. that temperature STABLE, not merely low (stability_ok is the
         test of record);
      4. the dwell since THIS ladder's previous step in EITHER direction
         has passed, backoff included.

    Pure decision — reads facts and state, writes nothing, so `status` can
    show the caller exactly the refusal the next tick will act on.

    Gate ZERO, before all of them: a latched verification failure ON THIS
    SURFACE. A fan that is not obeying the curve (verify_tick) makes its
    ladder's applied duty a fiction, so no descent may be taken on any
    evidence while it lasts — and equally, the OTHER surface's failure is
    not evidence here and does not appear.

    ``floor_rung``, when given, is the lowest rung this ladder may occupy
    for a reason OUTSIDE its own signals — today only the eGPU-load
    coupling on the Ally (coupled_floor). It refuses the step and names
    itself GATE_COUPLED rather than borrowing a thermal gate's name,
    because the operator asking "why are the Ally fans up?" deserves the
    real answer: not the APU's temperature, the card's load."""
    target = st['rung'] + 1
    enter_c = surf['ladder_c'][target]
    need_c = enter_c - STEP_HYSTERESIS_C
    temp = surface_temp(surf, facts)
    if st['verify_failed']:
        return False, GATE_VERIFY, (f'VERIFICATION FAILURE — '
                                    f'{st["verify_failed"]}; descent '
                                    f'REFUSED until readings recover'), 0.0
    loaded, load_why = load_now(surf, facts)
    if loaded:
        return False, GATE_LOAD, load_why, 0.0
    rise_why = rising_fast(surf, st, t)
    if rise_why:
        return False, GATE_RISE, rise_why, 0.0
    if temp is None:
        return False, GATE_SENSOR, (f'the {surf["temp_name"]} sensor is '
                                    f'UNREADABLE'), 0.0
    excursion = ceiling_excursion(surf, facts)
    if excursion:
        return False, GATE_JUNCTION, excursion, 0.0
    if floor_rung is not None and st['rung'] >= floor_rung:
        return False, GATE_COUPLED, (
            f'rung {floor_rung} is this surface\'s floor while the dock '
            f'runs sustained eGPU load — the APU feeds that card'), 0.0
    if temp > need_c:
        return False, GATE_EDGE, (f'{surf["temp_name"]} {temp:.1f}C above '
                                  f'{need_c:.1f}C (rung {target} enters '
                                  f'{STEP_HYSTERESIS_C:.0f}C under its '
                                  f'{enter_c:.0f}C threshold)'), 0.0
    ok, why = stability_ok(st, t, need_c)
    if not ok:
        return False, GATE_STABILITY, why, 0.0
    required = dwell_required_s(st)
    since = t - st['last_step_at']
    if since < required:
        # The remaining seconds are returned as a NUMBER (status reports
        # them as dwell_remaining_s); spelling them out again in the text
        # would be the same fact twice on a line that carries two
        # ladders.
        return False, GATE_DWELL, (f'dwell: {since:.0f}s of {required:.0f}s '
                                   f'since the last step'), required - since
    return True, '', '', 0.0


def descent_phrase(ctx, surf, st, facts, t, floor_rung=None):
    """(category, field) — one ladder's answer to "why is it loud right
    now, and what happens next?". The category is stable across countdown
    ticks so the main loop can log on category CHANGES without printing a
    480s backoff hold sixty times; the field is one greppable clause.

    A verification failure outranks everything, the pin included: it must
    not read like a normal hold, because it is not one — the hardware is
    not doing what the journal says it was asked."""
    if st['verify_failed']:
        return GATE_VERIFY, (f'REFUSED — verification failure: '
                             f'{st["verify_failed"]}')
    if ctx['pinned']:
        return 'pinned', 'pinned by mode max (mode quiet re-enables)'
    if st['rung'] >= surf['bottom']:
        return 'bottom', 'at bottom'
    allowed, gate, why, _rem = descent_gate(surf, st, facts, t, floor_rung)
    if allowed:
        return 'ready', f'to rung {st["rung"] + 1} on the next tick'
    # No 'held — ' prefix: the gate token already says it is held, and on
    # a line carrying two ladders every repeated word is paid for twice.
    return gate, why


def _record_step(ctx, surf, st, t, rung, cause):
    """Move THIS ladder to ``rung`` and stamp its dwell clock — every
    transition, both directions, exactly one way, so no path can move a
    curve without the journal saying which surface moved and why."""
    log(f'ladder[{surf["key"]}]: rung {st["rung"]} -> {rung} — {cause}')
    st['rung'] = rung
    st['last_step_at'] = t
    ctx['dirty'] = True


def _thrown_back(ctx, surf, st, t, rung, cause):
    """A step up caused by evidence the offered rung is untenable — a
    temperature that rose out of it, or a verification failure at it: take
    the step, then make THIS ladder's next attempt wait longer. One
    increment per event, not per rung crossed, and never charged to the
    other surface."""
    st['probation_until'] = None
    st['backoff_k'] += 1
    _record_step(ctx, surf, st, t, rung, cause + (
        f' (next attempt waits {dwell_required_s(st):.0f}s)'))
    if BACKOFF_FACTOR ** st['backoff_k'] >= BACKOFF_MAX_MULT \
            and not st['backoff_capped_logged']:
        st['backoff_capped_logged'] = True
        log(f'ladder[{surf["key"]}]: descent backoff CAPPED at '
            f'{MIN_STEP_DWELL_S * BACKOFF_MAX_MULT:.0f}s — this surface '
            f'keeps warming out of the rung it is offered; asking every '
            f'{MIN_STEP_DWELL_S * BACKOFF_MAX_MULT:.0f}s from here')


def maybe_release_watchdog(ctx, ok, t):
    """The spin-up watchdog: a socket-requested max whose load never
    materialised (or has come and gone) must not stay pinned because the
    requester died mid-sequence. Releases the PIN — never places the fans
    anywhere — and the ladder walks down through the same gates as every
    other descent. ``ok`` is the live interlock verdict (no significant
    load, card cool, sensor readable); the hold-down is the same
    QUIET_HOLD_S every other caller serves."""
    if ctx['pinned'] and ctx['watchdog_armed'] \
            and ctx['max_requested_at'] is not None \
            and t - ctx['max_requested_at'] >= SPINUP_WATCHDOG_S \
            and ok and ctx['interlock_ok_since'] is not None \
            and (t - ctx['interlock_ok_since']) >= QUIET_HOLD_S:
        log(f'watchdog: no significant load '
            f'{SPINUP_WATCHDOG_S:.0f}s after a requested spin-up and '
            f'the interlock has held {QUIET_HOLD_S:.0f}s — releasing '
            f'the max pin; descent resumes on the ladder\'s gates')
        ctx['pinned'] = False
        ctx['watchdog_armed'] = False
        ctx['dirty'] = True


#: The order the ladders are GOVERNED in, which is not the order they are
#: reported in. The dock goes first because its rung is an input to the
#: Ally's floor this tick (update_coupling), and nothing about the Ally
#: is ever an input to the dock — so first-then-second reads a fresh
#: rung rather than last tick's. If a second coupling is ever added in
#: the other direction, this order stops being sufficient and the answer
#: is to compute both floors from the PREVIOUS tick's rungs, not to
#: reorder again.
GOVERN_ORDER = (DOCK, ALLY)


def govern(ctx, facts, t):
    """One governance decision per tick PER LADDER: walk each on its own
    evidence. Neither reads the other's state — the whole content of the
    2026-08-13 split is that this function is a loop rather than a
    special case — with ONE exception, passed in explicitly as a floor
    rather than smuggled in as evidence: sustained eGPU load on the dock
    raises the Ally's floor, because the APU feeds that card."""
    for key in GOVERN_ORDER:
        floor = None
        if key == ALLY:
            # Measured AFTER the dock has been governed this tick, so the
            # floor reads the rung the dock is at NOW rather than the one
            # it was at ten seconds ago — which on the tick a model starts
            # is the difference between "at maximum under load" and "at
            # its floor".
            update_coupling(ctx, facts, t)
            floor = coupled_floor(ctx, t)
        govern_surface(ctx, SURFACES[key], ctx['surfaces'][key], facts, t,
                       floor)


def govern_surface(ctx, surf, st, facts, t, floor_rung=None):
    """Walk ONE ladder on ITS OWN evidence.

    UP first, and to the TOP — significant load on this surface's signals
    (load_now), its control sensor unreadable, its emergency ceiling
    exceeded, a fast rise on its control temperature (rising_fast), or
    that temperature above the current rung's own ceiling: any of them
    goes straight to RUNG_TOP on the same tick, no dwell, no gate, no
    intermediate stops. The rungs exist only on the way down. A
    recognisable engine APPEARING is not on that list — it is a
    PRE-POSITION hint toward PREPOSITION_RUNG, taken after every real
    cause has had its chance, because a name is a guess about the future
    and the load signals are the evidence. DOWN only afterwards, one
    rung, through descent_gate. A daemon-initiated move up NEVER pins:
    the whole point of the ladder is that revocation is a step, not a
    sentence, and the walk back down needs no socket verb.

    NOTHING HERE READS THE OTHER SURFACE. Every escalation above is
    scoped to the ladder whose evidence it is, including the unreadable
    sensor: a missing eGPU edge reading says nothing about the APU's fans
    and a missing Tctl says nothing about the eGPU's. Getting that wrong
    in the SAFE direction (both to maximum) silently rebuilds the
    coupling the split removed, which is why it is stated here as well as
    proved in the tests."""
    temp = surface_temp(surf, facts)
    engines = facts.get('engine_pids') or []

    # The stability and rise record for THIS surface. An unreadable
    # sensor voids its own — a window that bridges a gap in the record is
    # a window that saw nothing.
    if temp is not None:
        st['temp_history'].append((t, temp))
        horizon = t - max(STABILITY_WINDOW_S, surf['rise_window_s']) \
            - 2 * STABILITY_SLACK_S
        st['temp_history'] = [s for s in st['temp_history']
                              if s[0] >= horizon]
    else:
        st['temp_history'] = []

    # A step down that survived a full base dwell without being thrown
    # back is a step this surface accepted: the warming episode is over.
    if st['probation_until'] is not None and t >= st['probation_until']:
        st['probation_until'] = None
        st['backoff_k'] = 0
        st['backoff_capped_logged'] = False

    # ---- UP. Immediate and unconditional, largest cause first. --------
    loaded, load_why = load_now(surf, facts)
    if loaded:
        if st['rung'] != RUNG_TOP:
            # Load is a NEW thermal episode, not a thrown-back step — the
            # backoff it would inherit was evidence about an idle surface
            # that no longer exists.
            st['probation_until'] = None
            st['backoff_k'] = 0
            st['backoff_capped_logged'] = False
            _record_step(ctx, surf, st, t, RUNG_TOP,
                         f'significant load: {load_why}')
        return
    if temp is None:
        if st['rung'] != RUNG_TOP:
            st['probation_until'] = None
            _record_step(ctx, surf, st, t, RUNG_TOP,
                         f'the {surf["temp_name"]} sensor is UNREADABLE — '
                         f'holding this surface at maximum until it reads '
                         f'again')
        return
    excursion = ceiling_excursion(surf, facts)
    if excursion:
        if st['rung'] != RUNG_TOP:
            _thrown_back(ctx, surf, st, t, RUNG_TOP, excursion)
        return
    rise_why = rising_fast(surf, st, t)
    if rise_why:
        if st['rung'] != RUNG_TOP:
            # Arriving heat is a new episode too — and the whole point of
            # the rate trigger is to spend the fans BEFORE any threshold
            # confirms what the slope already says.
            st['probation_until'] = None
            st['backoff_k'] = 0
            st['backoff_capped_logged'] = False
            _record_step(ctx, surf, st, t, RUNG_TOP,
                         f'{rise_why} — heat is arriving; the fans go first')
        return
    if st['rung'] > RUNG_TOP and temp > surf['ladder_c'][st['rung']]:
        # Straight to the top — NOT to the rung matching the reading.
        # The operator, verbatim: "we step up to 100% from there quickly
        # - since load is going to be exponential - and use the
        # intermediate rungs not as gating the climb but the descent."
        # Thermal load from inference is not gradual, and a surface that
        # climbs one rung at a time while heating is behind the curve at
        # every step. Overshooting cooling costs noise; undershooting
        # costs the host.
        _thrown_back(ctx, surf, st, t, RUNG_TOP,
                     f'{surf["temp_name"]} {temp:.1f}C above the '
                     f'{surf["ladder_c"][st["rung"]]:.0f}C ceiling of rung '
                     f'{st["rung"]} — ascent goes to the top; the rungs '
                     f'gate only the descent')
        return

    # ---- THE COUPLING. Not this surface's evidence, so it never reaches
    # the top: it is a FLOOR, taken after every real cause and before the
    # hint, because sustained eGPU load is a better reason to move air
    # than a process name is and a worse one than a temperature.
    if floor_rung is not None and st['rung'] > floor_rung:
        _record_step(ctx, surf, st, t, floor_rung,
                     f'the dock has run maximum under sustained eGPU load '
                     f'for {COUPLE_HOLD_S:.0f}s — the APU feeds that card, '
                     f'so this surface floors at rung {floor_rung} (its own '
                     f'signals still own everything above)')
        return

    # ---- THE HINT. An engine in /proc is a pre-position, not a verdict.
    # Edge-triggered on pid APPEARANCE, deliberately: a level trigger
    # would hold this rung for as long as a model stays resident, which
    # is the exact false positive being retired. If the load never lands,
    # the ladder walks back down through its own gates; when it does
    # land, the load signals above take it to the top. The hint's
    # correctness is NOT load-bearing — it buys spin-up seconds, and
    # being wrong costs a few minutes of a middle rung's noise.
    #
    # It is the ONE input both ladders share, and deliberately: an engine
    # appearing is a guess about the machine, not evidence about either
    # surface — the same guess, about the same event, on the same tick.
    # It is affordable on both precisely because it is not load-bearing
    # on either. ctx['hint_pids'] is therefore cleared by the surface
    # governed LAST, so both see the same set of fresh pids on the tick
    # a pid appears.
    fresh = set(engines) - ctx['hint_pids']
    if surf['key'] == GOVERN_ORDER[-1]:
        ctx['hint_pids'] = set(engines)
    if fresh and st['rung'] > PREPOSITION_RUNG:
        _record_step(ctx, surf, st, t, PREPOSITION_RUNG,
                     f'engine hint: pid(s) '
                     f'{", ".join(str(p) for p in sorted(fresh))} appeared '
                     f'— pre-positioning so air is moving before any load '
                     f'lands; the load signals decide from here')
        return

    # ---- DOWN. One rung, gated, never while pinned. -------------------
    if ctx['pinned'] or st['rung'] >= surf['bottom']:
        return
    allowed, _gate, _why, _rem = descent_gate(surf, st, facts, t, floor_rung)
    if allowed:
        target = st['rung'] + 1
        # The span claimed is the span TESTED — the trailing stability
        # window, not the full retained history, whose older samples were
        # gated against a different rung's bar or not at all.
        window = [s for s in st['temp_history']
                  if s[0] >= t - STABILITY_WINDOW_S]
        window_span = t - window[0][0]
        _record_step(
            ctx, surf, st, t, target,
            f'{surf["temp_name"]} {temp:.1f}C held at or below '
            f'{surf["ladder_c"][target] - STEP_HYSTERESIS_C:.1f}C and flat '
            f'for {window_span:.0f}s, {t - st["last_step_at"]:.0f}s since '
            f'the last step')
        # last_step_at was just restamped by _record_step; probation runs
        # from the step itself.
        st['probation_until'] = t + MIN_STEP_DWELL_S


def _verify_readings(ctx, surf, st, readings):
    """{channel: None | (failing, detail)} — this tick's closed-loop
    verdict for ONE surface's channels. None means NOTHING OBSERVABLE
    (Ally curve not applied or tachometer unreadable; dock detached or its
    HID transaction failed) — no evidence either way, so the streak
    neither grows nor resets. An absent dock is deliberately not a failure
    here: the eGPU leaves with it, and the dock ladder's own
    unreadable-sensor escalation already owns that case.

    TWO KINDS OF READING, because the two surfaces emit different
    evidence and no amount of parameterisation makes a tachometer and an
    applied-duty byte the same measurement:

      'rpm'     — a real tachometer (fan1/fan2_input). Zero means
                  stopped, and it is directly observable.
      'applied' — the dock's byte[9], the ONLY signal that fan has. The
                  bar is that surface's floor duty, and that is NOT a
                  model of the requested->applied mapping (FINDINGS s13;
                  s15 fitted one, valid only over its tested band): every
                  measured healthy pair applied ABOVE the request floor
                  (89->115, 89->179, 134->178, 178->252, and the s15
                  sweep's floors applying 131-179 against the 45 bar), and
                  the one measured pair below it was 0->0 with the fan
                  STOPPED. Applied below the floor is therefore either the
                  stopped-fan shape or an unknown regime in the dangerous
                  direction, and both get the same answer."""
    out = {}
    for key, kind, name in surf['verify']:
        value = readings.get(key)
        if kind == 'rpm':
            value = value if ctx['ally_ok'] else None
            out[name] = None if value is None else (
                value == 0,
                f'{surf["key"]} {name} reads 0 rpm at rung {st["rung"]}')
        else:
            out[name] = None if value is None else (
                value < surf['floor_duty'],
                f'{surf["key"]} applied PWM {value} below the '
                f'{surf["floor_duty"]} floor at rung {st["rung"]}')
    return out


def verify_tick(ctx, readings, t):
    """The closed loop for BOTH ladders: returns the set of surface keys
    whose rung moved this tick (the caller must re-apply those curves
    within the same tick). ``readings`` carries every channel both
    surfaces read back — tachometers and the dock's applied PWM — and
    each surface judges only its own (SURFACE_SIGNALS[...]['verify'])."""
    moved = set()
    for key in SURFACE_KEYS:
        if verify_surface(ctx, SURFACES[key], ctx['surfaces'][key],
                          readings, t):
            moved.add(key)
    return moved


def verify_surface(ctx, surf, st, readings, t):
    """Closed loop for ONE surface: after applying, READ BACK — the
    daemon never trusts its own curve. FINDINGS s13 measured the same
    idle request (89) applying 115 under one curve shape and 179-180
    under another, and a third shape applying 0 with the fan stopped:
    requested duty does not determine applied duty, and the mechanism is
    unknown. So no rung's safety is ever assumed from its numbers; it is
    verified on the only signals that exist — the Ally tachometers and
    the dock's byte[9].

    One bad sample must not cause a step (spin-up reads 0 rpm briefly;
    dock telemetry is intermittent); VERIFY_FAIL_TICKS consecutive
    verifiable ones must. On the tick a channel's streak reaches the bar:

      1. escalate THIS ladder to its top rung IMMEDIATELY — same tick, no
         dwell (the caller re-applies the top curve before sleeping);
      2. log it LOUDLY and distinctly — this is not a normal transition;
      3. refuse descent ON THIS LADDER while the condition persists
         (descent_gate reads the latch), and say so in its `descent:`
         field.

    THE OTHER LADDER DOES NOT MOVE. A stopped Ally fan is evidence about
    the Ally fans; the dock fan is a different fan, on a different
    controller, cooling a different card, and escalating it too would be
    inventing evidence. Since the split this is the failure mode most
    likely to be got wrong in the "safe" direction, so it is scoped here
    and pinned in the tests.

    The latch clears only when a verifiable tick shows no failure — at
    which point descent resumes through the ladder's own gates, and a rung
    that stops the fan again is escalated again, backoff-spaced, which is
    the empirical answer to a hardware behaviour nobody can predict:
    measure, verify, react. Returns True when the rung moved (the caller
    must re-apply that surface's curve within the same tick)."""
    failed_now = []
    for channel, verdict in _verify_readings(ctx, surf, st, readings).items():
        if verdict is None:
            continue                     # no evidence: hold the count
        failing, detail = verdict
        if failing:
            st['verify_streaks'][channel] += 1
            if st['verify_streaks'][channel] >= VERIFY_FAIL_TICKS:
                failed_now.append(detail)
        else:
            st['verify_streaks'][channel] = 0
    if not failed_now:
        if st['verify_failed']:
            st['verify_failed'] = ''
            ctx['dirty'] = True
            log(f'verify[{surf["key"]}]: fan readings healthy again — '
                f'descent re-enabled through this ladder\'s own gates')
        return False
    detail = '; '.join(failed_now)
    newly = not st['verify_failed']
    st['verify_failed'] = detail
    if newly:
        ctx['dirty'] = True
        log(f'VERIFICATION FAILURE [{surf["key"]}]: {detail} — '
            f'{VERIFY_FAIL_TICKS} '
            f'consecutive ticks. The fan is NOT OBEYING the asserted curve '
            f'(applied duty is curve-shape-dependent and unmodelled, '
            f'FINDINGS s13): escalating this surface to maximum NOW and '
            f'refusing its descent until the readings recover')
    if st['rung'] != RUNG_TOP:
        _thrown_back(ctx, surf, st, t, RUNG_TOP,
                     f'verification failure: {detail}')
        return True
    return False

# ---------------------------------------------------------------------------
# the socket
# ---------------------------------------------------------------------------
SOCKET_PATH = "/run/glados-thermal.sock"
SOCKET_GROUP = "glados"
SOCKET_MODE = 0o660

#: Longest request accepted. The whole vocabulary is "mode quiet" (10 bytes);
#: anything approaching this is not a verb.
MAX_REQUEST = 64

REPORT_ID, SIZE = 0x5E, 300
def _ioc(d, t, nr, n): return (d << 30) | (n << 16) | (t << 8) | nr
HIDSET, HIDGET = _ioc(3, ord('H'), 0x06, SIZE), _ioc(3, ord('H'), 0x07, SIZE)

def log(m): print(m, flush=True)

def hwmon(name):
    for h in glob.glob(f'{HWMON_ROOT}/hwmon*/'):
        try:
            if open(h + 'name').read().strip() == name:
                return h.rstrip('/')
        except OSError:
            pass
    return None

def write(path, val):
    try:
        with open(path, 'w') as f:
            f.write(str(val))
        return True
    except OSError:
        return False

# ------------------------------------------------------------ eGPU sensors
def egpu_temps():
    """{'edge': C, 'junction': C, 'power_w': W} for the eGPU, or {} .

    Globbed BY PCI PATH and the channel found by reading tempN_label. Never
    by hwmon index: hwmon8 is the eGPU today and hwmon9 is the iGPU on the
    same driver name, and an index-pinned reader reports a cool card while
    the hot one burns. Never by the dock's own telemetry either — GetFanSpeed
    byte[6] read 0C on 57 of the last 60 ticks, and there is no dock
    tachometer at all.
    """
    out = {}
    for node in glob.glob(f'{PCI_ROOT}/{EGPU_PCI}/hwmon/hwmon*'):
        try:
            if open(f'{node}/name').read().strip() != 'amdgpu':
                continue
        except OSError:
            continue
        for lp in glob.glob(f'{node}/temp*_label'):
            try:
                label = open(lp).read().strip().lower()
                val = int(open(lp.replace('_label', '_input')).read().strip())
            except (OSError, ValueError):
                continue
            out[label] = val / 1000.0
        try:
            out['power_w'] = int(
                open(f'{node}/power1_average').read().strip()) / 1e6
        except (OSError, ValueError):
            pass
        return out
    return out

def engine_pids():
    """pids whose comm LOOKS LIKE an inference engine — prefix match
    against ENGINE_COMM_HINTS. This feeds the PRE-POSITION hint and the
    `status` report, and NOTHING else: since the load rework, no decision
    that matters rides on a process name (the exact-comm predecessor was
    load-bearing and wrong in both directions — blind to llama-bench at
    Tctl 95.0C, pinned at 100% by a resident idle server).

    Still pure /proc and still comm rather than cmdline: `-f` matches the
    full command line, so any caller whose own argv contains the string
    matches ITSELF — this project has already killed a test harness that
    way. comm cannot self-match a python daemon, and its 15-char
    truncation is why the match is a prefix. Loose is fine here: a false
    match costs a few minutes of a middle rung's noise, and a miss (a
    python-hosted engine reads "python3") costs only the spin-up seconds
    the hint would have bought — the load signals decide everything that
    matters.
    """
    pids = []
    for d in glob.glob(f'{PROC_ROOT}/[0-9]*'):
        try:
            if open(f'{d}/comm').read().strip() \
                    .startswith(ENGINE_COMM_HINTS):
                pids.append(int(os.path.basename(d)))
        except (OSError, ValueError):
            continue
    return sorted(pids)


def egpu_busy():
    """gpu_busy_percent for the eGPU, by PCI path, or None. Utilisation
    straight from the driver: work is work regardless of which binary
    submitted it, which is the property the retired name check lacked."""
    try:
        return int(open(f'{PCI_ROOT}/{EGPU_PCI}/gpu_busy_percent')
                   .read().strip())
    except (OSError, ValueError):
        return None


def cpu_temp():
    """The APU package temperature — k10temp's channel labelled Tctl — or
    None. The Ally-side thermal signal: catches builds, test suites and
    CPU inference, the loads a GPU-shaped view misses entirely (measured
    2026-08-12: Tctl 95.0C from llama-bench while the old daemon idled
    blind).

    UNREADABLE IS AN ESCALATION — OF THE ALLY LADDER, AND ONLY IT. This
    reverses the pre-split rule, deliberately and with a cost. Under one
    ladder, Tctl was an anticipation EXTRA on top of eGPU edge, so a
    missing k10temp lost a signal and no more, and refusing to condemn a
    reused bundle to permanent maximum was the right trade. Since
    2026-08-13 Tctl is the Ally ladder's ONLY control signal — exactly
    the role edge plays for the dock — and a ladder with no reading has
    no evidence for descending, so it holds maximum, like the dock does,
    through the same code path.

    THE COST, NAMED: on a host with no k10temp (a non-AMD reuse of this
    bundle), the Ally pair runs at maximum forever while the dock ladder
    governs normally. That is loud and safe, it is visible in one line of
    the journal ('the cpu Tctl sensor is UNREADABLE'), and the honest fix
    is to bind that surface to a sensor the host actually has — not to
    let a ladder descend on a sensor it cannot read. The asymmetric
    special case was worth more than the symmetry when this was an extra
    signal; it is worth less than the symmetry now that it is THE signal,
    because an asymmetry here is exactly the kind of exception that
    quietly grows back into a coupling."""
    h = hwmon('k10temp')
    if not h:
        return None
    for lp in glob.glob(f'{h}/temp*_label'):
        try:
            if open(lp).read().strip().lower() == 'tctl':
                return int(open(lp.replace('_label', '_input'))
                           .read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None

def read_facts():
    """Every signal both ladders read, gathered once per tick. Which of
    them matters to which surface is SURFACE_SIGNALS' business and
    nothing else's — this function knows only how to read a sensor."""
    facts = {}
    facts['engine_pids'] = engine_pids()      # reported, never a veto
    t = egpu_temps()
    facts['edge_c'] = t.get('edge')
    facts['junction_c'] = t.get('junction')
    facts['power_w'] = t.get('power_w')
    facts['busy_pct'] = egpu_busy()
    facts['cpu_c'] = cpu_temp()
    return facts


def surface_quiet_ok(surf, facts):
    """(ok, reason) — may THIS surface go passive right now?

    THE GRANT CONDITION IS "NO SIGNIFICANT LOAD", ON THE SAME EVIDENCE THE
    GOVERNOR ACTS ON — for the dock, eGPU power and utilisation plus its
    thermal ceilings; for the Ally, CPU package heat plus its own — and
    deliberately NOT "no engine process exists". Decided, not drifted
    into. Keeping a process check as a conservative extra was considered
    and rejected: a resident-but-idle model (14B at 10 W, measured) would
    refuse quiet FOREVER on a host whose normal state is a loaded model —
    the operator's named false positive, made permanent at the socket.
    What the check would buy is one tick of anticipation on a grant, and a
    grant does not drop a curve anyway: it releases the pin, every tick
    re-evaluates, and the first sign of load (10 -> 165 W inside a tick,
    measured) re-arms the ladder to the top. That is the same exposure as
    an engine started from the CLI mid-quiet, which this design already
    survives by construction. A name here would be a second, unexamined
    copy of the load question, answered with worse evidence.

    An unreadable control sensor still fails, for THIS surface. "Sensor
    unreadable" and "confirmed hot" are the same answer for this
    decision, because the only thing worse than a loud machine is a 165 W
    card cooled by the firmware curve that powered this host off once
    already.
    """
    loaded, why = load_now(surf, facts)
    if loaded:
        return False, f'significant load: {why}'
    temp = surface_temp(surf, facts)
    if temp is None:
        return False, (f'the {surf["temp_name"]} sensor is UNREADABLE — '
                       f'refusing to assume cool')
    if temp > surf['quiet_c']:
        return False, (f'{surf["temp_name"]} {temp:.1f}C is above the '
                       f'{surf["quiet_c"]:.0f}C quiet threshold')
    if ceiling_excursion(surf, facts):
        key, limit, name = surf['ceiling']
        return False, (f'{name} {facts[key]:.1f}C is above the '
                       f'{limit:.0f}C quiet ceiling')
    return True, 'no significant load, cool, sensor readable'


def quiet_verdicts(facts):
    """{surface key: (ok, reason)} — the grant question asked of each
    ladder separately, because since the split "no significant load" is
    two questions with two answers."""
    return {k: surface_quiet_ok(SURFACES[k], facts) for k in SURFACE_KEYS}


def interlock():
    """(ok, reason, facts) — may the fans go passive RIGHT NOW?

    THE MACHINE-LEVEL ANSWER, and it is the AND of both surfaces: the
    socket's `mode quiet` is one verb about one machine (see handle), so
    it is granted only when EVERY cooling surface would grant it, and the
    reason names the surface that refused. A grant releases the operator's
    pin and nothing else — each ladder still descends only through its own
    gates, so an AND here costs nothing a hot surface would not have
    refused anyway, and it keeps the socket's answer honest instead of
    reporting a half-quiet state nobody can state the meaning of.
    """
    facts = read_facts()
    verdicts = quiet_verdicts(facts)
    refused = [f'{k}: {why}' for k, (ok, why) in verdicts.items() if not ok]
    if refused:
        return False, '; '.join(refused), facts
    return True, ('no significant load, both surfaces cool, sensors '
                  'readable'), facts

# ---------------------------------------------------------------- Ally fans
def apply_ally(curve, floor=None):
    """Assert ``curve`` on BOTH Ally fans — pwm1 and pwm2, always
    together. They cool one package (the Z1 Extreme, CPU and GPU on the
    same die), so there is no thermally meaningful state in which they
    differ; the two-ladder split put the dock on its own ladder and
    deliberately did NOT split these from each other."""
    c = hwmon('asus_custom_fan_curve')
    if not c:
        return False
    # The operator's floor, re-applied at the hardware boundary: the
    # ladder is already floored at construction, so this bites only if a
    # future caller hands this function a curve the ladder never built —
    # and then a fan still cannot be asked to stop. ``floor`` is the
    # SURFACE's floor when the main loop calls; the module default is the
    # backstop for any other caller.
    curve = never_stops(curve, floor)
    ok = False
    for ch in (1, 2):
        if not os.path.exists(f'{c}/pwm{ch}_enable'):
            continue
        for i, (t, d) in enumerate(curve, 1):
            write(f'{c}/pwm{ch}_auto_point{i}_temp', t)
            write(f'{c}/pwm{ch}_auto_point{i}_pwm', d)
        # enable semantics vary by kernel version; 1 then 2
        if not write(f'{c}/pwm{ch}_enable', 1):
            write(f'{c}/pwm{ch}_enable', 2)
        ok = True
    return ok

def ally_release():
    """Hand the Ally fans back to firmware automatic control.

    F3, and the reason SIGTERM used to produce the exact INVERSE of what an
    operator means by "quiet": bye() released the dock and did nothing to the
    Ally, so `systemctl stop glados-thermal` left the eGPU uncooled on the
    firmware curve AND the Ally pinned at 178-255 with pwm_enable=1,
    persistent until reboot. Loud and dangerous at the same time.

    UNVALIDATED: pwm{1,2}_enable = 0 is the documented "restore automatic"
    value for this driver but has never been exercised on this host. Best
    effort — a failure here is logged and changes nothing, which is exactly
    today's behaviour.
    """
    c = hwmon('asus_custom_fan_curve')
    if not c:
        return False
    ok = False
    for ch in (1, 2):
        if os.path.exists(f'{c}/pwm{ch}_enable') and \
                write(f'{c}/pwm{ch}_enable', 0):
            ok = True
    return ok

def ally_rpm():
    a = hwmon('asus')
    out = {}
    if not a:
        return out
    for key, f in (('cpu_fan_rpm', 'fan1_input'), ('gpu_fan_rpm', 'fan2_input')):
        try:
            out[key] = int(open(f'{a}/{f}').read().strip())
        except (OSError, ValueError):
            out[key] = None
    return out

# ----------------------------------------------------------------- dock fan
def find_dock():
    for hr in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        d = os.path.join(hr, 'device')
        try:
            rd = open(os.path.join(d, 'report_descriptor'), 'rb').read().hex()
            ue = open(os.path.join(d, 'uevent')).read()
        except OSError:
            continue
        hid = (re.search(r'HID_ID=(.*)', ue) or [None, ''])[1].lower()
        if '0631ff0980' in rd and '1970' in hid:      # FF31/0x80 + ITE 8910
            return f'/dev/{os.path.basename(hr)}'
    return None

def dock_cmd(fd, payload):
    b = bytearray(SIZE); b[0] = REPORT_ID; b[1:1 + len(payload)] = bytes(payload)
    fcntl.ioctl(fd, HIDSET, b); time.sleep(0.02)
    r = bytearray(SIZE); r[0] = REPORT_ID
    fcntl.ioctl(fd, HIDGET, r); return bytes(r)

def _dock_open(state):
    dev = find_dock()
    if not dev:
        if state.get('fd') is not None:
            try: os.close(state['fd'])
            except OSError: pass
            state['fd'] = None
            log('dock: detached')
        return None
    if state.get('fd') is None or state.get('dev') != dev:
        try:
            state['fd'] = os.open(dev, os.O_RDWR); state['dev'] = dev
            log(f'dock: attached at {dev}')
        except OSError as e:
            state['fd'] = None; log(f'dock: open failed: {e}'); return None
        _dock_learn_floor(state)
    return state['fd']


def _dock_learn_floor(state):
    """Read the device's own per-point duty floors once per attach —
    GetFanCurveLowerLimit is one HID round-trip, and the device's answer
    beats a recording of it. Falls back to the RECORDED floors
    (DOCK_DUTY_FLOOR, FINDINGS s10) when the read fails or parses as
    nonsense, and merges point-wise-UPWARD when the two disagree: the
    clamp exists so the daemon never logs a duty the hardware is not
    honouring, and the higher floor is the honest one either way."""
    floors = None
    try:
        r = dock_cmd(state['fd'], [0xD0, 0x04])
        if r[1] == 0xD0 and r[2] == 0x04:
            got = tuple(r[3:3 + len(DOCK_DUTY_FLOOR)])
            # Sanity, not superstition: s10 shows the floors ascending
            # with the temperature points. A descending or garbage reply
            # is a failed read, not a new fact about the hardware.
            if list(got) == sorted(got):
                floors = got
    except OSError:
        pass
    if floors is None:
        state['floor'] = DOCK_DUTY_FLOOR
        log(f'dock: GetFanCurveLowerLimit unreadable — clamping to the '
            f'recorded floors {DOCK_DUTY_FLOOR} (FINDINGS s10)')
    elif floors != DOCK_DUTY_FLOOR:
        merged = tuple(max(a, b) for a, b in zip(floors, DOCK_DUTY_FLOOR))
        state['floor'] = merged
        log(f'dock: device duty floors {floors} differ from the recorded '
            f'{DOCK_DUTY_FLOOR} — clamping to the higher of each: {merged}')
    else:
        state['floor'] = floors
        log(f'dock: device duty floors confirm the recorded {DOCK_DUTY_FLOOR}')

def apply_dock(state, curve, floor=None):
    """Assert ``curve`` on the dock and report the APPLIED PWM (byte[9]).

    byte[6] is the dock's temperature and it is garbage — 57 of the last 60
    ticks read 0C, and there is no dock tachometer at all. It is deliberately
    not returned: nothing may build a readout or a decision on it.
    """
    fd = _dock_open(state)
    if fd is None:
        return None
    try:
        # Clamped to BOTH floors at the hardware boundary: the operator's
        # (never_stops — no curve may stop this fan, however it got here)
        # and the device's (learned at attach, recorded as fallback). The
        # ladder is already built above both, so these bite only for a
        # curve the ladder never built or a device reporting HIGHER
        # acceptance floors — and then the curve we log is still the curve
        # the hardware runs.
        floors = state.get('floor') or DOCK_DUTY_FLOOR
        clamped = clamp_to_floor(never_stops(curve, floor), floors)
        payload = ([0xD1, 0x01] + [t for t, _ in clamped]
                   + [d for _, d in clamped])
        dock_cmd(fd, payload)
        s = dock_cmd(fd, [0xC0, 0x01, 0x01] + [0] * 12)
        return s[9]                             # applied PWM, and only that
    except OSError as e:
        log(f'dock: write failed ({e}) — will re-detect')
        try: os.close(fd)
        except OSError: pass
        state['fd'] = None
        return None

def dock_release(state):
    """DisableFanCurve — hand the dock back to its FIRMWARE curve.

    THE STOP HANDOFF, AND NOTHING ELSE. Called from bye() when this daemon
    is exiting: a dead process cannot re-assert a volatile curve, so the
    only honest exit is to give the hardware back to the owner that will
    still be there. It is NOT the resting state — the bottom of the ladder
    is a low CUSTOM curve, because the firmware curve is duty 1 until
    ~65 C and reaches 34% only at 98 C, which is the curve implicated in
    the 2026-08-07 SW CTF host power-off. Resting there would surrender
    the card at exactly the moment nobody is watching it.

    (An earlier revision rested here in `mode quiet`, on the claim that
    applied PWM was floored at 175/255 under any custom curve — "quiet
    dock" and "custom curve" mutually exclusive. Overturned twice since:
    a shaped custom curve was measured driving applied PWM to 115, and the
    old bottom rung drove it to 0 — a STOPPED dock with our curve still
    active, FINDINGS s13. Which is exactly why the resting state is now
    FLOOR_DUTY and never zero: reachable silence turned out to be real,
    and really dangerous.)
    """
    if state.get('fd') is not None:
        try: dock_cmd(state['fd'], [0xD1, 0x02])      # DisableFanCurve
        except OSError: pass

def dock_close(state):
    if state.get('fd') is not None:
        try: os.close(state['fd'])
        except OSError: pass
        state['fd'] = None


def apply_surfaces(ctx, state):
    """Assert each ladder's CURRENT rung on its own hardware, and record
    what came back. The two paths genuinely differ — an hwmon write per
    fan channel versus one HID transaction that answers with the applied
    duty — so this is a dispatch, not a duplicated policy: the curve each
    surface gets is chosen by the same indexing on both.

    Every surface is clamped to ITS OWN floor at the boundary, which is
    the last place a curve can be stopped from stopping a fan."""
    for key in SURFACE_KEYS:
        surf = SURFACES[key]
        curve = surf['ladder'][ctx['surfaces'][key]['rung']]
        if key == ALLY:
            ctx['ally_ok'] = apply_ally(curve, surf['floor_duty'])
        else:
            ctx['dock_pwm'] = apply_dock(state, curve, surf['floor_duty'])

# --------------------------------------------------------------- the socket
def listen():
    """Bind the control socket root:<SOCKET_GROUP> 0660, or return None.

    Never widens on failure. A missing group means 0600 (root only) and a
    loud line in the journal — a control socket that fell back to
    world-writable because a group was missing would hand the fan lever to
    every process on the box.
    """
    try:
        os.unlink(SOCKET_PATH)
    except OSError as e:
        if e.errno != errno.ENOENT:
            log(f'socket: cannot clear {SOCKET_PATH}: {e}')
            return None
    try:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # umask first: bind() creates the node, and between bind and chmod
        # the socket is otherwise live at the process umask.
        old = os.umask(0o177)
        try:
            srv.bind(SOCKET_PATH)
        finally:
            os.umask(old)
        srv.listen(4)
        srv.setblocking(False)
    except OSError as e:
        log(f'socket: cannot listen on {SOCKET_PATH} ({e}) — running WITHOUT '
            f'a control interface; fan curves still asserted')
        return None
    try:
        gid = grp.getgrnam(SOCKET_GROUP).gr_gid
        os.chown(SOCKET_PATH, 0, gid)
        os.chmod(SOCKET_PATH, SOCKET_MODE)
        log(f'socket: {SOCKET_PATH} root:{SOCKET_GROUP} '
            f'{oct(SOCKET_MODE)} — verbs: status, mode max, mode quiet')
    except (KeyError, OSError) as e:
        os.chmod(SOCKET_PATH, 0o600)
        log(f'socket: group {SOCKET_GROUP!r} unavailable ({e}) — socket left '
            f'0600 (root only). The UI will not be able to move the fans.')
    return srv

def serve_one(srv, ctx):
    """Accept one connection, answer one verb, close. Never raises."""
    try:
        conn, _ = srv.accept()
    except OSError:
        return
    try:
        conn.settimeout(2.0)
        raw = conn.recv(MAX_REQUEST)
        verb = raw.decode('utf-8', 'replace').strip()
        reply = handle(verb, ctx)
        conn.sendall((json.dumps(reply) + '\n').encode('utf-8'))
    except (OSError, socket.timeout):
        pass
    finally:
        try: conn.close()
        except OSError: pass

def handle(verb, ctx):
    """The whole protocol. Three verbs, no parameters, no exceptions.

    An unknown verb is refused with the list of known ones rather than
    ignored — a caller that misspells "quiet" must not be able to mistake
    silence for success.
    """
    if verb == 'status':
        return {'ok': True, **status(ctx)}
    if verb == 'mode max':
        # ALWAYS allowed and never gated. Raising fans is loud and wasteful
        # and cannot hurt anything; there is no state of the machine in
        # which refusing to cool it is the right answer. This is the
        # operator's PIN: automatic descent stays disabled until a granted
        # `mode quiet` (or the spin-up watchdog) releases it.
        #
        # BOTH LADDERS, because the verb takes no target and never will
        # (see 'mode quiet' below): an operator who asks this machine for
        # maximum is asking for every fan it has.
        for key in SURFACE_KEYS:
            st = ctx['surfaces'][key]
            if st['rung'] != RUNG_TOP:
                _record_step(ctx, SURFACES[key], st, now(), RUNG_TOP,
                             'socket: mode max')
            st['backoff_k'] = 0
            st['backoff_capped_logged'] = False
            st['probation_until'] = None
        ctx['pinned'] = True
        ctx['max_requested_at'] = now()
        ctx['watchdog_armed'] = True
        ctx['dirty'] = True
        log('socket: mode max (ladder pinned at the top; watchdog armed)')
        return {'ok': True, 'mode': MODE_MAX,
                'note': 'fans to maximum; automatic descent disabled until '
                        'mode quiet'}
    if verb == 'mode quiet':
        # The grant condition is the interlock's — since the load rework,
        # "no significant load" on the same evidence the governor acts
        # on, NOT "no engine process exists": a resident-but-idle model
        # must be able to coexist with quiet (the operator's named false
        # positive), and the exposure is one tick, because a grant only
        # releases the operator pin — the ladder still walks down one
        # gated step at a time and re-arms on the first sign of load.
        # Still held for QUIET_HOLD_S, still judged by this process
        # alone. A refusal changes nothing, exactly as before.
        #
        # SINCE THE SPLIT, "no significant load" IS TWO QUESTIONS — and
        # this verb still asks one. The pin is whole-machine and the verb
        # takes no target, so the grant is the AND of both surfaces and
        # the refusal names which one said no (`status` reports both
        # verdicts either way). `mode quiet dock` was considered and
        # refused: a verb that takes a target is a parameter by another
        # name, and this socket is reachable unauthenticated on the LAN
        # by explicit operator decision — the smallest possible
        # vocabulary is the whole defence. The cost of the AND is
        # bounded and self-correcting: a grant only releases the pin,
        # each ladder then descends on its own gates anyway, and the
        # spin-up watchdog releases an unfulfilled pin by itself.
        ok, reason, facts = interlock()
        held = ctx['interlock_ok_since']
        waited = 0.0 if held is None else now() - held
        if not ok:
            log(f'socket: mode quiet REFUSED — {reason}')
            return {'ok': False, 'refused': True, 'reason': reason,
                    'mode': mode_label(ctx), **facts}
        if waited < QUIET_HOLD_S:
            reason = (f'the interlock has held for {waited:.0f}s of the '
                      f'{QUIET_HOLD_S:.0f}s hold-down')
            log(f'socket: mode quiet held off — {reason}')
            return {'ok': False, 'refused': True, 'reason': reason,
                    'mode': mode_label(ctx), 'held_s': round(waited, 1),
                    **facts}
        ctx['pinned'] = False
        ctx['watchdog_armed'] = False
        ctx['dirty'] = True
        log(f'socket: mode quiet GRANTED — {reason} (held {waited:.0f}s); '
            f'automatic descent enabled from '
            f'{rung_summary(ctx)}')
        return {'ok': True, 'mode': mode_label(ctx), 'reason': reason,
                **facts}
    return {'ok': False, 'error': f'unknown verb {verb[:32]!r}',
            'verbs': ['status', 'mode max', 'mode quiet']}

def surface_status(ctx, surf, st, facts, quiet, t, floor_rung=None):
    """One ladder made legible: where it is, what holds its next step
    down, and how long. REPORTING only — it computes with the same
    descent_gate the governor acts on, so what it says is what the next
    tick will do, not a second copy of the policy that can drift."""
    if st['verify_failed']:
        gate = GATE_VERIFY
        blocked = (f'VERIFICATION FAILURE — {st["verify_failed"]} '
                   f'(descent refused until readings recover)')
        dwell_remaining = 0.0
    elif ctx['pinned']:
        gate = 'pinned'
        blocked = 'pinned at max by mode max (mode quiet re-enables descent)'
        dwell_remaining = 0.0
    elif st['rung'] >= surf['bottom']:
        gate = 'bottom'
        blocked = 'at the bottom of the ladder'
        dwell_remaining = 0.0
    else:
        allowed, gate, why, dwell_remaining = descent_gate(surf, st, facts, t,
                                                           floor_rung)
        blocked = '' if allowed else why
    quiet_ok, quiet_why = quiet
    return {
        'mode': surface_mode(surf, st),
        'label': surf['label'],
        'curve': surf['ladder'][st['rung']],
        'rung': st['rung'],
        'rung_count': surf['rungs'],
        # The control temperature this ladder is thresholded against, and
        # which fact it came from — so a reader never has to guess which
        # sensor a rung is answering to.
        'temp_c': surface_temp(surf, facts),
        'temp_name': surf['temp_name'],
        'floor_duty': surf['floor_duty'],
        # '' means nothing blocks: the next tick steps down. The gate is
        # the stable token ('load', 'dwell', ...); blocked_by carries
        # the numbers.
        'descent_gate': gate,
        'descent_blocked_by': blocked,
        'dwell_remaining_s': round(dwell_remaining, 1),
        'backoff_mult': min(BACKOFF_FACTOR ** st['backoff_k'],
                            BACKOFF_MAX_MULT),
        # The closed loop's verdict for THIS surface: None when its
        # readings match what was asked, else the failure being screamed
        # about (FINDINGS s13).
        'verify_failed': st['verify_failed'] or None,
        'quiet_allowed': quiet_ok,
        'quiet_reason': quiet_why,
        'quiet_c': surf['quiet_c'],
        # None unless something outside this surface's own signals is
        # holding a floor under it (the eGPU-load coupling, on the Ally).
        'floor_rung': floor_rung,
    }


def status(ctx):
    """The whole machine: BOTH ladders under `surfaces`, plus the
    machine-level state (the pin, the hold-down, the raw readings) and
    the aggregate keys every existing consumer already reads.

    The top-level `mode`/`rung`/`descent_gate` keys are AGGREGATES over
    the surfaces — the loudest one wins, so a reader who looks at nothing
    else is never told a quieter story than the truth. Per-surface truth
    is in `surfaces`, and that is the block to read since 2026-08-13."""
    t = now()
    facts = read_facts()
    verdicts = quiet_verdicts(facts)
    refused = [f'{k}: {why}' for k, (ok, why) in verdicts.items() if not ok]
    quiet_allowed = not refused
    reason = ('; '.join(refused) if refused else
              'no significant load, both surfaces cool, sensors readable')
    held = ctx['interlock_ok_since']
    floor = coupled_floor(ctx, t)
    per = {k: surface_status(ctx, SURFACES[k], ctx['surfaces'][k], facts,
                             verdicts[k], t, floor if k == ALLY else None)
           for k in SURFACE_KEYS}
    # Which surface speaks for the machine in the flat keys: a
    # VERIFICATION FAILURE first — the hardware not obeying is a bigger
    # fact than any rung or any pin, and a reader who checks only the
    # flat keys must not be shown a tidy story while a fan is stopped —
    # then the loudest, lowest rung index being the most cooling.
    loudest = min(per.values(),
                  key=lambda s: (s['verify_failed'] is None, s['rung']))
    return {
        'surfaces': per,
        'mode': mode_label(ctx),
        'rung': loudest['rung'],
        'rung_count': loudest['rung_count'],
        'curve': loudest['curve'],
        'descent_gate': loudest['descent_gate'],
        'descent_blocked_by': loudest['descent_blocked_by'],
        'dwell_remaining_s': loudest['dwell_remaining_s'],
        'backoff_mult': loudest['backoff_mult'],
        'pinned': ctx['pinned'],
        'ally_applied': ctx['ally_ok'],
        'dock_attached': ctx['dock_pwm'] is not None,
        # byte[9], the APPLIED pwm. The dock reports no tachometer and its
        # temperature byte is garbage, so this single number is the entire
        # ground truth available about that fan.
        'dock_applied_pwm': ctx['dock_pwm'],
        # Aggregated so a single failing surface cannot hide behind a
        # healthy one; per-surface detail is in `surfaces`.
        'verify_failed': '; '.join(
            s['verify_failed'] for s in per.values()
            if s['verify_failed']) or None,
        'verify_fail_ticks': VERIFY_FAIL_TICKS,
        **ally_rpm(),
        'quiet_allowed': quiet_allowed,
        'quiet_reason': reason,
        'quiet_held_s': (None if held is None
                         else round(time.monotonic() - held, 1)),
        'quiet_hold_required_s': QUIET_HOLD_S,
        'quiet_edge_c': SURFACES[DOCK]['quiet_c'],
        'quiet_junction_c': SURFACES[DOCK]['ceiling'][1],
        # The one cross-surface fact, reported where an operator watching
        # a live inference run will look for it: how long the dock has
        # been at maximum under eGPU load, and whether that has floored
        # the Ally yet.
        'coupled_held_s': (None if ctx['coupled_since'] is None
                           else round(t - ctx['coupled_since'], 1)),
        'coupled_floor_rung': floor,
        'couple_hold_required_s': COUPLE_HOLD_S,
        'watchdog_armed': ctx['watchdog_armed'],
        'watchdog_s': SPINUP_WATCHDOG_S,
        **facts,
    }

def banner_lines():
    """The startup decoder ring — ONE LINE PER LADDER plus one for what
    they share, so every `ally=rung2/4` and `dock=rung0/4` that follows
    in the journal is a fact rather than a riddle. Which sensor drives
    each ladder, its entry thresholds, what each rung asks at idle, what
    takes it to the top, and what it verifies."""
    lines = [
        f'SystemThermalDaemon: TWO ladders — ally (cpu_fan+gpu_fan, one '
        f'package) and dock (XG Mobile) — one per cooling surface '
        f'(unit glados-thermal, socket {SOCKET_PATH})']
    for key in SURFACE_KEYS:
        surf = SURFACES[key]
        lines.append(
            f'ladder[{key}]: {surf["label"]}, driven by '
            f'{surf["temp_name"]} | ' + ' '.join(
                (f'rung{r}=max(idle {surf["ladder"][r][0][1]})' if e is None
                 else f'rung{r}<={e:.0f}C(idle {surf["ladder"][r][0][1]})')
                for r, e in enumerate(surf['ladder_c']))
            + f' | up: straight to rung {RUNG_TOP} on {surf["temp_name"]} '
              f'rate +{surf["rise_trip_c"]:.1f}C/'
              f'{surf["rise_window_s"]:.0f}s, an unreadable '
              f'{surf["temp_name"]}, '
            + ', '.join(f'{k}>={th:g}' for k, th, _ in surf['load'])
            + (f', {surf["ceiling"][2]}>{surf["ceiling"][1]:.0f}C'
               if surf['ceiling'] else '')
            + ' | verify: '
            + ', '.join(name for _, _, name in surf['verify'])
            + f', {VERIFY_FAIL_TICKS} strikes -> max | quiet needs '
              f'{surf["temp_name"]}<={surf["quiet_c"]:.0f}C')
    lines.append(
        f'shared: down one rung per >={MIN_STEP_DWELL_S:.0f}s on '
        f'{STABILITY_WINDOW_S:.0f}s stability, backoff to '
        f'{MIN_STEP_DWELL_S * BACKOFF_MAX_MULT:.0f}s, hysteresis '
        f'{STEP_HYSTERESIS_C:.0f}C | engine comm '
        f'{"/".join(ENGINE_COMM_HINTS)}* appearing -> rung '
        f'{PREPOSITION_RUNG} on both (hint only) | mode quiet needs BOTH '
        f'surfaces | coupling: dock at max under eGPU load for '
        f'{COUPLE_HOLD_S:.0f}s floors ally at rung {COUPLE_FLOOR_RUNG}')
    lines.append('max curve (both surfaces): ' + ' '.join(
        f'{t}C:{d}' for t, d in SURFACES[ALLY]['max_curve']))
    return lines


def tick_line(ctx, facts, rpm, reason, t, quiet_ok=False):
    """THE tick line: one line, both ladders, greppable.

    `mode=` first because ten months of journal habit and every existing
    grep start there, then one field per ladder — `ally=rung2/4`,
    `dock=rung0/4` — each carrying its own gate token, its own readings
    and its own reason, then the quiet verdict. It is longer than the
    single-ladder line it replaces because there are two ladders now;
    it is NOT two lines, and it is not twice as long: the shared
    machinery is printed once at startup (banner_lines) and never
    repeated here.

    Called for its string; the caller decides whether this tick has
    earned a line. It also updates each surface's `logged_gate`, which
    is what "earned" means — a gate CHANGING is news, a countdown inside
    one gate is not."""
    floor = coupled_floor(ctx, t)
    fields = []
    for key in SURFACE_KEYS:
        surf, st = SURFACES[key], ctx['surfaces'][key]
        gate, phrase = descent_phrase(ctx, surf, st, facts, t,
                                      floor if key == ALLY else None)
        if gate != st['logged_gate']:
            st['logged_gate'] = gate
            ctx['dirty'] = True
        # The readings are the ones verify_tick judged this tick — on an
        # escalation tick the line shows the FAILING readings, not a
        # re-read that might have recovered.
        if key == ALLY:
            seen = (f'{"ok" if ctx["ally_ok"] else "FAIL"} '
                    f'{rpm.get("cpu_fan_rpm")}/{rpm.get("gpu_fan_rpm")}rpm')
        else:
            seen = (f'pwm={ctx["dock_pwm"]}'
                    if ctx['dock_pwm'] is not None else 'absent')
        fields.append(f'{key}=rung{st["rung"]}/{surf["bottom"]} '
                      f'gate={gate} {seen} {phrase}')
    # The quiet verdict gets a greppable token of its own rather than a
    # bare sentence: `quiet=no` is the thing an operator scans for, and
    # the reason after it already names the surface that refused.
    verdict = 'quiet=yes' if quiet_ok else f'quiet=no {reason}'
    return (f'mode={mode_label(ctx)} | ' + ' | '.join(fields)
            + f' | {verdict}')


# --------------------------------------------------------------------- main
def main():
    # The tuning source, resolved before anything is asserted so the very
    # first curve applied is already the operator's — and logged first,
    # so every number the banner prints is traceable to its source.
    init_config()
    state = {'fd': None, 'dev': None}
    # Boot state: top of the ladder, unpinned — full cooling now, automatic
    # descent as soon as the gates pass. See fresh_ctx for why.
    ctx = fresh_ctx(now())

    def bye(*_):
        # Both fans released, not just the dock (F3). Releasing one and
        # holding the other is how `systemctl stop` used to produce the
        # exact inverse of "quiet": eGPU uncooled AND the machine loud.
        dock_release(state)
        dock_close(state)
        ally_release()
        try: os.unlink(SOCKET_PATH)
        except OSError: pass
        log('stopped — dock returned to firmware curve, Ally to automatic')
        os._exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    srv = listen()
    for line in banner_lines():
        log(line)
    tick = 0
    while True:
        # profile: performance also moves throttle_thermal_policy to 1
        write('/sys/firmware/acpi/platform_profile', 'performance')
        # eGPU must stay out of D3cold or its sensors and fan go away — and
        # the interlock is built on those sensors, so this is asserted at
        # every rung. A passive card whose thermometer has been suspended
        # is the one state from which nothing can be verified.
        write(f'{PCI_ROOT}/{EGPU_PCI}/power/control', 'on')

        facts = read_facts()
        verdicts = quiet_verdicts(facts)
        refused = [f'{k}: {why}'
                   for k, (vok, why) in verdicts.items() if not vok]
        ok = not refused
        reason = ('; '.join(refused) if refused else
                  'no significant load, both surfaces cool, sensors readable')
        t = now()
        if ok and ctx['interlock_ok_since'] is None:
            ctx['interlock_ok_since'] = t
        elif not ok:
            ctx['interlock_ok_since'] = None

        maybe_release_watchdog(ctx, ok, t)

        # THE LADDER IS CONDITIONAL, NOT A LATCH — in both directions.
        # Somebody starting a model from the CLI while the fans are low
        # must not be able to leave them low, and a re-arm that outlives
        # its cause must not be able to hold the top for ten hours. One
        # function moves the rung, on evidence, every tick.
        govern(ctx, facts, t)

        # EVERY rung is a custom curve on its own surface, the bottom
        # included, and the two surfaces are now at INDEPENDENT rungs.
        # The dock is never released to firmware while this daemon runs
        # (that happens once, in bye(), when nothing will re-assert): the
        # firmware curve is the one that powered this host off, and the
        # floors that made release look necessary were a misreading —
        # FINDINGS s10. Re-asserted every tick because the dock curve is
        # volatile.
        apply_surfaces(ctx, state)

        # CLOSED LOOP (FINDINGS s13): read back what the hardware is
        # actually doing and never assume the curve was obeyed. On the
        # tick a failure latches, the escalation is applied IMMEDIATELY —
        # same tick, no dwell — not on the next re-assert, and only on
        # the surface whose reading failed.
        rpm = ally_rpm()
        readings = dict(rpm, dock_pwm=ctx['dock_pwm'])
        if verify_tick(ctx, readings, t):
            apply_surfaces(ctx, state)

        # The tick line answers "why is it loud RIGHT NOW?", for both
        # ladders, in one line. Built first (it is what notices a gate
        # CHANGING, which is itself a reason to log), printed when
        # something changed — a step, a verb, a gate — or on the ~5 min
        # heartbeat. The ten-hour incident printed ten hours of
        # identical, intent-free lines; the fix is a line whose every
        # change earns its place, not more lines, and certainly not two
        # lines where there was one.
        line = tick_line(ctx, facts, rpm, reason, t, ok)
        if ctx['dirty'] or tick % 30 == 0:        # ~5 min at 10 s
            log(line)
            ctx['dirty'] = False
        tick += 1

        # select() instead of sleep(): the re-assert cadence is unchanged,
        # but a control request is answered within milliseconds instead of
        # up to ten seconds later. Single-threaded on purpose — the fan
        # state is touched from exactly one place, so there is no lock to
        # get wrong and no way for a request to land mid-curve-write.
        deadline = time.monotonic() + REASSERT
        while srv is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([srv], [], [], remaining)
            except (OSError, InterruptedError):
                break
            if not ready:
                break
            serve_one(srv, ctx)
            if ctx['dirty']:
                break            # apply the new mode now, not in 10 s
        if srv is None:
            time.sleep(REASSERT)

if __name__ == '__main__':
    main()
