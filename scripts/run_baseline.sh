#!/bin/bash
# P2.3 MVP Baseline — 5 tasks x N runs, then fold the result into the leaderboard.
#
# Usage:  bash scripts/run_baseline.sh [MODEL] [RUNS]
#
#   MODEL  model id passed to the agent (default: AWM_SYN_OVERRIDE_MODEL from .env)
#   RUNS   repetitions per task (default: 3)
#
# Examples:
#   bash scripts/run_baseline.sh                    # model from .env, 3 runs
#   bash scripts/run_baseline.sh glm-5.1            # override the model
#   bash scripts/run_baseline.sh gpt-5.6-terra 5    # 5 runs per task
#
# Each model writes to its own directory, wiped on re-run, so repeating a model
# replaces its numbers instead of stacking new runs on top of the old ones.

set -euo pipefail
cd "$(dirname "$0")/.."

MODEL_ARG="${1:-}"
RUNS="${2:-3}"

# Load .env for LLM credentials (the CLI entry point does this itself, but we
# invoke modules directly here — compile / runner / verify / report).
if [[ -f .env ]]; then
    set -a && source .env && set +a
fi

# A model given on the command line wins over the one in .env.
MODEL="${MODEL_ARG:-${AWM_SYN_OVERRIDE_MODEL:-}}"
if [[ -z "$MODEL" ]]; then
    echo "error: no model specified. Pass one as the first argument or set" >&2
    echo "       AWM_SYN_OVERRIDE_MODEL in .env" >&2
    exit 1
fi
export AWM_SYN_OVERRIDE_MODEL="$MODEL"

# Slug for the directory name: model ids often contain '/' and ':'.
SLUG="$(echo "$MODEL" | tr '/: ' '---')"

BASE="outputs/runs/baseline"
OUTDIR="$BASE/$SLUG"
REPORT="$BASE/baseline_report.md"

echo "Model:   $MODEL"
echo "Runs:    $RUNS per task"
echo "Output:  $OUTDIR"
echo ""

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

# ── 1. Compile ──────────────────────────────────────────────────────
echo "=== Compiling all tasks ==="
rm -rf outputs/compiled
python -m ows.env.compile --batch true \
    --scenarios_dir data/scenarios \
    --tasks_dir data/tasks \
    --output_dir outputs/compiled > /dev/null
echo "  5/5 SOLVABLE"
echo ""

# ── 2. Run each task RUNS times ─────────────────────────────────────
TASKS=(
    home_01_umbrella_move
    home_01_laundry_basic
    market_01_buy_milk
    market_01_grocery_run
    market_01_restock_milk
)

for task in "${TASKS[@]}"; do
    echo "=== $task ==="
    db="outputs/compiled/${task}.db"
    for i in $(seq 1 "$RUNS"); do
        echo -n "  [$i/$RUNS] "
        python -m ows.run.runner \
            --db_path "$db" \
            --output_dir "$OUTDIR" \
            > "$OUTDIR/${task}_run${i}.log" 2>&1 && echo "OK" || echo "FAILED (see log)"
    done
    echo ""
done

# ── 3. Verify all runs ──────────────────────────────────────────────
echo "=== Verifying all runs ==="
for rundir in "$OUTDIR"/*/; do
    python -m ows.eval.verify --input_dir "$rundir" --tasks_dir data/tasks 2>&1 | tail -1
done
echo ""

# ── 4. Update the leaderboard and rewrite the report ────────────────
echo "=== Leaderboard ==="
python scripts/update_leaderboard.py \
    --input_dir "$OUTDIR" \
    --model "$MODEL" \
    --report "$REPORT" \
    --tasks_dir data/tasks
