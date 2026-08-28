# rpi5-novelist

A Raspberry Pi 5 running **NixOS** that writes a **Norwegian (bokmål) crime
novel**, one chapter per day, with a **local LLM** via Ollama.

- Mystery is **improvised** (no fixed solution up front), nudged by a rotating
  "director beat" so it escalates and pays off instead of wandering.
- Coherence over many chapters comes from a **compressed notebook** (synopsis,
  characters, clues, timeline, rolling recap) — not from stuffing old chapters
  into the tiny context window.
- Chapters are **drafted in English and translated to bokmål** in a second pass.
  At 2B, composing in Norwegian produces Danish/Swedish soup; translating *into*
  Norwegian is a much easier task than writing in it. See §3.
- Runs **forever-ish**: a new chapter every morning at 06:00 Europe/Oslo.

## Two repos

GitHub Pages will not serve a **private** repo on a free plan, so the project is
split. The Pi pushes to both, with a separate deploy key for each.

| | repo | holds |
|---|---|---|
| **private** | `larseliassen/rpi5-novelist` (this one) | NixOS config, orchestrator, `state/` |
| **public** | [`larseliassen/mikromidas`](https://github.com/larseliassen/mikromidas) | `chapters/`, Astro site → [Pages](https://larseliassen.github.io/mikromidas/) |

`state/` — the notebook — stays private on purpose: it contains the planted
clues and red herrings, so publishing it would spoil the story as it is written.

```
rpi5-novelist/                 -> cloned to /var/lib/novelist
├── flake.nix                  # NixOS system (RPi5, boots from SD)
├── nixos/configuration.nix    # ollama + daily systemd timer + user
├── orchestrator/
│   ├── modelfile/Modelfile    # builds the `novelist` Ollama model
│   ├── write_chapter.py       # the brain: draft (en) → translate (nb) → notebook
│   └── run.sh                 # daily entrypoint (write → push to both repos)
└── state/                     # the compressed notebook (git-tracked, private)
    └── drafts/                # the English draft behind each published chapter

mikromidas/                    -> cloned to /var/lib/novelist-web
├── chapters/                  # kapittel-NNN.md, written by the Pi
├── web/                       # Astro site
└── .github/workflows/deploy.yml
```

## 1. Flash NixOS onto the SD card

You can't build an aarch64 image on macOS natively — but you don't need to. Since
nixpkgs PR #537862 the **generic nixos-unstable aarch64 SD image boots the Pi 5
directly**. The 26.05 stable image does *not* (it lacks the BCM2712 device trees
and the 64-bit U-Boot), so unstable is mandatory here.

1. Download the current image (this wipes the card, so back up anything on it):
   ```bash
   curl -L -o nixos-sd-aarch64.img.zst \
     "https://hydra.nixos.org/job/nixos/unstable/nixos.sd_image.aarch64-linux/latest/download/1"
   ```
2. Write it to the card (`/dev/diskN` on macOS — check with `diskutil list`; use
   the **raw** `rdiskN` node, it's far faster):
   ```bash
   diskutil unmountDisk /dev/diskN
   zstdcat nixos-sd-aarch64.img.zst | sudo dd of=/dev/rdiskN bs=4m status=progress
   sync && diskutil eject /dev/diskN
   ```
3. Boot the Pi 5 from the card. The stock image logs in as `nixos` with no
   password; use **ethernet** for this first step (Wi-Fi isn't configured yet).
4. On the Pi, clone this repo, then apply the real config:
   ```bash
   sudo -i
   printf 'PSK_HOME=<your-wifi-psk>\n' > /var/lib/wifi.secrets && chmod 600 /var/lib/wifi.secrets
   nix --extra-experimental-features 'nix-command flakes' \
     run nixpkgs#git -- clone https://github.com/USERNAME/rpi5-novelist /tmp/novelist
   nixos-rebuild switch --flake /tmp/novelist#novelist
   ```
   This installs Ollama, Python, git, Wi-Fi, your SSH key, and the daily timer,
   all declaratively. After the reboot you're at `ssh novelist@novelist`.

Edit `nixos/configuration.nix` before step 4 to set your own SSH public key, the
`repoUrl`, and your Wi-Fi SSID.

> The config keeps `/` on the SD card. To move to an SSD later, repartition it and
> point `fileSystems."/"` at the new label — the rest of the config is unchanged.

> Alternative "flash-and-go": build `nix build .#images.novelist` on an
> **aarch64-linux** builder (a cloud ARM box, another Pi, or a Linux VM), then
> `dd` the result to the card. Same config either way.

### After that: push, don't ssh

Step 4 is the only manual `nixos-rebuild`. From then on the `novelist-sync` timer
fetches this repo every 5 minutes and *applies* what it finds:

| what you changed | what the Pi does |
| --- | --- |
| `nixos/`, `flake.nix`, `flake.lock` | `nixos-rebuild switch` (as a detached `novelist-rebuild` unit) |
| `state/` only | just updates the checkout — that's the Pi's own notebook commit coming back |
| anything else (incl. `orchestrator/`) | starts `novelist.service` — writes a chapter now |

So a push is the deploy — and, for anything outside `state/`, also a chapter. That
last part is deliberate (edit a prompt, push, read the result) but it is not free:
each chapter is ~10–20 minutes of Pi CPU. Batch trivial edits, or push them as a
`state/`-only commit if you just want the checkout updated.

Two more consequences worth knowing:

- A config that fails to **build** leaves the Pi on its current generation and the
  error in `journalctl -u novelist-rebuild`. One that builds but breaks *booting*
  needs the extlinux generation menu on a monitor — there is no remote rollback.
- The sync `reset --hard`s the checkout, but **skips** the reset when local commits
  are unpushed, so a failed notebook push is never silently erased. If the Pi ever
  looks stuck at an old commit, that's the first thing to check.

## 2. First-time app setup on the Pi

**Deploy keys are per-repo**, so the Pi needs *two* — one for this private repo
(to clone and to push the notebook) and one for `mikromidas` (to push chapters).
Generate both *before* the first `nixos-rebuild`, or `novelist-bootstrap` fails:

```bash
# as the `novelist` user, on the Pi
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519      # -> rpi5-novelist
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_mikromidas   # -> mikromidas
cat ~/.ssh/id_ed25519.pub ~/.ssh/id_mikromidas.pub
```

Add each public key under **Settings → Deploy keys** of its own repo, with
**"Allow write access" ticked** — the daily job pushes, so a read-only key is
not enough.

> **Do not list both keys under `Host github.com`.** GitHub binds an SSH
> connection to whichever deploy key authenticates *first*, so ssh never reaches
> the second key: it authenticates as `rpi5-novelist` and is then denied on
> `mikromidas`. `configuration.nix` therefore defines a `github-mikromidas` host
> alias with its own `IdentityFile` and `IdentitiesOnly yes`, and the public
> repo's remote uses `git@github-mikromidas:...`. This is handled declaratively;
> no `~/.ssh/config` is needed.

`novelist-bootstrap` then clones this repo into `/var/lib/novelist` and
`mikromidas` into `/var/lib/novelist-web`. The remaining one-time steps:

```bash
# as the `novelist` user — the daily commits need an author identity in BOTH
git config --global user.name  "novelist (rpi5)"
git config --global user.email "novelist@localhost"

# Dry run one chapter now (instead of waiting for 06:00):
sudo systemctl start novelist.service
journalctl -u novelist.service -f
```

The drafting model is built from the Modelfile on every run and the translation
model is pulled if missing, so there is no manual `ollama pull` / `ollama create`
step. The first run after changing the translator downloads ~2.5GB.

## 3. Choosing the model (Norwegian quality vs. RAM)

**Know which board you have first** — `head -1 /proc/meminfo`. This matters more
than anything else in this repo.

### On a 4GB Pi 5

Measured on the actual hardware, same prompt (`num_ctx 4096`) for each:

| Model | Weights | Fits? | Norwegian |
|---|---|---|---|
| `gemma2:2b` | 1.6GB | yes | **best available** — real words, but semantically odd images |
| `qwen3:4b-instruct-2507-q4_K_M` | 2.6GB | yes | drifts into Danish (`strømmed`, `gutterne`) |
| `qwen2.5:3b-instruct` | 1.9GB | yes | heavy Danish drift, adds metatext |
| `mistral:7b-instruct-v0.3-q3_K_S` | 3.2GB | barely | **unusable** — invented non-words |
| `gemma3:4b` | 3.3GB | no | OOM at load |
| `llama3.2:3b-instruct-q8_0` | 3.4GB | no | OOM at load |

Three lessons worth keeping:

- **Anything over ~3GB of weights is OOM-killed at load** on this board.
- **Aggressive quantization wrecks non-English far faster than English.** A 7B at
  Q3 is *worse* at Norwegian than a 2B at Q4, so "shrink the big model" is a dead
  end here — Q2 is worse still. Pick a smaller model, not a coarser quant.
- **Don't ask a small model to compose in Norwegian at all.** Every model in the
  table is markedly better in English, so the pipeline drafts in English and
  translates as a separate pass. That is the single largest quality lever
  available at this RAM budget — larger than any swap within the table.

### The two-language pipeline

Per chapter: one English draft call, N translation calls, four notebook calls.
Drafting and the notebook run on `novelist` (gemma2:2b); **translation runs on a
separate, Norwegian-specialised model** set by `NOVELIST_TRANSLATE_MODEL`. Only
one model is resident at a time (`OLLAMA_MAX_LOADED_MODELS=1`), so peak RAM is
the larger of the two rather than their sum — the cost is two reloads from the SD
card per chapter, visible as `load_seconds` in the frontmatter.

Current translator: **Borealis 4B** (`hf.co/NbAiLab/borealis-4b-instruct-preview-gguf:Q4_K_M`,
2.49GB), the National Library's Norwegian-centric Gemma 3. Two traps:

- **NorMistral does not fit.** It is the better translator — the Borealis authors
  say so themselves — but the family is 7B/11B only and the smallest GGUF
  (Q3_K_M) is 3.28–3.52GB, over `MemoryMax`. Q3 is also the quant level measured
  above as destroying Norwegian. It becomes the obvious choice on an 8GB board.
- **Do not use the ollama.com tag** `NbAiLab/borealis-instruct-preview:4b`. It
  bundles an 851MB CLIP vision projector we never use, totalling 3.3GB, and is
  cgroup-killed at load. The `hf.co/...-gguf:Q4_K_M` build above is text-only.

If the translator cannot be pulled, or is killed on load, the run **does not
fail** — `run.sh` tolerates a failed `pull` and `translate_chapter()` downgrades
to `novelist` for the rest of the chapter. The published frontmatter records
`translate_model:` as the model that actually ran, so a fallback is visible
after the fact. This matters because there is no console on this box: the
alternative to falling back is a day with no chapter and no way to see why.

The translation is chunked paragraph-by-paragraph because `num_ctx` is 4096 and
a whole chapter plus its translation does not fit — Ollama would truncate
silently and publish half a chapter. Chunks never cross a paragraph boundary: cut
mid-sentence, the model *finishes* the thought instead of translating it. The
notebook stays in **English** (it is internal scaffolding that only ever feeds
the English prompt). `state/drafts/kapittel-NNN.en.md` keeps each English draft
— the only way to tell a bad chapter from a bad translation.

### The notebook

The notebook is the novel's entire long-range memory, so how it is written
matters more than how the chapters are. Four rules, each of them learned the
hard way over the first seven chapters:

- **One narrow question per call, not one big one.** Asking a 2B model for four
  differently-shaped sections in a single response does not work: it copies the
  label template out of the prompt and fills in at most the last one. That is
  why chapter 7 updated `recap.md` and nothing else. `update_notebook()` now
  makes four small calls, each with a short prompt and a small answer.
- **Characters, clues and timeline are append-only.** "List what is new in this
  chapter" is a task this model can do. "Rewrite this document without losing or
  corrupting anything" is not, and every rewrite was another chance to destroy
  continuity. Only the recap is regenerated.
- **Everything written back is validated.** A "bullet" longer than ~160
  characters is the model pasting prose at us, not a clue — `bullets()` drops
  it. This is how `clues.md` came to contain a whole paragraph of chapter 6,
  verbatim, which then rode along in every subsequent prompt.
- **Both lists are capped** (`KEEP_CLUES`, `KEEP_TIMELINE`), trimmed from the
  front. Append-only without a cap grows the prompt until it fills `num_ctx`,
  at which point Ollama drops the *front* of it — the system prompt and the
  instructions. The chapter-6 notebook call went in at 3783 tokens against a
  4096 window, and what came back was the model parroting its input.

Every notebook response is also dumped verbatim to
`state/drafts/kapittel-NNN.notebook.md` and committed. There is no console on
this box, so that file is the only way to see why the notebook did something
strange.

Each published chapter records what it cost in its frontmatter under
`generation:` — wall time split between `write:` and `translate:`, token counts,
tok/s, and the director beat it was given.

Repeated OOMs will hang the board hard enough to need a physical power cycle, so
`configuration.nix` caps `ollama.service` with `MemoryMax` and pins
`OLLAMA_MAX_LOADED_MODELS=1`. Don't remove those on a 4GB board.

### On an 8GB/16GB Pi 5

The interesting models come back into range — try these first:

- **`mistral:7b-instruct`** (q4_0, 4.4GB) — good instruction following, usable
  Norwegian. ~1.5–3 tok/s on Pi CPU.
- **NorMistral 7B/11B instruct (GGUF)** — more natively Norwegian. Point `FROM`
  at the downloaded `.gguf`. Slower.
- Avoid the *base* NorMistral "warm" model — it's a completion model and ignores
  instructions.

Chapter 7 took 9m32s of model time (4m09s drafting at 3.84 tok/s, 5m23s
translating at 2.72 tok/s) before the notebook was split into four calls; budget
~20–30 min end to end now. Fine for a once-daily job, and `TimeoutStartSec`
is 3h.
`num_ctx` is deliberately small (4096) because memory lives in the notebook.

## 4. The website

Lives in the public repo — see
[`larseliassen/mikromidas`](https://github.com/larseliassen/mikromidas). Its
Action symlinks that repo's `chapters/` into Astro's content collection, builds,
and deploys to <https://larseliassen.github.io/mikromidas/>. Every chapter the
Pi pushes rebuilds the site automatically; nothing here needs to change.

The frontmatter this repo writes is consumed by a zod schema in
`web/src/content/config.ts` over there, and **zod silently strips keys it does
not know about** — a new field added here shows up nowhere until the schema
learns about it. The `generation:` block is declared `.partial().passthrough()`
precisely so that stops being true.

## Tuning the story

- **Pace / payoffs:** edit `DIRECTOR_BEATS` in `write_chapter.py` (English — they
  go into the drafting prompt).
- **Voice / rules:** edit the `SYSTEM` block in the Modelfile. That is the
  *drafting* persona; `TRANSLATOR_SYSTEM` and `EDITOR_SYSTEM` in
  `write_chapter.py` override it for the other two passes.
- **Norwegian style:** the translation prompt in `translate_chapter()` — dialogue
  convention, register, how literal to be.
- **Reset the novel:** delete `state/` here and `chapters/` in `mikromidas`; the
  next run bootstraps a fresh premise.
- **Chapter length:** the word count in `build_chapter_prompt()` (500) and
  `num_predict` on the draft call (900).
  Raising it much past that overflows `num_ctx 4096` once the notebook prompt is
  in front of it, and the overflow is silent.
