#!/usr/bin/env bash
set -eo pipefail
COMPETITION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL=p600
ARGS=("$@")
for ((i=0; i<${#ARGS[@]}; i++)); do
  if [[ "${ARGS[$i]}" == --model ]]; then MODEL="${ARGS[$((i+1))]:-}"; fi
  if [[ "${ARGS[$i]}" == --model=* ]]; then MODEL="${ARGS[$i]#--model=}"; fi
  if [[ "${ARGS[$i]}" == --help || "${ARGS[$i]}" == -h ]]; then exec python3 "$COMPETITION_ROOT/tools/onboard_preflight.py" --help; fi
done
case "$MODEL" in p600|su17) ;; *) echo '机型必须是 p600 或 su17。' >&2; exit 2 ;; esac
VENDOR_ROOT="${COMPETITION_VENDOR_WORKSPACE:-${HOME}/${MODEL}_experiment}"
COMPETITION_SETUP="${COMPETITION_ROOT}/devel_${MODEL}/setup.bash"
if [[ ! -r "$COMPETITION_SETUP" ]]; then
  echo "请先执行 bash tools/build_onboard.sh --model $MODEL" >&2; exit 1
fi
for setup_file in /opt/ros/noetic/setup.bash "${VENDOR_ROOT}/devel/setup.bash" "$COMPETITION_SETUP"; do
  if [[ ! -r "$setup_file" ]]; then echo "环境文件不可读：$setup_file" >&2; exit 1; fi
done
source /opt/ros/noetic/setup.bash
source "${VENDOR_ROOT}/devel/setup.bash" --extend
source "$COMPETITION_SETUP" --extend
if [[ -r "${COMPETITION_ROOT}/tools/local_tokens.env" ]]; then
  set -a
  source "${COMPETITION_ROOT}/tools/local_tokens.env"
  set +a
fi
export PYTHONPATH="${COMPETITION_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

# roslaunch 对同名节点采用“后启动者替换先启动者”的规则，重复执行
# 本脚本会导致旧节点被踢出并不断重启。启动前只检查本竞赛栈的三个
# 固定节点；全部存在时直接复用，部分存在时拒绝再次启动，避免破坏
# 已经运行的图像回传或任务执行链路。
if command -v rosnode >/dev/null 2>&1; then
  existing_nodes="$(rosnode list 2>/dev/null || true)"
  competition_nodes=(
    "/competition_image_stamp_adapter"
    "/su17_competition_executor"
    "/su17_onboard_image_sender"
  )
  existing_count=0
  for node in "${competition_nodes[@]}"; do
    if printf '%s\n' "$existing_nodes" | grep -Fxq "$node"; then
      existing_count=$((existing_count + 1))
    fi
  done
  if [[ "$existing_count" -eq "${#competition_nodes[@]}" ]]; then
    echo "竞赛机载程序已在运行，本次跳过重复启动。"
    exit 0
  fi
  if [[ "$existing_count" -gt 0 ]]; then
    echo "检测到竞赛机载程序已有 ${existing_count}/${#competition_nodes[@]} 个节点运行，拒绝重复启动。请先完整停止旧竞赛程序，再重新启动。" >&2
    exit 1
  fi
fi
exec python3 "$COMPETITION_ROOT/tools/onboard_preflight.py" "$@"
