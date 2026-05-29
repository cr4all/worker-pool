#!/bin/bash

CHROME_COMMON=(
  --no-sandbox
  --disable-sync
  --disable-popup-blocking
  --disable-dev-shm-usage
  --disable-gpu
  --start-maximized
  --force-device-scale-factor=1
  --user-data-dir=/tmp/chrome-user-data
  --no-first-run
  --no-default-browser-check
  --remote-debugging-port=9223
  --remote-debugging-address=127.0.0.1

  # stability for VNC/headful environments
  --disable-features=VizDisplayCompositor
  --disable-background-timer-throttling
  --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows
)

setup_chrome_profile() {
  local prefs_dir="/tmp/chrome-user-data/Default"
  mkdir -p "$prefs_dir"
  rm -f /tmp/chrome-user-data/"First Run" 2>/dev/null || true

  if [ ! -f "$prefs_dir/Preferences" ]; then
    cat > "$prefs_dir/Preferences" <<'EOF'
{
  "browser": {
    "check_default_browser": false
  },
  "distribution": {
    "import_bookmarks": false,
    "import_history": false,
    "import_search_engine": false,
    "make_chrome_default_for_user": false,
    "skip_first_run_ui": true
  }
}
EOF
  fi
}

wait_for_tun() {
  for _ in $(seq 1 60); do
    if ip link show tun0 >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "ERROR: tun0 did not come up; check sing-box logs" >&2
  return 1
}

setup_chrome_profile

if [ -n "${PROXY_HOST:-}" ]; then
  wait_for_tun
fi

google-chrome-stable "${CHROME_COMMON[@]}" "$START_URL" &

sleep 2

socat TCP-LISTEN:9222,reuseaddr,fork TCP:127.0.0.1:9223
