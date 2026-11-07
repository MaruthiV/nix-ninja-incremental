{ source ? ../.work/nix-ninja, cliRuntime ? false }:
let
  upstream = builtins.getFlake ("path:" + toString source);
  packages = upstream.inputs.nix.packages.x86_64-linux;
  runtime = if cliRuntime then packages.nix-cli else packages.nix;
  pkgs = import upstream.inputs.nixpkgs {
    system = "x86_64-linux";
    overlays = [ upstream.overlays.internal (final: prev: {
      nix-ninja-task = final.writeShellScriptBin "nix-ninja-task" ''
        exec ${prev.nix-ninja-task}/bin/nix-ninja-task \
          --build-dir "''${NIX_BUILD_TOP:?expected a Nix build}/source/build" "$@"
      '';
      mkMesonPackage = final.callPackage
        (upstream.outPath + "/modules/flake/pkgs/mkMesonPackage") {
          inherit (final) nix-ninja nix-ninja-task;
          nix = runtime;
        };
    }) ];
  };
in {
  inherit runtime;
  inherit (pkgs) nix-ninja nix-ninja-task;
  hello = pkgs.example-hello.target;
  shell = pkgs.mkShell {
    packages = with pkgs; [ nix-ninja nix-ninja-task runtime gcc coreutils patchelf meson ninja python3 ];
  };
}
