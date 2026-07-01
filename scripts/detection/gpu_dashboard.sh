#!/bin/bash

clear

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                    🚀 GPU LIVE DASHBOARD                    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# === GPU SUMMARY ===
nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit \
--format=csv,noheader,nounits | while IFS=',' read -r name temp util mem_used mem_total power power_limit
do
    mem_perc=$((mem_used * 100 / mem_total))
    power=$(printf "%.0f" "$power")
    power_limit=$(printf "%.0f" "$power_limit")

    printf "🧠 GPU        : %s\n" "$name"
    printf "🌡️ Temp       : %s°C\n" "$temp"
    printf "⚡ Utilization: %s%%\n" "$util"
    printf "🔋 Power      : %sW / %sW (%s%%)\n" "$power" "$power_limit" "$power_perc"

    # Memory bar
    bar_len=40
    filled=$((mem_perc * bar_len / 100))
    empty=$((bar_len - filled))

    printf "💾 Memory     : ["
    printf "%0.s█" $(seq 1 $filled)
    printf "%0.s░" $(seq 1 $empty)
    printf "] %s%% (%sMB / %sMB)\n" "$mem_perc" "$mem_used" "$mem_total"
done

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                    ⚙️ GPU PROCESSES                         ║"
echo "╚══════════════════════════════════════════════════════════════╝"

printf "%-8s %-10s %-12s %s\n" "PID" "TYPE" "VRAM(MB)" "COMMAND"
echo "────────────────────────────────────────────────────────────────────────────"

# === PROCESS INFO ===
nvidia-smi --query-compute-apps=pid,process_name,used_memory \
--format=csv,noheader,nounits | while IFS=',' read -r pid pname mem
do
    cmd=$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)

    # fallback if cmdline empty
    if [ -z "$cmd" ]; then
        cmd=$pname
    fi

    printf "%-8s %-10s %-12s %s\n" "$pid" "$pname" "$mem" "$cmd"
done

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                    🔍 QUICK INSIGHTS                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"

# === INSIGHTS ===
util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits)
mem_used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
mem_total=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits)

mem_perc=$((mem_used * 100 / mem_total))

if [ "$util" -gt 80 ]; then
    echo "🔥 GPU is heavily utilized"
else
    echo "🟢 GPU utilization is moderate"
fi

if [ "$mem_perc" -gt 80 ]; then
    echo "⚠️ VRAM almost full"
else
    echo "🟢 VRAM usage is healthy"
fi

echo ""
echo "💡 Tip: Use 'watch -n 1 ./gpu_dashboard.sh' for live view"