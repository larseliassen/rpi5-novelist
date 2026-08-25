{ config, pkgs, lib, ... }:

let
  # The whole appliance lives here. The bootstrap service below clones the repo
  # into this path on first boot; the daily systemd service runs from it.
  appDir = "/var/lib/novelist";
  novelUser = "novelist";

  # The repo is private, so both the bootstrap clone and the daily push need a
  # deploy key on the Pi — anonymous https cannot read it. See README §2.
  repoUrl = "git@github.com:larseliassen/rpi5-novelist.git";

  # The chapters and the Astro site live in a separate PUBLIC repo, because
  # GitHub Pages will not serve a private repo on a free plan. The Pi pushes each
  # chapter there.
  #
  # Note the host ALIAS rather than plain github.com. Deploy keys are per-repo,
  # and GitHub binds an SSH connection to whichever key authenticates first — so
  # listing two IdentityFiles for github.com always authenticates as the
  # rpi5-novelist key and then gets denied on mikromidas. A distinct Host block
  # (see programs.ssh.extraConfig) is the only reliable way to pick the key.
  webDir = "/var/lib/novelist-web";
  webRepoUrl = "git@github-mikromidas:larseliassen/mikromidas.git";
in
{
  imports = [ ];

  ###### Boot: SD card, via the U-Boot shipped in the stock aarch64 SD image ######
  # The Raspberry Pi firmware on the FIRMWARE partition starts U-Boot, which reads
  # the extlinux.conf that this bootloader module generates. That gives us the
  # NixOS generation menu and rollback on the Pi 5.
  boot.loader.grub.enable = false;
  boot.loader.generic-extlinux-compatible.enable = true;

  # Filesystems live in ./sd-boot-fs.nix, imported from the flake, so that the
  # `nix build .#images.novelist` path can leave them to the sd-image module.

  # Broadcom Wi-Fi/Bluetooth firmware. The Pi 5 board profile does NOT pull this
  # in by itself, so without it wlan0 never appears.
  hardware.enableRedistributableFirmware = true;

  networking.hostName = "novelist";

  ###### Wi-Fi ######
  # The PSK is a secret, so it is NOT stored in this (public) repo. wpa_supplicant
  # reads it from a file on the Pi; create it once as root:
  #
  #   printf 'PSK_HOME=<64-hex-psk-or-plaintext-passphrase>\n' > /var/lib/wifi.secrets
  #   chmod 600 /var/lib/wifi.secrets
  #
  networking.wireless = {
    enable = true;
    secretsFile = "/var/lib/wifi.secrets";
    networks."isslottet".pskRaw = "ext:PSK_HOME";
  };
  # Wired works out of the box and is the easiest path for the very first deploy.
  networking.useDHCP = lib.mkDefault true;

  time.timeZone = "Europe/Oslo";
  i18n.defaultLocale = "nb_NO.UTF-8";

  ###### Users ######
  users.users.${novelUser} = {
    isNormalUser = true;
    description = "The novelist daemon owner";
    extraGroups = [ "wheel" ];
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPBzVhrtnnATZZsssR+uwRwcX+GsAM7O431RnLBGQ5F/ lame@dips.no"
    ];
  };
  users.users.root.openssh.authorizedKeys.keys =
    config.users.users.${novelUser}.openssh.authorizedKeys.keys;

  # Login is SSH-key-only and the accounts have no password, so a password prompt
  # on sudo would lock us out of our own box.
  security.sudo.wheelNeedsPassword = false;

  services.openssh = {
    enable = true;
    settings.PasswordAuthentication = false;
  };

  # mDNS, so the box answers to `novelist.local` from the Mac. The Pi is on DHCP
  # and its lease moves, so without this you are reduced to sweeping the LAN.
  services.avahi = {
    enable = true;
    publish = {
      enable = true;
      addresses = true;
      workstation = true;
    };
    nssmdns4 = true;
  };

  # The bootstrap clone and the daily push run non-interactively, so github.com's
  # host key must already be trusted or ssh aborts at the prompt. The alias below
  # resolves to the same host, hence the same key under both names.
  programs.ssh.knownHosts.github = {
    hostNames = [ "github.com" "github-mikromidas" ];
    publicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl";
  };

  # One deploy key per repo, selected by host alias. IdentitiesOnly is essential:
  # without it ssh also offers the agent's/default keys, authenticates as the
  # wrong repo's deploy key, and GitHub then refuses the push.
  programs.ssh.extraConfig = ''
    Host github.com
      IdentitiesOnly yes
      IdentityFile /home/${novelUser}/.ssh/id_ed25519

    Host github-mikromidas
      HostName github.com
      IdentitiesOnly yes
      IdentityFile /home/${novelUser}/.ssh/id_mikromidas
  '';

  ###### Ollama (local LLM runtime) ######
  services.ollama = {
    enable = true;
    # CPU inference on the Pi. Keep it modest so the box stays responsive.
    host = "127.0.0.1";
    port = 11434;
    environmentVariables = {
      # This board has 4GB. Two resident models do not fit, and ollama will happily
      # keep the previous one loaded while it loads the next — which is exactly how
      # we OOM-killed the box. One at a time, evicted promptly.
      OLLAMA_MAX_LOADED_MODELS = "1";
      OLLAMA_NUM_PARALLEL = "1";
      OLLAMA_KEEP_ALIVE = "60s";
    };
  };

  # A model that is too large for 4GB must fail as a *service* failure, not by
  # inviting the kernel OOM killer to shoot at the whole system. Without this the
  # board hangs hard and needs a physical power cycle (learned the hard way).
  systemd.services.ollama.serviceConfig = {
    MemoryHigh = "2900M";     # throttle + reclaim before things get desperate
    MemoryMax = "3200M";      # hard cap: cgroup OOM kills only ollama
    OOMPolicy = "continue";
    Restart = "always";
    RestartSec = "10s";
  };

  ###### Packages ######
  environment.systemPackages = with pkgs; [
    git
    (python3.withPackages (ps: with ps; [ requests pyyaml ]))
    jq
    curl
    vim
  ];

  ###### First-boot bootstrap ######
  # Clone the appliance repo into appDir once, so the daily job has something to
  # run. Safe to re-run: it does nothing if the checkout already exists.
  systemd.services.novelist-bootstrap = {
    description = "Clone the novelist repo on first boot";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    # openssh is required even though we never invoke ssh directly: git shells out
    # to it for git@ remotes, and systemd units get an empty PATH by default.
    path = [ pkgs.git pkgs.openssh ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = novelUser;
      StateDirectory = "novelist novelist-web";
    };
    script = ''
      if [ ! -d ${appDir}/.git ]; then
        git clone ${repoUrl} ${appDir}
      fi
      # The public site repo. Cloned over ssh rather than https so the daily push
      # authenticates with its own deploy key (deploy keys are per-repo).
      if [ ! -d ${webDir}/.git ]; then
        git clone ${webRepoUrl} ${webDir}
      fi
    '';
  };

  ###### The daily writing job ######
  # Runs orchestrator/write_chapter.py once a day. It talks to Ollama,
  # writes a new chapter + updates the notebook, then git-pushes.
  systemd.services.novelist = {
    description = "Write one crime-novel chapter (Norwegian)";
    after = [ "network-online.target" "ollama.service" "novelist-bootstrap.service" ];
    wants = [ "network-online.target" "ollama.service" "novelist-bootstrap.service" ];
    # openssh: run.sh does `git push` over a git@ remote (see novelist-bootstrap).
    path = with pkgs; [ git openssh ollama (python3.withPackages (ps: with ps; [ requests ])) ];
    serviceConfig = {
      Type = "oneshot";
      User = novelUser;
      WorkingDirectory = appDir;
      StateDirectory = "novelist novelist-web";
      # Give a single chapter plenty of time on slow CPU inference.
      TimeoutStartSec = "3h";
      ExecStart = "${pkgs.bash}/bin/bash ${appDir}/orchestrator/run.sh";
      Environment = [
        "OLLAMA_HOST=http://127.0.0.1:11434"
        "NOVELIST_MODEL=novelist"        # the Modelfile-built model name
        "NOVELIST_DIR=${appDir}"
        "NOVELIST_WEB_DIR=${webDir}"     # public repo: chapters get written here
      ];
    };
  };

  systemd.timers.novelist = {
    description = "Daily novel chapter";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 06:00:00";   # 06:00 Europe/Oslo each day
      Persistent = true;               # catch up if the Pi was off
      RandomizedDelaySec = "5m";
    };
  };

  ###### Housekeeping ######
  # This board is a 4GB Pi 5, so zram is not a nicety — it is what keeps the
  # working set alive while a ~2.5GB model is resident.
  zramSwap.enable = true;
  zramSwap.memoryPercent = 50;

  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  # This is a fresh install, so it tracks the release we first installed from —
  # it is not a "keep this up to date" field. Do not bump it after installing.
  system.stateVersion = "26.05";
}
