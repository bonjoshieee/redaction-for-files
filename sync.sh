#!/usr/bin/env bash
# Usage: ./sync.sh [--dry-run] [FOLDER ...]
#   no folders  -> every folder in config.toml
#   FOLDER ...  -> only those folders
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m nas_s3_sync "$@"
