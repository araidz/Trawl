#!/usr/bin/env bash
#
# Tag, release, and update the Homebrew formula in one shot.
#
#   ./release.sh <version>          e.g. ./release.sh 0.1.3
#
# The formula builds from the source tarball GitHub generates for the tag, so
# there's no artifact to upload — pushing the tag is enough. Bump the version in
# the tool's source first; this only ships it.
set -euo pipefail
cd "$(dirname "$0")"

version="${1:?usage: release.sh <version>}"
name="$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
tap="../homebrew-tap"

git push origin main
git tag -a "v$version" -m "$name v$version" 2>/dev/null || true
git push origin "v$version"
gh release create "v$version" --title "$name v$version" --generate-notes 2>/dev/null || true

"$tap/bump.sh" "$name" "$version"
echo "✓ released $name v$version"
