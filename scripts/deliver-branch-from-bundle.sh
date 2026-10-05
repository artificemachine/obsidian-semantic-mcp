#!/usr/bin/env bash
set -euo pipefail

REMOTE_URL="https://github.com/artificemachine/obsidian-semantic-mcp.git"
GITLEAKS_BIN="/usr/local/bin/gitleaks"

usage() {
  printf '%s\n' \
    'usage: deliver-branch-from-bundle.sh BUNDLE BRANCH BASE_SHA TARGET_SHA' \
    '' \
    'Run on vm740 as the human account whose GitHub credential helper can push.' \
    'The bundle must contain BASE_SHA and TARGET_SHA. The script scans exactly' \
    'BASE_SHA..TARGET_SHA with gitleaks, pushes TARGET_SHA to BRANCH, and verifies' \
    'the remote branch SHA.'
}

die() {
  echo "error: $1" >&2
  exit 1
}

if [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

if [ "$#" -ne 4 ]; then
  usage >&2
  exit 2
fi

bundle=$1
branch=$2
base_sha=$3
target_sha=$4

[ -f "$bundle" ] || die "bundle does not exist: $bundle"
git check-ref-format --branch "$branch" >/dev/null || die "invalid branch: $branch"
[[ "$base_sha" =~ ^[0-9a-fA-F]{7,64}$ ]] || die "invalid base SHA"
[[ "$target_sha" =~ ^[0-9a-fA-F]{7,64}$ ]] || die "invalid target SHA"
command -v git >/dev/null 2>&1 || die "git is not on PATH"
[ -x "$GITLEAKS_BIN" ] || die "gitleaks is not executable: $GITLEAKS_BIN"

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/osm-delivery.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT

git clone --no-checkout "$bundle" "$work_dir/repository" >/dev/null
cd "$work_dir/repository"

base_commit=$(git rev-parse --verify "${base_sha}^{commit}") \
  || die "base SHA is not in the bundle"
target_commit=$(git rev-parse --verify "${target_sha}^{commit}") \
  || die "target SHA is not in the bundle"
git merge-base --is-ancestor "$base_commit" "$target_commit" \
  || die "base SHA is not an ancestor of target SHA"

"$GITLEAKS_BIN" git --redact --no-banner \
  --log-opts="${base_commit}..${target_commit}" .

git remote set-url origin "$REMOTE_URL"
git push origin "${target_commit}:refs/heads/${branch}"

remote_commit=$(git ls-remote "$REMOTE_URL" "refs/heads/${branch}" | awk '{print $1}')
[ "$remote_commit" = "$target_commit" ] \
  || die "remote branch SHA does not match pushed target"

echo "delivered: branch=$branch sha=$target_commit"
