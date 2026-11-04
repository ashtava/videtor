#!/bin/bash
# Set Videtor up for Claude Code (the terminal, Claude Code desktop, VS Code) on this Mac.
#
#   scripts/setup-claude-code.sh           link the skill and add the auto-push hook (both idempotent)
#   scripts/setup-claude-code.sh --check   say what is set up, change nothing
#
# 1. The skill: ~/.claude/skills/cut-from-brief becomes a link to this repo's .claude/skills/cut-from-brief, so
#    /cut-from-brief works in every local session, whichever folder it opens in. There stays one copy, the repo's
#    (versioned on GitHub). A personal skill wins over a project skill of the same name; here both are that file.
# 2. The hook: .claude/settings.local.json (gitignored, so a fresh clone lacks it) gets the Stop hook that
#    commits and pushes every change (scripts/hooks/autopush.sh).
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd -P)
SRC="$REPO/.claude/skills/cut-from-brief"
DEST="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/cut-from-brief"  # a config dir per account is honoured
LOCAL="$REPO/.claude/settings.local.json"
HOOK='"$CLAUDE_PROJECT_DIR"/scripts/hooks/autopush.sh'
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

# 1. the skill
if [ -L "$DEST" ] && [ "$(readlink "$DEST")" = "$SRC" ]; then
  echo "skill: linked ($DEST -> $SRC)"
elif [ -e "$DEST" ] && [ ! -L "$DEST" ]; then
  echo "skill: $DEST exists and is not a link; left alone (move it away and rerun to link the repo's skill)"
elif [ "$CHECK" = 1 ]; then
  if [ -L "$DEST" ]; then echo "skill: not linked to this repo ($DEST -> $(readlink "$DEST"))"; else echo "skill: not linked ($DEST is missing)"; fi
else
  [ -L "$DEST" ] && echo "skill: replacing the link to $(readlink "$DEST")"
  mkdir -p "$(dirname "$DEST")"
  ln -sfn "$SRC" "$DEST"
  echo "skill: linked ($DEST -> $SRC)"
fi

# 2. the auto-push hook
if [ -f "$LOCAL" ] && jq -e --arg c "$HOOK" '[.hooks.Stop[]?.hooks[]? | select(.command == $c)] | length > 0' "$LOCAL" >/dev/null 2>&1; then
  echo "hook: set in .claude/settings.local.json"
elif [ "$CHECK" = 1 ]; then
  echo "hook: not set"
else
  entry=$(jq -n --arg c "$HOOK" '{hooks: [{type: "command", command: $c, timeout: 60, statusMessage: "Pushing changes to GitHub"}]}')
  if [ -f "$LOCAL" ]; then
    jq -e . "$LOCAL" >/dev/null || { echo "hook: $LOCAL is not valid JSON; fix it first"; exit 1; }
    tmp=$(mktemp); jq --argjson e "$entry" '.hooks.Stop = ((.hooks.Stop // []) + [$e])' "$LOCAL" >"$tmp" && mv "$tmp" "$LOCAL"
  else
    mkdir -p "$(dirname "$LOCAL")"; jq -n --argjson e "$entry" '{hooks: {Stop: [$e]}}' >"$LOCAL"
  fi
  echo "hook: added to .claude/settings.local.json (open /hooks once, or restart, in sessions already running)"
fi

cat <<EOF

In Claude Code desktop: open $REPO as the session's folder and leave Worktree off for deliveries (the default),
so the session works where .venv/, cache/ and out/ are. Accept the folder-trust prompt (hooks wait for it).
/cut-from-brief also works from sessions opened elsewhere: the skill runs every command from $REPO.
EOF
