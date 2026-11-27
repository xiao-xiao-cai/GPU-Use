"""Read cached locations and images, and queue an image catalog refresh."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from gpu_console import storage
from gpu_console.api.auth import require_session
from gpu_console.api.resources import Db
from gpu_console.application.purchase_options import create_options
from gpu_console.storage import catalog

router = APIRouter(prefix="/api", dependencies=[Depends(require_session)])
ScopeId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class ImageScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: ScopeId
    region: ScopeId
    zone: ScopeId


class CreationScope(ImageScope):
    gpu_model: Annotated[str, Field(min_length=1, max_length=100)]
    instance_type: Literal["uhost", "spot"]
    image_id: ScopeId


def _require_scope(conn: Db, scope: ImageScope) -> None:
    if not catalog.project_known(conn, scope.project_id):
        raise HTTPException(404, "平台项目目录中没有该项目，请先同步项目")
    if not catalog.location_known(conn, scope.region, scope.zone):
        raise HTTPException(404, "平台可用区目录中没有该区域，请先同步机型目录")


@router.get("/locations")
def locations(conn: Db) -> dict:
    return catalog.locations(conn)


@router.get("/images")
def images(scope: Annotated[ImageScope, Query()], conn: Db) -> dict:
    _require_scope(conn, scope)
    return catalog.image_snapshots(conn, scope.project_id, scope.region, scope.zone)


@router.get("/instance-create-options")
def instance_create_options(scope: Annotated[CreationScope, Query()], conn: Db) -> dict:
    _require_scope(conn, scope)
    return create_options(conn, **scope.model_dump(), queue_cfs=True)


@router.post("/images/refresh", status_code=202)
def refresh_images(scope: ImageScope, conn: Db) -> dict:
    with storage.transaction(conn):
        _require_scope(conn, scope)
        batch_id = catalog.queue_images(conn, scope.project_id, scope.region, scope.zone)
    return {"queued": True, "id": batch_id}
