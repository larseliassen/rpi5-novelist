#!/usr/bin/env python3
"""
Daily crime-novel chapter generator (Norwegian / bokmål).

Design goals for an RPi5 8GB running a 7B model with a small context window:
  * NEVER stuff old chapters into the prompt. Keep a compressed "notatbok".
  * Two LLM calls per day:
       1) write the next chapter from (notebook + last recap + a director beat)
       2) update the notebook from the new chapter (facts, deaths, clues, timeline)
  * Improvised mystery: a light "regissor" injects escalating beats so the story
    builds tension and eventually converges instead of wandering forever.

State layout (all markdown/json, git-friendly):
  state/synopsis.md      one paragraph, drifts slowly
  state/characters.md    who they are, alive/dead, what they know
  state/clues.md         planted facts + red herrings
  state/timeline.md      what happened when
  state/recap.md         rolling ~300-word "historien så langt"
  state/meta.json        {chapter_count, ...}

Chapters are written to $NOVELIST_CHAPTERS_DIR (the public site repo), not here:
  <public repo>/chapters/kapittel-NN.md
"""

import json
import os
import platform
import re
import sys
import textwrap
import time
import datetime as dt
from pathlib import Path

import requests

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("NOVELIST_MODEL", "novelist")
ROOT = Path(os.environ.get("NOVELIST_DIR", ".")).resolve()


def base_model() -> str:
    """Return the active FROM model from the Modelfile (first non-commented FROM line)."""
    modelfile = ROOT / "orchestrator" / "modelfile" / "Modelfile"
    for line in modelfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FROM") and not stripped.startswith("#"):
            return stripped.split(None, 1)[1].strip()
    return MODEL

STATE = ROOT / "state"
# Chapters are published from a separate PUBLIC repo (larseliassen/mikromidas),
# because GitHub Pages will not serve a private repo on a free plan. The notebook
# in state/ stays here in the private repo — it holds the planted clues and red
# herrings. Defaults to ROOT/chapters so a single-repo checkout still works.
CHAPTERS = Path(os.environ.get("NOVELIST_CHAPTERS_DIR", ROOT / "chapters")).resolve()

# ---- Director beats: injected on a cadence to shape the improvisation ----
# The loop cycles through these so an "improvised" mystery still escalates and
# periodically pays things off, instead of meandering forever.
DIRECTOR_BEATS = [
    "Introduser en ny detalj som ikke stemmer med det etterforskeren trodde.",
    "La en biperson oppføre seg mistenkelig. Ikke avslør hvorfor ennå.",
    "Grav fram et spor fra et tidligere kapittel og gi det ny betydning.",
    "Øk presset: en trussel, en frist, eller noe personlig står på spill.",
    "La et tidligere spor vise seg å være villedende (rød sild).",
    "Avdekk en hemmelighet om en av hovedpersonene.",
    "En vending: noen leseren stolte på, viser en ny side.",
    "Bind sammen to tråder som til nå har virket urelaterte.",
]


def ollama_call(prompt: str, system: str | None = None,
                temperature: float = 0.85, num_predict: int = 1600) -> dict:
    """One-shot generation against Ollama's /api/generate.

    Returns the raw response body. Besides "response" it carries the timing
    counters (total_duration, eval_count, eval_duration, ... — all in ns) that
    end up in the chapter's frontmatter, plus a wall-clock "_wall_seconds" in
    case an older Ollama leaves the counters out.
    """
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
        },
    }
    if system:
        payload["system"] = system
    started = time.monotonic()
    r = requests.post(f"{OLLAMA}/api/generate", json=payload, timeout=3 * 3600)
    r.raise_for_status()
    body = r.json()
    body["_wall_seconds"] = time.monotonic() - started
    return body


def ollama_generate(prompt: str, system: str | None = None,
                    temperature: float = 0.85, num_predict: int = 1600) -> str:
    """ollama_call, for the callers that only want the text."""
    return ollama_call(prompt, system, temperature, num_predict).get("response", "").strip()


