#!/usr/bin/env bash
# Stage 45 — eGPU attach handling and the role resolver.
#
# HOW ATTACH ACTUALLY WORKS ON THIS PLATFORM, so nobody re-adds machinery
# that is deliberately absent:
#
#   1. PCIe enumeration is done by FIRMWARE AND THE KERNEL at physical
#      connect. There is no software path from "dock detached" to "card
#      present" — no rescan, no bind, no setpci. `echo 1 > /sys/bus/pci/rescan`
#      re-runs enumeration on links that are ALREADY UP; with the dock's
#      link down it is a no-op that returns success and changes nothing.
#      Verified on this host: buses 01-05 (the dock's reserved window under
#      root port 00:01.1) are simply empty when the dock is off.
#
#   2. Once enumerated, the card comes back with runtime PM "auto". It then
#      drops to D3cold at idle, where every hwmon attribute returns EBUSY —
#      which reads exactly like a dead sensor, and which stops the fan being
#      pre-spun before load arrives (FINDINGS s7).
#
#   3. The daemon fixes that on a LOOP, not once: every 10 s tick it
#      re-asserts power/control=on and re-detects the dock HID. So a cold
#      boot with the dock attached, a hotplug, and a resume are all the same
#      code path, and all heal within one tick.
#
# THEREFORE NO UDEV RULE. Older installers shipped one that restarted the
# service on hotplug; it bought nothing once the re-assert loop existed, cost
# a curve gap while the unit bounced, and hardcoded a unit name that was
# wrong wherever the daemon ran under another. This stage REMOVES any such
# rule left behind by an older install.
#
# WHAT THIS STAGE DOES INSTALL is the part that is genuinely easy to get
# wrong: role resolution. HIP and Vulkan do not agree on device order, and
# neither is stable across reboots or hotplug. Measured on this host on
# 2026-09-28, after a reboot, the HIP order had INVERTED from the value
# recorded on 2026-08-08 — the eGPU moved from HIP index 0 to HIP index 1.
# Anything that had hardcoded 0 would have silently landed on the 4 GiB iGPU
# and OOMed a 14B model in the most confusing way available. Resolve by
# identity (gfx target + PCI address), every launch, never persist an index.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
need_root
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say "eGPU attach handling + role resolver"

# --- retire any legacy hotplug rule ----------------------------------------
removed=0
for r in /etc/udev/rules.d/99-ally-egpu-fans.rules \
         /etc/udev/rules.d/99-glados-egpu-fans.rules; do
  [ -e "$r" ] && { rm -f "$r"; ok "removed legacy udev rule $r"; removed=1; }
done
[ "$removed" = 1 ] && udevadm control --reload 2>/dev/null
[ "$removed" = 0 ] && skip "no legacy udev hotplug rule present (correct)"

# --- platform tools ---------------------------------------------------------
install -d -m 0755 /usr/local/lib/ally-platform
for f in gpu_resolve.sh gpu-watch.sh thermal-guard.sh \
         xgm-fan.py xgm-fan-hold.py xgm-fan-probe.py \
         egpu-status.sh egpu-wait.sh; do
  [ -e "$HERE/bin/$f" ] || { warn "missing bin/$f — skipped"; continue; }
  install -m 0755 "$HERE/bin/$f" "/usr/local/bin/$f"
done
install -m 0644 "$HERE/lib/gpu_roles.py" /usr/local/lib/ally-platform/gpu_roles.py
ok "tools -> /usr/local/bin, gpu_roles.py -> /usr/local/lib/ally-platform"

# --- report the live attach state -------------------------------------------
if egpu_present; then
  drv="$(basename "$(readlink -f "/sys/bus/pci/devices/${EGPU_PCI}/driver" 2>/dev/null)" 2>/dev/null)"
  pm="$(cat "/sys/bus/pci/devices/${EGPU_PCI}/power/control" 2>/dev/null)"
  rt="$(cat "/sys/bus/pci/devices/${EGPU_PCI}/power/runtime_status" 2>/dev/null)"
  ok "eGPU ${EGPU_PCI}: driver=${drv:-none} power/control=${pm} runtime=${rt}"
  [ "$pm" = "on" ] || warn "power/control is '${pm}', not 'on' — the daemon re-asserts this within one 10s tick"
  [ -e "/dev/dri/by-path/pci-${EGPU_PCI}-render" ] \
    && ok "render node present" \
    || warn "render node /dev/dri/by-path/pci-${EGPU_PCI}-render absent"
else
  say "eGPU absent — dock detached."
  say "  Attach the dock; the daemon picks it up within one 10 s tick."
  say "  Nothing further is required at boot: enumeration is the kernel's job."
fi

ok "eGPU attach stage done"
