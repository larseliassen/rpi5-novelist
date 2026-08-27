#!/usr/bin/env python3
"""
Daily crime-novel chapter generator. Drafts in English, publishes in bokmål.

Design goals for a 4GB RPi5 running a 2B model at num_ctx 4096:
  * NEVER stuff old chapters into the prompt. Keep a compressed notebook.
  * Write in ENGLISH, then translate. A 2B model's Norwegian is a
    Scandinavian soup of Danish and Swedish loanwords, but its English prose is
    passable and translation is a far easier task than composition. So the
    creative work happens in English and only the last step is Norwegian.
  * The notebook is English too: it is internal scaffolding that only ever feeds
    the English writing prompt. It self-migrates — update_notebook rewrites all
    four sections every run, so the first English chapter converts it.
  * Improvised mystery: a light "director" injects escalating beats so the story
    builds tension and eventually converges instead of wandering forever.

Per chapter that is: 1 write call + N translate calls + 1 notebook call.

The 4096-token context is the binding constraint. A whole chapter cannot be
translated in one call — source plus output would overflow and Ollama would
silently truncate, publishing half a chapter. Hence translate_chapter() works
paragraph by paragraph. The chapter target is 500 words, which leaves the draft
call comfortably inside the window.

State layout (all markdown/json, git-friendly):
  state/synopsis.md      one paragraph, drifts slowly
  state/characters.md    who they are, alive/dead, what they know
  state/clues.md         planted facts + red herrings
  state/timeline.md      what happened when
  state/recap.md         rolling ~250-word "story so far"
  state/meta.json        {chapter_count, ...}
  state/drafts/          the English draft of each chapter, kept for comparison

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
    "Introduce a new detail that contradicts what the investigator believed.",
    "Let a minor character behave suspiciously. Do not reveal why yet.",
    "Dig up a clue from an earlier chapter and give it new meaning.",
    "Raise the pressure: a threat, a deadline, or something personal at stake.",
    "Reveal that an earlier clue was misleading — a red herring.",
    "Uncover a secret about one of the main characters.",
    "A turn: someone the reader trusted shows another side.",
    "Tie together two threads that have seemed unrelated until now.",
]

# The Modelfile's SYSTEM makes the model an English-writing crime novelist, which
# is right for the chapter call and wrong for the other two. /api/generate's
# "system" field overrides it per call.
TRANSLATOR_SYSTEM = (
    "Du er en litterær oversetter. Du oversetter engelsk skjønnlitteratur til "
    "korrekt norsk bokmål. Du skriver idiomatisk norsk, aldri ord-for-ord. "
    "Aldri dansk, aldri svensk, aldri nynorsk. Du svarer KUN med oversettelsen — "
    "ingen forklaring, ingen kommentar, ingen engelsk originaltekst."
)
EDITOR_SYSTEM = (
    "You are a terse story editor. You maintain a factual continuity notebook. "
    "You answer only in the requested format, with no commentary."
)


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


def merge_stats(bodies: list[dict]) -> dict:
    """Roll the per-chunk translation calls up into one set of numbers."""
    parts = [generation_stats(b) for b in bodies]
    total = {"calls": len(parts)}
    for key in ("seconds", "load_seconds", "prompt_tokens", "tokens"):
        values = [p[key] for p in parts if key in p]
        if values:
            total[key] = round(sum(values), 1) if "seconds" in key else sum(values)
    total.setdefault("seconds", 0.0)
    if total.get("tokens") and total.get("seconds"):
        total["tokens_per_second"] = round(total["tokens"] / total["seconds"], 2)
    return total


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
    # English, like the rest of the notebook — except the title, which is the one
    # bootstrap field that reaches readers untranslated.
    premise_prompt = textwrap.dedent("""\
        Invent the premise for a new Norwegian crime novel. Improvise freely, but
        keep it concrete and Norwegian: real place names, a specific setting and
        season. Answer with ONLY these four labels:

        TITLE: <a striking title, in Norwegian>
        SYNOPSIS: <one paragraph: the crime, the investigator, the setting, the tone>
        CHARACTERS: <3-5 people, each a name and one line. Mark the investigator.>
        OPENING_CLUE: <one concrete clue or mystery that sets everything off>
    """)
    out = ollama_generate(premise_prompt, temperature=0.9, num_predict=700)
    fields = split_sections(out, ("TITLE", "SYNOPSIS", "CHARACTERS", "OPENING_CLUE"))

    title = fields.get("TITLE", "Mørketid").splitlines()[0].strip()
    write(STATE / "synopsis.md", fields.get("SYNOPSIS") or out)
    write(STATE / "characters.md", fields.get("CHARACTERS", "- Investigator: (unknown)"))
    write(STATE / "clues.md", "- " + fields.get("OPENING_CLUE", "An unexplained find."))
    write(STATE / "timeline.md", "- Day 0: The story begins.")
    write(STATE / "recap.md", fields.get("SYNOPSIS", "The story has just begun."))
    meta = load_meta()
    meta["title"] = title
    save_meta(meta)
    print(f"[bootstrap] Tittel: {title}")


def build_chapter_prompt(n: int, beat: str) -> str:
    return textwrap.dedent(f"""\
        NOTEBOOK (facts you MUST respect):

        # Synopsis
        {read(STATE / 'synopsis.md')}

        # Characters (who is alive, who knows what)
        {read(STATE / 'characters.md')}

        # Clues and red herrings
        {read(STATE / 'clues.md')}

        # Timeline
        {read(STATE / 'timeline.md')}

        # Story so far
        {read(STATE / 'recap.md')}

        ---
        TASK: Write CHAPTER {n} of the novel, in English.
        The director's note for this chapter: {beat}

        The novel is set in Norway and will be published in Norwegian, so keep
        every proper noun exactly as the notebook spells it — names of people,
        places, boats, streets. Do not anglicise them.

        Write 500 words of continuous prose. Open with the chapter heading
        in the form "Chapter {n}". Do not restate the notebook. End on a hook.
    """)


def split_for_translation(text: str, budget: int = 900) -> list[str]:
    """Split a chapter into paragraph groups small enough to translate in one call.

    The 4096-token context has to hold the source chunk, the instructions, the
    name glossary AND the Norwegian output, so chunks stay near ~900 characters.
    Paragraph boundaries are never crossed — a chunk that ends mid-sentence makes
    the model "finish" the thought instead of translating it. A single paragraph
    longer than the budget is passed through whole rather than cut.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for para in re.split(r"\n\s*\n", text.strip()):
        para = para.strip()
        if not para:
            continue
        if current and size + len(para) > budget:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# Small models like to introduce themselves before doing the work. Strip the
