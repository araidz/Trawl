#!/usr/bin/env bash
#
# Tag, release, and update the Homebrew formula in one shot.
#
#   ./release.sh 0.2.9
#
# The formula builds from the source tarball GitHub generates for the tag, so
# there's no artifact to upload — pushing the tag is enough. Bump the version in
# the tool's source first; this only ships it.
set -euo pipefail

usage() { echo "usage: ./release.sh 0.2.9" >&2; exit 1; }
[ "$#" -eq 1 ] || usage
version="$1"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage

for tool in git gh python3 dirname; do
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
tap_branch="$(git -C "$tap" symbolic-ref --quiet --short HEAD)" || { echo "Homebrew tap is not on a branch" >&2; exit 1; }
[ "$tap_branch" = main ] || { echo "Homebrew tap branch must be main (found $tap_branch)" >&2; exit 1; }
[ -z "$(git -C "$tap" status --porcelain)" ] \
  || { echo "Homebrew tap working tree must be clean (including untracked files)" >&2; exit 1; }
git -C "$tap" fetch origin main
git -C "$tap" merge-base --is-ancestor origin/main HEAD \
  || { echo "Homebrew tap main is behind or diverged from origin/main" >&2; exit 1; }

head="$(git rev-parse HEAD)"
local_tag=false
remote_tag=false
if git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null; then
  local_tag=true
  [ "$(git rev-parse "$tag^{commit}")" = "$head" ] \
    || { echo "local $tag does not resolve to HEAD" >&2; exit 1; }
fi
remote_refs="$(git ls-remote --tags origin "refs/tags/$tag" "refs/tags/$tag^{}")"
if [ -n "$remote_refs" ]; then
  remote_tag=true
  remote_commit="$(while read -r oid ref; do
    [ "$ref" = "refs/tags/$tag^{}" ] && { echo "$oid"; break; }
    echo "$oid"
  done <<< "$remote_refs" | python3 -c 'import sys; print(sys.stdin.read().splitlines()[-1])')"
  [ "$remote_commit" = "$head" ] \
    || { echo "remote $tag does not resolve to HEAD" >&2; exit 1; }
fi

if [ "$local_tag" = false ] && [ "$remote_tag" = false ]; then
  [ "$(git cat-file -t refs/tags/v0.2.8)" = commit ] \
    || { echo "v0.2.8 is not the expected lightweight tag" >&2; exit 1; }
  git tag "$tag"
  local_tag=true
fi
if [ "$local_tag" = false ]; then
  git fetch origin "refs/tags/$tag:refs/tags/$tag"
  local_tag=true
fi
if [ "$remote_tag" = false ]; then
  git push origin "$tag"
fi

releases="$(gh release list --limit 1000 --json tagName,isDraft,isPrerelease)"
release="$(python3 -c '
import json, sys
matches = [r for r in json.load(sys.stdin) if r["tagName"] == sys.argv[1]]
if len(matches) > 1:
    raise SystemExit(f"multiple releases found for {sys.argv[1]}")
if matches:
    print(json.dumps(matches[0]))
' "$tag" <<< "$releases")"
if [ -z "$release" ]; then
  gh release create "$tag" --title "trawl $tag" --generate-notes
else
  IFS=$'\t' read -r release_tag release_draft release_prerelease <<< "$(python3 -c '
import json, sys
r = json.load(sys.stdin)
print(r["tagName"], str(r["isDraft"]).lower(), str(r["isPrerelease"]).lower(), sep="\t")
' <<< "$release")"
  [ "$release_tag" = "$tag" ] || { echo "release tag mismatch" >&2; exit 1; }
  [ "$release_prerelease" = false ] || { echo "$tag is unexpectedly a prerelease" >&2; exit 1; }
  if [ "$release_draft" = true ]; then
    gh release edit "$tag" --draft=false
  fi
fi

IFS=$'\t' read -r release_tag release_draft release_prerelease release_url \
  <<< "$(gh release view "$tag" --json tagName,isDraft,isPrerelease,url --jq '[.tagName, .isDraft, .isPrerelease, .url] | @tsv')"
[ "$release_tag" = "$tag" ] || { echo "release tag mismatch" >&2; exit 1; }
[ "$release_draft" = false ] || { echo "$tag release is still a draft" >&2; exit 1; }
[ "$release_prerelease" = false ] || { echo "$tag is unexpectedly a prerelease" >&2; exit 1; }

"$tap/bump.sh" trawl "$version"
echo "✓ released trawl $tag: $release_url"
