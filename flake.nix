{
  description = "magic_frame - recolor the border of any Scryfall MTG card to white";

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
          ]);
        in
        pkgs.writeShellScriptBin "white-border" ''
          exec ${pythonEnv}/bin/python3 ${./white_border.py} "$@"
        '';
    in
    {
      packages = forAllSystems (pkgs: {
        default = mkPackage pkgs;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${mkPackage pkgs}/bin/white-border";
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (mkPackage pkgs)
            (pkgs.python3.withPackages (ps: [
              ps.pillow
              ps.numpy
              ps.cairosvg # make_masks.py rasterizes CardConjurer border.svg assets
            ]))
          ];
          shellHook = ''
            echo "magic_frame dev shell"
            echo "  run the tool:      white-border <scryfall-url>"
            echo "  or via nix:        nix run .#default -- <scryfall-url>"
            echo "  (re)build masks:   python3 make_masks.py"
            echo "  soften mask edges: python3 blur_mask.py masks -o masks-blurred"
          '';
        };
      });
    };
}
