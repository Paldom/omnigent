#!/usr/bin/env bash
# Bring this fork up to date with omnigent-ai/omnigent.
#
# Upstream moves fast — roughly a hundred issues a week and a couple of hundred
# open PRs — so a fork that syncs monthly is a fork that conflicts monthly.
# Run this often enough that each merge is small.
#
#   scripts/sync-upstream.sh              # sync and report
#   scripts/sync-upstream.sh --check      # report only, change nothing
#
# What it does, in order:
#   1. fetches upstream
#   2. reports what changed
#   3. reports which carried patches upstream has now merged
#   4. merges upstream/main into the working branch
#   5. reruns the checks that the carried patches depend on
#
# Step 3 is the point. A carried patch whose upstream PR has merged should be
# dropped, not re-merged forever — that is how a fork delta shrinks instead of
# calcifying. FORK-DELTA.md lists what is carried and why.

set -euo pipefail

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

branch="$(git rev-parse --abbrev-ref HEAD)"

if ! git remote get-url upstream >/dev/null 2>&1; then
  echo "adding upstream remote"
  git remote add upstream https://github.com/omnigent-ai/omnigent.git
fi

echo "==> fetching upstream"
git fetch upstream --quiet
git fetch origin --quiet

behind="$(git rev-list --count HEAD..upstream/main)"
echo "==> $branch is $behind commit(s) behind upstream/main"

if [[ "$behind" != "0" ]]; then
  echo
  echo "==> what changed upstream"
  # `| head` would SIGPIPE the log under `set -o pipefail` once there are more
  # than 40 commits, which is a normal week here.
  git log --oneline --no-merges -40 HEAD..upstream/main
  [[ "$behind" -gt 40 ]] && echo "    ... and $((behind - 40)) more"
fi

# A carried patch is worth dropping the moment its PR lands upstream. Checking
# by PR number rather than by content, because a maintainer may have reworked
# it on the way in and a content match would miss that.
echo
echo "==> carried patches, and whether upstream has taken them"
if command -v gh >/dev/null 2>&1; then
  grep -oE 'omnigent/(pull|issues)/[0-9]+' FORK-DELTA.md 2>/dev/null \
    | grep -oE '[0-9]+$' | sort -u | while read -r number; do
      state="$(gh pr view "$number" --repo omnigent-ai/omnigent --json state \
        --jq .state 2>/dev/null || echo "not-a-pr")"
      case "$state" in
        MERGED) echo "    #$number MERGED — drop it from FORK-DELTA.md and stop carrying it" ;;
        CLOSED) echo "    #$number CLOSED — decide whether to keep carrying it, and record why" ;;
        OPEN)   echo "    #$number still open" ;;
        *)      echo "    #$number is an issue, not a PR" ;;
      esac
    done
else
  echo "    (install gh to have this checked for you)"
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo
  echo "==> --check: stopping before the merge"
  exit 0
fi

if [[ "$behind" == "0" ]]; then
  echo
  echo "==> already current — nothing to merge"
  exit 0
fi

echo
echo "==> merging upstream/main into $branch"
if ! git merge --no-edit upstream/main; then
  echo
  echo "CONFLICT. The usual suspects, in order of likelihood:"
  echo "  omnigent/db/db_models.py     — every schema PR touches it"
  echo "  omnigent/runtime/pending_elicitations.py"
  echo "  omnigent/server/routes/_sessions/helpers.py"
  echo "Resolve, then rerun the checks below by hand."
  exit 1
fi

echo
echo "==> rerunning the checks the carried patches depend on"
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync pyrefly check
uv run --no-sync pytest \
  tests/runtime/test_pending_elicitations.py \
  tests/stores/test_elicitation_store.py \
  tests/db/test_migration_elicitations.py \
  tests/test_harness_capabilities.py \
  tests/server/integration/test_sessions_elicitation_restart.py \
  -q
PYTHONPATH=. uv run --no-sync pytest tests/army -q

echo
echo "==> synced. Review FORK-DELTA.md, then push."
