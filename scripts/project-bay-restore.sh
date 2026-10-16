#!/usr/bin/env bash
# Offline entry point; configuration is parsed as data, never sourced.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$SCRIPT_DIR/project_bay.py" --root "$SCRIPT_DIR/.." restore "$@"
