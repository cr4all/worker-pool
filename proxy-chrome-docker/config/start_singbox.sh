#!/bin/bash
set -euo pipefail

if [ -z "${PROXY_HOST:-}" ]; then
  exec sleep infinity
fi

: "${PROXY_PORT:?PROXY_PORT is required when PROXY_HOST is set}"

CONFIG_DIR=/tmp/sing-box
CONFIG_FILE="${CONFIG_DIR}/config.json"
mkdir -p "$CONFIG_DIR"

sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true

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
  --argjson proxy "$proxy_outbound" \
  --argjson proxy_direct_rule "$proxy_direct_rule" \
  '{
    log: { level: "info", timestamp: true },
    dns: {
      servers: [
        {
          tag: "local",
          address: "local",
          detour: "direct"
        }
      ],
      strategy: "prefer_ipv4"
    },
    inbounds: [
      {
        type: "tun",
        tag: "tun-in",
        interface_name: "tun0",
        address: ["172.19.0.1/30"],
        mtu: 1500,
        auto_route: true,
        strict_route: false,
        auto_redirect: false,
        route_address: ["0.0.0.0/1", "128.0.0.0/1"],
        route_exclude_address: ["172.19.0.0/30"],
        stack: "system",
        sniff: true,
        sniff_override_destination: true
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
        {
          ip_is_private: true,
          outbound: "direct"
        },
        $proxy_direct_rule
      ],
      final: "proxy",
      auto_detect_interface: true
    }
  }' > "$CONFIG_FILE"

for _ in $(seq 1 30); do
  if [ -c /dev/net/tun ]; then
    break
  fi
  sleep 0.2
done

if [ ! -c /dev/net/tun ]; then
  echo "ERROR: /dev/net/tun is not available (container needs --device /dev/net/tun and NET_ADMIN)" >&2
  exit 1
fi

exec sing-box run -c "$CONFIG_FILE"
