#!/bin/sh
# Build the single-file `trawl` executable with stdlib zipapp.
# Nests the package under an absolute-import launcher so the package's relative
# imports resolve inside the archive (a bare archive-root __main__ can't).
set -e
cd "$(dirname "$0")"
rm -rf build dist
mkdir -p build dist
cp -r trawl build/trawl
rm -rf build/trawl/__pycache__
printf 'import sys\nfrom trawl.__main__ import main\nsys.exit(main())\n' > build/__main__.py
python3 -m zipapp build -o dist/trawl -p "/usr/bin/env python3"
chmod +x dist/trawl
rm -rf build
echo "built dist/trawl  —  install: ln -sf \"$(pwd)/dist/trawl\" /usr/local/bin/trawl"
