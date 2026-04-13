from __future__ import annotations

import asyncio
import time
import uuid
from typing import Annotated, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import docker_ops
from app.docker_ops import DockerError, validate_container_name
from app.ports import allocate_sequential_pool_ports


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    vnc_pass: str = "mystakechrome"
    chrome_docker_image: str = "suyash5053/chromium-vnc-cdp"
    start_cdp_timeout_sec: float = 60.0
    api_key: Optional[str] = None


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


class StartBody(BaseModel):
    name: Optional[str] = None


class StartResponse(BaseModel):
    name: str
    vnc_port: int
    cdp_port: int
    vnc_password: str


class ErrorResponse(BaseModel):
    error: str


class StopBody(BaseModel):
    name: str


class StopResponse(BaseModel):
    ok: bool
    name: str


class InstanceOut(BaseModel):
    name: str
    vnc_port: int
    cdp_port: int


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
        try:
            instances = docker_ops.list_pool_instances()
        except DockerError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        used: set[int] = set()
        for inst in instances:
            used.add(inst.vnc_port)
            used.add(inst.cdp_port)
        try:
            vnc_p, cdp_p = allocate_sequential_pool_ports(used)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        try:
            docker_ops.run_chrome_pool_container(
                name=name,
                host_vnc=vnc_p,
                host_cdp=cdp_p,
                vnc_pass=s.vnc_pass,
                image=s.chrome_docker_image,
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

    return StartResponse(
        name=name,
        vnc_port=vnc_p,
        cdp_port=cdp_p,
        vnc_password=s.vnc_pass,
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
            InstanceOut(name=i.name, vnc_port=i.vnc_port, cdp_port=i.cdp_port)
            for i in items
        ]
    )
