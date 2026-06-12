from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from app.proxy_csv import ProxyRow

POOL_LABEL = "chrome-pool.managed=1"
CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


def validate_container_name(name: str) -> bool:
    return bool(CONTAINER_NAME_RE.match(name))


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


class DockerError(Exception):
    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        self.exit_code = exit_code


def docker_version_ok() -> tuple[bool, str | None]:
    p = _docker(["version", "--format", "{{.Client.Version}}"])
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip() or "docker command failed"
        return False, err
    return True, None


def run_chrome_pool_container(
    name: str,
    host_vnc: int,
    host_cdp: int,
    host_novnc: int,
    vnc_pass: str,
    image: str,
    proxy: ProxyRow | None = None,
    proxy_index: int | None = None,
    owner: str | None = None,
) -> None:
    args = [
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"{host_vnc}:5900",
        "-p",
        f"{host_cdp}:9222",
        "-p",
        f"{host_novnc}:8080",
        "-e",
        f"VNC_PASS={vnc_pass}",
        "--label",
        POOL_LABEL,
    ]
    if owner:
        args.extend(["--label", f"chrome-pool.owner={owner}"])
    if proxy is not None:
        args.extend(
            [
                "-e",
                f"PROXY_HOST={proxy.host}",
                "-e",
                f"PROXY_PORT={proxy.port}",
                "-e",
                f"PROXY_USER={proxy.user}",
                "-e",
                f"PROXY_PASS={proxy.password}",
            ]
        )
        if proxy_index is not None:
            args.extend(
                [
                    "--label",
                    f"chrome-pool.proxy-index={proxy_index}",
                    "--label",
                    f"chrome-pool.proxy-region={proxy.region}",
                ]
            )
        else:
            args.extend(
                [
                    "--label",
                    "chrome-pool.proxy-source=user",
                    "--label",
                    f"chrome-pool.proxy-region={proxy.region}",
                ]
            )
    args.append(image)
    p = _docker(args)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip() or "docker run failed"
        raise DockerError(msg, p.returncode)


def remove_container(name_or_id: str) -> None:
    p = _docker(["rm", "-f", name_or_id])
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip() or "docker rm failed"
        raise DockerError(msg, p.returncode)


def container_exists(name_or_id: str) -> bool:
    p = _docker(["inspect", name_or_id])
    return p.returncode == 0


def list_pool_container_names() -> list[str]:
    p = _docker(
        [
            "ps",
            "--filter",
            f"label={POOL_LABEL}",
            "--format",
            "{{.Names}}",
        ]
    )
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip() or "docker ps failed"
        raise DockerError(msg, p.returncode)
    lines = (p.stdout or "").strip().splitlines()
    return [ln.strip() for ln in lines if ln.strip()]


@dataclass
class PoolInstance:
    name: str
    vnc_port: int | None
    cdp_port: int | None
    novnc_port: int | None = None
    proxy_index: int | None = None
    proxy_region: str | None = None
    owner: str | None = None


def _host_port(binding: list[dict[str, str]] | None) -> int | None:
    if not binding:
        return None
    hp = binding[0].get("HostPort")
    if not hp:
        return None
    try:
        return int(hp)
    except ValueError:
        return None


def _is_managed_pool_container(inspected: dict[str, Any]) -> bool:
    labels = (inspected.get("Config") or {}).get("Labels") or {}
    return labels.get("chrome-pool.managed") == "1"


def _owner_from_labels(labels: dict[str, Any]) -> str | None:
    raw = labels.get("chrome-pool.owner")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _proxy_meta_from_labels(labels: dict[str, Any]) -> tuple[int | None, str | None]:
    idx_raw = labels.get("chrome-pool.proxy-index")
    region_raw = labels.get("chrome-pool.proxy-region")
    region = region_raw if isinstance(region_raw, str) and region_raw else None
    if idx_raw is None or idx_raw == "":
        return None, region
    try:
        return int(str(idx_raw)), region
    except ValueError:
        return None, region


def _container_display_name(inspected: dict[str, Any]) -> str | None:
    # docker inspect returns Name like "/container-name"
    n = inspected.get("Name")
    if isinstance(n, str) and n.startswith("/"):
        return n[1:]
    if isinstance(n, str) and n:
        return n
    return None


