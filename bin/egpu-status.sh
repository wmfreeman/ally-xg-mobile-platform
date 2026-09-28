#!/usr/bin/env bash
# One screen of truth about the eGPU platform. Read-only, no root needed.
#
# Answers, in order: is the card on the bus, is it bound and awake, can
# ROCm and Vulkan see it, is the thermal daemon holding both surfaces, and
# how hot is it. Every line is MEASURED — nothing here is inferred from a
# previous reading or from the config.
set -uo pipefail

EGPU_PCI="${EGPU_PCI:-0000:03:00.0}"
IGPU_PCI="${IGPU_PCI:-0000:09:00.0}"
EGPU_GFX="${EGPU_GFX:-gfx1031}"
SOCK="${SOCK:-/run/glados-thermal.sock}"
UNIT="${UNIT_NAME:-glados-thermal}"

b() { printf '\n\033[1m%s\033[0m\n' "$*"; }
r() { cat "$1" 2>/dev/null || echo '?'; }

b "PCI"
if [ -d "/sys/bus/pci/devices/${EGPU_PCI}" ]; then
  d="/sys/bus/pci/devices/${EGPU_PCI}"
  printf '  eGPU %s  PRESENT\n' "$EGPU_PCI"
  printf '    driver=%s  power/control=%s  runtime=%s\n' \
    "$(basename "$(readlink -f "$d/driver" 2>/dev/null)" 2>/dev/null)" \
    "$(r "$d/power/control")" "$(r "$d/power/runtime_status")"
  lspci -nn -s "${EGPU_PCI#0000:}" 2>/dev/null | sed 's/^/    /'
else
  printf '  eGPU %s  ABSENT — dock detached\n' "$EGPU_PCI"
  printf '    (PCIe enumeration happens at physical connect; a bus rescan\n'
  printf '     cannot bring up a link that is down.)\n'
fi
printf '  iGPU %s  %s\n' "$IGPU_PCI" \
  "$([ -d "/sys/bus/pci/devices/${IGPU_PCI}" ] && echo PRESENT || echo ABSENT)"

b "Render nodes"
ls -l /dev/dri/by-path/ 2>/dev/null | awk 'NR>1{printf "  %s %s %s\n",$9,$10,$11}' \
  || echo '  none'

b "ROCm / Vulkan"
if [ -x /opt/rocm/bin/rocm_agent_enumerator ]; then
  printf '  agents: %s\n' \
    "$(/opt/rocm/bin/rocm_agent_enumerator 2>/dev/null | grep -v '^gfx000$' | tr '\n' ' ')"
else
  echo '  rocm_agent_enumerator not installed'
fi
if command -v gpu_resolve.sh >/dev/null 2>&1; then
  for role in egpu igpu; do
    printf '  %s\n' "$(gpu_resolve.sh "$role" check 2>&1 | head -1)"
  done
  echo '  (indices are valid until the next reboot/hotplug — never persist them)'
fi

b "Thermal daemon"
if systemctl is-active --quiet "${UNIT}.service" 2>/dev/null; then
  printf '  %s.service active\n' "$UNIT"
else
  printf '  %s.service NOT ACTIVE\n' "$UNIT"
fi
if [ -S "$SOCK" ]; then
  python3 - "$SOCK" <<'PY' 2>/dev/null || echo '  socket read failed (are you in the socket group?)'
import socket, sys, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5)
s.connect(sys.argv[1]); s.sendall(b'status\n')
d = json.loads(s.recv(65536).decode())
for k in ('ally', 'dock'):
    su = d['surfaces'][k]
    extra = f"pwm={d['dock_applied_pwm']}" if k == 'dock' else \
            f"cpu={d['cpu_fan_rpm']}rpm gpu={d['gpu_fan_rpm']}rpm"
    print(f"  {k:5s} rung={su['rung']}/{su['rung_count']} {extra} "
          f"temp={su['temp_c']}C ({su['temp_name']})")
    print(f"        {su['descent_blocked_by']}")
print(f"  dock_attached={d['dock_attached']} ally_applied={d['ally_applied']} "
      f"verify_failed={d['verify_failed']} backoff={d['backoff_mult']}")
PY
else
  echo "  no control socket at $SOCK"
fi

b "Thermals"
H=''
for h in /sys/bus/pci/devices/"${EGPU_PCI}"/hwmon/hwmon*/; do
  [ -d "$h" ] && [ "$(r "$h/name")" = amdgpu ] && H="${h%/}"
done
if [ -n "$H" ]; then
  printf '  edge=%sC junction=%sC mem=%sC power=%sW busy=%s%%\n' \
    $(( $(r "$H/temp1_input")/1000 )) $(( $(r "$H/temp2_input")/1000 )) \
    $(( $(r "$H/temp3_input")/1000 )) $(( $(r "$H/power1_average")/1000000 )) \
    "$(r "/sys/bus/pci/devices/${EGPU_PCI}/gpu_busy_percent")"
  printf '  power cap=%sW (min=%s max=%s)\n' \
    $(( $(r "$H/power1_cap")/1000000 )) $(( $(r "$H/power1_cap_min")/1000000 )) \
    $(( $(r "$H/power1_cap_max")/1000000 ))
  j=$(( $(r "$H/temp2_input")/1000 ))
  [ "$j" -ge 90 ] && printf '  *** JUNCTION %sC — SW CTF trips at 100C ***\n' "$j"
else
  echo '  no amdgpu hwmon (card absent, or in D3cold with sensors blinded)'
fi

printf '\n  ppfeaturemask=%s (OverDrive wants 0xfff7ffff)\n' \
  "$(r /sys/module/amdgpu/parameters/ppfeaturemask)"
echo
