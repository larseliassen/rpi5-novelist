{
  description = "RPi5 Norwegian crime-novelist appliance (NixOS)";

  inputs = {
    # Pi 5 support in the generic aarch64 SD image landed after the 26.05 release,
    # so unstable is required — stable images will not boot on a Pi 5.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixos-hardware.url = "github:NixOS/nixos-hardware";
  };

  outputs = { self, nixpkgs, nixos-hardware, ... }:
  let
    system = "aarch64-linux";
  in {
    nixosConfigurations.novelist = nixpkgs.lib.nixosSystem {
      inherit system;
      modules = [
        # NOTE: we deliberately do NOT import
        #   nixos-hardware.nixosModules.raspberry-pi-5
        # here. That profile pins the downstream Raspberry Pi kernel, which has no
        # binary cache and takes hours to compile on the Pi itself. The stock
        # nixos-unstable SD image boots the Pi 5 on the mainline kernel, so we stay
        # on cached mainline. Import the profile only if you need vendor-kernel
        # features (e.g. certain camera/HAT overlays).
        ./nixos/sd-boot-fs.nix
        ./nixos/configuration.nix
      ];
    };

    # Convenience: a full SD image, buildable only on an aarch64-linux machine or
    # remote builder. The normal path is to flash the upstream unstable image and
    # then `nixos-rebuild switch --flake .#novelist` on the Pi — see README.
    #   nix build .#images.novelist
    images.novelist =
      (nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          # The sd-image module provides the root filesystem itself, so
          # ./nixos/sd-boot-fs.nix is deliberately not imported here.
          "${nixpkgs}/nixos/modules/installer/sd-card/sd-image-aarch64.nix"
          ./nixos/configuration.nix
        ];
      }).config.system.build.sdImage;
  };
}
