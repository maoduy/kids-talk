#!/bin/bash
# Install / update the Gọi điện - Nhắn tin con agent on the child's laptop. Run as root from the agent/ folder.
#   ./install.sh <relay-url> <agent-token> <child-account> [family-key]   (first install)
#   ./install.sh                                              (update code only)
set -euo pipefail
cd "$(dirname "$0")"
KID=${3:-}
FKEY=${4:-}
if [ $# -ge 2 ] && ! id "$KID" >/dev/null 2>&1; then
  echo "usage: $0 <relay-url> <agent-token> <child-account>  (the child's standard Linux account)" >&2; exit 1
fi
APP=/opt/kids-talk
CONF=/etc/kids-talk/agent.json
VOICE_URL=https://huggingface.co/rhasspy/piper-voices/resolve/main/vi/vi_VN/vais1000/medium

install -d -m 755 $APP $APP/voices /etc/kids-talk
install -d -m 700 /var/lib/kids-talk
install -m 755 kids_talk_agent.py $APP/kids_talk_agent.py
install -m 644 kid.html call.html icon.png $APP/
# Calls: the call window may use the mic and play sound without a prompt.
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/kids-talk.json <<'POL'
{"AudioCaptureAllowedUrls": ["http://127.0.0.1:8765"], "AutoplayAllowlist": ["http://127.0.0.1:8765"]}
POL
# Calls go through your TURN server (TCP/UDP 3478). If the child's account sits behind a firewall
# that only allows listed hosts, allow that server too.

if [ ! -x $APP/venv/bin/python3 ]; then
  python3 -m venv $APP/venv
fi
$APP/venv/bin/pip install -q --upgrade pip
$APP/venv/bin/pip install -q piper-tts pillow cryptography
# Silero VAD v5 (ends the child's voice answer after 2 s of silence)
[ -s $APP/silero_vad.onnx ] || curl -fsSL -o $APP/silero_vad.onnx \
  https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx
for f in vi_VN-vais1000-medium.onnx vi_VN-vais1000-medium.onnx.json; do
  [ -s $APP/voices/$f ] || curl -fsSL -o $APP/voices/$f "$VOICE_URL/$f"
done

if [ $# -ge 2 ]; then
  : > $CONF && chmod 600 $CONF
  cat > $CONF <<EOF
{"url": "$1", "token": "$2", "kid_user": "$KID", "key": "$FKEY", "port": 8765, "volume": 0.85}
EOF
fi
[ -f $CONF ] || { echo "missing $CONF: run ./install.sh <url> <token>"; exit 1; }
chmod 600 $CONF

# GNOME extension, enabled and locked for every user (the child cannot switch it off).
rm -rf /usr/share/gnome-shell/extensions/kids-talk@kids-talk.app
cp -r gnome-ext/kids-talk@kids-talk.app /usr/share/gnome-shell/extensions/
chmod -R a+rX /usr/share/gnome-shell/extensions/kids-talk@kids-talk.app
[ -f /etc/dconf/profile/user ] || printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user
install -d /etc/dconf/db/local.d/locks
cat > /etc/dconf/db/local.d/50-kids-talk <<'EOF'
[org/gnome/shell]
enabled-extensions=['ding@rastersoft.com', 'ubuntu-dock@ubuntu.com', 'tiling-assistant@ubuntu.com', 'kids-talk@kids-talk.app']
disabled-extensions=@as []
disable-user-extensions=false
EOF
cat > /etc/dconf/db/local.d/locks/50-kids-talk <<'EOF'
/org/gnome/shell/enabled-extensions
/org/gnome/shell/disabled-extensions
/org/gnome/shell/disable-user-extensions
EOF
chmod 644 /etc/dconf/profile/user /etc/dconf/db/local.d/50-kids-talk /etc/dconf/db/local.d/locks/50-kids-talk
dconf update
chmod 644 /etc/dconf/db/local

cat > /usr/share/applications/kids-talk.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Gọi điện - Nhắn tin con
Comment=Tin nhắn và việc cần làm từ bố
Exec=google-chrome --ozone-platform=wayland --app=http://127.0.0.1:8765/
Icon=/opt/kids-talk/icon.png
Categories=Utility;
StartupWMClass=127.0.0.1__
EOF

cat > /etc/systemd/system/kids-talk.service <<EOF
[Unit]
Description=Gọi điện - Nhắn tin con agent (parent messages, tasks, activity)
After=network-online.target systemd-logind.service
Wants=network-online.target

[Service]
ExecStart=$APP/venv/bin/python3 $APP/kids_talk_agent.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now kids-talk.service
systemctl restart kids-talk.service
sleep 2
systemctl --no-pager -l status kids-talk.service | head -15
