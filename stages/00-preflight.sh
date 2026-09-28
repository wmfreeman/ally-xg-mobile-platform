#!/usr/bin/env bash
# Stage 00 — preflight. Read-only. Proves this is the platform the rest of
# the repo assumes, and fails LOUDLY rather than installing something inert
# on a machine it does not fit.
#
# The dock being detached is NOT a failure. Every later stage is written to
# install correctly with no eGPU on the bus; the daemon attaches to it when
# it appears. Preflight reports the dock's state and moves on.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

say "preflight"
fail=0

# --- OS ---------------------------------------------------------------------
if [ -r /etc/os-release ]; then
  . /etc/os-release
  say "os: ${PRETTY_NAME:-unknown} (kernel $(uname -r))"
  case "${ID:-}" in
    ubuntu|debian) ;;
    *) warn "validated on Ubuntu 24.04; ${ID:-unknown} may need different package names" ;;
  esac
else
  warn "no /etc/os-release — cannot identify the distribution"
fi

# --- CPU / platform ---------------------------------------------------------
# asus_custom_fan_curve is the ONLY path to the Ally's cpu_fan and gpu_fan.
# Without it the daemon still runs and still drives the dock, but the Ally
# surface is inert — which is precisely the capped-curve condition that
# caused the 2026-08-07 SW CTF poweroff. Loud warning, not a failure, so a
# rebuild on a half-assembled machine still completes.
if [ -e /sys/firmware/acpi/platform_profile ]; then
  ok "platform_profile present ($(cat /sys/firmware/acpi/platform_profile 2>/dev/null))"
else
  warn "no /sys/firmware/acpi/platform_profile — not an ASUS platform?"
fi

found=0
for h in /sys/class/hwmon/hwmon*/; do
  [ "$(cat "$h/name" 2>/dev/null)" = "asus_custom_fan_curve" ] && found=1
done
if [ "$found" = 1 ]; then
  ok "asus_custom_fan_curve hwmon present (Ally fans controllable)"
else
  warn "asus_custom_fan_curve ABSENT — Ally cpu_fan/gpu_fan will NOT be controlled."
  warn "  This is the capped-curve condition that caused the 2026-08-07 poweroff."
  warn "  Check the asus-nb-wmi / asus_wmi_sensors modules before running load."
fi

# --- iGPU -------------------------------------------------------------------
if [ -d "/sys/bus/pci/devices/${IGPU_PCI}" ]; then
  ok "iGPU present at ${IGPU_PCI} (${IGPU_GFX})"
else
  warn "iGPU not at ${IGPU_PCI} — PCI layout differs from the validated platform"
fi

# --- eGPU / dock ------------------------------------------------------------
if egpu_present; then
  drv=$(basename "$(readlink -f "/sys/bus/pci/devices/${EGPU_PCI}/driver" 2>/dev/null)" 2>/dev/null)
  ok "eGPU present at ${EGPU_PCI} (driver: ${drv:-none})"
else
  say "eGPU ABSENT at ${EGPU_PCI} — dock detached."
  say "  Not a problem: install proceeds and the daemon attaches on hotplug."
fi

# The XG Mobile fan is reachable ONLY over USB HID report 0x5E on the ITE
# 8910 (usage page 0xFF31, usage 0x80). amdgpu's pwm1 is accepted and
# silently ignored on this card, and asus-nb-wmi has no XG Mobile fan id.
dock=0
for hr in /sys/class/hidraw/hidraw*; do
  d="$hr/device"
  [ -d "$d" ] || continue
  if grep -qi "1970" "$d/uevent" 2>/dev/null &&
     xxd -p "$d/report_descriptor" 2>/dev/null | tr -d '\n' | grep -q "0631ff0980"; then
    dock=1
  fi
done
if [ "$dock" = 1 ]; then
  ok "XG Mobile HID found (0b05:1970, usage 0xFF31/0x80)"
else
  say "XG Mobile HID not found — dock undocked (expected if the eGPU is absent)"
fi

# --- tooling ----------------------------------------------------------------
have python3 || { warn "MISSING: python3"; fail=1; }
have systemctl || { warn "MISSING: systemctl (not a systemd host)"; fail=1; }

# --- conflicting thermal managers -------------------------------------------
# thermald ships enabled on Ubuntu and manages Intel thermal zones. It does
# not touch the ASUS fan curve or the dock, so it is not a true conflict,
# but a second thermal manager on the box is worth stating out loud.
if systemctl is-enabled thermald.service >/dev/null 2>&1; then
  say "note: thermald.service is enabled. It does not drive the ASUS fan"
  say "      curve or the XG Mobile dock, so it does not contend with this"
  say "      daemon — left alone deliberately."
fi

[ "$fail" = 1 ] && die "preflight failed"
ok "preflight passed"
