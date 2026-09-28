#!/usr/bin/env bash
# Stage 10 — base packages. Build toolchain, Vulkan runtime + tools, and the
# small utilities the other stages and the daemon's preflight actually shell
# out to.
#
# Vulkan matters as much as HIP here: gpu_resolve.sh resolves a Vulkan index
# separately because HIP and Vulkan do NOT agree on device order, and a HIP
# index handed to a Vulkan build pins the wrong card silently.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
need_root

say "base packages"

PKGS=(
  build-essential cmake git pkg-config ccache
  python3 python3-venv python3-pip
  libcurl4-openssl-dev          # llama.cpp model download support
  libdrm-dev libdrm-amdgpu1
  mesa-vulkan-drivers           # RADV — the Vulkan backend's driver
  libvulkan-dev vulkan-tools    # vulkaninfo, used by gpu_resolve.sh vkindex
  glslc                         # Vulkan shader compilation for ggml-vulkan
  xxd                           # dock HID report_descriptor check in preflight
  pciutils usbutils lm-sensors
  jq curl ca-certificates gnupg
  linux-firmware               # amdgpu blobs; stage 15 verifies the families
)

missing=()
for p in "${PKGS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
done

if [ "${#missing[@]}" -eq 0 ]; then
  skip "all base packages already installed"
else
  say "installing: ${missing[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || die "apt-get update failed"
  # glslc lives in glslc on noble but shipped inside shaderc-utils elsewhere;
  # a missing shader compiler must not abort the whole platform rebuild,
  # because only the Vulkan llama.cpp build needs it.
  apt-get install -y "${missing[@]}" || {
    warn "batch install failed — retrying individually to isolate the bad package"
    for p in "${missing[@]}"; do
      apt-get install -y "$p" || warn "could not install: $p"
    done
  }
fi

have vulkaninfo && ok "vulkaninfo present" || warn "vulkaninfo missing — gpu_resolve.sh vkindex will not work"
have cmake && ok "cmake $(cmake --version | head -1 | awk '{print $3}')"
ok "base packages done"
