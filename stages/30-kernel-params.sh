#!/usr/bin/env bash
# Stage 30 — amdgpu OverDrive, via the kernel command line.
#
# WHY. Upstream's default amdgpu_pp_feature_mask is 0xfff7bfff, which CLEARS
# PP_OVERDRIVE_MASK (bit 14, 0x4000) — amdgpu_drv.c says so in a comment.
# smu->od_enabled is set only `if (adev->pm.pp_feature & PP_OVERDRIVE_MASK)`,
# and without it amdgpu_pm.c marks the OverDrive files ATTR_STATE_UNSUPPORTED
# so they never appear. The symptom is power1_cap reading min == max == 165W
# and pp_od_clk_voltage being absent entirely: the limits look hard-locked by
# the vBIOS when they are actually locked by a DEFAULT.
#
#   0xfff7bfff | 0x4000 = 0xfff7ffff
#
# This is what makes the eGPU's sustained power level tunable at all, which
# is the lever that keeps junction away from the 100 C SW CTF trip. Requires
# a REBOOT — the mask is read at module load.
#
# Revert:  sudo ./stages/30-kernel-params.sh --revert
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
need_root

G=/etc/default/grub
PARAM="amdgpu.ppfeaturemask=${PPFEATUREMASK}"

say "amdgpu OverDrive (${PARAM})"

[ -f "$G" ] || die "$G not found — not a GRUB system. Set ${PARAM} by whatever
means this bootloader uses, then re-run the remaining stages."

# --- revert -----------------------------------------------------------------
if [ "${1:-}" = "--revert" ]; then
  # Strip any ppfeaturemask setting, ours or otherwise, and tidy the spacing
  # the removal leaves behind.
  sed -i -E 's/[[:space:]]*amdgpu\.ppfeaturemask=[^"[:space:]]*//g' "$G"
  sed -i -E 's/(GRUB_CMDLINE_LINUX_DEFAULT=")[[:space:]]+/\1/' "$G"
  update-grub >/dev/null 2>&1 || warn "update-grub reported a problem"
  ok "reverted — REBOOT to apply"
  grep -E '^GRUB_CMDLINE_LINUX_DEFAULT' "$G"
  exit 0
fi

cp -n "$G" "$G.bak-preod" 2>/dev/null && say "backup: $G.bak-preod"

current="$(grep -E '^GRUB_CMDLINE_LINUX_DEFAULT=' "$G" | head -1)"

if printf '%s' "$current" | grep -q "amdgpu.ppfeaturemask=${PPFEATUREMASK}"; then
  skip "already set: $current"
elif printf '%s' "$current" | grep -q 'amdgpu.ppfeaturemask='; then
  # A DIFFERENT mask is present. Replace the value rather than appending a
  # second one — the kernel takes the last occurrence, so appending would
  # "work" while leaving a contradictory line for the next reader.
  warn "a different ppfeaturemask is set; replacing it"
  sed -i -E "s/amdgpu\.ppfeaturemask=[^\"[:space:]]*/${PARAM}/" "$G"
  ok "replaced: $(grep -E '^GRUB_CMDLINE_LINUX_DEFAULT' "$G")"
else
  # Append INSIDE the quotes, preserving whatever is already there (quiet,
  # splash, anything the installer set). Never rewrite the whole value.
  sed -i -E "s/^(GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*)\"/\1 ${PARAM}\"/" "$G"
  sed -i -E 's/(GRUB_CMDLINE_LINUX_DEFAULT=")[[:space:]]+/\1/' "$G"
  ok "appended: $(grep -E '^GRUB_CMDLINE_LINUX_DEFAULT' "$G")"
fi

update-grub >/dev/null 2>&1 || warn "update-grub reported a problem"

live="$(cat /sys/module/amdgpu/parameters/ppfeaturemask 2>/dev/null || echo '?')"
say "live ppfeaturemask: ${live}  (want ${PPFEATUREMASK} after reboot)"
if [ "$live" = "${PPFEATUREMASK}" ]; then
  ok "already active — no reboot needed for this stage"
else
  warn "REBOOT REQUIRED before the power cap becomes writable"
fi
