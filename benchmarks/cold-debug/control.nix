{ source ? ../../.work/nix-ninja-recording }:
let
  upstream = builtins.getFlake ("path:" + toString source);
  pkgs = import upstream.inputs.nixpkgs { system = "x86_64-linux"; };
  baseline = import ../../environment/baseline.nix { inherit source; cliRuntime = true; };
in pkgs.writeShellScriptBin "nix-ninja-task" ''
  set -e
  ${builtins.readFile ./task-flags.sh}
  exec ${baseline.nix-ninja-task}/bin/nix-ninja-task "$@"
''
