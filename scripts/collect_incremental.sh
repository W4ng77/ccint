#!/usr/bin/env bash
# incremental 采集入口，供 cron / systemd timer 调用。
#
# [MUST] HANDOFF §8 step 4：采集必须是最早上线且永不停机的组件。
# 语料随挂钟时间累积，采集停一天就永久少一天数据，而 trend 层需要 baseline。
set -euo pipefail
cd "$(dirname "$0")/.."
LOG_DIR="${CCINT_LOG_DIR:-$PWD/logs}"
mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/incremental.log" 2>&1
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) incremental start ==="
./.venv/bin/ccint collect incremental
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) incremental done ==="