def _extract_ports(
    inspected: dict[str, Any],
) -> tuple[int | None, int | None, int | None]:
    # Prefer HostConfig.PortBindings, but fall back to NetworkSettings.Ports.
    bindings = (inspected.get("HostConfig") or {}).get("PortBindings") or {}
    vnc = _host_port(bindings.get("5900/tcp"))
    cdp = _host_port(bindings.get("9222/tcp"))
    novnc = _host_port(bindings.get("8080/tcp"))
    if vnc is not None and cdp is not None and novnc is not None:
        return vnc, cdp, novnc

    ports = (inspected.get("NetworkSettings") or {}).get("Ports") or {}
    vnc2 = _host_port(ports.get("5900/tcp"))
    cdp2 = _host_port(ports.get("9222/tcp"))
    novnc2 = _host_port(ports.get("8080/tcp"))
    return (
        (vnc if vnc is not None else vnc2),
        (cdp if cdp is not None else cdp2),
        (novnc if novnc is not None else novnc2),
    )


def inspect_instance(name: str) -> PoolInstance | None:
    """
    Inspect a single instance.

    Returns PoolInstance even if port bindings are not yet visible (ports may be None).
    """
    p = _docker(["inspect", name])
    if p.returncode != 0:
        return None
    try:
        data: list[dict[str, Any]] = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    c = data[0]
    if not _is_managed_pool_container(c):
        return None
    vnc, cdp, novnc = _extract_ports(c)
    display = _container_display_name(c) or name
    lbl = (c.get("Config") or {}).get("Labels") or {}
    lbl_dict = lbl if isinstance(lbl, dict) else {}
    pi, pr = _proxy_meta_from_labels(lbl_dict)
    owner = _owner_from_labels(lbl_dict)
    return PoolInstance(
        name=display,
        vnc_port=vnc,
        cdp_port=cdp,
        novnc_port=novnc,
        proxy_index=pi,
        proxy_region=pr,
        owner=owner,
    )


def _bulk_inspect(names: list[str]) -> list[dict[str, Any]]:
    if not names:
        return []
    p = _docker(["inspect", *names])
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip() or "docker inspect failed"
        raise DockerError(msg, p.returncode)
    try:
        data = json.loads(p.stdout or "[]")
    except json.JSONDecodeError as e:
        raise DockerError(f"docker inspect returned invalid JSON: {e}") from e
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def list_pool_instances(retries: int = 5, retry_delay_sec: float = 0.2) -> list[PoolInstance]:
    """
    List running pool containers.

    Under concurrent /start calls, Docker may briefly report a container without port bindings.
    This function retries a few times so /list returns a complete container list.
    If ports are still not available after retries, the instance is still returned with any of
    vnc_port/cdp_port/novnc_port possibly None.
    """
    names = list_pool_container_names()
    if not names:
        return []

    pending: dict[str, PoolInstance] = {}
    ready: dict[str, PoolInstance] = {}

    for attempt in range(max(1, retries)):
        inspected = _bulk_inspect(names)
        for c in inspected:
            if not _is_managed_pool_container(c):
                continue
            display = _container_display_name(c)
            if not display:
                continue
            vnc, cdp, novnc = _extract_ports(c)
            lbl = (c.get("Config") or {}).get("Labels") or {}
            lbl_dict = lbl if isinstance(lbl, dict) else {}
            pi, pr = _proxy_meta_from_labels(lbl_dict)
            owner = _owner_from_labels(lbl_dict)
            inst = PoolInstance(
                name=display,
                vnc_port=vnc,
                cdp_port=cdp,
                novnc_port=novnc,
                proxy_index=pi,
                proxy_region=pr,
                owner=owner,
            )
            if vnc is None or cdp is None or novnc is None:
                pending[display] = inst
            else:
                ready[display] = inst
                pending.pop(display, None)

        if not pending:
            break
        if attempt < retries - 1:
            time.sleep(retry_delay_sec)

    # Preserve a stable order: by name
    merged = {**pending, **ready}
    return [merged[k] for k in sorted(merged.keys())]


def stop_all_pool_containers(owner: str | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    if owner:
        instances = list_pool_instances()
        names = [i.name for i in instances if i.owner == owner]
    else:
        names = list_pool_container_names()
    stopped: list[str] = []
    errors: list[tuple[str, str]] = []
    for n in names:
        try:
            remove_container(n)
            stopped.append(n)
        except DockerError as e:
            errors.append((n, str(e)))
    return stopped, errors
