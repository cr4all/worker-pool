from __future__ import annotations

import asyncio
import math
import time
import uuid
from urllib.parse import quote
from enum import Enum
from typing import Annotated, Optional, Self

import httpx
import psutil
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import docker_ops
from app.docker_ops import DockerError, PoolInstance, validate_container_name
from app.ports import allocate_sequential_pool_ports
from app.proxy_csv import ProxyRow, load_proxies, pick_balanced_proxy_index


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    vnc_pass: str = "mystakechrome"
    chrome_docker_image: str = "proxy-chrome:latest"
    proxies_csv: str = "proxies.csv"
    start_cdp_timeout_sec: float = 60.0
    api_key: Optional[str] = None
    max_running: Optional[int] = None
    novnc_domain: Optional[str] = None


settings = Settings()


def get_settings() -> Settings:
    return settings


async def require_api_key(
    authorization: Annotated[Optional[str], Header()] = None,
    s: Settings = Depends(get_settings),
) -> None:
    if not s.api_key:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if token != s.api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")


app = FastAPI(title="Chrome pool manager", version="1.0.0")

# Serial port allocation + avoid races before docker run
_start_lock = asyncio.Lock()


def effective_max_running(s: Settings) -> int:
    """
    If MAX_RUNNING is set, use it. Otherwise compute:
      floor(total_ram_bytes * 0.85 / 1GiB), minimum 1.
    """
    if s.max_running is not None:
        if s.max_running < 1:
            return 1
        return int(s.max_running)
    total = int(psutil.virtual_memory().total)
    gib = 1024**3
    computed = int(math.floor((total * 0.85) / gib))
    return max(1, computed)


class ProxyMode(str, Enum):
    AUTO = "AUTO"
    NONE = "NONE"
    USER = "USER"


