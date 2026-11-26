"""差分查询领域服务：在通用骨架之上补齐噪声配置绑定和原结果登记。

结果修订的流转在 `app/services/result_revision.py`；本服务只负责查询记录本身的
查询、登记与审计动作，保持已交付结果不可被覆盖。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DifferentialSqlService(BaseModuleService):
    MODULE = "differential_sql"
    ENTITY = "查询记录"
    OPERATION_TERM = "查询"
    LIST_FIELDS = [
        "查询编号", "查询语句", "查询用户", "消耗Epsilon", "噪声注入量",
        "返回行数", "执行时间", "预算剩余", "原结果",
    ]
    REQUIRED_FIELDS = ["查询编号", "查询语句", "查询用户"]
    STATUS_ORDER = ["已执行", "预算不足", "已拒绝", "已审计"]
    ACTION_RULES = {"标记拒绝": "已拒绝", "标记审计": "已审计"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记查询时同步绑定噪声配置；原结果按“尚未执行”登记，后续只能追加更正。"""
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        config_no = str(values.get("噪声配置编号") or "").strip()
        config_id = 0
        if config_no:
            matched = [
                row for row in store.rows("noise_engine")
                if str(row.get("配置编号", "")).casefold() == config_no.casefold()
            ]
            if not matched:
                rows = store.rows(self.MODULE)
                rows.remove(entry)
                return None, ["有效的噪声配置编号"]
            config_id = int(matched[0].get("id", 0))
        entry["噪声配置ID"] = config_id
        entry["噪声配置编号"] = config_no
        # 登记只能记录“尚未交付”：交付结果必须来自真实执行，不允许在登记时手工声明。
        entry["原结果"] = "尚未执行，无交付结果"
        # 新登记记录以“已执行”状态落库但没有真实结果，先按预算不足堵住修订入口，
        # 只有带返回行数与噪声注入量的真实交付才允许提交修订。
        if not str(entry.get("返回行数") or "").strip():
            entry["status"] = self.STATUS_ORDER[1]
            entry["pending"] = True
        return entry, []