def generation_stats(body: dict) -> dict:
    """Frontmatter-ready numbers from an /api/generate response.

    Ollama reports durations in nanoseconds; tokens/s is computed from the eval
    phase alone (that is the generation speed — model load and prompt ingestion
    are reported separately so a cold start does not look like a slow model).
    """
    ns = 1_000_000_000

    def secs(key: str) -> float | None:
        v = body.get(key)
        return round(v / ns, 1) if isinstance(v, (int, float)) else None

    stats = {
        "seconds": secs("total_duration") or round(body["_wall_seconds"], 1),
        "load_seconds": secs("load_duration"),
        "prompt_tokens": body.get("prompt_eval_count"),
        "tokens": body.get("eval_count"),
    }
    eval_ns, eval_count = body.get("eval_duration"), body.get("eval_count")
    if eval_ns and eval_count:
        stats["tokens_per_second"] = round(eval_count / (eval_ns / ns), 2)
    return {k: v for k, v in stats.items() if v is not None}


def read(path: Path, default: str = "") -> str:
    return path.read_text(encoding="utf-8") if path.exists() else default


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def load_meta() -> dict:
    p = STATE / "meta.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"chapter_count": 0, "title": "Uten tittel"}


def save_meta(meta: dict) -> None:
    write(STATE / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))


def bootstrap_if_empty() -> None:
    """Create the story bible on the very first run (improvised premise)."""
    if (STATE / "synopsis.md").exists():
        return
    print("[bootstrap] Ingen historie funnet — skaper premiss ...")
    premise_prompt = textwrap.dedent("""\
        Skap premisset for en ny norsk kriminalroman. Improviser fritt, men gjør det
        konkret og norsk (stedsnavn, miljø, årstid). Svar KUN med disse feltene:

        TITTEL: <en fengende norsk tittel>
        SYNOPSIS: <ett avsnitt: forbrytelsen, etterforskeren, settingen, tonen>
        PERSONER: <3-5 personer, hver med navn og én linje. Marker etterforsker.>
        ÅPNINGSSPOR: <ett konkret spor eller mysterium som starter alt>
    """)
    out = ollama_generate(premise_prompt, temperature=0.9, num_predict=700)

    def field(name: str) -> str:
        m = re.search(rf"{name}:\s*(.+?)(?=\n[A-ZÅØÆ]+:|\Z)", out, re.S)
        return m.group(1).strip() if m else ""

    title = field("TITTEL") or "Mørketid"
    write(STATE / "synopsis.md", field("SYNOPSIS") or out)
    write(STATE / "characters.md", field("PERSONER") or "- Etterforsker: (ukjent)")
    write(STATE / "clues.md", "- " + (field("ÅPNINGSSPOR") or "Et uforklarlig funn."))
    write(STATE / "timeline.md", "- Dag 0: Historien begynner.")
    write(STATE / "recap.md", field("SYNOPSIS") or "Historien har akkurat begynt.")
    meta = load_meta()
    meta["title"] = title
    save_meta(meta)
    print(f"[bootstrap] Tittel: {title}")


def build_chapter_prompt(n: int, beat: str) -> str:
    return textwrap.dedent(f"""\
        NOTATBOK (fakta du MÅ respektere):

        # Synopsis
        {read(STATE / 'synopsis.md')}

        # Personer (og hvem som lever/vet hva)
        {read(STATE / 'characters.md')}

        # Spor og røde sild
        {read(STATE / 'clues.md')}

        # Tidslinje
        {read(STATE / 'timeline.md')}

        # Historien så langt
        {read(STATE / 'recap.md')}

        ---
        OPPGAVE: Skriv KAPITTEL {n} i romanen, på bokmål.
        Regissørens føring for dette kapittelet: {beat}

        Skriv 500 ord sammenhengende prosa. Start med kapitteloverskriften
        på formen "Kapittel {n}". Ikke gjenta notatboka. Avslutt med en krok.
    """)


