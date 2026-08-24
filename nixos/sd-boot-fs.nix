# Filesystems for a system running off the SD card that was flashed from the
# upstream nixos-unstable aarch64 sd-image. The labels below are the ones that
# image creates, so declaring them here means no generated
# hardware-configuration.nix is needed.
#
# This module is imported by nixosConfigurations.novelist only. The
# `nix build .#images.novelist` path must NOT import it, because the sd-image
# module declares its own root filesystem.
{ ... }:

{
  fileSystems."/" = {
    device = "/dev/disk/by-label/NIXOS_SD";
    fsType = "ext4";
  };

  fileSystems."/boot/firmware" = {
    device = "/dev/disk/by-label/FIRMWARE";
    fsType = "vfat";
    options = [ "nofail" "noauto" ];
  };
}
