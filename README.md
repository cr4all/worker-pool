# Chrome browser pool manager

HTTP API to create, stop, and list Docker-based Chrome instances (VNC + CDP). Default image: `proxy-chrome:latest` (build locally from `proxy-chromium-docker`; see below). You can set `CHROME_DOCKER_IMAGE` to `suyash5053/chromium-vnc-cdp` if you do not use the proxy image.

API specification (Korean): [docs/API.md](docs/API.md). With the server running, OpenAPI JSON is at `/openapi.json` and Swagger UI at `/docs`.

## Requirements

- Python 3.11+
- Docker CLI on `PATH` with a running daemon (e.g. Docker Desktop)
- The API process and containers must run on the **same host** so post-`/start` CDP readiness checks against `127.0.0.1:<cdp_port>` are valid.

## Install

```bash
cd worker-pool
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If the network is slow, use `pip install -r requirements.txt --default-timeout=300`.

## Proxy Chrome image (optional)

To use CSV-based proxies and MV3 auth proxy inside the browser, build the child image once:

```bash
docker build -t proxy-chrome:latest -f proxy-chromium-docker/dockerfile proxy-chromium-docker
```

At container start, if `PROXY_HOST` is set, the image applies `PROXY_*` to the extension; otherwise Chromium runs **without** the proxy extension. The API passes `PROXY_*` only when `proxies.csv` (see `PROXIES_CSV`) has at least one valid row; if the file is missing or empty, `/start` runs the same image **without** those environment variables.

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `VNC_PASS` | VNC password | `mystakechrome` |
| `CHROME_DOCKER_IMAGE` | Image to run | `proxy-chrome:latest` |
| `PROXIES_CSV` | Path to CSV (`Region,Host,Port,User,Pass`). Missing or no valid rows: no `PROXY_*` on `docker run`. | `proxies.csv` |
| `START_CDP_TIMEOUT_SEC` | Seconds to wait for CDP after start | `60` |
| `API_KEY` | If set, requires `Authorization: Bearer <key>` | (unset) |
| `MAX_RUNNING` | Max number of running pool containers. If unset, auto-computed from RAM (`floor(totalGiB * 0.85)`, min 1). | (auto) |
| `NOVNC_DOMAIN` | If set (e.g. `example.com`), `/start` includes `novnc_url`: `https://web-<novnc_port>.<domain>/index.html?password=…` | (unset) |

You can also set these in a `.env` file.

## API

If `API_KEY` is set, every endpoint except `/health` requires:

```http
Authorization: Bearer <API_KEY>
```

### `GET /health`

Process liveness and Docker client availability.

### `POST /start`

Body (JSON, optional): `{ "name": "my-chrome" }` — omit to auto-assign `chrome-pool-<random>`.

On success:

```json
{
  "name": "chrome-pool-abc123",
  "vnc_port": 5901,
  "cdp_port": 9223,
  "novnc_port": 6080,
  "vnc_password": "mystakechrome",
  "novnc_url": "https://web-6080.example.com/index.html?password=mystakechrome",
  "proxy_index": 0,
  "proxy_region": "UK"
}
```

If `NOVNC_DOMAIN` is not set, `novnc_url` is `null`.

When no proxy row was used, `proxy_index` and `proxy_region` are `null`.

VNC listens on `vnc_port`; Chrome DevTools Protocol is at `http://127.0.0.1:<cdp_port>`; noVNC web UI is at `http://127.0.0.1:<novnc_port>`.

**Proxies**: If `PROXIES_CSV` has valid rows, each `/start` picks a row with **minimum current use** among running pool containers (random tie-break) and passes `PROXY_HOST`, `PROXY_PORT`, `PROXY_USER`, `PROXY_PASS` to Docker.

**Port rules**: The first instance uses VNC **5901**, CDP **9223**, and noVNC **6080**; each additional instance uses the next triple (**5902·9224·6081**, **5903·9225·6082**, …). Slots already used by pool containers or otherwise bound on the host are skipped.

### `POST /stop`

```json
{ "name": "chrome-pool-abc123" }
```

Returns **400** if the container exists but is not managed by this pool (missing label).

### `POST /stopall`

Removes all running containers with label `chrome-pool.managed=1`.

```json
{
  "stopped": ["chrome-pool-a", "chrome-pool-b"],
  "errors": []
}
```

### `GET /list`

```json
{
  "instances": [
    { "name": "chrome-pool-a", "vnc_port": 5901, "cdp_port": 9223, "proxy_index": 0, "proxy_region": "UK" }
  ]
}
```

## curl examples

```bash
curl -s http://127.0.0.1:8080/health
curl -s -X POST http://127.0.0.1:8080/start -H "Content-Type: application/json" -d "{}"
curl -s http://127.0.0.1:8080/list
curl -s -X POST http://127.0.0.1:8080/stopall
```

## Implementation notes

- Pool containers are marked with `--label chrome-pool.managed=1` at `docker run`. When a proxy row is used, `chrome-pool.proxy-index` and `chrome-pool.proxy-region` are set (no credentials in labels).
- Host ports are chosen from **5901 / 9223** upward with the same offset; the next free slot uses the pool list plus local bind checks.
