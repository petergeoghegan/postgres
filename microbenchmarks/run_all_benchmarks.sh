#!/bin/bash
#
# Run all 8 benchmark variants and then compare results.
#
# Usage:
#   ./run_all_benchmarks.sh                        # smart master reuse
#   ./run_all_benchmarks.sh --force-fresh-master    # always run master fresh
#   ./run_all_benchmarks.sh --force-old-master      # always reuse old master
#   ./run_all_benchmarks.sh --baseline abc1234      # compare against specific patch commit
#   ./run_all_benchmarks.sh --runs 5 --pin          # extra flags passed through
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BENCHMARK="$SCRIPT_DIR/prefetch_benchmark.py"
COMPARE="$SCRIPT_DIR/compare_benchmarks.py"
RESULTS_DIR="$SCRIPT_DIR/prefetch_results"
MASTER_SOURCE_DIR="/mnt/nvme/postgresql/master/source"

# Parse our own flags, collect the rest as passthrough
MASTER_MODE="smart"  # smart | fresh | old
BASELINE=""
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force-fresh-master)
            MASTER_MODE="fresh"
            shift
            ;;
        --force-old-master)
            MASTER_MODE="old"
            shift
            ;;
        --baseline)
            BASELINE="$2"
            shift 2
            ;;
        *)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
    esac
done

# Get current master git hash
get_master_hash() {
    git -C "$MASTER_SOURCE_DIR" rev-parse --short HEAD 2>/dev/null || echo ""
}

# Get master_hash from the most recent JSON result file for a mode
get_last_master_hash() {
    local mode="$1"
    local prefix="${mode}_"
    local latest=""

    for f in "$RESULTS_DIR"/${prefix}*.json; do
        [ -f "$f" ] || continue
        if [ -z "$latest" ] || [ "$f" -nt "$latest" ]; then
            latest="$f"
        fi
    done

    if [ -n "$latest" ]; then
        python3 -c "import json; print(json.load(open('$latest')).get('master_hash', ''))" 2>/dev/null || echo ""
    fi
}

# Decide whether to add --old-master-results for a given mode
should_reuse_master() {
    local mode="$1"

    case "$MASTER_MODE" in
        fresh) return 1 ;;  # never reuse
        old)   return 0 ;;  # always reuse
        smart)
            local current_hash
            current_hash="$(get_master_hash)"
            if [ -z "$current_hash" ]; then
                return 1  # can't determine, run fresh
            fi
            local last_hash
            last_hash="$(get_last_master_hash "$mode")"
            if [ "$current_hash" = "$last_hash" ]; then
                return 0  # same hash, reuse
            else
                return 1  # different hash, run fresh
            fi
            ;;
    esac
}

# The 8 benchmark variants: mode_name:flags:default_runs
# Cached variants use 10 runs (noisier), uncached use 3.
# User-supplied --runs in PASSTHROUGH_ARGS overrides the default
# (argparse uses the last --runs value).
VARIANTS=(
    "benchmark_uncached::3"
    "benchmark_cached:--cached:10"
    "readstream_uncached:--readstream-tests:3"
    "readstream_cached:--readstream-tests --cached:10"
    "random_backwards_uncached:--random-backwards-tests:3"
    "random_backwards_cached:--random-backwards-tests --cached:10"
    "munro_uncached:--munro:3"
    "munro_cached:--munro --cached:10"
)

TOTAL_START=$(date +%s)
VARIANT_TIMES=()
FAILED_VARIANT=""

echo "========================================"
echo "Running all 8 benchmark variants"
echo "Master mode: $MASTER_MODE"
if [ -n "$BASELINE" ]; then
    echo "Baseline patch hash: $BASELINE"
fi
echo "Passthrough args: ${PASSTHROUGH_ARGS[*]:-none}"
echo "========================================"

for entry in "${VARIANTS[@]}"; do
    # Parse mode_name:flags:default_runs
    IFS=':' read -r mode flags default_runs <<< "$entry"

    echo ""
    echo "────────────────────────────────────────"
    echo "Starting: $mode (default --runs $default_runs)"
    echo "────────────────────────────────────────"

    # Build command
    CMD=("$BENCHMARK")
    if [ -n "$flags" ]; then
        # Split flags on space (safe since our flags don't contain spaces)
        read -ra FLAG_ARRAY <<< "$flags"
        CMD+=("${FLAG_ARRAY[@]}")
    fi

    # Add default --runs before passthrough so user's --runs overrides
    CMD+=("--runs" "$default_runs" "--prefetch-only")

    # Smart master reuse
    if should_reuse_master "$mode"; then
        echo "  (reusing old master results - hash unchanged)"
        CMD+=("--old-master-results")
    fi

    CMD+=("${PASSTHROUGH_ARGS[@]}")

    VARIANT_START=$(date +%s)

    if ! "${CMD[@]}"; then
        FAILED_VARIANT="$mode"
        echo ""
        echo "FAILED: $mode"
        break
    fi

    VARIANT_END=$(date +%s)
    ELAPSED=$((VARIANT_END - VARIANT_START))
    VARIANT_TIMES+=("$mode: ${ELAPSED}s")
    echo "  Completed $mode in ${ELAPSED}s"
done

TOTAL_END=$(date +%s)
TOTAL_ELAPSED=$((TOTAL_END - TOTAL_START))

echo ""
echo "========================================"
echo "TIMING SUMMARY"
echo "========================================"
for t in "${VARIANT_TIMES[@]}"; do
    echo "  $t"
done
MINS=$((TOTAL_ELAPSED / 60))
SECS=$((TOTAL_ELAPSED % 60))
echo "  ────────────────────────────────"
echo "  Total: ${MINS}m ${SECS}s"
echo "========================================"

if [ -n "$FAILED_VARIANT" ]; then
    echo ""
    echo "Benchmark run aborted due to failure in: $FAILED_VARIANT"
    exit 1
fi

# Run comparison
echo ""
echo "========================================"
echo "COMPARING RESULTS"
echo "========================================"
COMPARE_ARGS=()
if [ -n "$BASELINE" ]; then
    COMPARE_ARGS+=("--baseline" "$BASELINE")
fi
python3 "$COMPARE" "${COMPARE_ARGS[@]}"
