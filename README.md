# Chrome browser pool manager

HTTP API to create, stop, and list Docker-based Chrome instances (VNC + CDP). Default image: `proxy-chrome:latest` (build locally from `proxy-chromium-docker`; see below). You can set `CHROME_DOCKER_IMAGE` to `suyash5053/chromium-vnc-cdp` if you do not use the proxy image.

API specification (Korean): [docs/API.md](docs/API.md). With the server running, OpenAPI JSON is at `/openapi.json` and Swagger UI at `/docs`.

## Requirements

- Python 3.11+
- **Linux**: Docker CLI on `PATH` with a running daemon (e.g. Docker Desktop)
- **Windows Server**: Google Chrome installed; set **`CHROME_EXE_PATH`** in `.env` (native CDP mode — no Docker)
- The API process and Chrome instances must run on the **same host** so post-`/start` CDP readiness checks against `127.0.0.1:<cdp_port>` are valid.

## Install

```bash
cd worker-pool
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If the network is slow, use `pip install -r requirements.txt --default-timeout=300`.

## Proxy Chrome image (optional)

The pool’s default image is `proxy-chrome:latest`, built from [`proxy-chromium-docker/dockerfile`](proxy-chromium-docker/dockerfile). Build context must be the whole `proxy-chromium-docker` directory (it copies `config/` and `proxyext/` into the image).

### Build / rebuild

From the `worker-pool` repo root:

```bash
docker build -t proxy-chrome:latest -f proxy-chromium-docker/dockerfile proxy-chromium-docker
```

Rebuild after changing the Dockerfile, anything under `proxy-chromium-docker/config/`, or `proxy-chromium-docker/proxyext/`. On Windows hosts, the Dockerfile runs `chmod +x /config/*.sh` so supervisord can execute the startup scripts (execute bits from `COPY` are not reliable cross‑platform).

### What’s in the image

- **Base**: Alpine edge; **process init**: `tini`; **supervisor**: `supervisord` runs Xvfb, Openbox, x11vnc, websockify, and Chromium.
- **Packages**: Chromium, Xvfb, x11vnc, Openbox, websockify, and libraries needed for headful Chromium (GTK, Mesa, fonts, etc.).
- **Defaults** (overridable at `docker run`): `DISPLAY=:0`, `VNC_WIDTH` / `VNC_HEIGHT`, `VNC_PASS`, `START_URL` (see the Dockerfile `ENV` lines).

At container start, if `PROXY_HOST` is set, the image applies `PROXY_*` to the extension; otherwise Chromium runs **without** the proxy extension. The API passes `PROXY_*` only when `proxies.csv` (see `PROXIES_CSV`) has at least one valid row; if the file is missing or empty, `/start` runs the same image **without** those environment variables.

## Proxy Google Chrome image (Ubuntu, optional)

For **Google Chrome** (not Chromium) on **Ubuntu 22.04**, build from [`proxy-chrome-docker/dockerfile`](proxy-chrome-docker/dockerfile). The existing Alpine + Chromium image above is unchanged.

### Build / rebuild

From the `worker-pool` repo root:

```bash
docker build -t proxy-google-chrome:latest -f proxy-chrome-docker/dockerfile proxy-chrome-docker
```

Use it by setting `CHROME_DOCKER_IMAGE=proxy-google-chrome:latest` in `.env`. Same ports (5900 VNC, 9222 CDP, 8080 noVNC), `VNC_PASS`, and `PROXY_*` contract as the Chromium image. **amd64 only** (Google Chrome .deb).

When `PROXY_HOST` is set, Chrome uses `--proxy-server` to a local **sing-box mixed inbound** (`127.0.0.1:7890`), which forwards to the HTTP upstream (`PROXY_*` env vars). No TUN / `NET_ADMIN` required.

### What’s in the image

- **Base**: Ubuntu 22.04; **browser**: `google-chrome-stable` from Google apt repo.
- **Proxy**: [sing-box](https://github.com/SagerNet/sing-box) mixed inbound on `127.0.0.1:7890` → HTTP outbound (`PROXY_*` env vars).
- **Process init**: `tini`; **supervisor**: `supervisord` runs Xvfb, Openbox, x11vnc, websockify (with noVNC + self-signed cert), sing-box (when proxied), and Chrome.
- Includes `PORT=8080` for websockify and `--user-data-dir=/tmp/chrome-user-data` for Chrome in Docker.

## Run

From the `worker-pool` directory (after activating the venv). Listen port is read from **`API_PORT`** in `.env` (default `8080`):

```bash
python -m app.main
```

You can still run Uvicorn directly; then set `--port` yourself (it does not read `API_PORT`):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Windows Server (native CDP)

On **Windows**, the pool runs **`chrome.exe` directly** — no Docker, VNC, or noVNC. Only **CDP ports** are allocated (`9223`, `9224`, …). Linux deployments continue to use Docker unchanged.

### Setup

1. Install Python 3.11+ and Google Chrome.
2. Copy `.env.sample` to `.env` and set at minimum:

```env
API_PORT=8080
CHROME_EXE_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
CHROME_HEADLESS=true
```

3. Install dependencies and run:

```powershell
cd worker-pool
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app.main
```

4. Chrome binds CDP to `127.0.0.0` is **not** used (many browsers reject it). Instead the pool runs an in-process **TCP relay**:

```text
0.0.0.0:<cdp_port>  →  127.0.0.1:<cdp_port>   (Python relay, no Administrator required)
```

5. Allow inbound TCP on CDP ports (**9223+**) in Windows Firewall if remote clients connect via `cdpHost`.

### Windows `/start` response

VNC-related fields are `null`:

```json
{
  "name": "chrome-pool-abc123",
  "vnc_port": null,
  "cdp_port": 9223,
  "novnc_port": null,
  "vnc_password": null,
  "novnc_url": null,
  "proxy_index": null,
  "proxy_region": null
}
```

Proxy modes (`AUTO`, `NONE`, `USER`) work the same as Linux; the pool loads the MV3 proxy extension from `proxy-chromium-docker/proxyext/`.

Chrome listens on **`127.0.0.1:<cdp_port>`** for CDP. Remote access uses an in-process **TCP relay** (`0.0.0.0:<cdp_port>` → `127.0.0.1:<cdp_port>`), stopped automatically on `/stop` and `/stopall`.

Instance state is stored in `%LOCALAPPDATA%\chrome-pool\instances.json`. Chrome profiles live under `%TEMP%\chrome-pool\<name>\` (override with `CHROME_USER_DATA_ROOT`).

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `API_PORT` | TCP port for FastAPI when using `python -m app.main` | `8080` |
| `VNC_PASS` | VNC password (Docker/Linux only) | `mystakechrome` |
| `CHROME_DOCKER_IMAGE` | Docker image to run (Linux only) | `proxy-chrome:latest` |
| `CHROME_EXE_PATH` | Path to `chrome.exe` (**required on Windows**) | (unset) |
| `CHROME_HEADLESS` | Run Chrome headless on Windows | `true` |
| `CHROME_USER_DATA_ROOT` | Profile root directory on Windows | `%TEMP%\chrome-pool` |
| `PROXIES_CSV` | Path to CSV (`Region,Host,Port,User,Pass`). Missing or no valid rows: no `PROXY_*` on `docker run`. | `proxies.csv` |
| `START_CDP_TIMEOUT_SEC` | Seconds to wait for CDP after start | `60` |
| `API_KEY` | If set, requires `Authorization: Bearer <key>` | (unset) |
| `MAX_RUNNING` | Max number of running pool containers. If unset, auto-computed from RAM (`floor(totalGiB * 0.85)`, min 1). | (auto) |
| `NOVNC_DOMAIN` | If set (e.g. `example.com`), `/start` includes `novnc_url`: `https://web-<novnc_port>.<domain>/vnc.html?autoconnect=1&password=…` | (unset) |

You can also set these in a `.env` file.

## Reverse proxy with nginx (`nginx.conf.sample`)

[`nginx.conf.sample`](nginx.conf.sample) is a **reference** layout for running the pool API and noVNC behind nginx on the same machine as Docker and Uvicorn. It is not a complete production `nginx.conf` by itself (adjust `include` paths and TLS to match your OS layout).

### What it does

1. **Pool HTTP API** — `server { server_name docker1.ultrasportsbot.com; ... }` proxies `/` to `http://127.0.0.1:8888`. Point that port at whatever you use for **`API_PORT`** (the sample uses `8888`; change it to match `.env`).
2. **noVNC by hostname** — A `map $host $backend_port` parses subdomains `web-<port>.<domain>` for **6080–6200** and proxies to `http://127.0.0.1:$backend_port`. That matches how **`NOVNC_DOMAIN`** builds `novnc_url` in `/start` responses (`web-<novnc_port>.<NOVNC_DOMAIN>`). Hostnames outside that pattern get **403**.

### How to use it

1. Copy the `http { ... }` blocks you need into your real nginx config (e.g. main `nginx.conf` under `http { }`, or a snippet under `conf.d/` included from `http`).
2. Replace placeholder domains:
   - `ultrasportsbot.com` → the same domain you set in **`NOVNC_DOMAIN`** (so returned `novnc_url` hosts resolve through this nginx).
   - `docker1.ultrasportsbot.com` → the hostname you want for the FastAPI pool (DNS must point at this nginx).
3. Set `proxy_pass http://127.0.0.1:<API_PORT>;` in the API `server` block so it matches **`API_PORT`** in `.env`.
4. Keep the **`map` regex** and the **second `server_name` regex** aligned with each other and with the port range your pool actually uses (default pool noVNC starts at **6080**; the sample allows **6080–6200**).
5. Run `nginx -t` and reload nginx (`nginx -s reload` or your service manager).

The sample listens on **port 80** only. In production, add **TLS** (`listen 443 ssl`, certificates) or terminate TLS in front of nginx. WebSockets are enabled for noVNC (`Upgrade` / `Connection` headers, long timeouts).

## API

If `API_KEY` is set, every endpoint except `/health` requires:

```http
Authorization: Bearer <API_KEY>
```

### `GET /health`

Process liveness and runtime availability check.

**Auth**: not required

- **Linux (Docker)**: checks Docker CLI availability.
- **Windows (native)**: checks that `CHROME_EXE_PATH` exists; `docker` is always `false`.

```json
{
  "ok": true,
  "docker": true,
  "docker_error": null,
  "runtime": "docker"
}
```

Windows example:

```json
{
  "ok": true,
  "docker": false,
  "docker_error": null,
  "runtime": "native",
  "chrome_exe": true,
  "chrome_exe_error": null
}
```

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
  "novnc_url": "https://web-6080.example.com/vnc.html?autoconnect=1&password=mystakechrome",
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