class UserProxyIn(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    user: str = ""
    password: str = ""
    region: Optional[str] = None

    @field_validator("host")
    @classmethod
    def strip_host(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("host must not be empty")
        return s

    @field_validator("region")
    @classmethod
    def strip_region(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        return s or None


class StartBody(BaseModel):
    name: Optional[str] = None
    vnc_password: Optional[str] = Field(default=None, max_length=128)
    proxy: ProxyMode = ProxyMode.AUTO
    user_proxy: Optional[UserProxyIn] = None

    @field_validator("vnc_password")
    @classmethod
    def vnc_password_strip_nonempty(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("vnc_password must not be empty when provided")
        return s

    @model_validator(mode="after")
    def user_proxy_matches_mode(self) -> Self:
        if self.proxy == ProxyMode.USER:
            if self.user_proxy is None:
                raise ValueError("user_proxy is required when proxy is USER")
        elif self.user_proxy is not None:
            raise ValueError("user_proxy is only allowed when proxy is USER")
        return self


class StartResponse(BaseModel):
    name: str
    vnc_port: int
    cdp_port: int
    novnc_port: int
    vnc_password: str
    novnc_url: Optional[str] = None
    proxy_index: Optional[int] = None
    proxy_region: Optional[str] = None


class ErrorResponse(BaseModel):
    error: str


class StopBody(BaseModel):
    name: str


class StopResponse(BaseModel):
    ok: bool
    name: str


class InstanceOut(BaseModel):
    name: str
    vnc_port: Optional[int]
    cdp_port: Optional[int]
    novnc_port: Optional[int] = None
    proxy_index: Optional[int] = None
    proxy_region: Optional[str] = None


class ListResponse(BaseModel):
    instances: list[InstanceOut]


class StopAllError(BaseModel):
    name: str
    error: str


class StopAllResponse(BaseModel):
    stopped: list[str]
    errors: list[StopAllError]


class HealthResponse(BaseModel):
    ok: bool
    docker: bool
    docker_error: Optional[str] = None


def _proxy_usage_counts(instances: list[PoolInstance], num_proxies: int) -> list[int]:
    counts = [0] * num_proxies
    for inst in instances:
        if inst.proxy_index is not None and 0 <= inst.proxy_index < num_proxies:
            counts[inst.proxy_index] += 1
    return counts


def _novnc_public_url(domain: str, novnc_host_port: int, password: str) -> str:
    """https://web-<hostPort>.<domain>/index.html?password=<url-encoded password>"""
    d = domain.strip().strip("/")
    host = f"web-{novnc_host_port}.{d}"
    return f"https://{host}/index.html?password={quote(password, safe='')}"


async def _wait_cdp_ready(port: int, timeout_sec: float) -> None:
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout_sec
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(url, timeout=2.0)
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"CDP not ready on port {port} within {timeout_sec}s")
            await asyncio.sleep(0.5)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    ok, err = docker_ops.docker_version_ok()
    return HealthResponse(ok=True, docker=ok, docker_error=err)


@app.post(
    "/start",
    response_model=StartResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    dependencies=[Depends(require_api_key)],
)
async def start_pool(
    body: StartBody,
    s: Settings = Depends(get_settings),
) -> StartResponse | ErrorResponse:
    if body.name is not None:
        name = body.name.strip()
        if not name or not validate_container_name(name):
            raise HTTPException(status_code=400, detail="Invalid container name")
    else:
        name = f"chrome-pool-{uuid.uuid4().hex[:12]}"

    if docker_ops.container_exists(name):
        raise HTTPException(status_code=409, detail=f"Container already exists: {name}")

    async with _start_lock:
        max_allowed = effective_max_running(s)
        try:
            current_names = docker_ops.list_pool_container_names()
        except DockerError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        current = len(current_names)
        if current >= max_allowed:
            raise HTTPException(
                status_code=429,
                detail={"error": "limit reached", "current": current, "max": max_allowed},
            )
        try:
            instances = docker_ops.list_pool_instances()
        except DockerError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        used: set[int] = set()
        for inst in instances:
            if inst.vnc_port is not None:
                used.add(inst.vnc_port)
            if inst.cdp_port is not None:
                used.add(inst.cdp_port)
            if inst.novnc_port is not None:
                used.add(inst.novnc_port)
        try:
            vnc_p, cdp_p, novnc_p = allocate_sequential_pool_ports(used)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        vnc_pass_effective = body.vnc_password if body.vnc_password is not None else s.vnc_pass
        proxy_row: ProxyRow | None = None
        proxy_idx: Optional[int] = None
        if body.proxy == ProxyMode.NONE:
            pass
        elif body.proxy == ProxyMode.USER:
            assert body.user_proxy is not None
            u = body.user_proxy
            proxy_row = ProxyRow(
                region=u.region or "",
                host=u.host,
                port=u.port,
                user=u.user,
                password=u.password,
            )
            proxy_idx = None
        else:
            proxies = load_proxies(s.proxies_csv)
            if proxies:
                counts = _proxy_usage_counts(instances, len(proxies))
                proxy_idx = pick_balanced_proxy_index(counts)
                proxy_row = proxies[proxy_idx]
        try:
            docker_ops.run_chrome_pool_container(
                name=name,
                host_vnc=vnc_p,
                host_cdp=cdp_p,
                host_novnc=novnc_p,
                vnc_pass=vnc_pass_effective,
                image=s.chrome_docker_image,
                proxy=proxy_row,
                proxy_index=proxy_idx,
            )
        except DockerError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    try:
        await _wait_cdp_ready(cdp_p, s.start_cdp_timeout_sec)
    except TimeoutError as e:
        try:
            docker_ops.remove_container(name)
        except DockerError:
            pass
        raise HTTPException(status_code=500, detail=str(e)) from e

    novnc_url: str | None = None
    if s.novnc_domain and s.novnc_domain.strip():
        novnc_url = _novnc_public_url(s.novnc_domain, novnc_p, vnc_pass_effective)

    return StartResponse(
        name=name,
        vnc_port=vnc_p,
        cdp_port=cdp_p,
        novnc_port=novnc_p,
        vnc_password=vnc_pass_effective,
        novnc_url=novnc_url,
        proxy_index=proxy_idx,
        proxy_region=(proxy_row.region if proxy_row else None),
    )


@app.post("/stop", response_model=StopResponse, dependencies=[Depends(require_api_key)])
def stop_pool(body: StopBody) -> StopResponse:
    n = body.name.strip()
    if not n:
        raise HTTPException(status_code=400, detail="name is required")
    if not docker_ops.container_exists(n):
        raise HTTPException(status_code=404, detail=f"Container not found: {n}")
    inst = docker_ops.inspect_instance(n)
    if inst is None:
        raise HTTPException(
            status_code=400,
            detail="Container exists but is not a chrome-pool managed instance",
        )
    try:
        docker_ops.remove_container(n)
    except DockerError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return StopResponse(ok=True, name=n)


@app.post("/stopall", response_model=StopAllResponse, dependencies=[Depends(require_api_key)])
def stop_all() -> StopAllResponse:
    try:
        stopped, errs = docker_ops.stop_all_pool_containers()
    except DockerError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return StopAllResponse(
        stopped=stopped,
        errors=[StopAllError(name=n, error=msg) for n, msg in errs],
    )


@app.get("/list", response_model=ListResponse, dependencies=[Depends(require_api_key)])
def list_pool() -> ListResponse:
    try:
        items = docker_ops.list_pool_instances()
    except DockerError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return ListResponse(
        instances=[
            InstanceOut(
                name=i.name,
                vnc_port=i.vnc_port,
                cdp_port=i.cdp_port,
                novnc_port=i.novnc_port,
                proxy_index=i.proxy_index,
                proxy_region=i.proxy_region,
            )
            for i in items
        ]
    )
