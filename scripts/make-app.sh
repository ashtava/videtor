#!/bin/bash
# Make Videtor.app: a double-clickable Mac app that opens the Videtor UI, for someone who never opens a terminal.
#
#   scripts/make-app.sh                 → ~/Applications/Videtor.app (and it is opened in Finder)
#   scripts/make-app.sh /Applications   → there instead
#
# The app is a launcher: it runs this repo's `.venv/bin/videtor ui` (the local page in its own window), installs the
# window package the first time (uv sync --extra analyze --extra ui) and logs to ~/Library/Logs/Videtor.log. Made on
# this Mac, so Gatekeeper lets it run. Run it again after moving the repo. Drag the app to the Dock to keep it there.
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd -P)
DEST="${1:-$HOME/Applications}"
APP="$DEST/Videtor.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat >"$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Videtor</string>
  <key>CFBundleDisplayName</key><string>Videtor</string>
  <key>CFBundleIdentifier</key><string>local.videtor.ui</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Videtor</string>
  <key>CFBundleIconFile</key><string>Videtor</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

cat >"$APP/Contents/MacOS/Videtor" <<EOF
#!/bin/bash
# Opens the Videtor UI from the repo this app was made for. Log: ~/Library/Logs/Videtor.log
REPO="$REPO"
LOG="\$HOME/Library/Logs/Videtor.log"
export PATH=/opt/homebrew/bin:/usr/local/bin:\$PATH
mkdir -p "\$(dirname "\$LOG")"
{
  echo "--- \$(date) starting from \$REPO"
  cd "\$REPO" || { osascript -e 'display alert "Videtor" message "The Videtor folder has moved: run scripts/make-app.sh again."'; exit 1; }
  [ -x .venv/bin/videtor ] || { osascript -e 'display alert "Videtor" message "Videtor is not installed in its folder (.venv is missing): run uv sync --extra analyze --extra ui there."'; exit 1; }
  if ! .venv/bin/python -c "import webview" >/dev/null 2>&1; then
    osascript -e 'display notification "Installing the window package (first run)" with title "Videtor"' || true
    uv sync --extra analyze --extra ui || osascript -e 'display notification "Could not install the window package: opening in the browser" with title "Videtor"' || true
  fi
  [ "\${VIDETOR_APP_CHECK:-}" = 1 ] && { echo "check ok"; exit 0; }
  exec .venv/bin/videtor ui
} >>"\$LOG" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/Videtor"

# the icon: a dark rounded square with a cyan V, rendered at every size macOS asks for
"$REPO/.venv/bin/python" - "$APP/Contents/Resources" <<'EOF'
import subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageDraw
out = Path(sys.argv[1]); iconset = Path(tempfile.mkdtemp()) / "Videtor.iconset"; iconset.mkdir()
def draw(size):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0)); d = ImageDraw.Draw(im); r = size * 0.22
    d.rounded_rectangle([size * 0.05, size * 0.05, size * 0.95, size * 0.95], radius=r, fill=(24, 26, 30, 255))
    w = size * 0.11
    d.line([(size * 0.27, size * 0.28), (size * 0.5, size * 0.76)], fill=(69, 188, 198, 255), width=int(w))
    d.line([(size * 0.5, size * 0.76), (size * 0.73, size * 0.28)], fill=(69, 188, 198, 255), width=int(w))
    d.ellipse([size * 0.5 - w / 2, size * 0.76 - w / 2, size * 0.5 + w / 2, size * 0.76 + w / 2], fill=(69, 188, 198, 255))
    return im
for s in (16, 32, 64, 128, 256, 512, 1024):
    draw(s).save(iconset / f"icon_{s}x{s}.png")
    if s >= 32: draw(s).save(iconset / f"icon_{s // 2}x{s // 2}@2x.png")
subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "Videtor.icns")], check=True)
print("icon written")
EOF

touch "$APP"  # Finder picks up the new icon
echo "made $APP (launches $REPO/.venv/bin/videtor ui; log in ~/Library/Logs/Videtor.log)"
[ -t 1 ] && open -R "$APP" || true
