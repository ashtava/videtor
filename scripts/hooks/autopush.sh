#!/bin/bash
# Claude Code Stop hook: commit whatever changed in the repo and push it to GitHub.
#
# Wired in .claude/settings.local.json ("hooks" > "Stop"), so it runs at the end of every Claude turn
# in this repo (the user asked on 2026-11-01: "push to github and create a push everytime a change
# happens"). out/ and cache/ are gitignored and never pushed. It stays out of the way: no output when
# nothing changed, a one-line systemMessage otherwise, and exit 0 always (a Stop hook must not block).
#
# It skips: a merge/rebase/cherry-pick in progress, unmerged paths, a held index.lock, a detached HEAD.
# It refuses to commit: leftover conflict markers, anything that looks like a secret (the repo is public).
# It unstages files over 50 MB (GitHub refuses files over 100 MB) and says so.
# Try it by hand: echo '{}' | scripts/hooks/autopush.sh

cat >/dev/null  # the hook's JSON input; not needed
REPO="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
cd "$REPO" 2>/dev/null || exit 0
MAX_BYTES=$((50 * 1024 * 1024))
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=15}"
export GIT_TERMINAL_PROMPT=0

say() {  # a message the user sees under the turn
  if command -v jq >/dev/null 2>&1; then jq -cn --arg m "$1" '{systemMessage: $m}'
  else python3 -c 'import json,sys; print(json.dumps({"systemMessage": sys.argv[1]}))' "$1"; fi
  exit 0
}

GIT_DIR=$(git rev-parse --git-dir 2>/dev/null) || exit 0
for f in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
  case "$f" in MERGE_HEAD) op=merge ;; CHERRY_PICK_HEAD) op=cherry-pick ;; REVERT_HEAD) op=revert ;; *) op=rebase ;; esac
  [ -e "$GIT_DIR/$f" ] && say "Auto-push skipped: a git $op is in progress; finish it and the next turn pushes."
done
[ -e "$GIT_DIR/index.lock" ] && say "Auto-push skipped: another git command holds the index (index.lock)."
BRANCH=$(git symbolic-ref --quiet --short HEAD) || say "Auto-push skipped: HEAD is detached."
[ -n "$(git diff --name-only --diff-filter=U)" ] && say "Auto-push skipped: unmerged files; resolve them first."

if [ -n "$(git status --porcelain)" ]; then
  git add -A || say "Auto-push: git add failed."

  big=""
  while IFS= read -r -d '' f; do
    [ -f "$f" ] && [ "$(wc -c <"$f" | tr -d ' ')" -gt "$MAX_BYTES" ] && { git reset -q -- "$f"; big="$big $f"; }
  done < <(git diff --cached --name-only -z --diff-filter=AM)

  markers=$(git diff --cached --check 2>/dev/null | grep 'leftover conflict marker' | cut -d: -f1 | sort -u | tr '\n' ' ')
  if [ -n "$markers" ]; then
    git reset -q
    say "Auto-push blocked: leftover conflict markers in ${markers}— fix them and the next turn pushes."
  fi
  if git diff --cached -U0 | grep -E -q '^\+.*(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)'; then
    git reset -q
    say "Auto-push blocked: the changes look like they contain a secret (token or private key). Nothing was committed."
  fi

  if ! git diff --cached --quiet; then
    files=$(git diff --cached --name-only)
    n=$(printf '%s\n' "$files" | wc -l | tr -d ' ')
    head3=$(printf '%s\n' "$files" | head -3 | tr '\n' ',' | sed 's/,$//; s/,/, /g')
    [ "$n" -gt 3 ] && head3="$head3 +$((n - 3)) more"
    printf 'Auto-save: %s\n\n%s\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
      "$head3" "$(printf '%s\n' "$files" | head -40)" | git commit -q -F - || say "Auto-push: git commit failed."
  fi
fi

# push when the branch is ahead of its upstream (or has none yet); this also retries an earlier failed push
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
else
  ahead=1
fi
note=""; [ -n "$big" ] && note=" Left out (over 50 MB):$big."
[ "$ahead" -gt 0 ] 2>/dev/null || { [ -n "$note" ] && say "Auto-push:$note"; exit 0; }
sha=$(git rev-parse --short HEAD)
if err=$(git push -q -u origin "$BRANCH" 2>&1); then
  say "Auto-pushed $sha to origin/$BRANCH ($ahead commit(s)).$note"
else
  say "Auto-push: committed $sha locally, but the push failed: $(printf '%s' "$err" | grep -v '^$' | tail -1). The next turn retries.$note"
fi
