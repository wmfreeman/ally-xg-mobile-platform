#!/usr/bin/env bash
# Stage 40 — SystemThermalDaemon: the system's single thermal authority.
#
# Installs the daemon, its unit and its default config, then enables it.
# This is the stage that makes the box safe to put load on, so it is also
# the stage that refuses to be half-done.
#
# WHAT IT OWNS, and why nothing else may: the Ally's cpu_fan + gpu_fan via
# asus_custom_fan_curve, the XG Mobile dock fan over USB HID 0x5E, the
# platform profile, and the eGPU's runtime PM. On 2026-08-07 this host
# hard-powered-off mid-inference —
#   amdgpu 0000:03:00.0: ERROR: GPU over temperature range(SW CTF) detected!
# — because two fan curves were CAPPED, not slow. SW CTF debounces 50 ms
# then calls orderly_poweroff() on the HOST: no throttle stage, no GPU-only
# reset. One owner, asserting on a loop, is the fix.
#
# THE DEPLOYED NAMES ARE LOAD-BEARING. The unit is glados-thermal.service
# and the socket is /run/glados-thermal.sock. The socket path is compiled
# into the daemon, and Palo_Toolkit's core/lifecycle/fan_client.py connects
# to it by that exact path. Renaming the unit here without rebuilding the
# daemon and every consumer gives you a host whose fans run but whose tools
# cannot see them.
#
# NO UDEV RULE, deliberately. Earlier versions installed one that restarted
# the service on dock hotplug, because a re-plugged eGPU returns with
# runtime PM "auto", drops to D3cold and blinds the sensors (FINDINGS s7).
# The daemon has since grown a per-tick re-assert of power/control=on plus
# per-tick dock re-detection, so the restart bought nothing and cost a curve
# gap while the unit bounced. Startup attach and hotplug attach are now the
# same code path: see stages/45-egpu-attach.sh for what that guarantees.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
need_root
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"

say "thermal daemon (${UNIT_NAME})"

# --- uninstall --------------------------------------------------------------
if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now "${UNIT_NAME}.service" 2>/dev/null
  rm -f "$UNIT_PATH" "$DAEMON_PATH" "$CONF_PATH"
  rm -f /etc/udev/rules.d/99-ally-egpu-fans.rules \
        /etc/udev/rules.d/99-glados-egpu-fans.rules
  systemctl daemon-reload
  udevadm control --reload 2>/dev/null
  # Hand the fans back to firmware rather than leaving them wherever the
  # daemon last put them.
  for h in /sys/class/hwmon/hwmon*/; do
    [ "$(cat "$h/name" 2>/dev/null)" = "asus_custom_fan_curve" ] || continue
    for ch in 1 2; do
      [ -e "$h/pwm${ch}_enable" ] && echo 0 > "$h/pwm${ch}_enable" 2>/dev/null
    done
  done
  [ -e "/sys/bus/pci/devices/${EGPU_PCI}/power/control" ] &&
    echo auto > "/sys/bus/pci/devices/${EGPU_PCI}/power/control"
  [ -w /sys/firmware/acpi/platform_profile ] &&
    echo balanced > /sys/firmware/acpi/platform_profile
  ok "uninstalled; factory fan curves restored"
  exit 0
fi

# --- one daemon per knob ----------------------------------------------------
# Two daemons on one fan is the failure this project has hit twice. Refuse
# rather than create it.
for other in ally-thermal glados-thermal system-thermal-daemon; do
  [ "$other" = "${UNIT_NAME}" ] && continue
  if systemctl cat "${other}.service" >/dev/null 2>&1; then
    die "REFUSING: ${other}.service also exists on this host.
Two daemons on the same fans fight each other. Remove the other unit first:
  sudo systemctl disable --now ${other}.service && sudo rm /etc/systemd/system/${other}.service"
  fi
done

# --- the control socket's group --------------------------------------------
# SOCKET_GROUP is compiled into the daemon. If the group does not exist the
# daemon deliberately leaves the socket 0600 (root only) rather than
# widening it — safe, but unprivileged status reads stop working.
SOCK_GROUP="$(grep -oP '^SOCKET_GROUP\s*=\s*"\K[^"]+' "$HERE/thermal/bin/system-thermal-daemon.py" | head -1)"
SOCK_GROUP="${SOCK_GROUP:-glados}"
U="$(target_user)"
if getent group "$SOCK_GROUP" >/dev/null 2>&1; then
  ok "socket group '$SOCK_GROUP' exists"
else
  groupadd "$SOCK_GROUP" && ok "created socket group '$SOCK_GROUP'"
fi
if id -nG "$U" | tr ' ' '\n' | grep -qx "$SOCK_GROUP"; then
  skip "$U already in '$SOCK_GROUP'"
else
  usermod -aG "$SOCK_GROUP" "$U" &&
    warn "added $U to '$SOCK_GROUP' — takes effect on NEXT LOGIN"
fi

# --- install ----------------------------------------------------------------
install -m 0755 "$HERE/thermal/bin/system-thermal-daemon.py" "$DAEMON_PATH"
ok "daemon -> $DAEMON_PATH"

# The unit ships with the deployed ExecStart already correct; rewrite the
# path anyway so an overridden DAEMON_PATH cannot produce a unit pointing
# at a file that is not there.
sed -e "s#^ExecStart=.*#ExecStart=${DAEMON_PATH}#" \
    "$HERE/thermal/systemd/glados-thermal.service" > "$UNIT_PATH"
chmod 0644 "$UNIT_PATH"
ok "unit -> $UNIT_PATH"

# An EXISTING config is the operator's tuning. Reinstalling must never
# silently revert a tuned host. The daemon logs which source it ran either
# way, and refuses a bad file loudly in favour of its compiled-in defaults.
if [ -e "$CONF_PATH" ]; then
  skip "kept existing $CONF_PATH (operator tuning)"
else
  install -m 0644 "$HERE/thermal/config/system-thermal-daemon.conf" "$CONF_PATH"
  ok "config -> $CONF_PATH (shipped defaults)"
fi

systemctl daemon-reload
systemctl enable "${UNIT_NAME}.service" >/dev/null 2>&1
systemctl restart "${UNIT_NAME}.service" || die "daemon failed to start — journalctl -u ${UNIT_NAME} -n 50"

# The daemon asserts on a 10 s tick; give it two ticks plus slack before
# judging it, and do it by polling rather than assuming.
for _ in $(seq 1 12); do
  systemctl is-active --quiet "${UNIT_NAME}.service" || break
  [ -S /run/"${UNIT_NAME}".sock ] && break
  sleep 2
done

systemctl is-active --quiet "${UNIT_NAME}.service" \
  || die "daemon is not running — journalctl -u ${UNIT_NAME} -n 50"
ok "${UNIT_NAME}.service active"

# CONFIG REFUSED means the daemon rejected the file and fell back to
# compiled-in defaults. The fans are safe, but the tuning is NOT what the
# file says, so surface it rather than letting it sit in the journal.
if journalctl -u "${UNIT_NAME}" -n 200 --no-pager 2>/dev/null | grep -q 'CONFIG REFUSED'; then
  warn "the daemon REFUSED $CONF_PATH and is running compiled-in defaults:"
  journalctl -u "${UNIT_NAME}" -n 200 --no-pager 2>/dev/null | grep 'CONFIG REFUSED' | tail -5
fi

ok "thermal stage done — watch it with: journalctl -u ${UNIT_NAME} -f"
