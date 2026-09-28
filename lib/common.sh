#!/usr/bin/env bash
# Shared helpers for every stage. Sourced, never executed.
#
# Stages are INDEPENDENTLY RE-RUNNABLE. install.sh runs them in order on a
# fresh box, but each one must also be safe to run alone on a live host —
# that is how a partial rebuild is repaired without starting over. Anything
# that cannot be made idempotent (a GRUB edit, a package install) checks for
# its own effect first and reports "already" rather than repeating it.

# Deployed names. These are LOAD-BEARING and deliberately not derived from
# the repo name: the daemon compiles /run/glados-thermal.sock in as its
# control socket, and Palo_Toolkit's core/lifecycle/fan_client.py connects
# to that exact path. Renaming the unit without also rebuilding the daemon
# and every socket consumer produces a host where the fans run but nothing
# can read them. Override only if you are migrating all three together.
: "${UNIT_NAME:=glados-thermal}"
: "${DAEMON_PATH:=/usr/local/sbin/glados-thermal.py}"
: "${CONF_PATH:=/etc/system-thermal-daemon.conf}"

# Hardware identity of this platform. Both halves matter: the PCI address
# locates the card, the gfx target proves it is the card we meant.
: "${EGPU_PCI:=0000:03:00.0}"
: "${EGPU_GFX:=gfx1031}"
: "${IGPU_PCI:=0000:09:00.0}"
: "${IGPU_GFX:=gfx1103}"

# amdgpu default ppfeaturemask (0xfff7bfff) CLEARS PP_OVERDRIVE_MASK (bit 14),
# which leaves power1_cap fixed at min=max=165W and hides pp_od_clk_voltage.
# 0xfff7bfff | 0x4000 = 0xfff7ffff.
: "${PPFEATUREMASK:=0xfff7ffff}"

: "${LLAMA_REPO:=https://github.com/ggml-org/llama.cpp}"
# Pinned to the commit this platform was validated against. Moving it is a
# deliberate act: re-measure inference before and after, because a llama.cpp
# bump can change VRAM headroom and therefore the thermal envelope.
: "${LLAMA_COMMIT:=0ef6e55edb306fcbcf73e6f1f41923cccb9cf7f8}"
: "${LLAMA_DIR:=${SUDO_USER_HOME:-/home/${SUDO_USER:-$USER}}/llama.cpp}"
: "${ROCM_VERSION:=7.2.4}"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
[ -t 1 ] || { RED=; GRN=; YLW=; BLU=; RST=; }

say()  { printf '%s==>%s %s\n' "$BLU" "$RST" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '%s[FAIL]%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# A stage that changed nothing says so, so a re-run reads as a no-op rather
# than as success it did not earn.
skip() { printf '%s[skip]%s %s\n' "$YLW" "$RST" "$*"; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "run with sudo: sudo $0"
}

# The unprivileged account the desktop/inference stack belongs to. Under
# sudo that is SUDO_USER; without it, whoever is running. Root is never a
# valid answer — the render/video groups and the llama.cpp checkout belong
# to a human account.
target_user() {
  local u="${SUDO_USER:-$USER}"
  [ -n "$u" ] && [ "$u" != "root" ] || die "cannot determine target user; run via sudo from your own account, not as root"
  printf '%s' "$u"
}

target_home() {
  getent passwd "$(target_user)" | cut -d: -f6
}

have() { command -v "$1" >/dev/null 2>&1; }

# True when the eGPU is enumerated on the PCI bus RIGHT NOW. A detached dock
# is not an error anywhere in this repo — the whole design is that the
# platform installs and boots correctly with the dock absent, and the daemon
# picks the card up within one tick when it appears.
egpu_present() {
  [ -d "/sys/bus/pci/devices/${EGPU_PCI}" ]
}

# asus-nb-wmi, where the eGPU PCIe gate lives. Two paths depending on
# kernel version; the bus path is usually a symlink to the devices path.
wmi_path() {
  local d
  for d in /sys/bus/platform/devices/asus-nb-wmi /sys/devices/platform/asus-nb-wmi; do
    [ -d "$d" ] && { printf '%s' "$d"; return 0; }
  done
  return 1
}

# 1 = dock physically attached. Says nothing about the PCIe link.
egpu_connected() {
  local w; w="$(wmi_path)" || return 1
  [ "$(cat "$w/egpu_connected" 2>/dev/null)" = 1 ]
}

# 1 = PCIe interface enabled. Writable, and NOT persistent across reboot.
egpu_gate_open() {
  local w; w="$(wmi_path)" || return 1
  [ "$(cat "$w/egpu_enable" 2>/dev/null)" = 1 ]
}

egpu_hwmon() {
  local h
  for h in /sys/bus/pci/devices/"${EGPU_PCI}"/hwmon/hwmon*/; do
    [ -d "$h" ] || continue
    [ "$(cat "$h/name" 2>/dev/null)" = "amdgpu" ] || continue
    printf '%s' "${h%/}"
    return 0
  done
  return 1
}
