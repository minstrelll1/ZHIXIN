#!/usr/bin/env bash
set -eo pipefail
MODEL=p600
if [[ "${1:-}" == --model ]]; then MODEL="${2:-}"; elif [[ $# -gt 0 ]]; then echo '用法：bash tools/build_onboard.sh --model p600|su17'; exit 2; fi
case "$MODEL" in p600|su17) ;; *) echo '机型必须是 p600 或 su17。'; exit 2 ;; esac
COMPETITION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_ROOT="${COMPETITION_VENDOR_WORKSPACE:-${HOME}/${MODEL}_experiment}"
source /opt/ros/noetic/setup.bash
source "${VENDOR_ROOT}/devel/setup.bash" --extend
cd "$COMPETITION_ROOT"
echo "正在编译竞赛程序（$MODEL），厂商目录仅作为已有依赖读取。"
catkin_make --build "build_${MODEL}" -DCATKIN_DEVEL_PREFIX="${COMPETITION_ROOT}/devel_${MODEL}" \
  --only-pkg-with-deps su17_competition_executor su17_image_transfer su17_pointcloud_bridge
