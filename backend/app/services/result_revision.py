"""差分查询结果修订域：核对原执行、修订原因与噪声配置，签发回执，核准新的执行资格和参数。

三条硬约束都在这一层完成：

- 原结果不可覆盖：修订只往更正流水追加记录，已交付的 `differential_sql` 行始终只读；
  每次流转都会重新比对交付时的结果指纹，被外部改写就拒绝确认。
- 一次修订最多确认一次执行费用：重复确认返回同一张回执，重复提交合并到同一修订。
- 参数版本与修订产物一一对应：同一 (查询, 配置版本, 参数组合) 只允许产生一个产物。
- 配置轮换与确认交错时，服务端给出冲突码、缺失项和实际产生更正结果的版本。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable

from app.store import store

QUERY_MODULE = "differential_sql"
NOISE_MODULE = "noise_engine"

# 只有真正交付了结果的行才能走修订；已审计同样算交付，但被拒绝/预算不足不算。
DELIVERED_STATUSES = {"已执行", "已审计"}
RESULT_FIELDS = [
    "查询编号", "查询语句", "查询用户", "消耗Epsilon", "噪声注入量", "返回行数", "执行时间", "原结果",
]
PARAM_FIELDS = ["消耗Epsilon", "噪声注入量", "返回行数", "执行时间"]
UNIT_FEE = 1.0

REV_SUBMITTED = "已提交"
REV_REVIEWED = "复核通过"
REV_REJECTED = "已驳回"
REV_APPROVED = "已核准"


class RevisionError(Exception):
    """修订域内的可预期失败：404 或 409（带冲突码、缺失项、版本信息）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        missing: list[str] | None = None,
        current_version: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.missing = missing or []
        self.current_version = current_version
        self.extra = extra or {}

    def as_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "conflict": {
                "code": self.code,
                "message": self.message,
                "missing": self.missing,
                "current_version": self.current_version,
                **self.extra,
            },
        }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def result_fingerprint(row: dict[str, Any]) -> str:
    """已交付结果的内容指纹：只覆盖结果字段，审计状态流转不影响它。"""
    payload = {field: str(row.get(field, "")) for field in RESULT_FIELDS}
    return f"{payload['查询编号'] or row.get('id')}#{_digest(payload)[:12]}"


def delivered_block_reason(row: dict[str, Any]) -> str | None:
    """返回不可修订的原因；None 表示该行的结果已经交付、可以提交修订。"""
    if row.get("status") not in DELIVERED_STATUSES:
        return f"当前状态「{row.get('status')}」尚未交付结果，不能提交修订"
    if "未执行" in str(row.get("噪声注入量", "")) or "被拒绝" in str(row.get("噪声注入量", "")):
        return "该查询没有实际注入噪声执行，原结果不构成可修订的交付"
    try:
        if int(str(row.get("返回行数") or 0)) <= 0:
            return "返回行数为 0，原结果不构成可修订的交付"
    except ValueError:
        return "返回行数无法解析，原结果不可核对"
    return None


def _param_key(query_id: int, config_no: str, version: str, params: dict[str, Any]) -> str:
    return _digest(
        {
            "query_id": query_id,
            "config": config_no,
            "version": version,
            "params": {field: str(params.get(field, "")) for field in PARAM_FIELDS},
        }
    )[:16]


def _dedup_key(
    query_id: int, reason: str, config_id: int, version: str,
    params: dict[str, Any], corrected_result: str, fingerprint: str,
) -> str:
    return _digest(
        {
            "query_id": query_id,
            "reason": reason,
            "config_id": config_id,
            "version": version,
            "params": {field: str(params.get(field, "")) for field in PARAM_FIELDS},
            "corrected_result": corrected_result,
            "fingerprint": fingerprint,
        }
    )[:18]


