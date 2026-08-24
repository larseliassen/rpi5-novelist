# rpi5-novelist

A Raspberry Pi 5 (8GB) running **NixOS** that writes a **Norwegian (bokmål) crime
novel**, one chapter per day, with a **local LLM** via Ollama, and publishes it as
an **Astro** site on **GitHub Pages**.

- Mystery is **improvised** (no fixed solution up front), nudged by a rotating
  "director beat" so it escalates and pays off instead of wandering.
- Coherence over many chapters comes from a **compressed notebook** (synopsis,
  characters, clues, timeline, rolling recap) — not from stuffing old chapters
  into the tiny context window.
- Runs **forever-ish**: a new chapter every morning at 06:00 Europe/Oslo.

```
rpi5-novelist/
├── flake.nix                  # NixOS system (RPi5, boots from SSD)
├── nixos/configuration.nix    # ollama + daily systemd timer + user
├── orchestrator/
│   ├── modelfile/Modelfile    # builds the `novelist` Ollama model
│   ├── write_chapter.py       # the brain: writes + updates notebook
│   └── run.sh                 # daily entrypoint (write → git push)
├── web/                       # Astro site → GitHub Pages
├── chapters/                  # kapittel-NNN.md (shared by Pi + site)
├── state/                     # the compressed notebook (git-tracked)
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

The repo is **private**, so the Pi needs its own key before it can clone or push.
Do this *before* the first `nixos-rebuild`, otherwise `novelist-bootstrap` fails:

```bash
# as the `novelist` user, on the Pi
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

Add that public key to the repo on GitHub under **Settings → Deploy keys**, with
**"Allow write access" ticked** — the daily job pushes each new chapter, so a
read-only deploy key is not enough.

The `novelist-bootstrap` service then clones the repo into `/var/lib/novelist`.
The remaining one-time steps:

```bash
# as the `novelist` user
cd /var/lib/novelist

# Pull a base model and build the persona model:
ollama pull mistral:7b-instruct        # or your chosen Norwegian GGUF
ollama create novelist -f orchestrator/modelfile/Modelfile

# The daily commit needs an author identity:
git config user.name  "novelist (rpi5)"
git config user.email "novelist@localhost"

# Dry run one chapter now (instead of waiting for 06:00):
sudo systemctl start novelist.service
journalctl -u novelist.service -f
```

> **GitHub Pages on a private repo requires a paid plan** (Pro/Team/Enterprise).
> On a free account `.github/workflows/deploy.yml` will build but fail to deploy.
> Either upgrade, or make the repo public and keep secrets out of it — which is
> what the Wi-Fi `secretsFile` and the deploy-key setup above are designed for.

## 3. Choosing the model (Norwegian quality vs. speed)

Edit `orchestrator/modelfile/Modelfile`:

- **`mistral:7b-instruct`** — good instruction following, usable Norwegian.
  Best default for the write→update-notebook loop. ~1.5–3 tok/s on Pi CPU.
- **NorMistral 11B instruct (GGUF)** — more natively Norwegian. Tight on 8GB at
  Q4 (~6.5GB). Point `FROM` at the downloaded `.gguf`. Slower.
- **`gemma2:2b`** — fast fallback; weaker prose.
- Avoid the *base* NorMistral "warm" model here — it's a completion model and
  ignores instructions.

A full chapter is ~20–40 min at these speeds; fine for a once-daily job.
`num_ctx` is deliberately small (4096) because memory lives in the notebook.

## 4. The website

The GitHub Action symlinks repo-root `chapters/` into Astro's content
collection, builds, and deploys to Pages. To enable:

1. Repo **Settings → Pages → Source: GitHub Actions**.
2. Set `site`/`base` in `web/astro.config.mjs` to your Pages URL.
3. Push. Every time the Pi commits a new chapter, the site rebuilds automatically.

Local preview:
```bash
cd web
ln -sfn ../../../chapters src/content/kapitler
npm install && npm run dev
```

## Tuning the story

- **Pace / payoffs:** edit `DIRECTOR_BEATS` in `write_chapter.py`.
- **Voice / rules:** edit the `SYSTEM` block in the Modelfile.
- **Reset the novel:** delete `state/` and `chapters/`; next run bootstraps a
  fresh premise.
- **Chapter length:** `num_predict` in `write_chapter.py` (~2600 ≈ 1500–2200 words).
```
