#!/bin/bash
set -euo pipefail

if [ -z "${PROXY_HOST:-}" ]; then
  exec sleep infinity
fi

: "${PROXY_PORT:?PROXY_PORT is required when PROXY_HOST is set}"

PROXY_LOCAL_PORT="${PROXY_LOCAL_PORT:-7890}"

CONFIG_DIR=/tmp/sing-box
CONFIG_FILE="${CONFIG_DIR}/config.json"
mkdir -p "$CONFIG_DIR"

proxy_outbound="$(jq -n \
  --arg host "$PROXY_HOST" \
  --argjson port "$PROXY_PORT" \
  --arg user "${PROXY_USER:-}" \
  --arg pass "${PROXY_PASS:-}" \
  '{
    type: "http",
    tag: "proxy",
    server: $host,
    server_port: $port
  }
  + (if ($user | length) > 0 then {username: $user} else {} end)
  + (if ($pass | length) > 0 then {password: $pass} else {} end)')"

if [[ "$PROXY_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  proxy_direct_rule="$(jq -n --arg ip "$PROXY_HOST/32" '{ip_cidr: [$ip], outbound: "direct"}')"
else
  proxy_direct_rule="$(jq -n --arg host "$PROXY_HOST" '{domain: [$host], outbound: "direct"}')"
fi

jq -n \
  --argjson port "$PROXY_LOCAL_PORT" \
  --argjson proxy "$proxy_outbound" \
  --argjson proxy_direct_rule "$proxy_direct_rule" \
  '{
    log: { level: "info", timestamp: true },
    inbounds: [
      {
        type: "mixed",
        tag: "mixed-in",
        listen: "127.0.0.1",
        listen_port: $port
      }
    ],
    outbounds: [
      $proxy,
      { type: "direct", tag: "direct" }
    ],
    route: {
      rules: [
        {
          ip_cidr: ["127.0.0.0/8"],
          outbound: "direct"
        },
        $proxy_direct_rule
      ],
      final: "proxy",
      auto_detect_interface: true
    }
  }' > "$CONFIG_FILE"

exec sing-box run -c "$CONFIG_FILE"
