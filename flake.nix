{
  description = "magic_frame - negate the black border of any Scryfall MTG card";

  # Pinned to the same nixpkgs revision as this machine's NixOS config,
  # so the flake resolves against the local store.
  inputs.nixpkgs.url = "github:nixos/nixpkgs/f7a2e428f5d71c47a5a938a3c5ad7138bb291093";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));

      mkPackage =
        pkgs:
        let
          pythonEnv = pkgs.python3.withPackages (ps: [
            ps.pillow
            ps.numpy
            ps.reportlab
          ]);
          # both scripts in one store dir so deck_to_pdf.py can import
          # negate_border.py when run via the nix store wrappers
          src = pkgs.runCommand "magic_frame-src" { } ''
            mkdir -p "$out"
            cp ${./negate_border.py} "$out/negate_border.py"
            cp ${./deck_to_pdf.py} "$out/deck_to_pdf.py"
          '';
        in
        pkgs.symlinkJoin {
          name = "magic_frame";
          paths = [
            src
            (pkgs.writeShellScriptBin "negate-border" ''
              exec ${pythonEnv}/bin/python3 ${src}/negate_border.py "$@"
            '')
            (pkgs.writeShellScriptBin "deck-to-pdf" ''
              exec ${pythonEnv}/bin/python3 ${src}/deck_to_pdf.py "$@"
            '')
          ];
        };
    in
    {
      packages = forAllSystems (pkgs: {
        default = mkPackage pkgs;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${mkPackage pkgs}/bin/negate-border";
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (mkPackage pkgs)
            (pkgs.python3.withPackages (ps: [
              ps.pillow
              ps.numpy
              ps.reportlab
            ]))
          ];
          shellHook = ''
            echo "magic_frame dev shell"
            echo "  run the tool:      negate-border <scryfall-url|image-file|dir>"
            echo "  deck to pdf:       deck-to-pdf <decklist> -o out.pdf"
            echo "  or via nix:        nix run .#default -- <scryfall-url>"
            echo "  reference masks: ref_masks_clean/ (manual, looked up first), then ref_masks/ (generated cache)"
          '';
        };
      });
    };
}
