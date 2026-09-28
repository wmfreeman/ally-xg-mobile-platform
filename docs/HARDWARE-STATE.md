# Captured hardware state

The reference state this repo reproduces, read off the running host on
**2026-09-28**. If a rebuild disagrees with something here, the rebuild is
wrong — or the hardware changed and this file needs re-measuring.

Everything below was **measured**, not derived. That matters on this
platform: dock PWM is shape-dependent and unmodelled, and the one affine fit
that exists is valid only over the band it was tested on — at the bottom it
predicts applied 50 where a *stopped fan* was measured.

## Host

| | |
|---|---|
| OS | Ubuntu 24.04.5 LTS |
| Kernel | 7.0.0-34-generic |
| Platform | ASUS ROG Ally, Z1 Extreme (Phoenix1) |
| Kernel cmdline | `amdgpu.ppfeaturemask=0xfff7ffff` |

## GPUs

| Role | gfx | PCI | Card | VRAM |
|---|---|---|---|---|
| `egpu` | `gfx1031` | `0000:03:00.0` | Navi 22 — RX 6850M XT `[1002:73df]` rev c2 | 12 GiB |
| `igpu` | `gfx1103` | `0000:09:00.0` | Phoenix1 — Radeon 780M `[1002:15bf]` rev 04 | 4 GiB |

The eGPU also presents `03:00.1` (Navi 21/23 HDMI/DP audio, `[1002:ab28]`).

### PCIe topology

The XG Mobile hangs off root port **`0000:00:01.1`**, whose bridge window is
`secondary_bus=1, subordinate_bus=5`. The dock chain enumerates as
`01:00.0 → 02:00.0 → 03:00.0/.1`.

With the card off the bus, **buses 01–05 are empty** and `0000:03:00.0` has
no sysfs node at all. Device count: **38 without the card / 42 with it**.

Link when up: **PCIe x4 at 8.0 GT/s — 31.504 Gb/s**, limited by root port
`0000:00:01.1` (the card is capable of x16 at 16.0 GT/s / 252.048 Gb/s).
The slot is `HotPlug+ Surprise+ LLActRep+`.

## The PCIe gate — `asus-nb-wmi`

Two attributes under `/sys/bus/platform/devices/asus-nb-wmi/`:

| Attribute | Access | Meaning |
|---|---|---|
| `egpu_connected` | read-only | 1 = dock physically attached and powered |
| `egpu_enable` | **read-write** | 1 = PCIe interface enabled |

**`egpu_enable` does not persist across a reboot.** Captured from the boot
of 2026-09-28 (boot at 19:57:45):

```
19:57:49  pcieport 0000:00:01.1: pciehp: Slot(0): Card present
19:57:49  pcieport 0000:00:01.1: pciehp: Slot(0): No link
19:58:55  pcieport 0000:00:01.1: pciehp: Slot(0): Card present
19:58:56  pcieport 0000:00:01.1: pciehp: Slot(0): No link
20:00:36  pcieport 0000:00:01.1: pciehp: Slot(0): Card present
20:00:36  pcieport 0000:00:01.1: pciehp: Slot(0): Link Up
20:00:36  pci 0000:03:00.0: [1002:73df] type 00 class 0x038000
20:00:36  amdgpu 0000:03:00.0: initializing kernel modesetting (NAVY_FLOUNDER ...)
```

The dock was attached for the whole window. `Card present` + `No link` is
the gated state; the link trained only after `egpu_enable` was written to 1.

Other GPU-related attributes on this platform, for reference:
`dgpu_disable = 0`, `throttle_thermal_policy = 1` (set by
`platform_profile=performance`).

A **bus rescan cannot substitute for the gate** — it re-enumerates links
that are already up and cannot train one the firmware holds down.

### Enumeration order is NOT stable

| Measured | Role | HIP | Vulkan |
|---|---|---|---|
| 2026-08-08 | `egpu` | 0 | 1 |
| | `igpu` | 1 | 0 |
| 2026-09-28 | `egpu` | **1** | 1 |
| | `igpu` | **0** | 0 |

Across one reboot the HIP order inverted, and the two backends — previously
inverted relative to each other — happened to agree. Resolve per launch by
gfx target + PCI address. Never persist an index.

## ROCm

| | |
|---|---|
| Version | 7.2.4 (`/opt/rocm` → `/opt/rocm-7.2.4`) |
| Source | `https://repo.radeon.com/rocm/apt/7.2.4 noble main` |
| Keyring | `/etc/apt/keyrings/rocm.gpg` |
| Packages | `rocm-hip-sdk`, `rocminfo`, `rocm-smi-lib` (+ deps) |
| `HSA_OVERRIDE_GFX_VERSION` | **not set anywhere** |

`gfx1031` is not on AMD's supported-GPU list but enumerates natively on
7.2.4 — no override in `.bashrc`, `.profile`, `/etc/environment` or
`/etc/profile.d`. This is a property of this version, not a guarantee.

## User

Groups required: `render` (993) and `video` (44) for the DRM nodes, plus the
daemon's socket group for unprivileged `status` reads. All take effect on
**next login**.

