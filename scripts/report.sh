#!/usr/bin/env bash
# Tempo report over the work log. See report.py for options.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/report.py" "$@"
