#!/usr/bin/env bash
# Stage 45 — eGPU attach handling and the role resolver.
#
# HOW ATTACH ACTUALLY WORKS ON THIS PLATFORM. There are THREE separate
# conditions, and confusing any two of them wastes an afternoon:
#
#   1. PHYSICALLY ATTACHED.  asus-nb-wmi's `egpu_connected` reads 1. The
#      dock is plugged in and powered; its fan HID enumerates over USB.
#      This says NOTHING about the PCIe link.
#
#   2. GATE OPEN.  asus-nb-wmi's `egpu_enable` is WRITABLE and must be 1
#      for the PCIe interface to come up at all. It does NOT PERSIST
#      ACROSS A REBOOT. Observed on this host:
#
#        19:57:49  pciehp: Slot(0): Card present   <- dock attached at boot
#        19:57:49  pciehp: Slot(0): No link        <- gate closed
#        20:00:36  pciehp: Slot(0): Link Up        <- after egpu_enable=1
#        20:00:36  pci 0000:03:00.0: [1002:73df]   <- card enumerates
#
#      So a box rebooted with the dock attached comes back with the dock
#      powered, egpu_connected=1, and NOTHING on the PCI bus. That is the
#      single most confusing state this platform has, and it is why this
#      stage installs egpu-enable.service.
#
#      A BUS RESCAN DOES NOT HELP. `echo 1 > /sys/bus/pci/rescan` re-runs
#      enumeration over links that are already up; it cannot train a link
#      the firmware is holding down. The GATE is the lever.
#
#   3. AWAKE.  Once enumerated the card returns with runtime PM "auto",
#      drops to D3cold at idle, and every hwmon attribute then returns
#      EBUSY — indistinguishable from a dead sensor, and it stops the fan
#      being pre-spun before load arrives (FINDINGS s7). The thermal
#      daemon re-asserts power/control=on every 10 s tick, so this one
#      heals itself and needs nothing installed.
#
# NO UDEV RULE FOR THE FAN, deliberately. Older installers shipped one that
# restarted the thermal service on hotplug; once the daemon's re-assert loop
# existed it bought nothing, cost a curve gap while the unit bounced, and
# hardcoded a unit name that was wrong wherever the daemon ran under
# another. This stage REMOVES any such rule left behind.
#
# WHAT ELSE THIS STAGE INSTALLS is role resolution, the other thing that is
# easy to get wrong: HIP and Vulkan do not agree on device order, and
# neither is stable across reboots or hotplug. Measured 2026-09-28 after a
# reboot, the HIP order had INVERTED from the 2026-08-08 reading — the eGPU
# moved from index 0 to 1. Anything holding a remembered 0 would have
# silently landed on the 4 GiB iGPU. Resolve by identity, every launch.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
need_root
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say "eGPU attach handling + role resolver"

# --- retire any legacy fan hotplug rule ------------------------------------
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
         egpu-status.sh egpu-wait.sh egpu-enable.sh; do
  [ -e "$HERE/bin/$f" ] || { warn "missing bin/$f — skipped"; continue; }
  install -m 0755 "$HERE/bin/$f" "/usr/local/bin/$f"
done
install -m 0644 "$HERE/lib/gpu_roles.py" /usr/local/lib/ally-platform/gpu_roles.py
ok "tools -> /usr/local/bin, gpu_roles.py -> /usr/local/lib/ally-platform"

# --- the PCIe gate unit -----------------------------------------------------
WMI=''
for d in /sys/bus/platform/devices/asus-nb-wmi /sys/devices/platform/asus-nb-wmi; do
  [ -d "$d" ] && { WMI="$d"; break; }
done

if [ -n "$WMI" ] && [ -e "$WMI/egpu_enable" ]; then
  install -m 0644 "$HERE/thermal/systemd/egpu-enable.service" \
    /etc/systemd/system/egpu-enable.service
  systemctl daemon-reload
  systemctl enable egpu-enable.service >/dev/null 2>&1 \
    && ok "egpu-enable.service enabled (opens the PCIe gate at boot)" \
    || warn "could not enable egpu-enable.service"

  conn="$(cat "$WMI/egpu_connected" 2>/dev/null || echo '?')"
  gate="$(cat "$WMI/egpu_enable" 2>/dev/null || echo '?')"
  say "gate now: egpu_connected=${conn} egpu_enable=${gate}"

  # Open it now if the dock is attached and the gate is shut, so the rebuild
  # finishes with a usable card rather than one that appears after a reboot.
  if [ "$conn" = 1 ] && [ "$gate" != 1 ]; then
    say "dock attached but gate closed — opening it now"
    /usr/local/bin/egpu-enable.sh --timeout 60 || warn "egpu-enable.sh did not bring the link up"
  fi
else
  warn "asus-nb-wmi egpu_enable NOT available on this kernel."
  warn "  Without it the XG Mobile PCIe interface cannot be enabled in software."
  warn "  egpu-enable.service was NOT installed. Check for a newer kernel, or"
  warn "  that the asus_nb_wmi module is loaded."
fi

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
  say "eGPU not on the PCI bus."
  if [ -n "$WMI" ] && [ "$(cat "$WMI/egpu_connected" 2>/dev/null)" = 1 ]; then
    warn "  ...but egpu_connected=1: the dock IS attached and the gate is shut."
    warn "  Run: sudo egpu-enable.sh"
  else
    say "  Dock not attached. Connect it, then: sudo egpu-enable.sh"
  fi
fi

ok "eGPU attach stage done"
