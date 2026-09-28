#!/usr/bin/env bash
# Stage 50 — llama.cpp, both backends.
#
# TWO BUILDS, ON PURPOSE:
#   build/     HIP/ROCm   AMDGPU_TARGETS=gfx1031;gfx1103  — one binary that
#              covers BOTH cards, so the eGPU and the iGPU can serve
#              concurrently without a second checkout.
#   build-vk/  Vulkan     — the fallback path, and the reason gpu_resolve.sh
#              has a separate `vkindex` verb at all.
#
# WHY BOTH MATTER TOGETHER: HIP and Vulkan enumerate devices in DIFFERENT
# ORDERS, and the relationship is not stable across boots. Handing a HIP
# index to a Vulkan build pins the wrong card — silently, and it still runs.
# Always resolve per launch with gpu_resolve.sh.
#
# Builds run as the invoking user, NOT root: the checkout and the build tree
# belong to a human account, and a root-owned build tree is a nuisance to
# clean up after.
#
# This stage is long. It is deliberately in the default run anyway — a
# platform rebuild that stops short of a working inference binary has not
# rebuilt the platform.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

U="$(target_user)"
H="$(target_home)"
DIR="${LLAMA_DIR:-$H/llama.cpp}"
JOBS="${JOBS:-$(nproc)}"

say "llama.cpp -> $DIR (as $U, -j$JOBS)"

asuser() { sudo -u "$U" -H "$@"; }

# --- checkout ---------------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  have_commit="$(asuser git -C "$DIR" rev-parse HEAD 2>/dev/null || echo '')"
  if [ "$have_commit" = "$LLAMA_COMMIT" ]; then
    skip "already at pinned commit ${LLAMA_COMMIT:0:12}"
  else
    say "fetching pinned commit ${LLAMA_COMMIT:0:12}"
    asuser git -C "$DIR" fetch --depth 1 origin "$LLAMA_COMMIT" \
      || asuser git -C "$DIR" fetch origin || die "git fetch failed"
    asuser git -C "$DIR" checkout -q "$LLAMA_COMMIT" \
      || die "could not check out $LLAMA_COMMIT"
    ok "checked out ${LLAMA_COMMIT:0:12}"
  fi
else
  say "cloning $LLAMA_REPO"
  asuser git clone "$LLAMA_REPO" "$DIR" || die "git clone failed"
  asuser git -C "$DIR" checkout -q "$LLAMA_COMMIT" \
    || warn "could not pin $LLAMA_COMMIT — building whatever HEAD is"
fi

# --- HIP build --------------------------------------------------------------
# hipcc must come from the ROCm install, not from PATH ambiguity.
say "configuring HIP build (gfx1031;gfx1103)"
asuser env \
  ROCM_PATH=/opt/rocm \
  PATH="/opt/rocm/bin:/opt/rocm/llvm/bin:$PATH" \
  HIPCXX=/opt/rocm/llvm/bin/clang++ \
  cmake -S "$DIR" -B "$DIR/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_HIP=ON \
    -DAMDGPU_TARGETS="gfx1031;gfx1103" \
    -DGGML_HIP_GRAPHS=ON \
    -DGGML_HIP_NO_VMM=ON \
  || die "HIP cmake configure failed"

say "building HIP (this takes a while)"
asuser cmake --build "$DIR/build" --config Release -j "$JOBS" \
  || die "HIP build failed"
ok "HIP build -> $DIR/build/bin"

# --- Vulkan build -----------------------------------------------------------
say "configuring Vulkan build"
if asuser cmake -S "$DIR" -B "$DIR/build-vk" \
     -DCMAKE_BUILD_TYPE=Release \
     -DGGML_VULKAN=ON \
     -DGGML_HIP=OFF; then
  say "building Vulkan"
  if asuser cmake --build "$DIR/build-vk" --config Release -j "$JOBS"; then
    ok "Vulkan build -> $DIR/build-vk/bin"
  else
    # The Vulkan backend is the fallback path, not the primary one. A
    # failure here must not throw away a good HIP build.
    warn "Vulkan build FAILED — HIP build is intact and usable."
    warn "  Usually a missing shader compiler: apt install glslc"
  fi
else
  warn "Vulkan cmake configure failed — skipping that backend"
fi

# --- report -----------------------------------------------------------------
for b in "$DIR/build/bin/llama-cli" "$DIR/build-vk/bin/llama-cli"; do
  [ -x "$b" ] && ok "built: $b" || warn "absent: $b"
done

say "pin a card at launch, never by a remembered index:"
say "  HIP:    HIP_VISIBLE_DEVICES=\$(gpu_resolve.sh egpu index)   $DIR/build/bin/llama-cli ..."
say "  Vulkan: GGML_VK_VISIBLE_DEVICES=\$(gpu_resolve.sh egpu vkindex) $DIR/build-vk/bin/llama-cli ..."
say "  and run it under thermal-guard.sh so a blinded sensor aborts the job."
ok "llama.cpp stage done"