def update_notebook(n: int, chapter_text: str) -> None:
    """Second pass: fold the new chapter back into the compressed state."""
    prompt = textwrap.dedent(f"""\
        Her er notatboka og et nytt kapittel. Oppdater notatboka så den forblir
        kort, presis og uten motsigelser. Svar KUN med disse seksjonene, hver
        som en kort punktliste (unntatt RECAP som er ett avsnitt på ~250 ord):

        PERSONER:
        SPOR:
        TIDSLINJE:
        RECAP:

        === NÅVÆRENDE NOTATBOK ===
        Personer:
        {read(STATE / 'characters.md')}
        Spor:
        {read(STATE / 'clues.md')}
        Tidslinje:
        {read(STATE / 'timeline.md')}

        === NYTT KAPITTEL {n} ===
        {chapter_text}
    """)
    out = ollama_generate(prompt, temperature=0.3, num_predict=900)

    def section(name: str) -> str:
        m = re.search(rf"{name}:\s*(.+?)(?=\n(?:PERSONER|SPOR|TIDSLINJE|RECAP):|\Z)",
                      out, re.S)
        return m.group(1).strip() if m else ""

    if section("PERSONER"):
        write(STATE / "characters.md", section("PERSONER"))
    if section("SPOR"):
        write(STATE / "clues.md", section("SPOR"))
    if section("TIDSLINJE"):
        write(STATE / "timeline.md", section("TIDSLINJE"))
    if section("RECAP"):
        write(STATE / "recap.md", section("RECAP"))


def slugify(s: str) -> str:
    s = s.lower()
    s = (s.replace("å", "a").replace("ø", "o").replace("æ", "ae"))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "kapittel"


def main() -> int:
    bootstrap_if_empty()
    meta = load_meta()
    n = meta["chapter_count"] + 1
    beat = DIRECTOR_BEATS[(n - 1) % len(DIRECTOR_BEATS)]
    today = dt.date.today().isoformat()

    print(f"[write] Kapittel {n} — føring: {beat}")
    body = ollama_call(build_chapter_prompt(n, beat), num_predict=2600)
    chapter = body.get("response", "").strip()
    if not chapter:
        print("[error] Tomt svar fra modellen.", file=sys.stderr)
        return 1

    stats = generation_stats(body)
    print("[write] " + ", ".join(f"{k}={v}" for k, v in stats.items()))

    # Try to lift a chapter title from the first line for nicer frontmatter.
    # The model picks the title, so quote-escape it: one stray " would otherwise
    # break the YAML and take the whole page down with it.
    first_line = chapter.splitlines()[0].strip("# ").strip().replace('"', "'")
    # generation:* is machine-written bookkeeping: how long this chapter took,
    # on what. Handy when swapping models — the site can show "skrevet på 14 min
    # av qwen2.5:7b" and old chapters keep the numbers they were written with.
    lines = [
        "---",
        f'title: "{first_line}"',
        f"chapter: {n}",
        f'date: "{today}"',
        f'model: "{base_model()}"',
        "generation:",
        f'  finished_at: "{dt.datetime.now().astimezone().isoformat(timespec="seconds")}"',
        f'  host: "{platform.node()}"',
        f'  beat: "{beat}"',
    ]
    lines += [f"  {k}: {v}" for k, v in stats.items()]
    lines += ["---", ""]
    fm = "\n".join(lines) + "\n"
    out_path = CHAPTERS / f"kapittel-{n:03d}.md"
    write(out_path, fm + chapter)
    print(f"[write] Lagret {out_path}")

    print("[state] Oppdaterer notatboka ...")
    update_notebook(n, chapter)

    meta["chapter_count"] = n
    meta["last_written"] = today
    save_meta(meta)
    print(f"[done] Kapittel {n} ferdig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
