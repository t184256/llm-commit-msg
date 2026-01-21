{
  description = "Suggest commit messages with an LLM";

  outputs = { self, nixpkgs, flake-utils }@inputs:
    let
      pyDeps = pyPackages: with pyPackages; [
        click requests gitpython unidiff
      ];
      pyTestDeps = pyPackages: with pyPackages; [
        pytest pytestCheckHook
        coverage pytest-cov
      ];
      pyTools = pyPackages: with pyPackages; [ mypy types-requests ];

      tools = pkgs: with pkgs; [
        pre-commit
        ruff
        codespell
        actionlint
        python3Packages.pre-commit-hooks
      ];

      llm-commit-msg-package = {pkgs, python3Packages}:
        python3Packages.buildPythonPackage {
          pname = "llm-commit-msg";
          version = "0.0.1";
          src = ./.;
          disabled = python3Packages.pythonOlder "3.13";
          format = "pyproject";
          build-system = [ python3Packages.setuptools ];
          propagatedBuildInputs = pyDeps python3Packages;
          checkInputs = pyTestDeps python3Packages;
          postInstall = "mv $out/bin/llm_commit_msg $out/bin/llm-commit-msg";
        };

      overlay = final: prev: {
        pythonPackagesExtensions =
          prev.pythonPackagesExtensions ++ [(pyFinal: pyPrev: {
            llm-commit-msg = final.callPackage llm-commit-msg-package {
              python3Packages = pyFinal;
            };
          })];
      };

      overlay-all = nixpkgs.lib.composeManyExtensions [
        overlay
      ];
    in
      flake-utils.lib.eachDefaultSystem (system:
        let
          pkgs = import nixpkgs { inherit system; overlays = [ overlay-all ]; };
          defaultPython3Packages = pkgs.python313Packages;  # force 3.13

          llm-commit-msg = defaultPython3Packages.llm-commit-msg;
          app = flake-utils.lib.mkApp {
            drv = llm-commit-msg;
            exePath = "/bin/llm-commit-msg";
          };
        in
        {
          devShells.default = pkgs.mkShell {
            buildInputs = [(defaultPython3Packages.python.withPackages (
              pyPkgs: pyDeps pyPkgs ++ pyTestDeps pyPkgs ++ pyTools pyPkgs
            ))];
            nativeBuildInputs = [(pkgs.buildEnv {
              name = "llm-commit-msg-tools-env";
              pathsToLink = [ "/bin" ];
              paths = tools pkgs;
            })];
            shellHook = ''
              [ -e .git/hooks/pre-commit ] || \
                echo "suggestion: pre-commit install --install-hooks" >&2
              export PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=error
            '';
          };
          packages.llm-commit-msg = llm-commit-msg;
          packages.default = llm-commit-msg;
          apps.llm-commit-msg = app;
          apps.default = app;
        }
    ) // { overlays.default = overlay; };
}
