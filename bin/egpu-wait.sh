#!/usr/bin/env bash
# Block until the eGPU is genuinely READY, or time out non-zero.
#
# For scripts and units that must not start inference before the card is
# usable. "Ready" is deliberately four independent checks, because each one
# alone has a false positive:
#
#   1. PCI device present            — but it may be unbound
#   2. amdgpu bound                  — but it may be in D3cold with sensors
#                                      returning EBUSY, which looks broken
#   3. render node present           — but ROCm may still not enumerate it
#   4. ROCm reports the gfx target   — the one that actually predicts a
#                                      successful HIP launch
#
# Exit 0 ready, 1 timeout. Never writes anything: bringing the card out of
# D3cold is the daemon's job, and this tool refuses to become a second
# writer on a knob the daemon owns.
#
#   egpu-wait.sh [timeout_seconds]     default 120
set -uo pipefail

EGPU_PCI="${EGPU_PCI:-0000:03:00.0}"
EGPU_GFX="${EGPU_GFX:-gfx1031}"
TIMEOUT="${1:-120}"
ENUM=/opt/rocm/bin/rocm_agent_enumerator

deadline=$(( $(date +%s) + TIMEOUT ))
last=''

while :; do
  why=''
  if [ ! -d "/sys/bus/pci/devices/${EGPU_PCI}" ]; then
    # Distinguish the two ways to be off the bus. "Dock attached but gated"
    # is actionable and common after a reboot; "no dock" is not.
    w=''
    for d in /sys/bus/platform/devices/asus-nb-wmi /sys/devices/platform/asus-nb-wmi; do
      [ -d "$d" ] && { w="$d"; break; }
    done
    if [ -n "$w" ] && [ "$(cat "$w/egpu_connected" 2>/dev/null)" = 1 ] \
                   && [ "$(cat "$w/egpu_enable" 2>/dev/null)" != 1 ]; then
      why="dock attached but the PCIe GATE IS CLOSED — run: sudo egpu-enable.sh"
    else
      why="not on the PCI bus (dock detached)"
    fi
  elif [ ! -e "/sys/bus/pci/devices/${EGPU_PCI}/driver" ]; then
    why="present but no driver bound"
  elif [ ! -e "/dev/dri/by-path/pci-${EGPU_PCI}-render" ]; then
    why="bound but no render node"
  elif [ -x "$ENUM" ] && ! "$ENUM" 2>/dev/null | grep -qx "$EGPU_GFX"; then
    why="render node present but ROCm does not enumerate ${EGPU_GFX}"
  fi

  if [ -z "$why" ]; then
    echo "egpu-wait: ready (${EGPU_PCI}, ${EGPU_GFX})"
    exit 0
  fi

  # Only reprint when the reason CHANGES, so a long wait stays readable
  # and a stuck stage is visible as a stuck reason.
  [ "$why" != "$last" ] && { echo "egpu-wait: $why"; last="$why"; }

  [ "$(date +%s)" -ge "$deadline" ] && {
    echo "egpu-wait: TIMEOUT after ${TIMEOUT}s — $why" >&2
    exit 1
  }
  sleep 2
done
