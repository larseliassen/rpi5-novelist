# rpi5-novelist

A Raspberry Pi 5 running **NixOS** that writes a **Norwegian (bokmål) crime
novel**, one chapter per day, with a **local LLM** via Ollama.

- Mystery is **improvised** (no fixed solution up front), nudged by a rotating
  "director beat" so it escalates and pays off instead of wandering.
- Coherence over many chapters comes from a **compressed notebook** (synopsis,
  characters, clues, timeline, rolling recap) — not from stuffing old chapters
  into the tiny context window.
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
│   ├── write_chapter.py       # the brain: writes + updates notebook
│   └── run.sh                 # daily entrypoint (write → push to both repos)
└── state/                     # the compressed notebook (git-tracked, private)

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

The model is built from the Modelfile automatically on every run, so there is no
manual `ollama pull` / `ollama create` step.

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

Two lessons worth keeping:

- **Anything over ~3GB of weights is OOM-killed at load** on this board.
- **Aggressive quantization wrecks non-English far faster than English.** A 7B at
  Q3 is *worse* at Norwegian than a 2B at Q4, so "shrink the big model" is a dead
  end here — Q2 is worse still. Pick a smaller model, not a coarser quant.

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

A full chapter is ~20–40 min at these speeds; fine for a once-daily job.
`num_ctx` is deliberately small (4096) because memory lives in the notebook.

## 4. The website

Lives in the public repo — see
[`larseliassen/mikromidas`](https://github.com/larseliassen/mikromidas). Its
Action symlinks that repo's `chapters/` into Astro's content collection, builds,
and deploys to <https://larseliassen.github.io/mikromidas/>. Every chapter the
Pi pushes rebuilds the site automatically; nothing here needs to change.

## Tuning the story

- **Pace / payoffs:** edit `DIRECTOR_BEATS` in `write_chapter.py`.
- **Voice / rules:** edit the `SYSTEM` block in the Modelfile.
- **Reset the novel:** delete `state/` here and `chapters/` in `mikromidas`; the
  next run bootstraps a fresh premise.
- **Chapter length:** `num_predict` in `write_chapter.py` (~2600 ≈ 1500–2200 words).
