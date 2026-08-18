#!/usr/bin/env bash
# vm-usage.sh — Show VM allocation vs actual container resource usage.
# Usage: scripts/vm-usage.sh

set -euo pipefail

bold=$(tput bold 2>/dev/null || true)
reset=$(tput sgr0 2>/dev/null || true)

# ── VM info ─────────────────────────────────────────────────────────────
vm_cpus=$(docker info --format '{{.NCPU}}' 2>/dev/null)
vm_mem_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null)
vm_mem_mib=$(awk "BEGIN { printf \"%.0f\", $vm_mem_bytes / 1048576 }")
vm_mem_gib=$(awk "BEGIN { printf \"%.1f\", $vm_mem_bytes / 1073741824 }")

echo "${bold}VM Allocation${reset}"
echo "  CPUs: ${vm_cpus}  |  RAM: ${vm_mem_gib} GiB (${vm_mem_mib} MiB)"
echo ""

# ── Per-container stats ─────────────────────────────────────────────────
stats=$(docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null)

if [ -z "$stats" ]; then
  echo "No running containers."
  exit 0
fi

echo "${bold}Container Usage${reset}"
printf "  %-40s %7s  %22s  %6s\n" "NAME" "CPU %" "MEM USAGE / LIMIT" "MEM %"
echo "  $(printf '%.0s─' {1..80})"
echo "$stats" | sort -t$'\t' -k4 -rn | while IFS=$'\t' read -r name cpu mem pct; do
  printf "  %-40s %7s  %22s  %6s\n" "$name" "$cpu" "$mem" "$pct"
done
echo ""

# ── Aggregates ──────────────────────────────────────────────────────────
echo "${bold}Summary${reset}"
docker stats --no-stream --format '{{.MemUsage}}\t{{.CPUPerc}}' 2>/dev/null | awk -F'\t' -v vm_mib="$vm_mem_mib" '
{
  # Parse memory used
  split($1, parts, "/")
  split(parts[1], a, " "); val = a[1]; unit = a[2]
  if (unit == "GiB") val *= 1024
  used += val

  # Parse memory limit
  split(parts[2], b, " "); lval = b[1]; lunit = b[2]
  if (lunit == "GiB") lval *= 1024
  limit += lval

  # Parse CPU
  gsub(/%/, "", $2)
  cpu += $2
  count++
}
END {
  headroom = vm_mib - used
  pct_used = (used / vm_mib) * 100
  printf "  Containers:    %d\n", count
  printf "  CPU used:      %.1f%%\n", cpu
  printf "  RAM used:      %.0f MiB / %s MiB  (%.0f%%)\n", used, vm_mib, pct_used
  printf "  RAM limits:    %.0f MiB  (sum of compose memory caps)\n", limit
  printf "  RAM headroom:  %.0f MiB  (%.0f%% free)\n", headroom, 100 - pct_used
  printf "\n"
  if (pct_used > 85)
    printf "  ⚠️  RAM usage above 85%% — consider increasing VM memory\n"
  else if (pct_used < 30)
    printf "  💡 RAM usage below 30%% — VM may be over-provisioned\n"
  else
    printf "  ✅ RAM usage looks well-balanced\n"
}
'
