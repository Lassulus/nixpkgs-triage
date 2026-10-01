{
  description = "Triage workflow for open NixOS/nixpkgs pull requests";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
    in
    {
      packages = forAllSystems (system: {
        default = nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix { };
      });

      # Uses the importing system's nixpkgs for the package; override services.nixpkgs-triage.package
      # to pin it to this flake's instead.
      nixosModules.default = ./nix/module.nix;

      checks = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (system: {
        module = import ./nix/test.nix {
          pkgs = nixpkgs.legacyPackages.${system};
          module = self.nixosModules.default;
        };
      });
    };
}
