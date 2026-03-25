#!/bin/bash
# Benchmark occystrap performance across workflows and parallelism settings.
#
# Requires:
#   - occystrap on PATH (or in a venv that's activated)
#   - A local Docker registry at localhost:5000 with test images
#     (busybox, ubuntu, hello-world under library/)
#
# Usage:
#   tools/benchmark.sh              # TSV output (default)
#   tools/benchmark.sh --json       # JSON output
#   tools/benchmark.sh --help       # Show help
#
# The script creates a temporary directory for output, cleans it
# between runs, and removes it at exit. Layer cache is disabled
# to measure raw transfer performance.

set -euo pipefail

REGISTRY="${BENCHMARK_REGISTRY:-localhost:5000}"
OUTPUT_FORMAT="tsv"
RUNS=1
RESULTS=()

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --json         Output results as JSON"
    echo "  --registry R   Registry to use (default: localhost:5000)"
    echo "  --runs N       Repeat each test N times, report median (default: 1)"
    echo "  --help         Show this help"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            OUTPUT_FORMAT="json"
            shift
            ;;
        --registry)
            REGISTRY="$2"
            shift 2
            ;;
        --runs)
            RUNS="$2"
            shift 2
            ;;
        --help)
            usage
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Check prerequisites
if ! command -v occystrap &>/dev/null; then
    echo "ERROR: occystrap not found on PATH" >&2
    echo "Install it or activate its venv first." >&2
    exit 1
fi

# Quick check that the registry is reachable
if ! curl -sf "http://${REGISTRY}/v2/" &>/dev/null; then
    echo "ERROR: Registry at ${REGISTRY} is not reachable" >&2
    echo "Start a local registry with:" >&2
    echo "  docker run -d -p 5000:5000 --name registry registry:2" >&2
    exit 1
fi

# Create temp dir for outputs, clean up on exit
WORK_DIR=$(mktemp -d -t occystrap-bench.XXXXXX)
trap 'rm -rf "$WORK_DIR"' EXIT

clean_output() {
    rm -rf "${WORK_DIR:?}/output"
    mkdir -p "$WORK_DIR/output"
}

# Run a single benchmark and record the wall-clock time.
# Arguments: workflow_name j_value J_value command...
run_bench() {
    local name="$1"
    local j_val="$2"
    local j_cap="$3"
    shift 3

    local times=()
    for _run in $(seq 1 "$RUNS"); do
        clean_output
        local start end elapsed
        start=$(date +%s.%N)
        local exit_code=0
        "$@" 2>/dev/null || exit_code=$?
        end=$(date +%s.%N)
        elapsed=$(echo "$end - $start" | bc)
        times+=("$elapsed")
    done

    # Use the median if multiple runs
    local median
    if [[ "$RUNS" -eq 1 ]]; then
        median="${times[0]}"
    else
        median=$(printf '%s\n' "${times[@]}" | sort -n | awk -v n="$RUNS" 'NR==int((n+1)/2){print}')
    fi

    RESULTS+=("${name}	${j_val}	${j_cap}	${median}	${exit_code}")
}

echo "Occystrap benchmark" >&2
echo "Registry: ${REGISTRY}" >&2
echo "Runs per test: ${RUNS}" >&2
echo "Working directory: ${WORK_DIR}" >&2
echo "" >&2

# --- Workflow definitions ---

# 1. Single image pull to directory
run_single_pull() {
    local j="$1"
    local jj="$2"
    run_bench "single-pull" "$j" "$jj" \
        occystrap -j "$j" -J "$jj" --insecure process \
        "registry://${REGISTRY}/library/ubuntu:latest" \
        "dir://${WORK_DIR}/output"
}

# 2. Single image pull to tarball
run_single_tar() {
    local j="$1"
    local jj="$2"
    run_bench "single-tar" "$j" "$jj" \
        occystrap -j "$j" -J "$jj" --insecure process \
        "registry://${REGISTRY}/library/ubuntu:latest" \
        "tar://${WORK_DIR}/output/image.tar"
}

# 3. Single image push (mirror)
run_single_push() {
    local j="$1"
    local jj="$2"
    run_bench "single-push" "$j" "$jj" \
        occystrap -j "$j" -J "$jj" --insecure process \
        "registry://${REGISTRY}/library/ubuntu:latest" \
        "registry://${REGISTRY}/bench/ubuntu:latest"
}

# 4. Multi-image pull to directory
run_multi_dir() {
    local j="$1"
    local jj="$2"
    run_bench "multi-dir" "$j" "$jj" \
        occystrap -j "$j" -J "$jj" --insecure process \
        "registry://${REGISTRY}/library/busybox:latest" \
        "dir://${WORK_DIR}/output?unique_names=true"
    clean_output
    # Process multiple images sequentially to simulate
    # multi-image workload without quay:// dependency
    run_bench "multi-dir-3img" "$j" "$jj" \
        bash -c "
            occystrap -j $j -J $jj --insecure process \
                'registry://${REGISTRY}/library/busybox:latest' \
                'dir://${WORK_DIR}/output?unique_names=true' && \
            occystrap -j $j -J $jj --insecure process \
                'registry://${REGISTRY}/library/ubuntu:latest' \
                'dir://${WORK_DIR}/output?unique_names=true' && \
            occystrap -j $j -J $jj --insecure process \
                'registry://${REGISTRY}/library/hello-world:latest' \
                'dir://${WORK_DIR}/output?unique_names=true'
        "
}

# --- Run all benchmarks ---

J_VALUES=(1 4 8 16)
JJ_VALUES=(1 3 6)

echo "Running single-image benchmarks..." >&2
for j in "${J_VALUES[@]}"; do
    echo "  -j ${j}..." >&2
    run_single_pull "$j" 1
    run_single_tar "$j" 1
    run_single_push "$j" 1
done

echo "Running multi-image benchmarks..." >&2
for j in 4 8; do
    for jj in "${JJ_VALUES[@]}"; do
        echo "  -j ${j} -J ${jj}..." >&2
        run_multi_dir "$j" "$jj"
    done
done

# --- Output results ---

if [[ "$OUTPUT_FORMAT" == "json" ]]; then
    echo "["
    first=true
    for row in "${RESULTS[@]}"; do
        IFS=$'\t' read -r name j jj wall_s exit_code <<< "$row"
        if [[ "$first" == "true" ]]; then
            first=false
        else
            echo ","
        fi
        printf '  {"workflow": "%s", "j": %s, "J": %s, "wall_s": %s, "exit_code": %s}' \
            "$name" "$j" "$jj" "$wall_s" "$exit_code"
    done
    echo ""
    echo "]"
else
    echo "workflow	j	J	wall_s	exit_code"
    for row in "${RESULTS[@]}"; do
        echo "$row"
    done
fi
