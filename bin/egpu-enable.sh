#!/usr/bin/env bash
# Open the XG Mobile PCIe gate, then wait for the link to train.
#
# THE THING EVERYONE MISSES. On this platform the dock being physically
# connected is NOT the same as the card being on the bus. asus-nb-wmi
# exposes two separate attributes:
#
#   egpu_connected   read-only  1 = the dock is physically attached
#   egpu_enable      writable   1 = the PCIe interface is actually ENABLED
#
# egpu_enable does NOT persist across a reboot. Observed on this host:
#
#   19:57:49  pciehp: Slot(0): Card present   <- dock attached at boot
#   19:57:49  pciehp: Slot(0): No link        <- but the gate was closed
#   20:00:36  pciehp: Slot(0): Link Up        <- after egpu_enable=1
#   20:00:36  pci 0000:03:00.0: [1002:73df]   <- card enumerates
#
# So after every boot the dock can be plugged in, powered, its fan HID
# visible, and `egpu_connected` reading 1, while lspci shows nothing at all.
#
# THIS IS ALSO WHY A BUS RESCAN IS USELESS HERE. `echo 1 > /sys/bus/pci/rescan`
# re-runs enumeration over links that are already up; it cannot train a link
# the firmware is holding down. The gate is the lever, not the rescan.
#
#   sudo egpu-enable.sh              enable and wait (default 60s)
#   sudo egpu-enable.sh --status     report the gate, change nothing
#   sudo egpu-enable.sh --disable    close the gate
#   sudo egpu-enable.sh --timeout 90
set -uo pipefail

EGPU_PCI="${EGPU_PCI:-0000:03:00.0}"
TIMEOUT=60
ACTION=enable

while [ $# -gt 0 ]; do
  case "$1" in
    --status)  ACTION=status; shift ;;
    --disable) ACTION=disable; shift ;;
    --timeout) TIMEOUT="${2:-60}"; shift 2 ;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# The platform device is reachable by two paths depending on kernel version.
WMI=''
for d in /sys/bus/platform/devices/asus-nb-wmi /sys/devices/platform/asus-nb-wmi; do
  [ -d "$d" ] && { WMI="$d"; break; }
done
[ -n "$WMI" ] || {
  echo "egpu-enable: asus-nb-wmi platform device not found." >&2
  echo "  Is the asus_wmi / asus-nb-wmi module loaded? (modprobe asus_nb_wmi)" >&2
  exit 3
}
GATE="$WMI/egpu_enable"
CONN="$WMI/egpu_connected"

[ -e "$GATE" ] || {
  echo "egpu-enable: $GATE does not exist — this kernel's asus-nb-wmi does not" >&2
  echo "  expose the eGPU gate. Without it the XG Mobile cannot be enabled in" >&2
  echo "  software; check for a newer kernel." >&2
  exit 3
}

rd() { cat "$1" 2>/dev/null || echo '?'; }

connected="$(rd "$CONN")"
gate="$(rd "$GATE")"
on_bus=$([ -d "/sys/bus/pci/devices/${EGPU_PCI}" ] && echo yes || echo no)

if [ "$ACTION" = status ]; then
  printf 'egpu_connected = %s   (1 = dock physically attached)\n' "$connected"
  printf 'egpu_enable    = %s   (1 = PCIe interface enabled)\n' "$gate"
  printf 'on PCI bus     = %s   (%s)\n' "$on_bus" "$EGPU_PCI"
  if [ "$connected" = 1 ] && [ "$gate" != 1 ]; then
    printf '\n  Dock attached but the GATE IS CLOSED. Run: sudo egpu-enable.sh\n'
  fi
  exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "egpu-enable: run with sudo" >&2; exit 1; }

if [ "$ACTION" = disable ]; then
  echo 0 > "$GATE" || { echo "egpu-enable: write failed" >&2; exit 1; }
  echo "egpu-enable: gate closed (egpu_enable=0)"
  exit 0
fi

# --- enable -----------------------------------------------------------------
# Refuse to open the gate with no dock attached. Writing it is not known to
# be harmful, but enabling an interface for hardware that is not there is
# not a thing to do silently, and the failure would surface later as a
# confusing "enabled but never enumerates".
if [ "$connected" != 1 ]; then
  echo "egpu-enable: egpu_connected=${connected} — no dock attached. Nothing to enable." >&2
  echo "  Connect the XG Mobile first, then re-run." >&2
  exit 4
fi

if [ "$gate" = 1 ] && [ "$on_bus" = yes ]; then
  echo "egpu-enable: already enabled and on the bus (${EGPU_PCI})"
  exit 0
fi

if [ "$gate" != 1 ]; then
  echo "egpu-enable: opening the gate (egpu_enable 1)"
  echo 1 > "$GATE" || { echo "egpu-enable: write to $GATE failed" >&2; exit 1; }
else
  echo "egpu-enable: gate already open but card not on the bus — waiting for the link"
fi

# --- wait for the link ------------------------------------------------------
# Link training plus enumeration plus amdgpu bind is seconds, not instant.
# Poll rather than sleeping a fixed guess.
deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  if [ -d "/sys/bus/pci/devices/${EGPU_PCI}" ]; then
    drv="$(basename "$(readlink -f "/sys/bus/pci/devices/${EGPU_PCI}/driver" 2>/dev/null)" 2>/dev/null)"
    if [ -n "$drv" ] && [ "$drv" != "." ]; then
      echo "egpu-enable: link up, ${EGPU_PCI} enumerated, driver=${drv}"
      exit 0
    fi
  fi
  [ "$(date +%s)" -ge "$deadline" ] && {
    echo "egpu-enable: TIMEOUT after ${TIMEOUT}s — gate is $(rd "$GATE"), card $( [ -d "/sys/bus/pci/devices/${EGPU_PCI}" ] && echo 'on bus but unbound' || echo 'not on the bus')" >&2
    echo "  Check: journalctl -k -b | grep pciehp" >&2
    exit 1
  }
  sleep 1
done
