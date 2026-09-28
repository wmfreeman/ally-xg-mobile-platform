#!/usr/bin/env bash
# Resolve a GPU ROLE to its CURRENT index / DRM path, per BACKEND.
# Enumeration order is NOT stable across reboots or eGPU hotplug, so nothing
# should ever hardcode an index. Identity is the gfx target + PCI address.
#
#   egpu -> gfx1031 @ pci 0000:03:00.0  (RX 6850M XT, 12 GiB)
#   igpu -> gfx1103 @ pci 0000:09:00.0  (Radeon 780M,   4 GiB)
#
# HIP AND VULKAN ENUMERATE IN DIFFERENT ORDERS, AND ON THIS HOST THE TWO ARE
# INVERTED. Measured 2026-08-08:
#
#     role   HIP index   Vulkan index
#     egpu       0            1
#     igpu       1            0
#
# So a HIP index handed to a Vulkan build pins the WRONG CARD — silently, and
# it still runs. `index` stays HIP-only for compatibility with every existing
# caller; Vulkan callers must ask for `vkindex`.
#
# Vulkan resolution keys on PCI ADDRESS, not on position and not on device
# name. Two reasons: position is the thing that just bit us, and RADV's device
# names carry the marketing string ("RX 6800M" for a 6850M XT) which is not
# ours to depend on.
#
# It also excludes llvmpipe BY CONSTRUCTION. Vulkan enumerates the CPU software
# rasteriser as a real device (GPU2 here); a fallthrough that selected it would
# run, report "Vulkan", and be unusably slow. llvmpipe publishes no
# VkPhysicalDevicePCIBusInfoPropertiesEXT block, so matching on PCI can never
# reach it. That is deliberate — do not "improve" this into a name match.
set -euo pipefail
ROLE="${1:-}"; WHAT="${2:-index}"
case "$ROLE" in
  egpu) GFX=gfx1031; PCI=0000:03:00.0 ;;
  igpu) GFX=gfx1103; PCI=0000:09:00.0 ;;
  *) echo "usage: $0 {egpu|igpu} [index|vkindex|gfx|pci|render|check|vkcheck]" >&2; exit 2 ;;
esac

# --- Vulkan index: position among devices that publish this PCI address ------
vkindex() {
  command -v vulkaninfo >/dev/null 2>&1 || { echo "ERROR: vulkaninfo not installed" >&2; return 1; }
  local bus dev fn idx
  bus=$((16#${PCI:5:2})); dev=$((16#${PCI:8:2})); fn=${PCI:11:1}
  # NOTE: awk must NOT `exit` on the first match. Doing so closes the pipe while
  # vulkaninfo is still writing, vulkaninfo takes SIGPIPE, `pipefail` reports
  # 141, and `set -e` then kills this script AFTER idx is computed but BEFORE it
  # is echoed — so the caller sees empty output and rc=141 while the value was
  # right all along. `vkcheck` hid this for a while because its `|| echo -`
  # suppresses set -e. Read the whole stream; it costs about a second.
  idx=$(vulkaninfo 2>/dev/null | awk -v b="$bus" -v d="$dev" -v f="$fn" '
    /^GPU[0-9]+:/            { g=$0; sub(/^GPU/,"",g); sub(/:.*/,"",g); pb=pd=pf=-1 }
    /pciBus[ \t]*=/          { pb=$3 }
    /pciDevice[ \t]*=/       { pd=$3 }
    /pciFunction[ \t]*=/     { pf=$3; if (!done && pb==b && pd==d && pf==f) { print g; done=1 } }
  ')
  [ -n "${idx:-}" ] || { echo "ERROR: ${ROLE} (pci ${PCI}) not enumerated by Vulkan" >&2; return 1; }
  echo "$idx"
}

case "$WHAT" in
  vkindex) vkindex; exit $? ;;
  vkcheck)
    vi=$(vkindex 2>/dev/null || echo "-")
    name=$(vulkaninfo --summary 2>/dev/null | awk -v g="GPU$vi:" '$0~"^"g{f=1} f&&/deviceName/{sub(/.*= /,""); print; exit}')
    echo "role=$ROLE pci=$PCI vk_index=$vi name=${name:-?}"
    echo "  pin with: GGML_VK_VISIBLE_DEVICES=$vi"
    [ "$vi" = "-" ] && echo "  NOT AVAILABLE under Vulkan" >&2
    exit 0 ;;
esac

# HIP index = position of this gfx among GPU agents, in enumerator order.
IDX=$(/opt/rocm/bin/rocm_agent_enumerator 2>/dev/null | grep -v "^gfx000$" | grep -n "^${GFX}$" | head -1 | cut -d: -f1)
[ -n "${IDX:-}" ] || { echo "ERROR: ${GFX} not present (eGPU detached?)" >&2; exit 1; }
IDX=$((IDX-1))
case "$WHAT" in
  index)  echo "$IDX" ;;
  gfx)    echo "$GFX" ;;
  pci)    echo "$PCI" ;;
  render) echo "/dev/dri/by-path/pci-${PCI}-render" ;;
  check)  echo "role=$ROLE gfx=$GFX hip_index=$IDX vk_index=$(vkindex 2>/dev/null || echo -) render=/dev/dri/by-path/pci-${PCI}-render present=$([ -e "/dev/dri/by-path/pci-${PCI}-render" ] && echo yes || echo NO)" ;;
esac
