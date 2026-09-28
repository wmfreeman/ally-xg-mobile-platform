# ally-xg-mobile-platform

Rebuilds an **ASUS ROG Ally (Z1 Extreme) + XG Mobile eGPU** box from a fresh
Ubuntu install back to a working, thermally-safe inference platform.

Clone it onto a newly-imaged machine, run one script, reboot once, and the
host is back: fan authority installed and enabled, OverDrive unlocked, ROCm
and Vulkan in place, llama.cpp built for both GPUs, and the eGPU handled
correctly whether the dock is attached at boot or plugged in later.

```bash
git clone https://github.com/<you>/ally-xg-mobile-platform.git
cd ally-xg-mobile-platform
sudo ./install.sh
```

Run it **from your own account via sudo**, not as root — several stages need
to know which human account owns the `render`/`video` groups and the
llama.cpp checkout.

---

## Why this repo exists

On **2026-08-07** the host hard-powered-off mid-inference:

```
amdgpu 0000:03:00.0: ERROR: GPU over temperature range(SW CTF) detected!
amdgpu 0000:03:00.0: ERROR: System is going to shutdown due to GPU SW CTF!
```

SW CTF debounces 50 ms and then calls `orderly_poweroff()` on the **host** —
no throttle stage, no GPU-only reset. The cause was two **capped** fan
curves, not slow ones:

| Surface | Cap as shipped |
|---|---|
| Ally `asus_custom_fan_curve` | `cpu_fan` 66/255 (26 %), `gpu_fan` 2/255 |
| XG Mobile dock | `table[2]` flat at 34/255; `table[3]` reaches 87/255 (34 %) only at 98 °C — two degrees under the CTF |

Everything here exists so that cannot happen again. The centerpiece is a
daemon that owns every thermal knob on the box and re-asserts on a loop.

---

## What gets installed

| Stage | What it does |
|---|---|
| `00-preflight` | Read-only. Proves this is the right platform; refuses to install something inert. |
| `10-packages` | Build toolchain, Vulkan runtime + `vulkaninfo`, `linux-firmware`, utilities. |
| `15-drivers` | Verifies the in-tree modules and amdgpu firmware blobs. Installs nothing but firmware. |
| `20-rocm` | ROCm 7.2.4 from AMD's apt repo; adds you to `render` + `video`. |
| `30-kernel-params` | `amdgpu.ppfeaturemask=0xfff7ffff` via GRUB. **Needs a reboot.** |
| `40-thermal` | SystemThermalDaemon → `glados-thermal.service`, enabled at boot. |
| `45-egpu-attach` | Role resolver + platform tools; removes legacy udev rules. |
| `50-llama` | llama.cpp, pinned, built for HIP (`gfx1031;gfx1103`) **and** Vulkan. |
| `60-verify` | Read-only pass/fail over the whole platform. |

```bash
sudo ./install.sh --list          # show stages
sudo ./install.sh --only 40       # one stage
sudo ./install.sh --from 40       # stage 40 onward
sudo ./install.sh --skip 50       # everything but the long build
sudo ./install.sh --verify        # read-only health check
sudo ./install.sh --uninstall     # remove the daemon, restore factory curves
```

Every stage is independently re-runnable. Re-running a completed stage
reports `[skip]` rather than repeating work.

---

## Drivers and firmware

**There is no driver to install.** Every driver this platform needs is an
in-tree kernel module shipping with `linux-image`:

| Module | Provides |
|---|---|
| `amdgpu` | both GPUs |
| **`asus_nb_wmi`** | the `asus` hwmon (fan RPM), **`asus_custom_fan_curve`** (the only way to set the Ally curve), **and `egpu_enable`** (the PCIe gate) |
| `asus_wmi` | the generic half of the above |
| `asus_armoury` | newer ROG attributes |
| `hid_asus` | N-KEY device |

No DKMS, no out-of-tree build, no vendor package. `asus_nb_wmi` carries three
things this platform cannot work without, which is why stage 15 checks it
explicitly rather than assuming it loaded.

