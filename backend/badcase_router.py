"""Explicit evaluation feedback controls; list reads are local and paginated."""
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field

from .badcases import BadCaseError, BadCaseService, schedule_refresh, pipeline_loop_summary
from .data_store import RevisionConflict
from .task_generation.router import _manager

router = APIRouter(tags=["badcases"])


class CandidatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str | None = Field(default=None, min_length=1, max_length=4000)
    app: str | None = Field(default=None, min_length=1, max_length=4000)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    candidate_ids: list[str] = Field(min_length=1)
    request_id: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    run_id: str


class EvaluationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    expected_revision: int


def _service():
    return BadCaseService(_manager().data_root)


def _report_store(service):
    from .phone_factory import store
    return store if store.root.resolve() == service.root.resolve() else None


def _call(callback):
    try:
        return callback()
    except BadCaseError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/badcases")
def candidates(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=50), status: str = "", search: str = ""):
    return _call(lambda: _service().page(run_id, offset=offset, limit=limit, status=status, search=search))


@router.post("/api/badcases/refresh", status_code=202)
def refresh(request: RefreshRequest):
    service = _service()
    if not service.records.get("model_iter_runs", request.run_id):
        raise HTTPException(status_code=404, detail="评测运行不存在")
    if not schedule_refresh(service.root, request.run_id, store=_report_store(service)):
        raise HTTPException(status_code=429, detail="BadCase 整理队列繁忙，请稍后重试")
    return {"run_id": request.run_id, "status": "preparing"}


@router.patch("/api/badcases/{candidate_id}")
def patch(candidate_id: str, request: CandidatePatch):
    return _call(lambda: _service().patch(candidate_id, request.model_dump(exclude_none=True)))


@router.post("/api/badcases/feedback", status_code=201)
def feedback(request: FeedbackRequest):
    return _call(lambda: _service().freeze(request.run_id, request.candidate_ids, request.request_id))


@router.get("/api/badcases/feedback/{feedback_id}")
def get_feedback(feedback_id: str):
    return _call(lambda: _service().get_feedback(feedback_id))


@router.post("/api/pipelines/{pipeline_id}/evaluation")
def bind_evaluation(pipeline_id: str, value: EvaluationBinding, request: Request):
    service = _service()
    def bind():
        pipeline = service.records.get("pipelines", pipeline_id)
        if not pipeline:
            raise BadCaseError("Pipeline 不存在", 404)
        if pipeline.get("start_mode") == "evaluation":
            raise BadCaseError("请从本轮评测入口启动评测，或确认已有结果的 BadCase")
        run = service.records.get("model_iter_runs", value.run_id)
        if not run:
            raise BadCaseError("评测运行不存在", 404)
        frozen = run.get("request", {})
        binding = {"pipeline_id": pipeline_id, "run_id": value.run_id,
                   "source": frozen.get("source"), "execution_mode": frozen.get("execution_mode", "collect_and_evaluate"),
                   "batch_id": run.get("batch_id"), "evaluation_profile_id": frozen.get("evaluation_profile_id"),
                   "evaluation_profile_digest": frozen.get("evaluation_profile_digest")}
        saved = service.records.put_many([
            {"namespace": "pipelines", "key": pipeline_id, "payload": pipeline, "expected_revision": value.expected_revision},
            {"namespace": "pipeline_evaluations", "key": pipeline_id, "payload": binding},
        ])[0]
        schedule_refresh(service.root, value.run_id, store=_report_store(service))
        return {"pipeline": {**saved, **pipeline_loop_summary(service.records, saved)}}
    return _call(bind)
