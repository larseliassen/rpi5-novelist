#!/usr/bin/env bash
# Daily entrypoint invoked by the systemd service.
#   1) rebuild the model, 2) write a chapter + update the notebook,
#   3) push the chapter to the PUBLIC site repo and the notebook to THIS one.
#
# Two repos, because GitHub Pages will not serve a private repo on a free plan:
#   private (here)          nixos/, orchestrator/, state/  <- clues, red herrings
#   public  ($WEB_DIR)      chapters/, web/                <- what readers see
set -euo pipefail

cd "${NOVELIST_DIR:-$(dirname "$0")/..}"

MODEL="${NOVELIST_MODEL:-novelist}"
WEB_DIR="${NOVELIST_WEB_DIR:-/var/lib/novelist-web}"
export NOVELIST_CHAPTERS_DIR="${WEB_DIR}/chapters"

# (Re)build the model from the Modelfile every run. This is near-free when nothing
# changed — `create` only rewrites a manifest over blobs that are already on disk —
# and it means a Modelfile edit takes effect on the next run instead of being
# silently ignored because a stale `novelist` model still exists.
echo "[run] Bygger modell '${MODEL}' fra Modelfile ..."
ollama create "${MODEL}" -f orchestrator/modelfile/Modelfile

# Refresh the public checkout before writing into it, so the new chapter lands on
# top of whatever is already published rather than colliding with it at push time.
if [ -d "${WEB_DIR}/.git" ]; then
  git -C "${WEB_DIR}" pull --rebase --autostash origin main \
    || echo "[run] Kunne ikke oppdatere ${WEB_DIR}; fortsetter."
else
  echo "[run] ADVARSEL: ${WEB_DIR} er ikke et git-repo — kapittelet blir ikke publisert."
fi

# Generate today's chapter (into WEB_DIR) + update the notebook (into state/).
python3 orchestrator/write_chapter.py

# Commit + push. Each repo is pushed independently: a failure to publish the
# chapter must not cost us the notebook update, or the story loses its memory.
publish() {
  local dir="$1" paths="$2" msg="$3"
  [ -d "${dir}/.git" ] || return 0
  git -C "${dir}" add ${paths}
  if git -C "${dir}" diff --cached --quiet; then
    echo "[run] ${dir}: ingen endringer å committe."
    return 0
  fi
  git -C "${dir}" commit -m "${msg}"
  # Rebase onto upstream: a single commit made from a laptop would otherwise
  # leave the Pi permanently non-fast-forward, silently ending publication.
  git -C "${dir}" pull --rebase --autostash origin \
    "$(git -C "${dir}" rev-parse --abbrev-ref HEAD)" \
    || echo "[run] ${dir}: pull --rebase feilet; prøver push likevel."
  git -C "${dir}" push origin HEAD \
    || echo "[run] ${dir}: git push feilet (sjekk deploy-nøkkel)."
}

N="$(python3 -c 'import json;print(json.load(open("state/meta.json"))["chapter_count"])')"
DATE="$(date +%F)"

publish "${WEB_DIR}" "chapters" "Kapittel ${N} (${DATE})"
publish "$(pwd)"     "state"    "Notatbok etter kapittel ${N} (${DATE})"
