"""Pull user messages and long assistant texts out of a Claude Code session transcript.

    python3 -I scripts/extract_transcript.py ~/.claude/projects/-Users-paritoshsrivastava-workspace-videtor/<session>.jsonl <out-dir>

Writes <out-dir>/users.txt (every message the user typed, with UTC timestamps) and
<out-dir>/assistant_long.txt (Claude's replies over 700 characters: delivery reports, analyses).
"""
import json, sys, os
src, out = sys.argv[1], sys.argv[2]
users, texts = [], {}
order = []
with open(src) as f:
    for line in f:
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        ts = d.get("timestamp", "")
        msg = d.get("message") or {}
        if t == "user" and not d.get("isMeta") and not d.get("isCompactSummary"):
            c = msg.get("content")
            if isinstance(c, str):
                s = c
            elif isinstance(c, list):
                s = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            else:
                s = ""
            s = s.strip()
            if s and not s.startswith("<local-command") and "tool_use_id" not in s:
                users.append((ts, s))
        elif t == "assistant":
            mid = msg.get("id") or d.get("uuid")
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    if mid not in texts:
                        texts[mid] = [ts, ""]
                        order.append(mid)
                    texts[mid][1] += b["text"] + "\n"
with open(os.path.join(out, "users.txt"), "w") as f:
    for i, (ts, s) in enumerate(users):
        f.write(f"=== U{i} {ts}\n{s}\n")
n = 0
with open(os.path.join(out, "assistant_long.txt"), "w") as f:
    for i, mid in enumerate(order):
        ts, s = texts[mid]
        if len(s) > 700:
            n += 1
            f.write(f"=== A{i} {ts} ({len(s)} chars)\n{s}\n")
print(len(users), "user messages;", len(order), "assistant texts;", n, "long")
