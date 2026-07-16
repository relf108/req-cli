{
  description = "req-cli: generate and send saved API requests via a lazygit-style TUI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;

        req-cli = python.pkgs.buildPythonApplication {
          pname = "req-cli";
          version = "0.1.0";
          pyproject = true;
          src = ./.;

          build-system = [ python.pkgs.hatchling ];

          dependencies = with python.pkgs; [
            textual
            requests
            pyyaml
            openapi-pydantic
            tomli-w
          ];

          nativeCheckInputs = with python.pkgs; [
            pytestCheckHook
            pytest-asyncio
          ];

          # coverage plugins aren't needed for the packaged build
          preCheck = ''
            substituteInPlace pyproject.toml \
              --replace-fail 'addopts = "--cov=req_cli --cov-report=term-missing"' 'addopts = ""'
          '';

          meta = {
            description = "Generate and send saved API requests via a lazygit-style TUI";
            mainProgram = "req";
          };
        };
      in
      {
        packages = {
          default = req-cli;
          req-cli = req-cli;
        };

        apps.default = {
          type = "app";
          program = "${req-cli}/bin/req";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.uv
            python
          ];
        };
      });
}
