from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import current_principal
from app.core.errors import PermissionDeniedError
from app.core.pagination import Page, page_result
from app.core.security import Principal
from app.database import get_connection, transaction
from app.hydro.params import ParameterSetService
from app.hydro.schemas import (
    EndmemberCreate,
    InversionRequest,
    ParameterSetCreate,
    ParameterSetUpdate,
    PublishRequest,
    RecomputeRequest,
    RetractRequest,
    ReviewDecision,
    SampleCreate,
    TransportRequest,
    WellCreate,
)
from app.hydro.service import HydroService
from app.services.audit import AuditContext, AuditService

router = APIRouter(prefix="/api/hydro", tags=["地下水科学计算"])


def service() -> HydroService:
    return HydroService()


def require_perm(code: str):
    """权限依赖：放行时返回 Principal；越权尝试先落审计再抛 403。"""

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(code):
            with transaction(immediate=True) as connection:
                AuditService(connection).record(
                    AuditContext(principal.user_id, principal.display_name),
                    action="hydro.access.denied",
                    resource_type="hydro_permission",
                    resource_id=code,
                    outcome="denied",
                    metadata={"required_permission": code},
                )
            raise PermissionDeniedError(f"缺少权限：{code}")
        return principal

    return dependency


# --------------------------------------------------------------------- 井点 / 样本 / 旧端元目录

@router.post("/wells", status_code=201)
def create_well(payload: WellCreate, principal: Principal = Depends(require_perm("hydro.write"))):
    try:
        return service().create_well(payload.model_dump(), actor=principal.username)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(409, "井点编码已存在") from exc
        raise


@router.get("/wells/{well_id}")
def get_well(well_id: int, principal: Principal = Depends(require_perm("hydro.read"))):
    del principal
    value = service().get_well(well_id)
    if value is None:
        raise HTTPException(404, "井点不存在")
    return value


@router.delete("/wells/{well_id}")
def delete_well(well_id: int, principal: Principal = Depends(require_perm("hydro.write"))):
    del principal
    try:
        service().delete_well(well_id)
        return {"message": "井点已删除"}
    except KeyError as exc:
        raise HTTPException(404, "井点不存在") from exc


@router.post("/endmembers", status_code=201)
def create_endmember(payload: EndmemberCreate, principal: Principal = Depends(require_perm("hydro.write"))):
    del principal
    return service().create_endmember(payload.model_dump())


