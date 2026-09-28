# Thermal guard — design brief

How to keep a sustained GPU workload alive on hardware that powers off the host
when it overheats. Written from a working implementation, generalised so another
project can build its own.

Read [`FINDINGS.md`](FINDINGS.md) first — sections 1, 4 and 9 are the evidence
behind every number here.

---

## The problem this solves

```
amdgpu: ERROR: GPU over temperature range(SW CTF) detected!
amdgpu: ERROR: System is going to shutdown due to GPU SW CTF!
```

50 ms of debounce, then `orderly_poweroff(true)` on the **host**. Not a GPU
reset, not a clock throttle. The session dies, and so does anything unsynced.

On hardware where you cannot cap power or clocks (see FINDINGS §5), **duty cycle
is the only control**, and it has to live in software.

## Two findings that will cost you a day if you skip them

### 1. Gate on EDGE, never on JUNCTION

Junction (`temp2_input`) is a hotspot — an instantaneous maximum across on-die
sensors. It collapses in the gap between compute kernels and spikes straight
back. Sampled at 0.5 s during real load:

| | max jump per 0.5 s sample | peak under load |
|---|---|---|
| junction | **30 °C** | 94 °C |
| edge (`temp1_input`) | **4 °C** | 68 °C |

A guard gating on junction paused at 84 °C and logged *"resumed at 66 °C after
0.5 s"* — straight back into full load, and the watchdog caught it at 94 °C.
Junction read 66 and 94 two seconds apart with edge unchanged at 65.

**Edge tracks the heatsink and is the control signal. Junction is an emergency
ceiling only.** They differ by ~26–30 °C under load, so a threshold set on one
is meaningless on the other — always name which sensor a decision used, in the
log line and in any trace.

### 2. Sampling interval is load-bearing

At the measured ~0.76 °C/s junction ramp, a 2-second poll overshot an 88 °C
threshold to **91 °C**. Use **0.5 s**. Put the arithmetic in a comment so nobody
relaxes it later.

## The contract

**Pause, never abort.** A thermal event should cost seconds, not a session.
Aborting is reserved for "we waited and it did not cool."

**Never silent.** A pause that looks like a hang is a bug. Say the numbers.

## Structure

Gate **every** call into the accelerator at one chokepoint. Not at the top of a
public method — at the point that actually issues work, because a *retry* is
another call. Pin it with a test that fails if any new call site bypasses the
guard; a guard something can route around is decoration.

```
before_call():
    while control_sensor >= pause_c:
        if waited > max_wait:  end cleanly, report what was gathered
        emit a live-updating event; sleep poll_s
    proceed

during streaming:
    control_sensor >= midstream_c  ->  stop, KEEP partial output, mark incomplete
    ceiling_sensor  >= abort_c     ->  same, independently
```

A pre-call check alone is **not sufficient**: edge climbs ~1.1 °C/s under load,
so a 20-second generation starting at edge 48 finishes near 70. You must sample
mid-stream too.

## Thresholds, and how to derive your own

Measured for a 165 W eGPU with fans forced to 100% (FINDINGS §9):

| | value | basis |
|---|---|---|
| pause | edge 58 °C | ≈ junction 84 under load |
| resume | edge 48 °C | ~20 s recovery at ~1 °C/s |
| mid-stream stop | edge 68 °C | observed equilibrium under continuous load |
| ceiling | junction 95 °C | emergency only; must tolerate 30 °C spikes |
| poll | 0.5 s | see above |
| max wait | 300 s | then end the turn cleanly |

To derive these for other hardware: log edge, junction and power at 0.5 s under
a real workload; find the **edge** value that corresponds to your junction
target under load; set resume ~10 °C below pause so pauses are ~20 s rather than
thrashing.

**Make them configuration, not literals.** They will be wrong the first time.

## What the operator sees

A pause is normal operation on thermally-constrained hardware, not an incident.
Never *error*, *failure*, *critical*, or *warning* for a pause — reserve
alarming language for the timeout, which genuinely is one. Always print the real
numbers; "cooling off" alone is mysterious.

```
cooling     GPU edge 59°C — cooling off, resuming at 48°C · 14s (junction 91°C)
cooling     resumed at edge 47°C after 18.5s (junction 88°C)
timeout     the GPU did not cool below 48°C within 5m (still 64°C). Stopping.
            Check airflow is not obstructed and the fan service is running.
abort       stopped generating at edge 69°C — the output below is INCOMPLETE.
summary     thermal: 4 pauses, 68s total, peak edge 68°C
```

Name the gated channel first with the other alongside — they differ by ~30 °C,
and someone who has only ever seen the junction figure will read a correct
`edge 59` as broken.

**Record paused time separately from elapsed.** A turn that took 40 s because it
waited 20 s must say so, or every later performance comparison is polluted.

## Failure modes to handle

- **No sensor** → guard disabled, and it must **say so** in readiness. Never
  fail open silently.
- **Sensor disappears mid-run** (dock unplugged, runtime suspend) → degrade to
  disabled after a few failed reads, log once, do not kill the work. A sensor
  that vanishes *mid-pause* must release the wait rather than block on a number
  nobody can read.
- **Runtime PM** → an idle device may drop to D3cold and return `EBUSY` on every
  attribute, which reads exactly like a dead sensor. Pin `power/control = on`.

## Keep an external watchdog anyway

The guard runs inside the process doing the work. Put a second, independent
watchdog outside it, above the guard's ceiling and below the hardware trip, that
kills the job. If it ever fires, the guard failed — which is worth knowing.

Everything it writes should be `fsync`'d. A host power-off takes the page cache
with it, and the log of the run that killed the machine is the one you want.

## What this does not do

It does not make the hardware faster or cooler. It trades **throughput for
uptime** — in the reference measurements, roughly half the wall clock on
back-to-back work became waiting. For bursty interactive use it rarely fires at
all; for continuous batch work it is the difference between finishing slowly and
not finishing.
