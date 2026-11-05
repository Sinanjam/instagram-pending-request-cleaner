{
  description = "Fail-closed Instagram pending follow-request cleaner";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);
    in {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python3;
        in {
          default = python.pkgs.buildPythonApplication {
            pname = "instagram-pending-request-cleaner";
            version = "0.1.0";
            pyproject = true;
            src = self;
            build-system = [ python.pkgs.setuptools ];
            dependencies = [ python.pkgs.selenium ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            nativeCheckInputs = [ pkgs.firefox pkgs.geckodriver ];
            doCheck = true;
            checkPhase = ''
              runHook preCheck
              export HOME="$TMPDIR/home"
              mkdir -p "$HOME"
              python -m unittest discover -s tests -v
              runHook postCheck
            '';
            postFixup = ''
              wrapProgram "$out/bin/instagram-request-cleaner" \
                --prefix PATH : ${nixpkgs.lib.makeBinPath [ pkgs.firefox pkgs.geckodriver ]}
            '';
            pythonImportsCheck = [ "instagram_request_cleaner" ];
          };
        });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/instagram-request-cleaner";
          meta.description = "Safely review and withdraw pending Instagram follow requests";
        };
      });

      checks = forAllSystems (system: {
        package = self.packages.${system}.default;
      });

      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: [ ps.selenium ps.setuptools ]))
              pkgs.firefox
              pkgs.geckodriver
              pkgs.gitleaks
              pkgs.git
            ];
            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            '';
          };
        });
    };
}
