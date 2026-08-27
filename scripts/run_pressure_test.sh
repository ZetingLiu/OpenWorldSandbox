#!/bin/bash
# 正式压测驱动：并发 1/4/8 三档顺序执行（档间不共享缓存，吞吐可比），
# 完成后合并生成机器可读 report.json + 中文领导报告。
set -u
cd "$(dirname "$0")/.."

LOG=outputs/pressure_driver.log
: > "$LOG"
echo "pressure driver start: $(date '+%F %T')" >> "$LOG"

for c in 1 4 8; do
  echo "=== TIER concurrency=$c START $(date '+%F %T') ===" | tee -a "$LOG"
  .venv/bin/ows gen run \
    --mode full \
    --num_scenarios 5 \
    --num_tasks_per_scenario 5 \
    --concurrency "$c" \
    --input_price_per_mtok 1.4 \
    --output_price_per_mtok 4.4 \
    --max_total_cost 5 \
    --max_total_tokens 1500000 \
    --output_dir outputs/synth_staging_pressure \
    2>&1 | tail -25 | tee -a "$LOG"
  echo "=== TIER concurrency=$c END $(date '+%F %T') ===" | tee -a "$LOG"
done

echo "=== REPORT MERGE $(date '+%F %T') ===" | tee -a "$LOG"
.venv/bin/ows gen report \
  --input_dir outputs/synth_staging_pressure \
  --output_dir outputs/reports \
  2>&1 | tee -a "$LOG"
echo "pressure driver done: $(date '+%F %T')" >> "$LOG"
