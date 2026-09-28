#!/usr/bin/env bash
# ROG Ally + XG Mobile eGPU platform — one-command rebuild.
#
#   sudo ./install.sh                 # every stage, in order
#   sudo ./install.sh --list          # show the stages, run nothing
#   sudo ./install.sh --only 40       # run one stage
#   sudo ./install.sh --from 40       # run stage 40 onward
#   sudo ./install.sh --skip 50       # everything except the long build
#   sudo ./install.sh --verify        # read-only check of a live host
#   sudo ./install.sh --uninstall     # remove the thermal daemon
#
# Run it from your own account via sudo, NOT as root directly: several
# stages need to know which human account owns the render/video groups and
# the llama.cpp checkout, and root is never the right answer.
#
# Stages are individually re-runnable; this script is just the ordering.
# A failing stage stops the run, because every later stage assumes the
# earlier ones took.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib/common.sh"

STAGES=(
  "00-preflight.sh:read-only platform checks"
  "10-packages.sh:build toolchain, Vulkan runtime, utilities"
  "20-rocm.sh:ROCm ${ROCM_VERSION} + render/video groups"
  "30-kernel-params.sh:amdgpu OverDrive via GRUB (needs reboot)"
  "40-thermal.sh:SystemThermalDaemon — fan authority"
  "45-egpu-attach.sh:eGPU attach handling + role resolver"
  "50-llama.sh:llama.cpp HIP + Vulkan builds (long)"
  "60-verify.sh:verification"
)

usage() { sed -n '2,26p' "$0" | sed 's/^# \?//'; exit 0; }

list_stages() {
  printf '\nStages:\n'
  for s in "${STAGES[@]}"; do printf '  %-22s %s\n' "${s%%:*}" "${s#*:}"; done
  printf '\n'
}

ONLY=''; FROM=''; SKIP=''
while [ $# -gt 0 ]; do
  case "$1" in
    --list) list_stages; exit 0 ;;
    -h|--help) usage ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    --from) FROM="${2:-}"; shift 2 ;;
    --skip) SKIP="${SKIP} ${2:-}"; shift 2 ;;
    --verify) need_root; exec "$HERE/stages/60-verify.sh" ;;
    --uninstall) need_root; exec "$HERE/stages/40-thermal.sh" --uninstall ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

need_root
U="$(target_user)"

cat <<BANNER

  ROG Ally + XG Mobile eGPU platform
  ----------------------------------
  target user : $U
  home        : $(target_home)
  eGPU        : ${EGPU_PCI} (${EGPU_GFX})   $(egpu_present && echo PRESENT || echo 'ABSENT — dock detached')
  unit        : ${UNIT_NAME}.service
  ROCm        : ${ROCM_VERSION}

BANNER

started=0
for entry in "${STAGES[@]}"; do
  s="${entry%%:*}"; desc="${entry#*:}"
  num="${s%%-*}"

  [ -n "$ONLY" ] && [ "$num" != "$ONLY" ] && continue
  if [ -n "$FROM" ] && [ "$started" = 0 ]; then
    [ "$num" = "$FROM" ] && started=1 || continue
  fi
  case " $SKIP " in *" $num "*) skip "stage $num ($desc) — skipped by request"; continue ;; esac

  printf '\n\033[1m── %s  %s\033[0m\n' "$s" "$desc"
  "$HERE/stages/$s" || die "stage $s FAILED — fix it and re-run:
  sudo $0 --from $num"
done

cat <<DONE

  ----------------------------------
  Rebuild complete.

  Next:
    * If stage 30 said REBOOT REQUIRED, reboot now — the eGPU power cap
      stays locked at 165W until the ppfeaturemask takes.
    * Log out and back in if groups changed (render, video, socket group).
    * Attach the XG Mobile dock. The daemon picks it up within one 10 s
      tick; nothing else is needed at boot.

  Check it:
    egpu-status.sh                    one screen of truth
    sudo $0 --verify                  full pass/fail
    journalctl -u ${UNIT_NAME} -f     watch the fan ladders

DONE
