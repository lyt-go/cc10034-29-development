"""结果修订 API：修正面板的提交、复核、版本改绑、确认，以及噪声配置轮换。

所有冲突都走 HTTP 409，并在 `conflict` 里带回冲突码、尚缺项和当前版本，
页面据此显示“哪一版本产生了更正结果”或“本次提交还缺什么”。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.services.result_revision import RevisionError, ledger

router = APIRouter(prefix="/api/revisions", tags=["结果修订"])


def _handle(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except RevisionError as error:
        return JSONResponse(status_code=error.status_code, content=error.as_payload())


@router.get("/ledger")
def revision_ledger() -> dict[str, Any]:
    """修订台账总览：已登记过的查询、修订历史、更正和费用流水。"""
    return ledger.ledger()


@router.get("/queries/{query_id}")
def revision_panel(query_id: int):
    """结果修订面板：原结果、生效结果、修订历史、回执、更正与费用一次返回。"""
    return _handle(ledger.snapshot, query_id)


@router.post("/queries/{query_id}/submit")
def submit_revision(query_id: int, payload: dict[str, Any] | None = None):
    payload = payload or {}
    values = dict(payload.get("values") or payload)
    return _handle(
        lambda: {"ok": True, **_result(ledger.submit(query_id, values))},
    )


def _result(outcome: tuple[dict[str, Any], bool]) -> dict[str, Any]:
    revision, duplicate = outcome
    return {
        "duplicate": duplicate,
        "message": (
            f"与修订 {revision['revision_no']} 内容相同，已合并，不重复计费"
            if duplicate else f"修订 {revision['revision_no']} 已提交，等待复核"
        ),
        "revision": revision,
    }


@router.post("/queries/{query_id}/revisions/{revision_no}/review")
def review_revision(query_id: int, revision_no: str, payload: dict[str, Any] | None = None):
    payload = payload or {}
    verdict = str(payload.get("verdict") or "").strip()
    return _handle(
        lambda: {
            "ok": True,
            "message": f"修订 {revision_no} 复核结论：{verdict or '未给出'}",
            "revision": ledger.review(
                query_id, revision_no,
                verdict=verdict,
                reviewer=str(payload.get("reviewer") or ""),
                note=str(payload.get("note") or ""),
            ),
        },
    )


@router.post("/queries/{query_id}/revisions/{revision_no}/rebind")
def rebind_revision(query_id: int, revision_no: str, payload: dict[str, Any] | None = None):
    payload = payload or {}
    return _handle(
        lambda: {
            "ok": True,
            "message": f"修订 {revision_no} 已改绑到当前生效版本",
            "revision": ledger.rebind(
                query_id, revision_no,
                version=str(payload.get("version") or ""),
                reason=str(payload.get("reason") or ""),
                operator=str(payload.get("operator") or ""),
            ),
        },
    )


@router.post("/queries/{query_id}/revisions/{revision_no}/confirm")
def confirm_revision(query_id: int, revision_no: str, payload: dict[str, Any] | None = None):
    payload = payload or {}
    return _handle(
        lambda: ledger.confirm(query_id, revision_no, operator=str(payload.get("operator") or "")),
    )


@router.get("/noise-configs/{config_id}/rotations")
def noise_rotations(config_id: int):
    """噪声配置的版本轮换记录：页面用来说明哪一版本产生了更正结果。"""
    return _handle(ledger.rotations, config_id)


@router.post("/noise-configs/{config_id}/rotate")
def rotate_noise_config(config_id: int, payload: dict[str, Any] | None = None):
    payload = payload or {}
    return _handle(
        lambda: ledger.rotate(
            config_id,
            target_version=str(payload.get("target_version") or payload.get("version") or ""),
            reason=str(payload.get("reason") or ""),
            operator=str(payload.get("operator") or ""),
        ),
    )
