#!/usr/bin/env bash
set -euo pipefail

UAV_ID="${1:-}"
GROUND_HOST="${2:-}"
AUTH_VALUE="${3:-${AUTH_TOKEN:-}}"

if [[ -z "${UAV_ID}" ]]; then
  echo "Usage: bash start_onboard_stack.sh UAV_ID [GROUND_HOST] [AUTH_TOKEN]" >&2
  exit 2
fi

if [[ -z "${GROUND_HOST}" ]]; then
  case "${UAV_ID}" in
    1) GROUND_HOST="192.168.1.121" ;;
    3) GROUND_HOST="192.168.1.123" ;;
    *) echo "GROUND_HOST is required for UAV${UAV_ID}." >&2; exit 2 ;;
  esac
fi

if [[ -z "${AUTH_VALUE}" ]]; then
  echo "AUTH_TOKEN is empty. Pass it as argument 3 or export AUTH_TOKEN first." >&2
  exit 2
fi

source /opt/ros/noetic/setup.bash
source /home/amov/su17_experiment/devel/setup.bash
source /home/amov/competition_development/devel/setup.bash

exec roslaunch su17_competition_executor onboard_competition_stack.launch \
  uav_id:="${UAV_ID}" \
  local_ros_uav_id:=1 \
  ground_host:="${GROUND_HOST}" \
  auth_token:="${AUTH_VALUE}" \
  enable_motion:=true \
  enable_offline_recovery:=true \
  reconcile_after_minutes:=19.0
