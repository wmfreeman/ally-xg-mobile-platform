#!/usr/bin/env bash
# Per-second eGPU telemetry. Run in a second terminal DURING a live inference
# run. No root needed. Ctrl-C to stop.
#   ./gpu-watch.sh            -> stdout
#   ./gpu-watch.sh out.csv    -> stdout + CSV
EGPU_PCI="0000:03:00.0"
EG=""; for h in /sys/bus/pci/devices/$EGPU_PCI/hwmon/hwmon*/; do [ -d "$h" ] && EG="${h%/}"; done
[ -z "$EG" ] && { echo "eGPU hwmon not found"; exit 1; }
AS=""; for h in /sys/class/hwmon/hwmon*/; do [ "$(cat "$h/name" 2>/dev/null)" = "asus" ] && AS="${h%/}"; done
CSV="${1:-}"; [ -n "$CSV" ] && echo "ts,edge_c,junction_c,mem_c,power_w,egpu_rpm,pwm,cpu_fan_rpm,apu_c" > "$CSV"
r(){ cat "$1" 2>/dev/null || echo 0; }
printf "%-8s %6s %6s %6s %8s %8s %5s %8s %6s\n" TIME EDGE JUNC MEM POWER eGPUrpm PWM CPUrpm APU
printf "%-8s %6s %6s %6s %8s %8s %5s %8s %6s\n" -------- ------ ------ ------ -------- -------- ----- -------- ------
while :; do
  T=$(date +%H:%M:%S)
  E=$(( $(r "$EG/temp1_input") / 1000 )); J=$(( $(r "$EG/temp2_input") / 1000 )); M=$(( $(r "$EG/temp3_input") / 1000 ))
  P=$(( $(r "$EG/power1_average") / 1000000 )); F=$(r "$EG/fan1_input"); W=$(r "$EG/pwm1")
  C=$([ -n "$AS" ] && r "$AS/fan1_input" || echo 0)
  A=$(( $(r /sys/class/hwmon/hwmon4/temp1_input) / 1000 ))
  FLAG=""; [ "$J" -ge 90 ] && FLAG="  <-- JUNCTION HIGH"; [ "$J" -ge 95 ] && FLAG="  <-- *** NEAR CTF (100C) ***"
  printf "%-8s %5sC %5sC %5sC %7sW %8s %5s %8s %5sC%s\n" "$T" "$E" "$J" "$M" "$P" "$F" "$W" "$C" "$A" "$FLAG"
  [ -n "$CSV" ] && echo "$T,$E,$J,$M,$P,$F,$W,$C,$A" >> "$CSV"
  sleep 1
done
