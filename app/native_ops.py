from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

from app.docker_ops import PoolInstance, validate_container_name
from app.proxy_csv import ProxyRow

_REGISTRY_DIR = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "chrome-pool"
_REGISTRY_FILE = _REGISTRY_DIR / "instances.json"
_DEFAULT_USER_DATA_ROOT = Path(os.environ.get("TEMP", os.path.expanduser("~"))) / "chrome-pool"
_DEFAULT_PROXY_EXT = (
    Path(__file__).resolve().parent.parent / "proxy-chromium-docker" / "proxyext"
)
_REGISTRY_LOCK = threading.Lock()
_RELAY_LOCK = threading.Lock()
_RELAYS: dict[str, threading.Event] = {}
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CDP_CONNECT_HOST = "127.0.0.1"


class NativeError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


@dataclass
class _RegistryEntry:
    pid: int
    cdp_port: int
    proxy_index: int | None = None
    proxy_region: str | None = None


def _default_user_data_root() -> Path:
    return _DEFAULT_USER_DATA_ROOT


def default_proxy_ext_dir() -> Path:
    return _DEFAULT_PROXY_EXT


def chrome_exe_ok(chrome_exe: str) -> tuple[bool, str | None]:
    p = Path(chrome_exe.strip())
    if not chrome_exe.strip():
        return False, "CHROME_EXE_PATH is not set"
    if not p.is_file():
        return False, f"Chrome executable not found: {p}"
    return True, None


def _read_registry() -> dict[str, _RegistryEntry]:
    if not _REGISTRY_FILE.is_file():
        return {}
    try:
        raw = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    instances = raw.get("instances") if isinstance(raw, dict) else None
    if not isinstance(instances, dict):
        return {}
    out: dict[str, _RegistryEntry] = {}
    for name, entry in instances.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        try:
            out[name] = _RegistryEntry(
                pid=int(entry["pid"]),
                cdp_port=int(entry["cdp_port"]),
                proxy_index=(
                    int(entry["proxy_index"])
                    if entry.get("proxy_index") is not None
                    else None
                ),
                proxy_region=(
                    str(entry["proxy_region"])
                    if entry.get("proxy_region") not in (None, "")
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _write_registry(data: dict[str, _RegistryEntry]) -> None:
    _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "instances": {
            name: {k: v for k, v in asdict(entry).items() if v is not None}
            for name, entry in data.items()
        }
    }
    _REGISTRY_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _pipe_bidirectional(client: socket.socket, remote: socket.socket) -> None:
    pair = [client, remote]
    try:
        while True:
            readable, _, exceptional = select.select(pair, [], pair, 60.0)
            if exceptional:
                break
            if not readable:
                break
            for sock in readable:
                other = remote if sock is client else client
                data = sock.recv(65536)
                if not data:
                    return
                other.sendall(data)
    finally:
        for sock in (client, remote):
            try:
                sock.close()
            except OSError:
                pass


def _relay_accept_loop(
    server: socket.socket,
    stop_event: threading.Event,
    relay_name: str,
    listen_port: int,
) -> None:
    server.settimeout(1.0)
    try:
        while not stop_event.is_set():
            try:
                client, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                remote = socket.create_connection((_CDP_CONNECT_HOST, listen_port), timeout=5.0)
            except OSError:
                client.close()
                continue
            threading.Thread(
                target=_pipe_bidirectional,
                args=(client, remote),
                daemon=True,
            ).start()
    finally:
        try:
            server.close()
        except OSError:
            pass


def _start_cdp_relay(name: str, port: int) -> None:
    _stop_cdp_relay(name)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(64)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_relay_accept_loop,
        args=(server, stop_event, name, port),
        name=f"cdp-relay-{name}",
        daemon=True,
    )
    thread.start()
    with _RELAY_LOCK:
        _RELAYS[name] = stop_event


def _stop_cdp_relay(name: str) -> None:
    with _RELAY_LOCK:
        stop_event = _RELAYS.pop(name, None)
    if stop_event is not None:
        stop_event.set()


