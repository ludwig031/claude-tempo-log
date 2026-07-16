#!/usr/bin/env bash
# Tempo report over the work log. See report.py for options.
# A sweep first rebuilds the log from the raw transcripts, so live and
# killed sessions (no SessionEnd) are always included.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python3 sweep.py
exec python3 report.py "$@"
