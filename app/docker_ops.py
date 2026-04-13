from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

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
    vnc_pass: str,
    image: str,
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
        "-e",
        f"VNC_PASS={vnc_pass}",
        "--label",
        POOL_LABEL,
        image,
    ]
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
    vnc_port: int
    cdp_port: int


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


def inspect_instance(name: str) -> PoolInstance | None:
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
    labels = (c.get("Config") or {}).get("Labels") or {}
    if labels.get("chrome-pool.managed") != "1":
        return None
    bindings = (c.get("HostConfig") or {}).get("PortBindings") or {}
    vnc = _host_port(bindings.get("5900/tcp"))
    cdp = _host_port(bindings.get("9222/tcp"))
    if vnc is None or cdp is None:
        return None
    return PoolInstance(name=name, vnc_port=vnc, cdp_port=cdp)


def list_pool_instances() -> list[PoolInstance]:
    names = list_pool_container_names()
    out: list[PoolInstance] = []
    for n in names:
        inst = inspect_instance(n)
        if inst:
            out.append(inst)
    return out


def stop_all_pool_containers() -> tuple[list[str], list[tuple[str, str]]]:
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