# usual openers so they never reach the published page.
PREAMBLE = re.compile(
    r"^\s*(here (?:is|'s)[^\n:]*:|translation:|norwegian:|oversettelse:|"
    r"norsk( oversettelse)?:|på norsk:)\s*",
    re.I,
)


def translate_chapter(chapter_en: str) -> tuple[str, list[dict]]:
    """Translate the English draft to bokmål, one paragraph group at a time.

    Returns the Norwegian text and the per-call stats, so the frontmatter can
    report what the translation pass cost.
    """
    # The notebook's character list doubles as a name glossary: it is the only
    # thing standing between "Elin Johansen" and a helpfully translated "Elin
    # Johnson", and it keeps spellings stable across chunks.
    glossary = read(STATE / "characters.md").strip()
    chunks = split_for_translation(chapter_en)
    print(f"[translate] {len(chunks)} biter å oversette ...")

    out: list[str] = []
    stats: list[dict] = []
    for i, chunk in enumerate(chunks, 1):
        prompt = textwrap.dedent(f"""\
            Oversett teksten under til norsk bokmål.

            Egennavn som skal stå UENDRET (personer og steder i romanen):
            {glossary}

            Regler:
            - Behold alle avsnittsskift. Ikke slå sammen eller del opp avsnitt.
            - Ikke legg til noe, ikke utelat noe, ikke oppsummer.
            - Skriv naturlig, litterær norsk — ikke ord-for-ord.
            - Dialog settes med norske anførselstegn: «slik».
            - Svar KUN med den norske teksten.

            === TEKST ===
            {chunk}
        """)
        body = ollama_call(
            prompt,
            system=TRANSLATOR_SYSTEM,
            temperature=0.25,
            # Norwegian needs more tokens than the English it came from, and this
            # tokenizer is not kind to it. Budget generously: a truncated chunk
            # is a hole in the middle of the published chapter.
            num_predict=min(1400, max(320, int(len(chunk) / 1.4))),
        )
        piece = PREAMBLE.sub("", body.get("response", "").strip()).strip()
        if not piece:
            # Better a visible English paragraph than a silent gap in the story.
            print(f"[translate] ADVARSEL: bit {i} kom tom tilbake — beholder engelsk.",
                  file=sys.stderr)
            piece = chunk
        out.append(piece)
        stats.append(body)
        print(f"[translate] {i}/{len(chunks)} ferdig")
    return "\n\n".join(out), stats


NOTEBOOK_SECTIONS = ("CHARACTERS", "CLUES", "TIMELINE", "RECAP")


def split_sections(out: str, names: tuple[str, ...]) -> dict[str, str]:
    """Pull LABEL: blocks out of a model response.

    Tolerant of the markdown the model decorates its labels with — it answers
    "## CLUES:" or "**CLUES:**" at least as often as the bare "CLUES:" it was
    asked for. Matching only the bare form is why state/ currently has "## SPOR:"
    embedded in the middle of characters.md and the whole premise stuffed into
    meta.json's title: the terminator never matched, so each section swallowed
    the rest of the response.
    """
    # Trailing \** matters as much as the leading one: "**CLUES:**" closes its
    # bold *after* the colon, and the leftover asterisks would head the section.
    label = r"^[ \t]*#{0,4}[ \t]*\**[ \t]*(?:%s)[ \t]*\**[ \t]*:[ \t]*\**[ \t]*"
    found = {}
    for name in names:
        start = re.search(label % name, out, re.M | re.I)
        if not start:
            continue
        rest = out[start.end():]
        end = re.search(label % "|".join(names), rest, re.M | re.I)
        body = rest[: end.start()] if end else rest
        body = body.strip()
        if body:
            found[name] = body
    return found


