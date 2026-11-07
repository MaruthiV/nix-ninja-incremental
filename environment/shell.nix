{ source ? ../.work/nix-ninja }:
let
  upstream = builtins.getFlake ("path:" + toString source);
  pkgs = import upstream.inputs.nixpkgs {
    system = "x86_64-linux";
    overlays = [ upstream.overlays.internal ];
  };
in pkgs.mkShell {
  packages = with pkgs; [
    cargo
    rustc
    rustfmt
    clippy
    pkg-config
    meson
    ninja
    python3
    gcc
    coreutils
    patchelf
    nix-ninja
    nix-ninja-task
  ];
}
