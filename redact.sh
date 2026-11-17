#!/usr/bin/env bash
# Usage: ./redact.sh [--dry-run] FOLDER [FOLDER ...]   (or `nas-redact ...` from anywhere)
#   writes FOLDER-redacted next to each FOLDER (the original is only read)
#   no FOLDER -> every share in config.toml
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# folder paths are resolved against where you ran the command, not the repo
args=()
for a in "$@"; do
  if [[ "$a" != -* && -e "$a" ]]; then args+=("$(readlink -f "$a")"); else args+=("$a"); fi
done

if [ ! -x "$here/.venv/bin/python" ]; then
  python3 -m venv "$here/.venv"
  "$here/.venv/bin/pip" install -q -r "$here/requirements.txt"
fi

# fail fast if the local model is needed but Ollama isn't answering
if [[ ! " $* " =~ " --no-llm " && ! " $* " =~ " -h " && ! " $* " =~ " --help " ]]; then
  if ! curl -s -m 3 http://localhost:11434/api/tags >/dev/null; then
    echo "Ollama is not running. Start it:  sudo systemctl start ollama"
    echo "or run without the model:        nas-redact --no-llm ..."
    exit 2
  fi
fi

cd "$here"
exec .venv/bin/python -m nas_s3_sync.twin_cli ${args[@]+"${args[@]}"}