def update_notebook(n: int, chapter_en: str) -> dict:
    """Second pass: fold the new chapter back into the compressed state.

    Fed the ENGLISH draft, not the translation: it is the same content, the model
    reads it better, and it keeps the notebook in the language the next chapter
    prompt will be written in.
    """
    prompt = textwrap.dedent(f"""\
        Here is the notebook and a new chapter. Update the notebook so it stays
        short, precise and free of contradictions. Answer with ONLY these four
        labels, each followed by a short bullet list (except RECAP, which is one
        paragraph of about 250 words):

        CHARACTERS:
        CLUES:
        TIMELINE:
        RECAP:

        Keep every proper noun spelled exactly as it appears below.

        === CURRENT NOTEBOOK ===
        Characters:
        {read(STATE / 'characters.md')}
        Clues:
        {read(STATE / 'clues.md')}
        Timeline:
        {read(STATE / 'timeline.md')}

        === NEW CHAPTER {n} ===
        {chapter_en}
    """)
    body = ollama_call(prompt, system=EDITOR_SYSTEM, temperature=0.3, num_predict=900)
    sections = split_sections(body.get("response", "").strip(), NOTEBOOK_SECTIONS)

    for name, path in (("CHARACTERS", "characters.md"), ("CLUES", "clues.md"),
                       ("TIMELINE", "timeline.md"), ("RECAP", "recap.md")):
        if name in sections:
            write(STATE / path, sections[name])
        else:
            print(f"[state] ADVARSEL: fant ingen {name}-seksjon — beholder forrige.",
                  file=sys.stderr)
    return body


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
    # num_predict 900: ~500 English words plus headroom. The notebook prompt sits
    # in front of it and num_ctx is 4096, so a larger budget is silently truncated.
    body = ollama_call(build_chapter_prompt(n, beat), num_predict=900)
    chapter_en = body.get("response", "").strip()
    if not chapter_en:
        print("[error] Tomt svar fra modellen.", file=sys.stderr)
        return 1
    write_stats = generation_stats(body)
    print("[write] " + ", ".join(f"{k}={v}" for k, v in write_stats.items()))

    # Keep the draft. It is the only way to tell a bad chapter from a bad
    # translation, which are very different problems to fix.
    write(STATE / "drafts" / f"kapittel-{n:03d}.en.md", chapter_en)

    chapter_nb, translate_bodies = translate_chapter(chapter_en)
    translate_stats = merge_stats(translate_bodies)
    print("[translate] " + ", ".join(f"{k}={v}" for k, v in translate_stats.items()))

    # The heading survives translation as "Kapittel N" if we are lucky and as
    # "Chapter N" if we are not. Normalise it so the site's chapter list does not
    # end up bilingual.
    chapter_nb = re.sub(r"\A\s*#*\s*(?:Chapter|Kapittel)\s+\d+", f"Kapittel {n}",
                        chapter_nb, count=1, flags=re.I)

    # Lift the title from the heading line. If translation dropped the heading
    # the first line is just prose, which would make a nonsense title — fall back
    # to the plain chapter number. Quote-escape either way: one stray " from the
    # model would break the YAML and take the whole page down with it.
    first_line = chapter_nb.splitlines()[0].strip("# ").strip().replace('"', "'")
    if not re.match(rf"^Kapittel\s+{n}\b", first_line):
        first_line = f"Kapittel {n}"
    # generation:* is machine-written bookkeeping: how long this chapter took, on
    # what, and how it split between drafting and translating. Handy when swapping
    # models — old chapters keep the numbers they were written with.
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
        '  pipeline: "en -> nb"',
        f'  seconds: {round(write_stats["seconds"] + translate_stats["seconds"], 1)}',
        "  write:",
    ]
    lines += [f"    {k}: {v}" for k, v in write_stats.items()]
    lines += ["  translate:"]
    lines += [f"    {k}: {v}" for k, v in translate_stats.items()]
    lines += ["---", ""]
    fm = "\n".join(lines) + "\n"
    out_path = CHAPTERS / f"kapittel-{n:03d}.md"
    write(out_path, fm + chapter_nb)
    print(f"[write] Lagret {out_path}")

    print("[state] Oppdaterer notatboka ...")
    update_notebook(n, chapter_en)

    meta["chapter_count"] = n
    meta["last_written"] = today
    save_meta(meta)
    print(f"[done] Kapittel {n} ferdig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
