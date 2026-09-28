#!/usr/bin/env bash
# Standalone thermal guard: run a command, SIGSTOP it while the GPU is hot.
#
# Reference implementation of THERMAL-GUARD.md for workloads you cannot
# instrument from the inside. Pauses the whole process group rather than
# gating individual calls, so it is coarser than an in-process guard — but it
# needs nothing from the workload and works on anything.
#
#   ./thermal-guard.sh -- python train.py
#   ./thermal-guard.sh --pause 55 --resume 45 --csv run.csv -- ./benchmark
#
# Gates on EDGE (temp1_input), not junction: junction is a hotspot that moves
# 30 C in half a second and will resume you straight back into full load.
# See THERMAL-GUARD.md and FINDINGS.md section 4.
set -uo pipefail

PCI="${GUARD_PCI:-0000:03:00.0}"
PAUSE=58; RESUME=48; CEIL=95; POLL=0.5; MAXWAIT=300; CSV=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pause)  PAUSE=$2; shift 2 ;;
    --resume) RESUME=$2; shift 2 ;;
    --ceiling) CEIL=$2; shift 2 ;;
    --poll)   POLL=$2; shift 2 ;;
    --max-wait) MAXWAIT=$2; shift 2 ;;
    --csv)    CSV=$2; shift 2 ;;
    --pci)    PCI=$2; shift 2 ;;
    --) shift; break ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ $# -gt 0 ] || { echo "usage: $0 [opts] -- <command...>" >&2; exit 2; }

H=$(echo /sys/bus/pci/devices/$PCI/hwmon/hwmon* 2>/dev/null)
[ -d "$H" ] || { echo "thermal-guard: no hwmon at $PCI — running UNGUARDED" >&2; exec "$@"; }
[ -r "$H/temp1_input" ] || { echo "thermal-guard: edge sensor unreadable (D3cold? try 'echo on > /sys/bus/pci/devices/$PCI/power/control') — running UNGUARDED" >&2; exec "$@"; }

edge() { echo $(( $(cat "$H/temp1_input" 2>/dev/null || echo 0)/1000 )); }
junc() { echo $(( $(cat "$H/temp2_input" 2>/dev/null || echo 0)/1000 )); }
watt() { echo $(( $(cat "$H/power1_average" 2>/dev/null || echo 0)/1000000 )); }

echo "thermal-guard: gate on EDGE — pause ${PAUSE}C, resume ${RESUME}C, ceiling junction ${CEIL}C, poll ${POLL}s"
setsid "$@" &
PID=$!
[ -n "$CSV" ] && echo "ts,edge_c,junction_c,watts,state" > "$CSV"

state=run; pauses=0; paused_total=0; peak=0; fails=0
trap 'kill -CONT -$PID 2>/dev/null; kill -TERM -$PID 2>/dev/null' EXIT INT TERM

while kill -0 "$PID" 2>/dev/null; do
  e=$(edge); j=$(junc)
  if [ "$e" -eq 0 ]; then
    fails=$((fails+1))
    [ $fails -eq 3 ] && echo "thermal-guard: sensor stopped responding — continuing UNGUARDED" >&2
    [ $fails -ge 3 ] && { kill -CONT -$PID 2>/dev/null; sleep "$POLL"; continue; }
  else fails=0; fi
  [ "$e" -gt "$peak" ] && peak=$e

  if [ "$state" = run ] && { [ "$e" -ge "$PAUSE" ] || [ "$j" -ge "$CEIL" ]; }; then
    kill -STOP -$PID 2>/dev/null; state=stop; pauses=$((pauses+1)); t0=$SECONDS
    printf 'thermal-guard: edge %sC (junction %sC) — cooling off, resuming at %sC\n' "$e" "$j" "$RESUME" >&2
  elif [ "$state" = stop ]; then
    if [ "$e" -le "$RESUME" ]; then
      kill -CONT -$PID 2>/dev/null; state=run
      w=$((SECONDS-t0)); paused_total=$((paused_total+w))
      printf 'thermal-guard: resumed at edge %sC after %ss (junction %sC)\n' "$e" "$w" "$j" >&2
    elif [ $((SECONDS-t0)) -ge "$MAXWAIT" ]; then
      printf 'thermal-guard: edge did not fall below %sC within %ss (still %sC). Killing.\n' "$RESUME" "$MAXWAIT" "$e" >&2
      printf '               Check airflow is not obstructed and the fan service is running.\n' >&2
      kill -CONT -$PID 2>/dev/null; kill -TERM -$PID 2>/dev/null; break
    fi
  fi
  [ -n "$CSV" ] && { echo "$(date +%s),$e,$j,$(watt),$state" >> "$CSV"; sync -d "$CSV" 2>/dev/null; }
  sleep "$POLL"
done

wait "$PID" 2>/dev/null; rc=$?
echo "thermal-guard: ${pauses} pause(s), ${paused_total}s total, peak edge ${peak}C — child rc=$rc" >&2
exit $rc
