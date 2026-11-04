#!/bin/bash
# Claude Code Stop hook: commit whatever changed in the repo and push it to GitHub.
#
# Wired in .claude/settings.local.json ("hooks" > "Stop"), so it runs at the end of every Claude turn
# in this repo, in the terminal and in Claude Code desktop (the user asked on 2026-11-01: "push to github
# and create a push everytime a change happens"). out/ and cache/ are gitignored and never pushed. It stays
# out of the way: no output when nothing changed, a one-line systemMessage otherwise, and exit 0 always (a
# Stop hook must not block).
#
# Worktree sessions (Claude Code desktop's Worktree toggle, .claude/worktrees/<name>): the hook runs in the
# main checkout (CLAUDE_PROJECT_DIR) and the session's own folder comes in the input's `cwd`. When that folder
# is a worktree of this repo, its branch is committed and pushed too; folders outside this repo are left alone.
#
# It skips: a merge/rebase/cherry-pick in progress, unmerged paths, a held index.lock, a detached HEAD.
# It refuses to commit: leftover conflict markers, anything that looks like a secret (the repo is public).
# It unstages files over 50 MB (GitHub refuses files over 100 MB) and says so.
# Try it by hand: echo '{}' | scripts/hooks/autopush.sh   (or '{"cwd": "<a worktree>"}')

INPUT=$(cat)
MAIN="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
MAX_BYTES=$((50 * 1024 * 1024))
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=15}"
export GIT_TERMINAL_PROMPT=0

common_dir() {  # the repo's shared .git directory, as a physical path (the same for all its worktrees)
  (cd "$1" 2>/dev/null && cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd -P)
}

save() {  # commit and push the checkout at $1; prints one line when it did, skipped or refused something
  cd "$1" 2>/dev/null || return 0
  local gd f op branch big markers files n head3 ahead note sha err
  gd=$(git rev-parse --git-dir 2>/dev/null) || return 0
  for f in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
    case "$f" in MERGE_HEAD) op=merge ;; CHERRY_PICK_HEAD) op=cherry-pick ;; REVERT_HEAD) op=revert ;; *) op=rebase ;; esac
    [ -e "$gd/$f" ] && { echo "skipped: a git $op is in progress; finish it and the next turn pushes."; return 0; }
  done
  [ -e "$gd/index.lock" ] && { echo "skipped: another git command holds the index (index.lock)."; return 0; }
  branch=$(git symbolic-ref --quiet --short HEAD) || { echo "skipped: HEAD is detached."; return 0; }
  [ -n "$(git diff --name-only --diff-filter=U)" ] && { echo "skipped: unmerged files; resolve them first."; return 0; }

  big=""
  if [ -n "$(git status --porcelain)" ]; then
    git add -A || { echo "git add failed."; return 0; }
    while IFS= read -r -d '' f; do
      [ -f "$f" ] && [ "$(wc -c <"$f" | tr -d ' ')" -gt "$MAX_BYTES" ] && { git reset -q -- "$f"; big="$big $f"; }
    done < <(git diff --cached --name-only -z --diff-filter=AM)

    markers=$(git diff --cached --check 2>/dev/null | grep 'leftover conflict marker' | cut -d: -f1 | sort -u | tr '\n' ' ')
    if [ -n "$markers" ]; then
      git reset -q
      echo "blocked: leftover conflict markers in ${markers}— fix them and the next turn pushes."; return 0
    fi
    if git diff --cached -U0 | grep -E -q '^\+.*(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|hf_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)'; then
      git reset -q
      echo "blocked: the changes look like they contain a secret (token or private key). Nothing was committed."; return 0
    fi

    if ! git diff --cached --quiet; then
      files=$(git diff --cached --name-only)
      n=$(printf '%s\n' "$files" | wc -l | tr -d ' ')
      head3=$(printf '%s\n' "$files" | head -3 | tr '\n' ',' | sed 's/,$//; s/,/, /g')
      [ "$n" -gt 3 ] && head3="$head3 +$((n - 3)) more"
      printf 'Auto-save: %s\n\n%s\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n' \
        "$head3" "$(printf '%s\n' "$files" | head -40)" | git commit -q -F - || { echo "git commit failed."; return 0; }
    fi
  fi

  # push when the branch is ahead of its upstream (or has none yet); this also retries an earlier failed push
  if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
  else
    ahead=1
  fi
  note=""; [ -n "$big" ] && note=" Left out (over 50 MB):$big."
  [ "$ahead" -gt 0 ] 2>/dev/null || { [ -n "$note" ] && echo "${note# }"; return 0; }
  sha=$(git rev-parse --short HEAD)
  if err=$(git push -q -u origin "$branch" 2>&1); then
    echo "pushed $sha to origin/$branch ($ahead commit(s)).$note"
  else
    echo "committed $sha on $branch, but the push failed: $(printf '%s' "$err" | grep -v '^$' | tail -1). The next turn retries.$note"
  fi
}

# the checkouts to save: the session's worktree (when it is one of this repo's), then the main checkout
roots=()
main_root=$(git -C "$MAIN" rev-parse --show-toplevel 2>/dev/null) || exit 0
cwd=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
if [ -n "$cwd" ] && wt_root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null) \
   && [ "$wt_root" != "$main_root" ] && [ "$(common_dir "$wt_root")" = "$(common_dir "$main_root")" ]; then
  roots+=("$wt_root")
fi
roots+=("$main_root")

msgs=""
for r in "${roots[@]}"; do
  m=$(save "$r")
  [ -z "$m" ] && continue
  [ "$r" != "$main_root" ] && m="worktree ${r##*/}: $m"
  msgs="${msgs:+$msgs · }$m"
done
[ -z "$msgs" ] && exit 0
if command -v jq >/dev/null 2>&1; then jq -cn --arg m "Auto-push: $msgs" '{systemMessage: $m}'
else python3 -c 'import json,sys; print(json.dumps({"systemMessage": sys.argv[1]}))' "Auto-push: $msgs"; fi
exit 0