## llama.cpp

| | |
|---|---|
| Commit | `0ef6e55edb306fcbcf73e6f1f41923cccb9cf7f8` |
| `build/` | `GGML_HIP=ON`, `AMDGPU_TARGETS=gfx1031;gfx1103`, `GGML_HIP_GRAPHS=ON`, `GGML_HIP_NO_VMM=ON`, Release |
| `build-vk/` | `GGML_VULKAN=ON`, `GGML_HIP=OFF`, Release |

One HIP binary covers both cards, so the eGPU and iGPU can serve
concurrently without a second checkout.

> On the host as captured, `build-vk/` is **incomplete**: it has the Vulkan
> libraries and only two `llama-*` binaries, with no `llama-cli`. The HIP
> build is complete. Stage 50 builds the full default target for both, so a
> rebuild produces a *more* complete Vulkan backend than the captured host
> had. Verification reports a missing Vulkan binary as SKIP, not FAIL,
> because HIP is the primary path.

## Thermal

| | |
|---|---|
| Unit | `glados-thermal.service` (enabled, `Restart=always`, `RestartSec=5`) |
| Daemon | `/usr/local/sbin/glados-thermal.py` — 3031 lines |
| Config | `/etc/system-thermal-daemon.conf` |
| Socket | `/run/glados-thermal.sock`, root:*group*, 0660 |
| Tick | 10 s |
| `floor_duty` | 45, both surfaces |
| Ladder | 5 rungs; rung 0 = maximum, rung 4 = floor |
| Dwell | 60 s per descent step; quiet-hold 30 s |

### Measured resting floor

**Dock PWM 131 (~51 %), Ally 3900 rpm.** Reproduced exactly on 2026-09-28
after a reboot and a fresh dock attach, matching the value originally
recorded. The fans never stop — the floor is asserted structurally by
`never_stops()` / `clamp_to_floor()`, which the config cannot reach.

### Measured descent after a dock attach

Attach → floor took about **7 minutes**:

| Dock rung | Dock PWM | Ally rung | CPU fan |
|---|---|---|---|
| 1/5 | 255 | 1/5 | 6500 |
| 3/5 | 155 | 2/5 | 4800 |
| 3/5 | 158 | 3/5 | 4300 |
| 4/5 | **131** | 3/5 | 4300 |
| 4/5 | **131** | 4/5 | **3900** |

Ascent is total and immediate; descent is gated at one rung per 60 s dwell.
Fans running hard for several minutes after an attach is **correct
behaviour**, not a fault.

### Idle thermals, dock attached

edge 33–37 °C, junction 34–37 °C, memory 32–34 °C, 10 W, busy 0 %. SW CTF
trips at **100 °C junction**.

## OverDrive: what it actually unlocked

With `ppfeaturemask=0xfff7ffff` live, measured 2026-09-28:

| Lever | State |
|---|---|
| `power1_cap` | **still fixed** — `min == max == 165 W` |
| `pp_od_clk_voltage` | **PRESENT**, exposing `OD_VDDGFX_OFFSET` (currently `0mV`) |

So the mask did take, but the direct power cap on this card remains
immovable; what OverDrive bought is the voltage/clock lever, not a writable
wattage. This is the second branch of `cap-power.sh`'s logic — "power cap
still fixed, but OverDrive is available" — and it means a sustained-power
target has to be reached with a GFXCLK ceiling or a voltage offset, swept
down while *measuring* power, not by writing a watt figure.

Do not read the 165 W cap as a failed install. Check
`/sys/module/amdgpu/parameters/ppfeaturemask` to know whether the mask took;
the cap's min/max says something different.

## Fan authority

Measured once uncapped: requested duty maps to applied PWM as
`(requested + 95)`, floored at 175 (69 %), saturating at 255 for requested
≳ 200. Valid only over the tested band.

### The two fans take different paths

| Surface | Path | Persistence |
|---|---|---|
| Ally | `asus_custom_fan_curve` hwmon, 8 `(temp,pwm)` points | until reboot |
| Dock | USB HID report `0x5E`, ITE 8910, usage `0xFF31`/`0x80` | **volatile** |

`amdgpu`'s `pwm1` is **accepted and silently ignored** on this card, and
`asus-nb-wmi` has no XG Mobile fan device id at all. The HID channel is the
only thing that reaches the dock fan. `SetFanCurve` writes the active curve,
which `GetFanCurveTable` cannot read back — trust the fan, not the readback.
Protocol reference: `osy/XG_Mobile_Station`, `Docs/Software.md`.

## Deliberately not done

- **`ppt_*` is left alone.** Those read 5 while the APU was measured drawing
  21 W, so 5 is not a 5 W cap; the units are undocumented
  (`sysfs-platform-asus-wmi` states none), and whether RC71L firmware clamps
  an out-of-design value is unknown. `platform_profile=performance` is the
  supported lever and is used instead.
- **No udev hotplug rule.** Retired in favour of the daemon's per-tick
  re-assert. See README.
- **`thermald.service`** is left enabled. It manages Intel thermal zones and
  touches neither the ASUS fan curve nor the dock, so it does not contend.
