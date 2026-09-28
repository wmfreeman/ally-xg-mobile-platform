#!/usr/bin/env bash
# Stage 60 — verification. Read-only. Proves the platform is actually in the
# state the earlier stages claim, and is safe to re-run any time.
#
# Anything that depends on the eGPU being attached is reported as SKIP when
# the dock is off, never as a failure — the platform is expected to be
# correct with the dock detached.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"

pass=0; failn=0; skipn=0
P() { printf '  \033[32mPASS\033[0m %s\n' "$*"; pass=$((pass+1)); }
F() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; failn=$((failn+1)); }
S() { printf '  \033[33mSKIP\033[0m %s\n' "$*"; skipn=$((skipn+1)); }

say "verification"

# --- thermal ----------------------------------------------------------------
printf '\nThermal\n'
systemctl is-enabled --quiet "${UNIT_NAME}.service" 2>/dev/null \
  && P "${UNIT_NAME}.service enabled at boot" \
  || F "${UNIT_NAME}.service NOT enabled — will not survive reboot"
systemctl is-active --quiet "${UNIT_NAME}.service" 2>/dev/null \
  && P "${UNIT_NAME}.service active" \
  || F "${UNIT_NAME}.service not running"
[ -x "$DAEMON_PATH" ] && P "daemon at $DAEMON_PATH" || F "daemon missing at $DAEMON_PATH"
[ -e "$CONF_PATH" ] && P "config at $CONF_PATH" || F "config missing at $CONF_PATH"

if journalctl -u "${UNIT_NAME}" -n 300 --no-pager 2>/dev/null | grep -q 'CONFIG REFUSED'; then
  F "daemon REFUSED its config and is on compiled-in defaults (journalctl -u ${UNIT_NAME} | grep 'CONFIG REFUSED')"
else
  P "config accepted (no CONFIG REFUSED in the journal)"
fi

# Ally fan surface. This is the one that killed the host when capped.
found=0
for h in /sys/class/hwmon/hwmon*/; do
  [ "$(cat "$h/name" 2>/dev/null)" = "asus_custom_fan_curve" ] && found=1
done
[ "$found" = 1 ] && P "asus_custom_fan_curve present" \
                 || F "asus_custom_fan_curve ABSENT — Ally fans uncontrolled"

# --- control socket ---------------------------------------------------------
printf '\nControl socket\n'
SOCK="/run/${UNIT_NAME}.sock"
if [ -S "$SOCK" ]; then
  P "socket $SOCK exists"
  if python3 - "$SOCK" <<'PY' 2>/dev/null
import socket, sys, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5)
s.connect(sys.argv[1]); s.sendall(b'status\n')
d = json.loads(s.recv(65536).decode())
assert d.get('ok') is True
for k in ('ally', 'dock'):
    su = d['surfaces'][k]
    extra = f"pwm={d['dock_applied_pwm']}" if k == 'dock' else f"cpu={d['cpu_fan_rpm']}rpm"
    print(f"       {k}: rung={su['rung']}/{su['rung_count']} {extra} {su['temp_c']}C")
print(f"       dock_attached={d['dock_attached']} verify_failed={d['verify_failed']}")
PY
  then P "socket answers 'status'"
  else S "socket present but unreadable — are you in the socket group? (re-login)"
  fi
else
  F "no control socket at $SOCK"
fi

# --- OverDrive --------------------------------------------------------------
printf '\nOverDrive\n'
live="$(cat /sys/module/amdgpu/parameters/ppfeaturemask 2>/dev/null || echo '')"
if [ "$live" = "$PPFEATUREMASK" ]; then
  P "ppfeaturemask=$live (OverDrive enabled)"
elif grep -q "amdgpu.ppfeaturemask=${PPFEATUREMASK}" /etc/default/grub 2>/dev/null; then
  F "ppfeaturemask is '$live' but GRUB is configured — REBOOT REQUIRED"
else
  F "ppfeaturemask is '$live' and GRUB is not configured (run stage 30)"
fi

# --- ROCm / Vulkan ----------------------------------------------------------
printf '\nROCm / Vulkan\n'
if [ -x /opt/rocm/bin/rocm_agent_enumerator ]; then
  agents="$(/opt/rocm/bin/rocm_agent_enumerator 2>/dev/null | grep -v '^gfx000$' | tr '\n' ' ')"
  P "rocm agents: ${agents:-<none>}"
  case " $agents " in *" ${IGPU_GFX} "*) P "iGPU ${IGPU_GFX} enumerated";;
                      *) F "iGPU ${IGPU_GFX} not enumerated";; esac