class RevisionLedger:
    """修订台账：按查询记录原结果、修订、回执、更正与费用；按噪声配置记录轮换轮次。"""

    def __init__(self) -> None:
        # 测试可以在确认提交前插入一次配置轮换，用来模拟“确认与轮换撞车”。
        self.before_commit_guard: Callable[[dict[str, Any]], None] | None = None
        self.reset()

    def reset(self) -> None:
        self._queries: dict[int, dict[str, Any]] = {}
        self._rotations: dict[int, list[dict[str, Any]]] = {}
        self._version_origin: dict[int, dict[str, str]] = {}
        self._charge_seq = 0
        self._event_seq = 0

    # ——————————————— 查询侧状态 ———————————————
    def _events(self, state: dict[str, Any], event: str, detail: str, operator: str) -> None:
        self._event_seq += 1
        state["events"].append(
            {
                "序号": self._event_seq,
                "事件": event,
                "说明": detail,
                "操作人": operator,
                "时间": _now(),
            }
        )

    def _state(self, query_id: int) -> dict[str, Any]:
        row = store.find(QUERY_MODULE, query_id)
        if row is None:
            raise RevisionError("QUERY_NOT_FOUND", f"查询记录 {query_id} 不存在或已归档", status_code=404)
        state = self._queries.get(query_id)
        if state is None:
            state = {
                "query_id": query_id,
                "row_snapshot": {field: str(row.get(field, "")) for field in RESULT_FIELDS},
                "fingerprint": result_fingerprint(row),
                "delivered_status": str(row.get("status")),
                "config_no": str(row.get("噪声配置编号", "")),
                "revisions": [],
                "corrections": [],
                "receipts": [],
                "charges": [],
                "artifacts": [],
                "events": [],
                "effective_revision": None,
            }
            self._queries[query_id] = state
            self._events(state, "登记原结果", f"捕获交付指纹 {state['fingerprint']}", "系统")
        return state

    def _live_unchanged(self, state: dict[str, Any]) -> bool:
        row = store.find(QUERY_MODULE, state["query_id"])
        if row is None:
            return False
        current = {field: str(row.get(field, "")) for field in RESULT_FIELDS}
        return current == state["row_snapshot"] and result_fingerprint(row) == state["fingerprint"]

    def _assert_original(self, state: dict[str, Any]) -> None:
        if not self._live_unchanged(state):
            raise RevisionError(
                "ORIGINAL_RESULT_REWRITTEN",
                "已交付的原结果被外部改写，指纹与登记时不一致；修订不能覆盖原结果，请先复核数据来源",
                missing=["与登记指纹一致的已交付原结果", "重新核对原执行"],
                extra={"fingerprint": state["fingerprint"]},
            )

    # ——————————————— 噪声配置与轮换 ———————————————
    def _config(self, config_id: int) -> dict[str, Any]:
        config = store.find(NOISE_MODULE, config_id)
        if config is None:
            raise RevisionError(
                "NOISE_CONFIG_NOT_FOUND", f"噪声配置 {config_id} 不存在", status_code=404,
                missing=["有效的噪声配置编号"],
            )
        return config

    def _rotation_count(self, config_id: int) -> int:
        return len(self._rotations.get(config_id, []))

    def rotations(self, config_id: int) -> dict[str, Any]:
        config = self._config(config_id)
        return {
            "config_id": config_id,
            "配置编号": config.get("配置编号"),
            "current_version": config.get("配置版本"),
            "status": config.get("status"),
            "rotations": list(self._rotations.get(config_id, [])),
        }

    def rotate(self, config_id: int, *, target_version: str, reason: str, operator: str) -> dict[str, Any]:
        config = self._config(config_id)
        target = str(target_version or "").strip()
        reason = str(reason or "").strip()
        missing: list[str] = []
        if not target:
            missing.append("目标配置版本")
        if not reason:
            missing.append("轮换原因")
        if missing:
            raise RevisionError("ROTATION_INPUT_MISSING", f"配置轮换缺少：{'、'.join(missing)}", missing=missing)
        if config.get("status") != "已生效":
            raise RevisionError(
                "NOISE_CONFIG_NOT_EFFECTIVE",
                f"配置 {config.get('配置编号')} 当前状态「{config.get('status')}」，只有已生效配置才能轮换",
                missing=["已生效的噪声配置"],
            )
        current = str(config.get("配置版本"))
        if target == current:
            raise RevisionError(
                "ROTATION_VERSION_UNCHANGED",
                f"目标版本 {target} 与当前版本相同，无需轮换",
                current_version=current,
            )
        history = self._rotations.setdefault(config_id, [])
        known = {str(self._version_origin.get(config_id, {}).get("initial", current))}
        known.update(str(item.get("to_version")) for item in history)
        if target in known:
            raise RevisionError(
                "ROTATION_VERSION_REUSED",
                f"版本 {target} 已在历史中出现，配置版本不能回退或复用",
                current_version=current,
                extra={"used_versions": sorted(known)},
            )
        self._version_origin.setdefault(config_id, {"initial": current})
        round_no = len(history) + 1
        snapshot = {
            "Epsilon值": config.get("Epsilon值"),
            "敏感度": config.get("敏感度"),
            "分布类型": config.get("分布类型"),
            "输出精度": config.get("输出精度"),
            "关联字段": config.get("关联字段"),
        }
        record = {
            "轮次": round_no,
            "from_version": current,
            "to_version": target,
            "操作人": operator or "噪声引擎",
            "原因": reason,
            "时间": _now(),
            "snapshot": snapshot,
        }
        history.append(record)
        config["配置版本"] = target
        # 轮换只升级版本，不改变“已生效”状态；旧版本从此冻结。
        return {
            "ok": True,
            "message": f"配置 {config.get('配置编号')} 已从 {current} 轮换到 {target}",
            "rotation": record,
            "current_version": target,
        }

    # ——————————————— 提交修订 ———————————————
    def _find_dedup(self, state: dict[str, Any], key: str) -> dict[str, Any] | None:
        for revision in state["revisions"]:
            if revision["dedup_key"] == key:
                return revision
        return None

    def submit(self, query_id: int, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        state = self._state(query_id)
        row = store.find(QUERY_MODULE, query_id)
        assert row is not None
        blocked = delivered_block_reason(row)
        if blocked:
            raise RevisionError(
                "RESULT_NOT_DELIVERED", blocked,
                missing=["已交付的查询结果"], extra={"current_status": row.get("status")},
            )
        self._assert_original(state)

        reason = str(values.get("reason") or values.get("修订原因") or "").strip()
        provided_fp = str(values.get("原执行核对值") or "").strip()
        corrected = str(values.get("corrected_result") or values.get("更正结果") or "").strip()
        missing: list[str] = []
        if not provided_fp:
            missing.append("原执行核对值")
        if not reason:
            missing.append("修订原因")
        if not corrected:
            missing.append("更正结果")
        try:
            config_id = int(values.get("noise_config_id") or 0)
        except (TypeError, ValueError):
            config_id = 0
        missing.extend(
            f"执行参数「{field}」" for field in PARAM_FIELDS if not str(values.get(field) or "").strip()
        )
        version = str(values.get("noise_version") or values.get("配置版本") or "").strip()
        if not version:
            missing.append("配置版本")
        if missing:
            raise RevisionError(
                "REVISION_INPUT_MISSING",
                f"修订提交尚缺：{'、'.join(missing)}",
                missing=missing,
                current_version=str(self._config(config_id).get("配置版本")) if config_id else None,
            )

        if provided_fp != state["fingerprint"]:
            raise RevisionError(
                "ORIGINAL_MISMATCH",
                "核对值与本服务登记的已交付结果不一致，原结果可能已被改写或核对对象错误",
                missing=["与已交付原结果一致的核对值"],
                extra={"fingerprint": state["fingerprint"]},
            )

        config = self._config(config_id)
        if str(config.get("配置编号")) != str(row.get("噪声配置编号", "")):
            raise RevisionError(
                "NOISE_CONFIG_MISMATCH",
                f"所选噪声配置 {config.get('配置编号')} 与已交付结果使用的 {row.get('噪声配置编号')} 不一致",
                missing=["与原执行一致的噪声配置"],
            )
        if config.get("status") != "已生效":
            raise RevisionError(
                "NOISE_CONFIG_NOT_EFFECTIVE",
                f"噪声配置 {config.get('配置编号')} 当前状态「{config.get('status')}」，核准前必须已生效",
                missing=["已生效的噪声配置"],
            )
        version = str(values.get("noise_version") or values.get("配置版本") or "").strip()
        current_version = str(config.get("配置版本"))
        if version != current_version:
            raise RevisionError(
                "NOISE_VERSION_ROTATED",
                f"本修订绑定版本 {version}，而配置已轮换到 {current_version}；请先重新绑定当前版本再提交复核",
                missing=[f"当前参数版本 {current_version} 的绑定"],
                current_version=current_version,
                extra={"pinned_version": version},
            )

        params = {field: str(values.get(field)) for field in PARAM_FIELDS}
        dedup_key = _dedup_key(query_id, reason, config_id, version, params, corrected, state["fingerprint"])
        existing = self._find_dedup(state, dedup_key)
        if existing is not None:
            self._events(
                state, "重复提交合并",
                f"相同修订内容合并到 {existing['revision_no']}，不重复计入执行费用", "系统",
            )
            return existing, True

        revision = {
            "revision_no": f"R-{len(state['revisions']) + 1:04d}",
            "status": REV_SUBMITTED,
            "dedup_key": dedup_key,
            "reason": reason,
            "params": params,
            "corrected_result": corrected,
            "config_id": config_id,
            "config_no": config.get("配置编号"),
            "pinned_version": version,
            "version_produced": None,
            "original_fingerprint": state["fingerprint"],
            "original_check": {
                "provided": provided_fp,
                "expected": state["fingerprint"],
                "match": True,
                "checked_at": _now(),
            },
            "review": None,
            "receipt": None,
            "artifact": None,
            "correction": None,
            "charge_id": None,
            "effective": False,
            "superseded_by": None,
            "conflict": None,
            "conflicts": [],
            "events": [],
            "submitted_at": _now(),
        }
        state["revisions"].append(revision)
        self._events(state, "提交修订", f"{revision['revision_no']} 绑定 {config.get('配置编号')} {version}", "数据工程组")
        return revision, False

    # ——————————————— 复核 ———————————————
    def review(
        self, query_id: int, revision_no: str, *, verdict: str, reviewer: str, note: str = "",
    ) -> dict[str, Any]:
        state = self._state(query_id)
        revision = self._revision(state, revision_no)
        if revision["status"] in (REV_APPROVED, REV_REJECTED):
            raise RevisionError(
                "REVIEW_NOT_ALLOWED", f"修订 {revision_no} 已是「{revision['status']}」，不能再复核",
            )
        if verdict not in ("通过", "驳回"):
            raise RevisionError("REVIEW_VERDICT_INVALID", "复核结论只能是「通过」或「驳回」", missing=["复核结论"])
        self._assert_original(state)
        revision["review"] = {
            "结论": verdict,
            "核对人": reviewer or "数据安全组",
            "备注": note or "",
            "时间": _now(),
        }
        revision["status"] = REV_REVIEWED if verdict == "通过" else REV_REJECTED
        self._events(
            state, "复核" + verdict, f"{revision_no} 复核{verdict}", revision["review"]["核对人"],
        )
        if verdict == "通过":
            # 复核通过后再校验一次版本，把轮换冲突提前暴露在页面上。
            config = self._config(revision["config_id"])
            current = str(config.get("配置版本"))
            if revision["pinned_version"] != current:
                self._record_conflict(
                    revision, "NOISE_VERSION_ROTATED",
                    f"复核时配置已轮换到 {current}，本次提交尚缺当前版本绑定", current,
                )
        return revision

    # ——————————————— 重新绑定版本 ———————————————
    def rebind(
        self, query_id: int, revision_no: str, *, version: str, reason: str, operator: str,
    ) -> dict[str, Any]:
        state = self._state(query_id)
        revision = self._revision(state, revision_no)
        if revision["status"] not in (REV_SUBMITTED, REV_REVIEWED):
            raise RevisionError(
                "REBIND_NOT_ALLOWED", f"修订 {revision_no} 已是「{revision['status']}」，不能再变更版本绑定",
            )
        config = self._config(revision["config_id"])
        current = str(config.get("配置版本"))
        target = str(version or "").strip()
        if not target:
            raise RevisionError("NOISE_VERSION_MISSING", "重新绑定尚缺：配置版本", missing=["配置版本"], current_version=current)
        if target != current:
            raise RevisionError(
                "NOISE_VERSION_ROTATED",
                f"只能绑定当前版本 {current}，{target} 已经不是生效版本",
                missing=[f"当前参数版本 {current} 的绑定"],
                current_version=current,
                extra={"pinned_version": revision["pinned_version"]},
            )
        if target == revision["pinned_version"]:
            raise RevisionError(
                "NOISE_VERSION_UNCHANGED", f"修订已绑定 {target}，无需重新绑定", current_version=current,
            )
        old = revision["pinned_version"]
        revision["pinned_version"] = target
        # 参数变了，之前的复核结论作废，必须重新复核。
        if revision["status"] == REV_REVIEWED:
            revision["status"] = REV_SUBMITTED
            revision["review"] = None
        revision["conflict"] = None
        revision["dedup_key"] = _dedup_key(
            query_id, revision["reason"], revision["config_id"], target,
            revision["params"], revision["corrected_result"], revision["original_fingerprint"],
        )
        self._events(state, "重新绑定版本", f"{revision_no} 由 {old} 改绑 {target}，复核结论作废", operator or "数据工程组")
        return revision

    # ——————————————— 确认修订 ———————————————
    def confirm(self, query_id: int, revision_no: str, *, operator: str) -> dict[str, Any]:
        state = self._state(query_id)
        revision = self._revision(state, revision_no)
        if revision["status"] == REV_APPROVED:
            # 幂等：同一修订重复确认只返回原回执，不再计费。
            revision["repeated_confirm"] = True
            self._events(state, "重复确认", f"{revision_no} 已核准，返回原回执且不重复计费", operator or "系统")
            return {
                "ok": True,
                "duplicate": True,
                "charged": False,
                "message": f"修订 {revision_no} 已确认，回执 {revision['receipt']['receipt_no']} 未重复计费",
                "revision": revision,
                "receipt": revision["receipt"],
            }
        if revision["status"] != REV_REVIEWED:
            self._record_conflict(
                revision, "REVIEW_MISSING",
                "修订尚未取得复核通过的结论，不能确认", str(self._config(revision["config_id"]).get("配置版本")),
            )
            self._sync_readiness(state, revision)
            raise RevisionError(
                "REVIEW_MISSING",
                f"修订 {revision_no} 当前「{revision['status']}」，确认前尚缺复核通过结论",
                missing=["复核结论（通过）"],
                current_version=str(self._config(revision["config_id"]).get("配置版本")),
            )
        self._assert_original(state)

        config = self._config(revision["config_id"])
        current = str(config.get("配置版本"))
        pinned = revision["pinned_version"]
        if pinned != current:
            self._record_conflict(
                revision, "NOISE_VERSION_ROTATED",
                f"确认时配置已轮换到 {current}，本修订仍绑定 {pinned}；请重新绑定当前版本并重新复核",
                current,
            )
            # 版本变化使复核结论失效，退回待复核。
            revision["status"] = REV_SUBMITTED
            revision["review"] = None
            raise RevisionError(
                "NOISE_VERSION_ROTATED",
                f"本修订绑定版本 {pinned}，而配置已轮换到 {current}，本次提交尚缺当前版本绑定",
                missing=[f"当前参数版本 {current} 的绑定", "在新版本上的复核结论"],
                current_version=current,
                extra={"pinned_version": pinned},
            )

        param_key = _param_key(state["query_id"], revision["config_no"], pinned, revision["params"])
        # 提交确认的瞬间另一路可能正在轮换：先把“即将核对的版本”记下来，提交后再核对
        # 台账轮次和实际版本；任何一项变化都整笔回滚，不计费、不产生产物。
        attempt_version = pinned
        if self.before_commit_guard is not None:
            self.before_commit_guard(revision)
        latest_rotation = (self._rotations.get(revision["config_id"]) or [None])[-1]
        if latest_rotation is not None and latest_rotation["to_version"] == attempt_version:
            latest_rotation = None
        if latest_rotation is not None:
            self._record_conflict(
                revision, "ROTATION_RACE",
                f"确认提交时与配置轮换撞车：{latest_rotation['from_version']}→{latest_rotation['to_version']}"
                f" 由 {latest_rotation['操作人']} 发起",
                latest_rotation["to_version"],
                keep_existing=True,
            )
            revision["status"] = REV_SUBMITTED
            revision["review"] = None
            raise RevisionError(
                "ROTATION_RACE",
                "确认与旧配置轮换同时发生，本次确认已回滚且不计费；请在新版本上重新复核后确认",
                missing=["轮换完成后的稳定版本绑定", "新版本上的复核结论"],
                current_version=latest_rotation["to_version"],
                extra={"rotation": latest_rotation},
            )
        # 轮换可能发生在校验之后，再核对一次实际版本。
        current_after = str(self._config(revision["config_id"]).get("配置版本"))
        if current_after != pinned:
            self._record_conflict(
                revision, "NOISE_VERSION_ROTATED",
                f"确认提交时配置版本已变为 {current_after}", current_after,
                keep_existing=True,
            )
            revision["status"] = REV_SUBMITTED
            revision["review"] = None
            raise RevisionError(
                "NOISE_VERSION_ROTATED",
                f"确认提交时配置版本已变为 {current_after}，本修订绑定 {pinned}",
                missing=[f"当前参数版本 {current_after} 的绑定"],
                current_version=current_after,
            )

        for artifact in state["artifacts"]:
            if artifact["param_key"] == param_key:
                self._record_conflict(
                    revision, "PARAM_VERSION_TAKEN",
                    f"参数版本 {pinned} + 该参数组合已由 {artifact['revision_no']} 产生产物 {artifact['artifact_no']}",
                    pinned,
                )
                raise RevisionError(
                    "PARAM_VERSION_TAKEN",
                    f"参数版本 {pinned} 与该参数组合已对应产物 {artifact['artifact_no']}，参数版本与产物必须一一对应",
                    missing=["一组尚未产生产物的执行参数"],
                    current_version=pinned,
                    extra={"artifact": artifact["artifact_no"], "revision_no": artifact["revision_no"]},
                )

        self._charge_seq += 1
        receipt_no = f"RCPT-{state['query_id']}-{len(state['receipts']) + 1:03d}"
        artifact_no = f"ART-{state['query_id']}-{len(state['artifacts']) + 1:03d}"
        correction_no = f"CORR-{state['query_id']}-{len(state['corrections']) + 1:03d}"
        charge_id = f"CHG-{self._charge_seq:04d}"

        previous = state["effective_revision"]
        previous_result = (
            previous["corrected_result"] if previous else state["row_snapshot"]["原结果"]
        )
        receipt = {
            "receipt_no": receipt_no,
            "revision_no": revision["revision_no"],
            "查询编号": state["row_snapshot"]["查询编号"],
            "参数版本": pinned,
            "产物编号": artifact_no,
            "更正编号": correction_no,
            "执行费用": UNIT_FEE,
            "原执行指纹": state["fingerprint"],
            "状态": "有效",
            "签发人": operator or "噪声引擎",
            "签发时间": _now(),
            "执行资格": {
                "状态": "已核准",
                "参数版本": pinned,
                "允许参数": dict(revision["params"]),
            },
        }
        artifact = {
            "artifact_no": artifact_no,
            "revision_no": revision["revision_no"],
            "param_key": param_key,
            "参数版本": pinned,
            "配置编号": revision["config_no"],
            "生成时间": _now(),
        }
        correction = {
            "correction_no": correction_no,
            "revision_no": revision["revision_no"],
            "参数版本": pinned,
            "产物编号": artifact_no,
            "变更前": previous_result,
            "变更后": revision["corrected_result"],
            "状态": "生效",
            "时间": _now(),
        }
        charge = {
            "流水编号": charge_id,
            "修订号": revision["revision_no"],
            "回执编号": receipt_no,
            "参数版本": pinned,
            "金额": UNIT_FEE,
            "状态": "已计费",
            "时间": _now(),
        }

        revision.update(
            {
                "status": REV_APPROVED,
                "receipt": receipt,
                "artifact": artifact_no,
                "correction": correction_no,
                "charge_id": charge_id,
                "version_produced": pinned,
                "effective": True,
                "conflict": None,
                "approved_at": _now(),
            }
        )
        if previous is not None:
            previous["effective"] = False
            previous["superseded_by"] = revision["revision_no"]
            for item in state["corrections"]:
                if item["correction_no"] == previous["correction"]:
                    item["状态"] = "已被取代"
                    item["superseded_by"] = revision["revision_no"]
        state["effective_revision"] = revision
        state["receipts"].append(receipt)
        state["artifacts"].append(artifact)
        state["corrections"].append(correction)
        state["charges"].append(charge)
        self._events(
            state, "确认修订",
            f"{revision['revision_no']} 按 {pinned} 核准，回执 {receipt_no}，产物 {artifact_no}，计费 {UNIT_FEE}",
            operator or "噪声引擎",
        )
        return {
            "ok": True,
            "duplicate": False,
            "charged": True,
            "message": f"修订 {revision['revision_no']} 已核准，签发回执 {receipt_no}，执行费用 {UNIT_FEE} 已入流水",
            "revision": revision,
            "receipt": receipt,
            "correction": correction,
            "charge": charge,
        }

    # ——————————————— 辅助 ———————————————
    def _revision(self, state: dict[str, Any], revision_no: str) -> dict[str, Any]:
        for revision in state["revisions"]:
            if revision["revision_no"] == revision_no:
                return revision
        raise RevisionError("REVISION_NOT_FOUND", f"修订 {revision_no} 不存在", status_code=404)

    def _record_conflict(
        self, revision: dict[str, Any], code: str, message: str, current_version: str,
        *, keep_existing: bool = False,
    ) -> None:
        """记录本轮冲突；`keep_existing` 用于竞态回滚：保留更早、更具体的冲突码。"""
        entry = {"code": code, "message": message, "current_version": current_version, "time": _now()}
        revision["conflicts"].append(entry)
        if not (keep_existing and revision.get("conflict")):
            revision["conflict"] = entry

    def _sync_readiness(self, state: dict[str, Any], revision: dict[str, Any]) -> None:
        """把最近的冲突整理成“本次提交尚缺什么”，供页面对照。"""
        readiness = self.readiness(state, revision)
        revision["missing"] = readiness["missing"]
        revision["conflict"] = readiness["conflict"]

    def readiness(self, state: dict[str, Any], revision: dict[str, Any]) -> dict[str, Any]:
        if revision["status"] == REV_APPROVED:
            return {"ready": True, "missing": [], "conflict": None}
        if revision["status"] == REV_REJECTED:
            return {
                "ready": False,
                "missing": ["新的修订原因或参数（本次复核已驳回）"],
                "conflict": {"code": "REVIEW_REJECTED", "message": "该修订已被复核驳回，不能确认"},
            }
        if not self._live_unchanged(state):
            return {
                "ready": False,
                "missing": ["与登记指纹一致的已交付原结果"],
                "conflict": {"code": "ORIGINAL_RESULT_REWRITTEN", "message": "已交付原结果被外部改写"},
            }
        config = self._config(revision["config_id"])
        current = str(config.get("配置版本"))
        if revision["status"] == REV_SUBMITTED:
            if revision["pinned_version"] != current:
                return {
                    "ready": False,
                    "missing": [f"当前参数版本 {current} 的绑定", "复核通过结论"],
                    "conflict": {
                        "code": "NOISE_VERSION_ROTATED",
                        "message": f"本修订绑定 {revision['pinned_version']}，配置已轮换到 {current}",
                        "current_version": current,
                    },
                }
            return {
                "ready": False,
                "missing": ["复核通过结论"],
                "conflict": revision["conflict"],
            }
        # 复核通过
        if revision["pinned_version"] != current:
            return {
                "ready": False,
                "missing": [f"当前参数版本 {current} 的绑定", "新版本上的复核结论"],
                "conflict": {
                    "code": "NOISE_VERSION_ROTATED",
                    "message": f"复核版本 {revision['pinned_version']} 已被轮换为 {current}",
                    "current_version": current,
                },
            }
        key = _param_key(state["query_id"], revision["config_no"], revision["pinned_version"], revision["params"])
        for artifact in state["artifacts"]:
            if artifact["param_key"] == key:
                return {
                    "ready": False,
                    "missing": ["一组尚未产生产物的执行参数"],
                    "conflict": {
                        "code": "PARAM_VERSION_TAKEN",
                        "message": f"参数版本 {revision['pinned_version']} 已由 {artifact['revision_no']} 产生产物 {artifact['artifact_no']}",
                        "current_version": revision["pinned_version"],
                        "artifact": artifact["artifact_no"],
                    },
                }
        return {"ready": True, "missing": [], "conflict": None}

    # ——————————————— 读取 ———————————————
    def snapshot(self, query_id: int) -> dict[str, Any]:
        state = self._state(query_id)
        row = store.find(QUERY_MODULE, query_id)
        assert row is not None
        blocked = delivered_block_reason(row)
        config_id = 0
        try:
            config_id = int(str(row.get("噪声配置ID") or 0))
        except ValueError:
            config_id = 0
        config = store.find(NOISE_MODULE, config_id) if config_id else None

        for revision in state["revisions"]:
            revision["missing"] = self.readiness(state, revision)["missing"]
            if revision["status"] != REV_APPROVED:
                revision["conflict"] = self.readiness(state, revision)["conflict"]
            revision["费用"] = UNIT_FEE if revision["status"] == REV_APPROVED else 0.0

        effective = state["effective_revision"]
        # 产物与参数版本一一对应核对：产物键必须唯一，回执必须指向对应产物。
        keys = [artifact["param_key"] for artifact in state["artifacts"]]
        artifact_consistent = len(keys) == len(set(keys)) and all(
            receipt["产物编号"] in {artifact["artifact_no"] for artifact in state["artifacts"]}
            for receipt in state["receipts"]
        )
        return {
            "ok": True,
            "query_id": query_id,
            "query": {
                "查询编号": row.get("查询编号"),
                "查询语句": row.get("查询语句"),
                "查询用户": row.get("查询用户"),
                "status": row.get("status"),
                "噪声配置编号": row.get("噪声配置编号"),
                "budget": row.get("预算剩余"),
            },
            "can_revise": blocked is None,
            "reason_unavailable": blocked,
            "audited": row.get("status") == "已审计",
            "original": {
                "fields": dict(state["row_snapshot"]),
                "fingerprint": state["fingerprint"],
                "immutable": True,
                "unchanged": self._live_unchanged(state),
                "delivered_status": state["delivered_status"],
            },
            "effective_result": (
                {
                    "来源": "更正结果",
                    "修订号": effective["revision_no"],
                    "参数版本": effective["version_produced"],
                    "产物编号": effective["artifact"],
                    "更正编号": effective["correction"],
                    "结果": effective["corrected_result"],
                    "生效时间": effective["approved_at"],
                }
                if effective
                else {
                    "来源": "原结果",
                    "修订号": None,
                    "参数版本": None,
                    "产物编号": None,
                    "更正编号": None,
                    "结果": state["row_snapshot"]["原结果"],
                    "生效时间": None,
                }
            ),
            "noise_config": (
                {
                    "id": config.get("id"),
                    "配置编号": config.get("配置编号"),
                    "status": config.get("status"),
                    "current_version": config.get("配置版本"),
                    "Epsilon值": config.get("Epsilon值"),
                    "噪声机制": config.get("噪声机制"),
                }
                if config
                else None
            ),
            "noise_rotations": list(self._rotations.get(config_id, [])) if config_id else [],
            "revisions": state["revisions"],
            "corrections": state["corrections"],
            "receipts": state["receipts"],
            "charges": state["charges"],
            "artifacts": state["artifacts"],
            "events": state["events"],
            "artifact_consistent": artifact_consistent,
            "totals": {
                "revisions": len(state["revisions"]),
                "approved": sum(1 for revision in state["revisions"] if revision["status"] == REV_APPROVED),
                "corrections": len(state["corrections"]),
                "charges": sum(float(charge["金额"]) for charge in state["charges"]),
            },
        }

    def ledger(self) -> dict[str, Any]:
        return {
            "queries": [self.snapshot(query_id) for query_id in sorted(self._queries)],
        }


ledger = RevisionLedger()
