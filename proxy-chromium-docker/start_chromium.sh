#!/bin/bash

CHROME_COMMON=(
  --no-sandbox --disable-sync --disable-popup-blocking --disable-dev-shm-usage
  --disable-gpu --start-maximized --force-device-scale-factor=1
  --remote-debugging-port=9223 --remote-debugging-address=127.0.0.1
)

if [ -n "${PROXY_HOST:-}" ]; then
  cp /proxyext/background.js.template /proxyext/background.js
  sed -i "s/__PROXY_HOST__/${PROXY_HOST}/g" /proxyext/background.js
  sed -i "s/__PROXY_PORT__/${PROXY_PORT}/g" /proxyext/background.js
  sed -i "s/__PROXY_USER__/${PROXY_USER}/g" /proxyext/background.js
  sed -i "s/__PROXY_PASS__/${PROXY_PASS}/g" /proxyext/background.js
  chromium --disable-extensions-except=/proxyext --load-extension=/proxyext "${CHROME_COMMON[@]}" $START_URL &
else
  chromium "${CHROME_COMMON[@]}" $START_URL &
fi

# Expose Chromium debugging
socat TCP-LISTEN:9222,FORK TCP:127.0.0.1:9223