else
  F "rocm_agent_enumerator missing"
fi
have vulkaninfo && P "vulkaninfo present" || F "vulkaninfo missing"

U="$(target_user 2>/dev/null || echo "${SUDO_USER:-$USER}")"
for g in render video; do
  id -nG "$U" 2>/dev/null | tr ' ' '\n' | grep -qx "$g" \
    && P "$U in group $g" || F "$U NOT in group $g (re-login after install)"
done

# --- tools ------------------------------------------------------------------
printf '\nTools\n'
for t in gpu_resolve.sh egpu-status.sh egpu-wait.sh thermal-guard.sh; do
  command -v "$t" >/dev/null 2>&1 && P "$t on PATH" || F "$t not installed"
done
[ -e /usr/local/lib/ally-platform/gpu_roles.py ] \
  && P "gpu_roles.py installed" || F "gpu_roles.py missing"
for r in /etc/udev/rules.d/99-ally-egpu-fans.rules /etc/udev/rules.d/99-glados-egpu-fans.rules; do
  [ -e "$r" ] && F "legacy udev hotplug rule still present: $r" || true
done
P "no legacy udev hotplug rule (correct — the daemon re-asserts per tick)"

# --- eGPU (dock-dependent) --------------------------------------------------
printf '\neGPU\n'
if egpu_present; then
  P "eGPU present at ${EGPU_PCI}"
  drv="$(basename "$(readlink -f "/sys/bus/pci/devices/${EGPU_PCI}/driver" 2>/dev/null)" 2>/dev/null)"
  [ "$drv" = amdgpu ] && P "amdgpu bound" || F "driver is '${drv:-none}', expected amdgpu"
  pm="$(cat "/sys/bus/pci/devices/${EGPU_PCI}/power/control" 2>/dev/null)"
  [ "$pm" = on ] && P "power/control=on (out of D3cold)" \
                 || F "power/control='$pm' — sensors will blind at idle"
  [ -e "/dev/dri/by-path/pci-${EGPU_PCI}-render" ] \
    && P "render node present" || F "render node absent"
  if [ -x /opt/rocm/bin/rocm_agent_enumerator ]; then
    /opt/rocm/bin/rocm_agent_enumerator 2>/dev/null | grep -qx "${EGPU_GFX}" \
      && P "ROCm enumerates ${EGPU_GFX}" \
      || F "ROCm does NOT enumerate ${EGPU_GFX} (try HSA_OVERRIDE_GFX_VERSION=10.3.0)"
  fi
  if H="$(egpu_hwmon)"; then
    P "sensors: edge=$(( $(cat "$H/temp1_input")/1000 ))C junction=$(( $(cat "$H/temp2_input")/1000 ))C power=$(( $(cat "$H/power1_average")/1000000 ))W"
  else
    F "no amdgpu hwmon for the eGPU"
  fi
  if command -v gpu_resolve.sh >/dev/null 2>&1; then
    gpu_resolve.sh egpu check >/dev/null 2>&1 \
      && P "role resolves: $(gpu_resolve.sh egpu check 2>&1 | head -1)" \
      || F "gpu_resolve.sh cannot resolve the egpu role"
  fi
else
  S "eGPU absent (dock detached) — all eGPU checks skipped."
  S "  This is a VALID state: attach the dock and re-run this stage."
fi

# --- llama.cpp --------------------------------------------------------------
printf '\nllama.cpp\n'
H_DIR="$(target_home 2>/dev/null || echo "$HOME")"
D="${LLAMA_DIR:-$H_DIR/llama.cpp}"
[ -x "$D/build/bin/llama-cli" ] && P "HIP binary: $D/build/bin/llama-cli" \
                               || F "HIP binary missing at $D/build/bin/llama-cli"
[ -x "$D/build-vk/bin/llama-cli" ] && P "Vulkan binary: $D/build-vk/bin/llama-cli" \
                                   || S "Vulkan binary absent (fallback backend)"

printf '\n%s\n' "-------------------------------------------"
printf '  %s passed, %s failed, %s skipped\n' "$pass" "$failn" "$skipn"
[ "$failn" -gt 0 ] && { printf '\n  %s check(s) FAILED — see above.\n\n' "$failn"; exit 1; }
printf '\n  platform verified\n\n'
