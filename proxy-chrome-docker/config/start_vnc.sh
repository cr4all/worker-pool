#!/bin/bash
set -e

export DISPLAY=:0

# Ensure /config exists
mkdir -p /config
chmod 777 /config

# Update the noVNC title in index.html
if [ -f /opt/noVNC/index.html ]; then
  sed -i "s/\$DESKTOP/$VNC_TITLE/g" /opt/noVNC/index.html
else
  echo "Warning: /opt/noVNC/index.html not found!"
fi

# Wait a few seconds to ensure Xvfb is running
for i in {1..5}; do
  if xdpyinfo -display $DISPLAY >/dev/null 2>&1; then
    break
  fi
  echo "Waiting for Xvfb to start..."
  sleep 1
done

# Create VNC password if not exists
if [ ! -f /config/.xpass ]; then
  x11vnc -storepasswd "$VNC_PASS" /config/.xpass
fi

# Start x11vnc in the appropriate mode
if [ "$VNC_SHARED" = "false" ]; then
  x11vnc -usepw -rfbport 5900 -rfbauth /config/.xpass \
    -geometry "${VNC_WIDTH}x${VNC_HEIGHT}" \
    -forever -alwaysshared -permitfiletransfer \
    -noxrecord -noxfixes -noxdamage -dpms \
    -bg -desktop "$VNC_TITLE"
else
  x11vnc -usepw -rfbport 5900 -rfbauth /config/.xpass \
    -geometry "${VNC_WIDTH}x${VNC_HEIGHT}" \
    -forever -shared -alwaysshared -permitfiletransfer
fi

# Health check loop
while true; do
  if ! pgrep x11vnc >/dev/null; then
    echo "VNC process died! Restarting..."
    # Re-run the script itself to restart VNC safely
    exec "$0"
  fi
  sleep 5
done
