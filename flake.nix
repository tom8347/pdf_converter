
{
  description = "PDF to EPUB converter using Claude Vision API";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
  let
    system = "x86_64-linux";
    pkgs = import nixpkgs { inherit system; };
  in
  {
    devShells.${system}.default = pkgs.mkShell {
      packages = [
        (pkgs.python3.withPackages (ps: with ps; [
          pip
          pynvim
          python-lsp-server
        ]))
      ];

      # PyMuPDF wheels need libstdc++ and other native libs at runtime
      LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
        pkgs.stdenv.cc.cc.lib
      ];

      shellHook = ''
        if [ ! -d .venv ]; then
          echo "Setting up Python virtual environment..."
          python -m venv .venv --system-site-packages
          source .venv/bin/activate
          pip install -q PyMuPDF anthropic Pillow EbookLib
        else
          source .venv/bin/activate
        fi
        echo "PDF Converter environment ready — Python $(python --version 2>&1 | cut -d' ' -f2)"
      '';
    };
  };
}
