# Chrome browser pool manager

HTTP API to create, stop, and list Docker-based Chrome instances (VNC + CDP). Default image: `suyash5053/chromium-vnc-cdp`.

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

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `VNC_PASS` | VNC password | `mystakechrome` |
| `CHROME_DOCKER_IMAGE` | Image to run | `suyash5053/chromium-vnc-cdp` |
| `START_CDP_TIMEOUT_SEC` | Seconds to wait for CDP after start | `60` |
| `API_KEY` | If set, requires `Authorization: Bearer <key>` | (unset) |
| `MAX_RUNNING` | Max number of running pool containers. If unset, auto-computed from RAM (`floor(totalGiB * 0.85)`, min 1). | (auto) |

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
  "vnc_password": "mystakechrome"
}
```

VNC listens on `vnc_port`; Chrome DevTools Protocol is at `http://127.0.0.1:<cdp_port>`.

**Port rules**: The first instance uses VNC **5901** and CDP **9223**; each additional instance uses the next pair (**5902·9224**, **5903·9225**, …). Slots already used by pool containers or otherwise bound on the host are skipped.

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
    { "name": "chrome-pool-a", "vnc_port": 5901, "cdp_port": 9223 }
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

- Pool containers are marked with `--label chrome-pool.managed=1` at `docker run`.
- Host ports are chosen from **5901 / 9223** upward with the same offset; the next free slot uses the pool list plus local bind checks.
