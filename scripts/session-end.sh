#!/usr/bin/env bash
# SessionEnd hook: compute real-work time slots from the transcript and
# write them to the work log. Delegates to session_end.py.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/session_end.py"
