#!/bin/bash

PROXY_EXT_DIR="/tmp/proxyext"

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
  --disable-features=VizDisplayCompositor,DisableLoadExtensionCommandLineSwitch
  --disable-background-timer-throttling
  --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows
)

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\\/&|]/\\&/g'
}

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

prepare_proxy_extension() {
  local host port user pass

  rm -rf "$PROXY_EXT_DIR"
  cp -a /proxyext/. "$PROXY_EXT_DIR/"
  cp "$PROXY_EXT_DIR/background.js.template" "$PROXY_EXT_DIR/background.js"

  host=$(escape_sed_replacement "$PROXY_HOST")
  port=$(escape_sed_replacement "$PROXY_PORT")
  user=$(escape_sed_replacement "$PROXY_USER")
  pass=$(escape_sed_replacement "$PROXY_PASS")

  sed -i "s|__PROXY_HOST__|${host}|g" "$PROXY_EXT_DIR/background.js"
  sed -i "s|__PROXY_PORT__|${port}|g" "$PROXY_EXT_DIR/background.js"
  sed -i "s|__PROXY_USER__|${user}|g" "$PROXY_EXT_DIR/background.js"
  sed -i "s|__PROXY_PASS__|${pass}|g" "$PROXY_EXT_DIR/background.js"
}

setup_chrome_profile

if [ -n "${PROXY_HOST:-}" ]; then
  prepare_proxy_extension

  google-chrome-stable \
    --disable-extensions-except="$PROXY_EXT_DIR" \
    --load-extension="$PROXY_EXT_DIR" \
    "${CHROME_COMMON[@]}" \
    "$START_URL" &
else
  google-chrome-stable "${CHROME_COMMON[@]}" "$START_URL" &
fi

sleep 2

socat TCP-LISTEN:9222,reuseaddr,fork TCP:127.0.0.1:9223