def _kill_pid_tree(pid: int) -> None:
    if pid <= 0:
        return
    subprocess.run(
        ["taskkill", "/T", "/F", "/PID", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )


def _cleanup_stale_entries(data: dict[str, _RegistryEntry]) -> dict[str, _RegistryEntry]:
    alive: dict[str, _RegistryEntry] = {}
    for name, entry in data.items():
        if _pid_alive(entry.pid):
            alive[name] = entry
        else:
            _stop_cdp_relay(name)
    if alive != data:
        _write_registry(alive)
    return alive


def _js_literal(value: str) -> str:
    return json.dumps(value)


def _prepare_proxy_extension(
    proxy_ext_src: Path,
    dest_dir: Path,
    proxy: ProxyRow,
) -> Path:
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    shutil.copytree(proxy_ext_src, dest_dir)

    template = (proxy_ext_src / "background.js.template").read_text(encoding="utf-8")
    background = (
        template.replace("__PROXY_HOST__", _js_literal(proxy.host)[1:-1])
        .replace("__PROXY_PORT__", str(proxy.port))
        .replace("__PROXY_USER__", _js_literal(proxy.user)[1:-1])
        .replace("__PROXY_PASS__", _js_literal(proxy.password)[1:-1])
    )
    (dest_dir / "background.js").write_text(background, encoding="utf-8")
    return dest_dir


def _instance_user_data_dir(user_data_root: Path, name: str) -> Path:
    return user_data_root / name


def _chrome_args(
    *,
    cdp_port: int,
    user_data_dir: Path,
    headless: bool,
    proxy_ext_dir: Path | None,
    start_url: str | None,
) -> list[str]:
    args = [
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        # "--disable-features=DisableLoadExtensionCommandLineSwitch"
        # "--disable-sync",
        # "--disable-popup-blocking",
        # "--disable-dev-shm-usage",
        # "--disable-background-timer-throttling",
        # "--disable-renderer-backgrounding",
        # "--disable-backgrounding-occluded-windows",
    ]
    if headless:
        args.extend(["--headless=new", "--disable-gpu"])
    if proxy_ext_dir is not None:
        ext = str(proxy_ext_dir)
        args.extend([f"--load-extension={ext}"])
    if start_url:
        args.append(start_url)
    return args


def start_native_instance(
    *,
    name: str,
    cdp_port: int,
    chrome_exe: str,
    headless: bool,
    user_data_root: Path | None = None,
    proxy_ext_src: Path | None = None,
    proxy: ProxyRow | None = None,
    proxy_index: int | None = None,
    start_url: str | None = "https://www.google.com/",
) -> None:
    if not validate_container_name(name):
        raise NativeError(f"Invalid instance name: {name}")

    ok, err = chrome_exe_ok(chrome_exe)
    if not ok:
        raise NativeError(err or "Invalid CHROME_EXE_PATH")

    root = user_data_root or _default_user_data_root()
    user_data_dir = _instance_user_data_dir(root, name)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    proxy_ext_dir: Path | None = None
    if proxy is not None:
        src = proxy_ext_src or default_proxy_ext_dir()
        if not src.is_dir():
            raise NativeError(f"Proxy extension directory not found: {src}")
        proxy_ext_dir = _prepare_proxy_extension(
            src,
            user_data_dir / "proxyext",
            proxy,
        )

    args = [chrome_exe, *_chrome_args(
        cdp_port=cdp_port,
        user_data_dir=user_data_dir,
        headless=headless,
        proxy_ext_dir=proxy_ext_dir,
        start_url=start_url,
    )]

    creationflags = _CREATE_NO_WINDOW if headless else 0
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as e:
        raise NativeError(f"Failed to start Chrome: {e}") from e

    try:
        _start_cdp_relay(name, cdp_port)
    except OSError as e:
        _kill_pid_tree(proc.pid)
        raise NativeError(f"CDP port forward failed on 0.0.0.0:{cdp_port}: {e}") from e

    with _REGISTRY_LOCK:
        data = _cleanup_stale_entries(_read_registry())
        if name in data:
            _stop_cdp_relay(name)
            _kill_pid_tree(proc.pid)
            raise NativeError(f"Instance already exists: {name}")
        data[name] = _RegistryEntry(
            pid=proc.pid,
            cdp_port=cdp_port,
            proxy_index=proxy_index,
            proxy_region=(proxy.region if proxy else None),
        )
        _write_registry(data)


def instance_exists(name: str) -> bool:
    with _REGISTRY_LOCK:
        data = _cleanup_stale_entries(_read_registry())
        return name in data


def list_pool_instance_names() -> list[str]:
    with _REGISTRY_LOCK:
        data = _cleanup_stale_entries(_read_registry())
        return sorted(data.keys())


def inspect_instance(name: str) -> PoolInstance | None:
    with _REGISTRY_LOCK:
        data = _cleanup_stale_entries(_read_registry())
        entry = data.get(name)
        if entry is None:
            return None
        return PoolInstance(
            name=name,
            vnc_port=None,
            cdp_port=entry.cdp_port,
            novnc_port=None,
            proxy_index=entry.proxy_index,
            proxy_region=entry.proxy_region,
        )


def list_pool_instances() -> list[PoolInstance]:
    with _REGISTRY_LOCK:
        data = _cleanup_stale_entries(_read_registry())
        return [
            PoolInstance(
                name=name,
                vnc_port=None,
                cdp_port=entry.cdp_port,
                novnc_port=None,
                proxy_index=entry.proxy_index,
                proxy_region=entry.proxy_region,
            )
            for name, entry in sorted(data.items())
        ]


def remove_instance(name: str) -> None:
    with _REGISTRY_LOCK:
        data = _cleanup_stale_entries(_read_registry())
        entry = data.pop(name, None)
        if entry is None:
            raise NativeError(f"Instance not found: {name}")
        _write_registry(data)

    _kill_pid_tree(entry.pid)
    _stop_cdp_relay(name)


def stop_all_pool_instances() -> tuple[list[str], list[tuple[str, str]]]:
    with _REGISTRY_LOCK:
        data = _cleanup_stale_entries(_read_registry())
        names = sorted(data.keys())

    stopped: list[str] = []
    errors: list[tuple[str, str]] = []
    for name in names:
        try:
            remove_instance(name)
            stopped.append(name)
        except NativeError as e:
            errors.append((name, str(e)))
    return stopped, errors
