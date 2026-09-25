from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.hydro.schemas import (
    EndmemberCreate,
    InversionRequest,
    ParameterSetDraft,
    ParameterSetRevise,
    PublishRequest,
    RecalculateRequest,
    RejectRequest,
    RevokeRequest,
    SampleCreate,
    TransportRequest,
    WellCreate,
)
from app.hydro.service import HydroService, ParameterSetService

router = APIRouter(prefix="/api/hydro", tags=["地下水科学计算"])


def hydro() -> HydroService:
    return HydroService()


def parameter_service() -> ParameterSetService:
    return ParameterSetService()


# ---- 井点 / 样本 / 端元目录 -----------------------------------------------


@router.post("/wells", status_code=201)
def create_well(payload: WellCreate, principal: Principal = Depends(current_principal)):
    return hydro().create_well(payload.model_dump(), principal)


@router.get("/wells/{well_id}")
def get_well(well_id: int, principal: Principal = Depends(current_principal)):
    principal.require("hydro.results.read")
    value = hydro().get_well(well_id)
    if value is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("井点不存在")
    return value


@router.delete("/wells/{well_id}")
def delete_well(well_id: int, principal: Principal = Depends(current_principal)):
    hydro().delete_well(well_id, principal)
    return {"message": "井点已删除"}


@router.post("/endmembers", status_code=201)
def create_endmember(payload: EndmemberCreate, principal: Principal = Depends(current_principal)):
    return hydro().create_endmember(payload.model_dump(), principal)


@router.post("/wells/{well_id}/samples", status_code=201)
def add_sample(well_id: int, payload: SampleCreate, principal: Principal = Depends(current_principal)):
    return hydro().add_sample(well_id, payload.model_dump(), principal)


# ---- 参数集：草稿 / 复核 / 发布 / 撤销 ------------------------------------


@router.post("/sites/{site_code}/parameter-sets", status_code=201)
def create_draft(site_code: str, payload: ParameterSetDraft, principal: Principal = Depends(current_principal)):
    return parameter_service().create_draft(principal, site_code, payload.model_dump())


@router.get("/parameter-sets")
def list_parameter_sets(
    site_code: str | None = None,
    status: str | None = Query(default=None, pattern="^(draft|in_review|published|revoked)$"),
    principal: Principal = Depends(current_principal),
):
    principal.require("hydro.parameters.read")
    return parameter_service().list_sets(site_code, status)


@router.get("/parameter-sets/{parameter_set_id}")
def get_parameter_set(parameter_set_id: int, principal: Principal = Depends(current_principal)):
    principal.require("hydro.parameters.read")
    return parameter_service().get_set(parameter_set_id)


@router.patch("/parameter-sets/{parameter_set_id}")
def revise_draft(parameter_set_id: int, payload: ParameterSetRevise, principal: Principal = Depends(current_principal)):
    return parameter_service().revise_draft(principal, parameter_set_id, payload.model_dump(exclude_unset=True))


@router.delete("/parameter-sets/{parameter_set_id}")
def delete_draft(parameter_set_id: int, principal: Principal = Depends(current_principal)):
    parameter_service().delete_draft(principal, parameter_set_id)
    return {"message": "草稿已删除"}


@router.post("/parameter-sets/{parameter_set_id}/submit")
def submit_for_review(parameter_set_id: int, principal: Principal = Depends(current_principal)):
    return parameter_service().submit_for_review(principal, parameter_set_id)


@router.post("/parameter-sets/{parameter_set_id}/reject")
def reject_draft(parameter_set_id: int, payload: RejectRequest, principal: Principal = Depends(current_principal)):
    return parameter_service().reject(principal, parameter_set_id, payload.reason)


@router.post("/parameter-sets/{parameter_set_id}/publish")
def publish_set(
    parameter_set_id: int,
    payload: PublishRequest | None = None,
    force: bool = Query(default=False, description="确认覆盖并发发布冲突"),
    principal: Principal = Depends(current_principal),
):
    note = payload.note if payload is not None else ""
    return parameter_service().publish(principal, parameter_set_id, note=note, force=force)


@router.post("/parameter-sets/{parameter_set_id}/revoke")
def revoke_set(parameter_set_id: int, payload: RevokeRequest, principal: Principal = Depends(current_principal)):
    return parameter_service().revoke(principal, parameter_set_id, payload.reason)


@router.get("/parameter-sets/{parameter_set_id}/diffs")
def list_diffs(parameter_set_id: int, principal: Principal = Depends(current_principal)):
    principal.require("hydro.results.read")
    return parameter_service().list_diffs(parameter_set_id)


@router.get("/affected-results")
def list_affected(
    site_code: str | None = None,
    kind: str | None = Query(default=None, pattern="^(inversion|transport)$"),
    principal: Principal = Depends(current_principal),
):
    principal.require("hydro.results.read")
    return parameter_service().list_affected(site_code, kind)


@router.post("/parameter-sets/{parameter_set_id}/recalculate", status_code=200)
def recalculate(
    parameter_set_id: int,
    payload: RecalculateRequest | None = None,
    principal: Principal = Depends(current_principal),
):
    kinds = payload.kinds if payload is not None else None
    return hydro().recalculate_affected(principal, parameter_set_id, kinds)


# ---- 反演与迁移任务（必须引用不可变发布版本） -----------------------------


@router.post("/samples/{sample_id}/inversions", status_code=202)
def enqueue_inversion(sample_id: int, payload: InversionRequest, principal: Principal = Depends(current_principal)):
    return hydro().enqueue_inversion(sample_id, payload.model_dump(), principal)


@router.post("/inversions/{task_id}/run")
def run_inversion(
    task_id: int,
    worker_id: str = Query(..., min_length=1),
    principal: Principal = Depends(current_principal),
):
    return hydro().run_inversion(task_id, worker_id, principal)


@router.get("/inversions/{task_id}")
def get_inversion(task_id: int, principal: Principal = Depends(current_principal)):
    principal.require("hydro.results.read")
    value = hydro().get_inversion(task_id)
    if value is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("任务不存在")
    return value


@router.post("/wells/{well_id}/transport", status_code=201)
def run_transport(well_id: int, payload: TransportRequest, principal: Principal = Depends(current_principal)):
    return hydro().run_transport(well_id, payload.model_dump(), principal)


@router.get("/transports/{task_id}")
def get_transport(task_id: int, principal: Principal = Depends(current_principal)):
    principal.require("hydro.results.read")
    value = hydro().get_transport(task_id)
    if value is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("任务不存在")
    return value
