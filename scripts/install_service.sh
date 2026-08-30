#!/usr/bin/env bash
# Install vision_preflight.service. Idempotent - safe to re-run after any change
# to the unit file.
#
# Run as your normal user, not root. It uses sudo where it needs to, which keeps
# the files it creates owned by you.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="vision_preflight.service"
EXPECTED="$HOME/vision/camera"

echo "camera repo: $REPO"

# The unit file has an absolute path in WorkingDirectory and ExecStart. Cloning
# somewhere else silently produces a service that cannot start, so say so now
# rather than at the next boot.
if [ "$REPO" != "$EXPECTED" ]; then
  echo
  echo "WARNING: this repo is at $REPO but $UNIT expects $EXPECTED."
  echo "         Either move it, or edit services/$UNIT before continuing."
  echo
fi

for tool in v4l2-ctl python3; do
  command -v "$tool" >/dev/null || {
    echo "missing $tool - sudo apt install -y v4l-utils python3-yaml"; exit 1; }
done
python3 -c 'import yaml' 2>/dev/null || {
  echo "missing PyYAML - sudo apt install -y python3-yaml"; exit 1; }

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "no venv - publishing needs pyntcore:"
  echo "  python3 -m venv --system-site-packages .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# Authored in services/ and copied into place. Never edit units directly in
# /etc/systemd/system - the copy there is a build artefact, not the source.
sudo cp "$REPO/services/$UNIT" "/etc/systemd/system/$UNIT"
sudo systemctl daemon-reload

# enable AND start: they are different. A service that is started but never
# enabled works until the first power cycle and then silently does not.
sudo systemctl enable --now "$UNIT"

echo
systemctl --no-pager status "$UNIT" || true
echo
echo "done. verify it is publishing:"
echo "  $REPO/.venv/bin/python $REPO/src/vd.py nt | grep Vision"