@router.post("/wells/{well_id}/samples", status_code=201)
def add_sample(well_id: int, payload: SampleCreate, principal: Principal = Depends(require_perm("hydro.write"))):
    del principal
    try:
        return service().add_sample(well_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(404, "井点不存在") from exc


# --------------------------------------------------------------------- 反演与迁移（必须引用已发布参数集版本）

@router.post("/samples/{sample_id}/inversions", status_code=202)
def enqueue_inversion(sample_id: int, payload: InversionRequest, principal: Principal = Depends(require_perm("hydro.write"))):
    del principal
    try:
        return service().enqueue_inversion(sample_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(404, "样本不存在") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/inversions/{task_id}/run")
def run_inversion(task_id: int, worker_id: str = Query(..., min_length=1), principal: Principal = Depends(require_perm("hydro.write"))):
    del principal
    try:
        return service().run_inversion(task_id, worker_id)
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/inversions/{task_id}")
def get_inversion(task_id: int, principal: Principal = Depends(require_perm("hydro.read"))):
    del principal
    value = service().get_inversion(task_id)
    if value is None:
        raise HTTPException(404, "任务不存在")
    return value


@router.get("/transport/{task_id}")
def get_transport_run(task_id: int, principal: Principal = Depends(require_perm("hydro.read"))):
    del principal
    value = service().get_transport_run(task_id)
    if value is None:
        raise HTTPException(404, "任务不存在")
    return value


@router.post("/wells/{well_id}/transport", status_code=201)
def run_transport(well_id: int, payload: TransportRequest, principal: Principal = Depends(require_perm("hydro.write"))):
    del principal
    try:
        return service().run_transport(well_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(404, "井点不存在") from exc


# --------------------------------------------------------------------- 参数集生命周期

param_router = APIRouter(prefix="/api/hydro/param-sets", tags=["地下水参数集版本"])


@param_router.get("")
def list_param_sets(
    site_code: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(require_perm("hydro.read")),
):
    pagination = Page(page, size)
    result = ParameterSetService(get_connection()).list_sets(
        principal, site_code=site_code, status=status, limit=size, offset=pagination.offset
    )
    return page_result(total=result["total"], page=pagination, rows=result["data"])


@param_router.post("", status_code=201)
def create_param_set(payload: ParameterSetCreate, principal: Principal = Depends(require_perm("hydro.params.write"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).create(principal, payload.model_dump())


@param_router.get("/group-versions")
def group_versions(
    site_code: str = Query(..., min_length=2),
    code: str = Query(..., min_length=1),
    principal: Principal = Depends(require_perm("hydro.read")),
):
    return ParameterSetService(get_connection()).list_versions(principal, site_code, code)


@param_router.get("/diff")
def diff_param_sets(
    old: int = Query(..., ge=1),
    new: int = Query(..., ge=1),
    principal: Principal = Depends(require_perm("hydro.read")),
):
    return ParameterSetService(get_connection()).compare_parameter_sets(principal, old, new)


@param_router.get("/{param_set_id}")
def get_param_set(param_set_id: int, principal: Principal = Depends(require_perm("hydro.read"))):
    return ParameterSetService(get_connection()).detail(principal, param_set_id)


@param_router.patch("/{param_set_id}")
def update_param_set(param_set_id: int, payload: ParameterSetUpdate, principal: Principal = Depends(require_perm("hydro.params.write"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).update(principal, param_set_id, payload.model_dump(exclude_unset=True))


@param_router.post("/{param_set_id}/submit")
def submit_param_set(param_set_id: int, payload: ReviewDecision, principal: Principal = Depends(require_perm("hydro.params.write"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).submit(principal, param_set_id, payload.comment)


@param_router.post("/{param_set_id}/approve")
def approve_param_set(param_set_id: int, payload: ReviewDecision, principal: Principal = Depends(require_perm("hydro.params.review"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).approve(principal, param_set_id, payload.comment)


@param_router.post("/{param_set_id}/reject")
def reject_param_set(param_set_id: int, payload: ReviewDecision, principal: Principal = Depends(require_perm("hydro.params.review"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).reject(principal, param_set_id, payload.comment)


@param_router.post("/{param_set_id}/rebase")
def rebase_param_set(param_set_id: int, principal: Principal = Depends(require_perm("hydro.params.write"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).rebase(principal, param_set_id)


@param_router.post("/{param_set_id}/publish")
def publish_param_set(param_set_id: int, payload: PublishRequest, principal: Principal = Depends(require_perm("hydro.params.publish"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).publish(principal, param_set_id, payload.expected_base_version_id)


@param_router.post("/{param_set_id}/retract")
def retract_param_set(param_set_id: int, payload: RetractRequest, principal: Principal = Depends(require_perm("hydro.params.publish"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).retract(principal, param_set_id, payload.reason)


@param_router.get("/{param_set_id}/affected")
def affected_results(
    param_set_id: int,
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(require_perm("hydro.read")),
):
    return ParameterSetService(get_connection()).affected_results(principal, param_set_id, limit)


@param_router.post("/{param_set_id}/recompute", status_code=202)
def recompute_results(param_set_id: int, payload: RecomputeRequest, principal: Principal = Depends(require_perm("hydro.recompute"))):
    with transaction(immediate=True) as connection:
        return ParameterSetService(connection).recompute(principal, param_set_id, payload.task_types, payload.limit)


# --------------------------------------------------------------------- 结果前后差异

result_router = APIRouter(prefix="/api/hydro/results", tags=["地下水结果差异"])


@result_router.get("/diff")
def diff_results(
    task_type: str = Query(..., pattern="^(inversion|transport)$"),
    old: int = Query(..., ge=1),
    new: int = Query(..., ge=1),
    principal: Principal = Depends(require_perm("hydro.read")),
):
    return ParameterSetService(get_connection()).compare_results(principal, task_type, old, new)