**Firmware is a separate package and is genuinely fatal if missing.** amdgpu
loads signed blobs from `/lib/firmware/amdgpu/` at probe:

| GPU | Blob family |
|---|---|
| eGPU — Navi 22 | `navy_flounder_*` |
| iGPU — Phoenix | `gc_11_0_1_*` |

Without them the card enumerates on the bus and then **fails to initialise** —
a different and more confusing failure than the card not being there. Stage 10
installs `linux-firmware`; stage 15 verifies both families are actually present
and checks the boot log for load failures.

Stage 15 also catches the silent killer: a **blacklisted** `asus_wmi` /
`asus_nb_wmi`. That failure is total and says nothing in the journal — no fan
curve, no gate, no explanation.

> **Kernel version.** Validated on **7.0.0-34-generic**. A stock Ubuntu 24.04
> install ships 6.8. Stage 15 tests for the *attributes* rather than trusting a
> version number; if `egpu_enable` or `asus_custom_fan_curve` is absent, it
> tells you to install `linux-generic-hwe-24.04` and re-run.

## The thermal daemon

Installed as `glados-thermal.service`, control socket
`/run/glados-thermal.sock` (root:*group*, 0660).

It is the **single owner** of: the Ally's `cpu_fan` + `gpu_fan`
(`asus_custom_fan_curve`), the XG Mobile dock fan (USB HID report `0x5E` on
the ITE 8910, usage page `0xFF31`/`0x80`), the platform profile, and the
eGPU's runtime PM.

### Two cooling surfaces, two ladders

The seam is **not** "CPU fan vs GPU fan". The Ally's two fans cool *one
package* (the Z1 Extreme, GPU on the same die), so they are never split from
each other. The genuinely independent surface is the **dock**, cooling the
eGPU in its own enclosure:

| Ladder | Fans | Driven by |
|---|---|---|
| `ally` | `pwm1` + `pwm2` on `asus_custom_fan_curve` | CPU Tctl (k10temp) and its rate of rise |
| `dock` | XG Mobile over HID `0x5E` | eGPU `power1_average`, `gpu_busy_percent`, edge, junction, and their rates |

Both are instances of one parameterised mechanism. Ascent is **total and
immediate**; descent is **gated and slow** (60 s dwell per rung, plus a
quiet-hold). That asymmetry is the whole safety argument, and it is why the
fans run hard for several minutes after a dock attach before settling.

### The floor is structural

Fans never stop. `floor_duty = 45`, and `never_stops()` / `clamp_to_floor()`
are deliberately **unreachable from the config** — `install_config()`
re-proves them on the built ladder and *refuses* rather than installs. A
config can move the floor; it cannot remove it.

Measured resting floor on this platform: **dock PWM 131 (~51 %), Ally
3900 rpm**.

### Tuning

Edit `/etc/system-thermal-daemon.conf`, then
`sudo systemctl restart glados-thermal`. A bad file is refused **loudly**
and the compiled-in defaults run — check the journal for `CONFIG REFUSED`.
Reinstalling never overwrites an existing config.

> **Deployed names are load-bearing.** The socket path is compiled into the
> daemon, and external consumers connect to it by that exact path. Renaming
> the unit without rebuilding the daemon and every consumer gives you a host
> whose fans run but whose tools cannot see them.

---

## How eGPU attach actually works

Three separate conditions. Confusing any two of them wastes an afternoon.

### 1. Physically attached — `egpu_connected`

`asus-nb-wmi` exposes a read-only `egpu_connected`. When it reads `1` the
dock is plugged in and powered, and its fan HID enumerates over USB. **This
says nothing about the PCIe link.**

### 2. Gate open — `egpu_enable`

`/sys/bus/platform/devices/asus-nb-wmi/egpu_enable` is **writable**, and must
be `1` for the PCIe interface to come up at all. It does **not persist across
a reboot**. Observed on this host:

```
19:57:49  pciehp: Slot(0): Card present   ← dock attached at boot
19:57:49  pciehp: Slot(0): No link        ← but the gate was closed
20:00:36  pciehp: Slot(0): Link Up        ← after egpu_enable=1
20:00:36  pci 0000:03:00.0: [1002:73df]   ← card enumerates, amdgpu binds
```

