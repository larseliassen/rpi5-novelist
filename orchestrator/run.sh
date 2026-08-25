#!/usr/bin/env bash
# Daily entrypoint invoked by the systemd service.
# 1) ensure the model exists, 2) write a chapter, 3) commit + push to GitHub.
set -euo pipefail

cd "${NOVELIST_DIR:-$(dirname "$0")/..}"

MODEL="${NOVELIST_MODEL:-novelist}"

# (Re)build the model from the Modelfile every run. This is near-free when nothing
# changed — `create` only rewrites a manifest over blobs that are already on disk —
# and it means a Modelfile edit takes effect on the next run instead of being
# silently ignored because a stale `novelist` model still exists.
echo "[run] Bygger modell '${MODEL}' fra Modelfile ..."
ollama create "${MODEL}" -f orchestrator/modelfile/Modelfile

# Generate today's chapter + update state.
python3 orchestrator/write_chapter.py

# Publish. Requires the repo remote + credentials to be set up once (see README).
if [ -d .git ]; then
  git add chapters state
  if ! git diff --cached --quiet; then
    N="$(python3 -c 'import json;print(json.load(open("state/meta.json"))["chapter_count"])')"
    git commit -m "Kapittel ${N} ($(date +%F))"
    git push origin HEAD || echo "[run] git push feilet (sjekk credentials)."
  else
    echo "[run] Ingen endringer å committe."
  fi
fi
