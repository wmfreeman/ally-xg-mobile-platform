#!/usr/bin/env bash
# Stage 15 — drivers and firmware.
#
# THERE IS NOTHING TO INSTALL, and that is the point of this stage: every
# driver this platform needs is an IN-TREE kernel module shipping with
# linux-image. Verified on this host — all five resolve under
# /lib/modules/$(uname -r)/kernel/:
#
#   amdgpu         drivers/gpu/drm/amd/amdgpu/   both GPUs
#   asus_nb_wmi    drivers/platform/x86/         THE critical one, below
#   asus_wmi       drivers/platform/x86/         its generic half
#   asus_armoury   drivers/platform/x86/         newer ROG attributes
#   hid_asus       drivers/hid/                  N-KEY device
#
# No DKMS, no out-of-tree build, no vendor package. What this stage does is
# PROVE they are present and working, and install the one thing that is a
# separate package and is genuinely fatal if missing: firmware.
#
# asus-nb-wmi CARRIES THREE THINGS THIS PLATFORM CANNOT WORK WITHOUT, all
# from the one module:
#   * the `asus` hwmon          — cpu_fan / gpu_fan RPM readback
#   * the `asus_custom_fan_curve` hwmon — the ONLY way to set the Ally fan
#                                 curve, i.e. the fix for the 2026-08-07
#                                 SW CTF poweroff
#   * egpu_enable / egpu_connected — the XG Mobile PCIe gate
# Lose that module and you lose fan control AND the ability to bring the
# dock up at all, which is why it is checked explicitly rather than assumed.
#
# FIRMWARE IS NOT IN-TREE. amdgpu loads signed blobs from /lib/firmware at
# probe. Without navy_flounder_* the eGPU enumerates on the bus and then
# FAILS TO INITIALISE — a distinct and confusing failure from the card not
# being there at all.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
need_root

say "drivers and firmware"
fail=0

# --- firmware package -------------------------------------------------------
if dpkg -s linux-firmware >/dev/null 2>&1; then
  skip "linux-firmware installed ($(dpkg-query -W -f='${Version}' linux-firmware 2>/dev/null))"
else
  say "installing linux-firmware (amdgpu blobs)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y linux-firmware || die "linux-firmware install FAILED — amdgpu cannot initialise either GPU without it"
fi

# --- firmware blobs ---------------------------------------------------------
# Checked by family, not by exact filename: the set changes between releases
# and .zst compression comes and goes. Presence of the family prefix is the
# signal that matters.
check_fw() {
  local label="$1" pat="$2" n
  n=$(find /lib/firmware/amdgpu -maxdepth 1 -name "${pat}*" 2>/dev/null | wc -l)
  if [ "$n" -gt 0 ]; then
    ok "$label firmware present (${n} blobs, ${pat}*)"
  else
    warn "$label firmware MISSING (${pat}*) — that GPU will fail to initialise"
    fail=1
  fi
}
check_fw "eGPU Navi 22 (NAVY_FLOUNDER)" "navy_flounder"
check_fw "iGPU Phoenix (gc_11_0_1)"     "gc_11_0_1"

# --- blacklists -------------------------------------------------------------
# A blacklisted asus_wmi/asus_nb_wmi is silent and total: no fan curve, no
# gate, and nothing in the journal saying why.
for m in asus_nb_wmi asus-nb-wmi asus_wmi asus-wmi amdgpu hid_asus hid-asus; do
  if grep -rqE "^[[:space:]]*blacklist[[:space:]]+${m}([[:space:]]|$)" /etc/modprobe.d/ 2>/dev/null; then
    warn "MODULE BLACKLISTED: $m — remove it from /etc/modprobe.d/ or nothing here works"
    fail=1
  fi
done

# --- modules ----------------------------------------------------------------
# asus_nb_wmi normally autoloads on a WMI GUID match, so a modprobe here is
# a repair path, not the usual route.
for m in amdgpu asus_wmi asus_nb_wmi hid_asus; do
  if lsmod | awk '{print $1}' | grep -qx "$m"; then
    ok "module loaded: $m"
  elif modinfo "$m" >/dev/null 2>&1; then
    modprobe "$m" 2>/dev/null \
      && ok "module loaded: $m (modprobe'd)" \
      || { warn "module $m exists but will not load"; fail=1; }
  else
    warn "module $m NOT AVAILABLE in kernel $(uname -r)"
    fail=1
  fi
done

# --- the surfaces those modules must expose ---------------------------------
# The real test is not "is the module loaded" but "did it produce the
# interface we depend on". A kernel can carry asus-nb-wmi and still not
# expose egpu_enable for this model.
found=0
for h in /sys/class/hwmon/hwmon*/; do
  [ "$(cat "$h/name" 2>/dev/null)" = "asus_custom_fan_curve" ] && found=1
done
[ "$found" = 1 ] && ok "asus_custom_fan_curve hwmon exposed (Ally fan control)" \
                 || { warn "asus_custom_fan_curve NOT exposed — Ally fans uncontrollable on this kernel"; fail=1; }

found=0
for h in /sys/class/hwmon/hwmon*/; do
  [ "$(cat "$h/name" 2>/dev/null)" = "asus" ] && found=1
done
[ "$found" = 1 ] && ok "asus hwmon exposed (fan RPM readback)" \
                 || warn "asus hwmon not exposed — no fan RPM verification"

if W="$(wmi_path)" && [ -e "$W/egpu_enable" ]; then
  ok "egpu_enable exposed (XG Mobile PCIe gate)"
else
  warn "egpu_enable NOT exposed — the XG Mobile cannot be enabled in software."
  warn "  This platform was validated on kernel 7.0.0-34-generic. A stock"
  warn "  Ubuntu 24.04 install ships 6.8; if the attribute is absent, install"
  warn "  a newer kernel (linux-generic-hwe-24.04) and re-run this stage."
  fail=1
fi

[ -e /sys/firmware/acpi/platform_profile ] \
  && ok "platform_profile exposed" \
  || warn "platform_profile not exposed — the performance lever is unavailable"

# --- amdgpu probe errors ----------------------------------------------------
if journalctl -k -b --no-pager 2>/dev/null | grep -qiE 'amdgpu.*(firmware load fail|Direct firmware load.*fail)'; then
  warn "amdgpu firmware load failures this boot:"
  journalctl -k -b --no-pager 2>/dev/null | grep -iE 'amdgpu.*(firmware|fail)' | tail -5
  fail=1
else
  ok "no amdgpu firmware load failures this boot"
fi

say "kernel: $(uname -r)  (validated on 7.0.0-34-generic)"
[ "$fail" = 1 ] && warn "driver/firmware stage completed WITH PROBLEMS — see above"
ok "drivers stage done"
