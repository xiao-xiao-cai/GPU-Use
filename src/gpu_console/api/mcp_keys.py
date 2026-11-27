"""Manage MCP connections and keys through the administrator's web session."""

from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

from gpu_console import storage
from gpu_console.api.auth import require_session
from gpu_console.mcp import auth
from gpu_console.mcp.server import connection_urls

router = APIRouter(prefix="/api/mcp", dependencies=[Depends(require_session)])
KeyId = Annotated[str, Path(pattern=auth.KEY_ID_PATTERN)]


class KeyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    manage: StrictBool = False
    ssh: StrictBool = False
    days: Literal[7, 30, 90] | None = 90


@router.get("")
async def mcp_info(request: Request) -> dict:
    config = request.app.state.config
    public, local = connection_urls(config)
    try:
        timeout = httpx.Timeout(1)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(config.mcp_health_url)
        healthy = response.is_success
    except httpx.HTTPError:
        healthy = False
    return {"url": public, "local_url": local, "healthy": healthy}


@router.get("/keys")
def keys(request: Request) -> list[dict]:
    with storage.connect(request.app.state.config.db) as conn:
        return auth.list_keys(conn)


@router.post("/keys", status_code=201)
def create_key(draft: KeyDraft, request: Request) -> dict:
    with storage.connect(request.app.state.config.db) as conn, storage.transaction(conn):
        return auth.issue(conn, **draft.model_dump())


@router.post("/keys/{key_id}/rotate")
def rotate_key(key_id: KeyId, draft: KeyDraft, request: Request) -> dict:
    with storage.connect(request.app.state.config.db) as conn, storage.transaction(conn):
        return auth.issue(conn, key_id=key_id, **draft.model_dump())


@router.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: KeyId, request: Request) -> None:
    with storage.connect(request.app.state.config.db) as conn, storage.transaction(conn):
        auth.revoke(conn, key_id)
