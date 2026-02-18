#!/bin/bash
# Pre-commit hook: check LOG.info() call counts per file.
#
# Flags any occystrap/*.py file with more than MAX_INFO
# LOG.info() calls. This catches verbosity regressions
# where someone adds many info calls without considering
# whether they should be debug.
#
# Threshold rationale: after the structured logging audit,
# no file has more than 7 LOG.info() calls. A threshold
# of 10 gives headroom for growth while catching major
# regressions.

set -euo pipefail

MAX_INFO=10
EXIT_CODE=0

while IFS= read -r -d '' f; do
    count=$(grep -v '^\s*#' "$f" | grep -c 'LOG\.info(' 2>/dev/null || true)
    if [ "$count" -gt "$MAX_INFO" ]; then
        echo "ERROR: $f has $count LOG.info() calls (max $MAX_INFO)"
        EXIT_CODE=1
    fi
done < <(find occystrap -name '*.py' -not -path '*/tests/*' -not -path '*/.tox/*' -print0)

if [ "$EXIT_CODE" -eq 0 ]; then
    echo "Log level check passed (all files <= $MAX_INFO LOG.info calls)"
fi

exit $EXIT_CODE
