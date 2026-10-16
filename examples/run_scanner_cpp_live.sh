#!/usr/bin/env bash
# This will start the ultrasound hardware.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
run=${1:-12}
usage='Usage: run_scanner_cpp_live.sh [frames: 1..1024|continuous] [--max-seconds=N] [--no-display] [--pretty] [--mimicknet] [--rollout-length=1|32] [--queue-capacity=1..8] [--ibv-name=DEVICE] [--rx-arm-delay-s=N] [--fpga-auto-sync-start] [--fpga-auto-sync-guard-us=N] [--fpga-auto-sync-pri-us=N] [--tx-dual-bank] [--fpga-tx-release-lead-us=N] [--rx-atten-code=0..48] [--rx-source-ohms=100|200|400|800] [--tx-dtgc-enable] [--tx-dtgc-start-db=N] [--tx-dtgc-start-us=N] [--tx-dtgc-ramp-us=N] [--record-rf-dir=NEW_PATH --record-rf-frames=N --record-rf-skip=N]'
if [[ $run == continuous ]]; then
    stopping=(--continuous)
    description='continuous acquisition'
elif [[ $run =~ ^[0-9]+$ ]] && (( run >= 1 && run <= 1024 )); then
    stopping=(--frame-limit "$run")
    description="$run frames"
else
    echo "$usage" >&2
    exit 2
fi
if (( $# )); then shift; fi
for option in "$@"; do
    case "$option" in
        --max-seconds=*|--no-display|--pretty|--mimicknet|--rollout-length=1|--rollout-length=32|--queue-capacity=*|--ibv-name=*|--rx-arm-delay-s=*|--fpga-auto-sync-start|--fpga-auto-sync-guard-us=*|--fpga-auto-sync-pri-us=*|--tx-dual-bank|--fpga-tx-release-lead-us=*|--rx-atten-code=*|--rx-source-ohms=*|--tx-dtgc-enable|--tx-dtgc-start-db=*|--tx-dtgc-start-us=*|--tx-dtgc-ramp-us=*|--record-rf-dir=*|--record-rf-frames=*|--record-rf-skip=*) ;;
        *) echo "Unsupported option: $option" >&2; exit 2 ;;
    esac
done
python=${NVUS_CPP_PYTHON:-/opt/nvus-env/bin/python}
cuda_environment=()
if [[ -n ${NVUS_CUDA_VISIBLE_DEVICES:-} ]]; then
    cuda_environment=(-e "CUDA_VISIBLE_DEVICES=$NVUS_CUDA_VISIBLE_DEVICES")
fi
# Import check is idle; an old commissioned extension must not silently run.
docker exec -w "$repo" -e NVUS_USE_INSTALLED=1 \
    "${cuda_environment[@]}" nvus "$python" -c \
    'from nvus import _scanner as cpp; c=cpp.HsbExecutionConfig(); h=cpp.HololinkTransportConfig(); limits=cpp.PolicyLimits(); assert cpp.sdk_available and cpp.native_source_sha256 and hasattr(c, "fpga_auto_start") and hasattr(c, "fpga_auto_guard_ticks") and hasattr(c, "fpga_auto_interval_ticks") and hasattr(c, "fpga_tx_release_ticks") and hasattr(c, "tx_dual_bank") and hasattr(c, "allow_sequence_wrap") and hasattr(h, "rx_arm_delay_s") and hasattr(limits, "continuous")'
mkdir -p "$repo/.runtime"
output=$(mktemp -d "$repo/.runtime/cpp-live-XXXXXX")
echo "LIVE C++ control, $description. Results: $output"
command=(docker exec -w "$repo" -e NVUS_USE_INSTALLED=1
    "${cuda_environment[@]}"
    -e HOLOLINK_ROCE_DIAGNOSTICS
    -e "MIMICKNET_ENGINE_PATH=${MIMICKNET_ENGINE_PATH:-$repo/.runtime/mimicknet.engine}"
    -e "DISPLAY=${NVUS_DISPLAY:-:1}"
    -e "XAUTHORITY=${NVUS_XAUTHORITY:-/home/nvidia/.Xauthority}"
    -e PYTHONUNBUFFERED=1 -e NVUS_TIMING=1 nvus)
if [[ $run != continuous ]]; then
    command+=(timeout --signal=INT --kill-after=10s 180s)
fi
command+=("$python" examples/scanner_cpp_live.py --commissioning
    "${stopping[@]}" --report "$output/report.json")
"${command[@]}" "$@" 2>&1 | tee "$output/run.log"
