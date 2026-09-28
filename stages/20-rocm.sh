#!/usr/bin/env bash
# Stage 20 — ROCm, from AMD's own apt repo, pinned to the validated version.
#
# WHY A PINNED VERSION. gfx1031 (Navi 22, the RX 6850M XT) is not on AMD's
# supported-GPU list. It works on ROCm 7.2.4 with NO HSA_OVERRIDE_GFX_VERSION
# — this was verified on the running host: no override appears in .bashrc,
# .profile, /etc/environment or /etc/profile.d, and rocm_agent_enumerator
# reports gfx1031 natively. That is a property of this ROCm version, not a
# guarantee. If you move the version and the eGPU stops enumerating, the
# first thing to try is HSA_OVERRIDE_GFX_VERSION=10.3.0 — and then record
# what you found, because it changes the platform's contract.
#
# The user also needs render + video group membership to open the render
# nodes unprivileged. That takes effect on next login, not immediately.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
need_root

say "ROCm ${ROCM_VERSION}"
U="$(target_user)"

KEYRING=/etc/apt/keyrings/rocm.gpg
LIST=/etc/apt/sources.list.d/rocm.list
REPO="https://repo.radeon.com/rocm/apt/${ROCM_VERSION}"
CODENAME="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-noble}")"

if [ -e /opt/rocm/.info/version ] &&
   [ "$(cat /opt/rocm/.info/version 2>/dev/null)" = "${ROCM_VERSION}" ]; then
  skip "ROCm ${ROCM_VERSION} already installed at /opt/rocm"
else
  say "adding AMD ROCm apt repo (${REPO} ${CODENAME})"
  install -d -m 0755 /etc/apt/keyrings
  if [ ! -s "$KEYRING" ]; then
    curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key \
      | gpg --dearmor -o "$KEYRING" || die "could not fetch AMD signing key"
    chmod 0644 "$KEYRING"
  fi
  echo "deb [arch=amd64 signed-by=${KEYRING}] ${REPO} ${CODENAME} main" > "$LIST"

  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || die "apt-get update failed after adding the ROCm repo"
  # rocm-hip-sdk pulls the HIP compiler, runtime and math libraries that
  # llama.cpp's HIP backend links against. rocminfo/rocm-smi are the
  # diagnostic pair every verification step in this repo leans on.
  apt-get install -y rocm-hip-sdk rocminfo rocm-smi-lib \
    || die "ROCm install failed"
fi

# --- groups -----------------------------------------------------------------
# render owns /dev/dri/renderD*, video owns /dev/dri/card*. Without both,
# HIP and Vulkan both fail as "no device" for an unprivileged process while
# root sees the card perfectly — a confusing failure worth preventing.
changed=0
for g in render video; do
  if getent group "$g" >/dev/null 2>&1; then
    if id -nG "$U" | tr ' ' '\n' | grep -qx "$g"; then
      skip "$U already in group $g"
    else
      usermod -aG "$g" "$U" && { ok "added $U to $g"; changed=1; }
    fi
  else
    warn "group $g does not exist on this host"
  fi
done
[ "$changed" = 1 ] && warn "group changes take effect on NEXT LOGIN (or: newgrp render)"

# --- verify -----------------------------------------------------------------
if [ -x /opt/rocm/bin/rocm_agent_enumerator ]; then
  agents="$(/opt/rocm/bin/rocm_agent_enumerator 2>/dev/null | grep -v '^gfx000$' | tr '\n' ' ')"
  ok "rocm agents: ${agents:-<none>}"
  case " $agents " in
    *" ${IGPU_GFX} "*) ok "iGPU ${IGPU_GFX} enumerated" ;;
    *) warn "iGPU ${IGPU_GFX} NOT enumerated by ROCm" ;;
  esac
  case " $agents " in
    *" ${EGPU_GFX} "*) ok "eGPU ${EGPU_GFX} enumerated" ;;
    *) if egpu_present; then
         warn "eGPU is on the bus but ROCm does not enumerate ${EGPU_GFX}."
         warn "  Try HSA_OVERRIDE_GFX_VERSION=10.3.0 and record the result."
       else
         say "eGPU ${EGPU_GFX} not enumerated — dock detached, expected"
       fi ;;
  esac
else
  warn "rocm_agent_enumerator missing — ROCm install may be incomplete"
fi

ok "ROCm stage done"
