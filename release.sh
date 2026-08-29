#!/usr/bin/env bash
#
# Tag, release, and update the Homebrew formula in one shot.
#
#   ./release.sh 0.2.9
#
# The formula builds from the source tarball GitHub generates for the tag, so
# there's no artifact to upload — pushing the tag is enough. Bump the version in
# the tool's source first; this only ships it.
# ponytail: idempotent for the normal path; exotic states (a foreign remote
# tag, a stuck draft) are fixed by hand-editing the release, not scripted.
set -euo pipefail

usage() { echo "usage: ./release.sh 0.2.9" >&2; exit 1; }
[ "$#" -eq 1 ] || usage
version="$1"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage

for tool in git gh python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "required tool not found: $tool" >&2; exit 1; }
done
gh auth status

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$script_dir"
tap="../homebrew-tap"
tag="v$version"

branch="$(git symbolic-ref --quiet --short HEAD)" || { echo "not on a branch" >&2; exit 1; }
[ "$branch" = main ] || { echo "current branch must be main (found $branch)" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "working tree must be clean (including untracked files)" >&2; exit 1; }
git fetch origin main --tags
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
  || { echo "HEAD must match origin/main" >&2; exit 1; }

committed_version="$(python3 -c 'import trawl; print(trawl.__version__)')"
[ "$committed_version" = "$version" ] \
  || { echo "trawl.__version__ is $committed_version, expected $version" >&2; exit 1; }

[ -d "$tap/.git" ] || { echo "missing Homebrew tap repository: $tap" >&2; exit 1; }
[ -x "$tap/bump.sh" ] || { echo "missing executable: $tap/bump.sh" >&2; exit 1; }

git rev-parse -q --verify "refs/tags/$tag" >/dev/null || git tag "$tag"
git push origin "$tag"
gh release view "$tag" >/dev/null 2>&1 || gh release create "$tag" --title "trawl $tag" --generate-notes

"$tap/bump.sh" trawl "$version"
echo "✓ released trawl $tag"