So a box rebooted with the dock attached comes back with the dock powered,
its fan spinning, `egpu_connected=1` — and **nothing on the PCI bus**. That
is the most confusing state this platform has.

```bash
sudo egpu-enable.sh              # open the gate, wait for the link
sudo egpu-enable.sh --status     # report only, change nothing
```

`egpu-enable.service` is installed and enabled by stage 45 so this happens
at boot, ordered before the thermal daemon. It exits cleanly when no dock is
attached.

> **A bus rescan does not help.** `echo 1 > /sys/bus/pci/rescan` re-runs
> enumeration over links that are *already up*; it cannot train a link the
> firmware is holding down. The gate is the lever, not the rescan. With the
> gate closed, buses 01–05 — the dock's reserved window under root port
> `00:01.1` — are simply empty.

### 3. Awake — runtime PM

Once enumerated the card returns with runtime PM `auto`, drops to **D3cold**
at idle, and every hwmon attribute then returns `EBUSY` — indistinguishable
from a dead sensor, and it stops the fan being pre-spun before load arrives.

The daemon fixes this **on a loop, not once**: every 10 s tick it re-asserts
`power/control=on` and re-detects the dock HID. This one needs nothing
installed and heals itself within a tick.

**There is deliberately no udev rule for the fan.** Older installers shipped
one that restarted the thermal service on hotplug; once the re-assert loop
existed it bought nothing, cost a curve gap while the unit bounced, and
hardcoded a unit name that was wrong wherever the daemon ran under another.
Stage 45 *removes* any such rule left behind.

---

## Never hardcode a GPU index

HIP and Vulkan enumerate devices in **different orders**, and neither is
stable across reboots or hotplug.

Measured on this host on **2026-08-08**:

| Role | HIP | Vulkan |
|---|---|---|
| `egpu` | 0 | 1 |
| `igpu` | 1 | 0 |

Measured again on **2026-09-28**, after a reboot:

| Role | HIP | Vulkan |
|---|---|---|
| `egpu` | **1** | 1 |
| `igpu` | **0** | 0 |

The eGPU moved from HIP index 0 to 1, and the two backends — previously
inverted relative to each other — happened to agree. Anything that had
persisted `HIP_VISIBLE_DEVICES=0` would have silently landed on the 4 GiB
iGPU and OOMed a 14B model in the most confusing way available.

**A role's identity is its gfx target + PCI address, resolved per launch:**

```
egpu -> gfx1031 @ 0000:03:00.0   (RX 6850M XT, 12 GiB)
igpu -> gfx1103 @ 0000:09:00.0   (Radeon 780M,   4 GiB)
```

```bash
gpu_resolve.sh egpu check                      # identity + both indices
HIP_VISIBLE_DEVICES=$(gpu_resolve.sh egpu index)      ~/llama.cpp/build/bin/llama-cli ...
GGML_VK_VISIBLE_DEVICES=$(gpu_resolve.sh egpu vkindex) ~/llama.cpp/build-vk/bin/llama-cli ...
```

`lib/gpu_roles.py` is the Python equivalent, installed to
`/usr/local/lib/ally-platform/`. It verifies **both** halves of the identity
and raises `GpuRoleUnavailable` loudly with no fallback — the `egpu` role
quietly landing on the iGPU is exactly the failure it exists to prevent.

Set **`HIP_VISIBLE_DEVICES` only.** It and `ROCR_VISIBLE_DEVICES` *compose*:
ROCR filters first and reindexes the survivor to 0, then HIP asks for index 1
of a one-device list. Setting both works by accident whenever the target is
index 0, which is how it survives casual testing.

---

## Tools

