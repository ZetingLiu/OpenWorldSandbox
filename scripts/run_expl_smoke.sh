#!/bin/bash
# 探索任务在线 smoke test（Step 6）：被测模型串行跑每个新任务 1 次。
# 协议：不与数据合成并发混跑；API/模型故障与数据错误分开统计。
set -uo pipefail
cd "$(dirname "$0")/.."

# 直调 python -m 不经过 CLI 入口，需显式加载 .env
if [[ -f .env ]]; then
    set -a && source .env && set +a
fi

PY=".venv/bin/python"
OUT=outputs/runs/expl_smoke
rm -rf "$OUT"
mkdir -p "$OUT"

TASKS=(home_01_laundry_supply_v1 home_01_laundry_supply_v2
       market_01_pick_good_apple_v1 market_01_pick_good_apple_v2)

for t in "${TASKS[@]}"; do
    echo "=== smoke $t $(date '+%H:%M:%S') ==="
    "$PY" -m ows.run.runner \
        --db_path "outputs/regress_all/${t}.db" \
        --output_dir "$OUT" \
        > "$OUT/${t}_runner.log" 2>&1 \
        && echo "  runner OK" || echo "  runner FAILED (see ${t}_runner.log)"
done

echo "=== verify $(date '+%H:%M:%S') ==="
for rundir in "$OUT"/*_*/; do
    echo "--- $(basename "$rundir") ---"
    "$PY" -m ows.eval.verify --input_dir "$rundir" --tasks_dir data/tasks 2>&1 | tail -1
done
echo "SMOKE DONE $(date '+%F %T')"