| Command | What it tells you |
|---|---|
| `egpu-enable.sh` | Opens the PCIe gate and waits for link-up. `--status`, `--disable`, `--timeout N`. |
| `egpu-status.sh` | One screen: gate, PCI, driver, render nodes, ROCm/Vulkan, both fan ladders, thermals. No root. |
| `egpu-wait.sh [secs]` | Blocks until the card is genuinely ready (4 independent checks). Exit 1 on timeout. Writes nothing. |
| `gpu_resolve.sh <role> <verb>` | `index`, `vkindex`, `gfx`, `pci`, `render`, `check`, `vkcheck`. |
| `gpu-watch.sh [out.csv]` | Per-second telemetry during a live run. |
| `thermal-guard.sh <cmd>` | Runs a job under a junction-temperature guard. |
| `xgm-fan.py`, `xgm-fan-probe.py`, `xgm-fan-hold.py` | Direct dock HID tools — diagnostics only. |

> The dock tools write to the same fan the daemon owns. Using them while the
> daemon runs makes you a second controller on one knob — the failure mode
> this project has hit twice. Stop the service first, or stick to
> `egpu-status.sh`.

---

## Verifying a rebuild

```bash
sudo ./install.sh --verify
```

Checks the unit is **enabled** (not just running), the config was accepted,
the socket answers, `ppfeaturemask` actually took, ROCm and Vulkan enumerate,
group membership, tools on PATH, no legacy udev rule, and both llama.cpp
binaries exist.

eGPU-dependent checks report **SKIP**, never FAIL, when the dock is
detached — the platform is expected to be correct with the dock off.

---

## Known traps

- **The dock can be attached and still invisible.** `egpu_enable` does not
  survive a reboot. If `lspci` shows no `03:00.0` but the dock is plugged in
  and its fan is running, the gate is closed — `sudo egpu-enable.sh`. A PCI
  rescan will not fix it. `egpu-enable.service` handles this at boot; it is
  the thing to check first if the card is missing after a rebuild.
- **Reboot after stage 30.** Until `ppfeaturemask` takes, `power1_cap` reads
  `min == max == 165 W` and `pp_od_clk_voltage` is absent. The limits look
  hard-locked by the vBIOS when they are locked by a *default*.
- **Log out after install.** `render`, `video` and the socket group only
  apply on next login. Symptom: HIP and Vulkan both report "no device" for
  you while root sees the card perfectly.
- **gfx1031 is not on AMD's supported list.** It works on ROCm 7.2.4 with
  **no** `HSA_OVERRIDE_GFX_VERSION` — verified on the running host. That is a
  property of this version, not a guarantee. If a ROCm bump breaks
  enumeration, try `HSA_OVERRIDE_GFX_VERSION=10.3.0` and record the result.
- **hwmon indices move** across reboots and hotplug. Always resolve by PCI
  address, never by `hwmonN`.
- **Don't trust a dock fan readback.** `SetFanCurve` writes the active curve,
  which `GetFanCurveTable` cannot read back. Trust the fan, not the readback.
  The curve is volatile — hence the re-assert loop.
- **`ppt_*` is deliberately left alone.** Those read 5 while the APU was
  measured drawing 21 W, the units are undocumented, and whether RC71L
  firmware clamps an out-of-design value is unknown.
  `platform_profile=performance` is the supported lever and is what's used.
- **Both GPUs can serve concurrently, at a cost.** eGPU decode 35.4 → 27.7 t/s
  (−22 %), iGPU 82.2 → 79.9 t/s (−3 %); eGPU peak 54 °C / 162 W. They share
  PCIe and memory bandwidth.

---

## Layout

```
install.sh              orchestrator
lib/common.sh           shared helpers, platform constants
lib/gpu_roles.py        role resolver (Python)
stages/                 00-preflight … 60-verify
thermal/bin/            system-thermal-daemon.py
thermal/config/         system-thermal-daemon.conf (shipped defaults)
thermal/systemd/        glados-thermal.service, egpu-enable.service
bin/                    platform tools → /usr/local/bin
docs/FINDINGS.md        the measurements and dead ends behind every choice
docs/THERMAL-GUARD.md   the inference-time guard
docs/HARDWARE-STATE.md  the captured state this repo reproduces
```

`docs/FINDINGS.md` is the real reference — it records what was measured,
what was tried and rejected, and why each number is the number. Read it
before changing any threshold.
