"""结果修订域测试：不可覆盖、幂等计费、版本/产物一一对应、轮换交错。"""
from __future__ import annotations

from fastapi.testclient import TestClient

PARAMS = {
    "消耗Epsilon": "0.5",
    "噪声注入量": "Binomial(40,0.5)-重算",
    "返回行数": "1",
    "执行时间": "2026-11-26 20:10",
}


def panel(client: TestClient, query_id: int = 1) -> dict:
    response = client.get(f"/api/revisions/queries/{query_id}")
    assert response.status_code == 200
    return response.json()


def submit(client: TestClient, query_id: int = 1, **overrides) -> dict:
    body = {
        "values": {
            "原执行核对值": panel(client, query_id)["original"]["fingerprint"],
            "修订原因": "原执行把 AVG 误取为 COUNT 的参数",
            "更正结果": "计数=1842，均值=341.05",
            "noise_config_id": 3,
            "noise_version": "v2.0",
            **PARAMS,
        }
    }
    body["values"].update(overrides)
    return client.post(f"/api/revisions/queries/{query_id}/submit", json=body)


def review(client: TestClient, revision_no: str, verdict: str = "通过", query_id: int = 1) -> dict:
    return client.post(
        f"/api/revisions/queries/{query_id}/revisions/{revision_no}/review",
        json={"verdict": verdict, "reviewer": "数据安全组", "note": "参数与噪声配置已核对"},
    )


def confirm(client: TestClient, revision_no: str, query_id: int = 1) -> dict:
    return client.post(
        f"/api/revisions/queries/{query_id}/revisions/{revision_no}/confirm",
        json={"operator": "噪声引擎"},
    )


def rotate(client: TestClient, version: str, config_id: int = 3) -> dict:
    return client.post(
        f"/api/revisions/noise-configs/{config_id}/rotate",
        json={"target_version": version, "reason": "敏感度复核后收紧参数", "operator": "噪声引擎"},
    )


def test_only_delivered_results_can_be_revised(client: TestClient) -> None:
    """未执行、被拒绝、返回 0 行的记录都不能修订。"""
    for query_id in (2, 3):
        response = submit(client, query_id)
        assert response.status_code == 409
        assert response.json()["conflict"]["code"] == "RESULT_NOT_DELIVERED"
    assert panel(client, 2)["can_revise"] is False
    assert panel(client, 4)["can_revise"] is True  # 已审计也算交付


def test_revision_requires_every_input(client: TestClient) -> None:
    response = submit(client, 1, 修订原因="", noise_version="")
    assert response.status_code == 409
    conflict = response.json()["conflict"]
    assert conflict["code"] == "REVISION_INPUT_MISSING"
    assert "修订原因" in conflict["missing"]
    assert "配置版本" in conflict["missing"]


def test_original_check_value_must_match(client: TestClient) -> None:
    response = submit(client, 1, 原执行核对值="DIFF-0001#deadbeef0000")
    assert response.status_code == 409
    assert response.json()["conflict"]["code"] == "ORIGINAL_MISMATCH"
    assert response.json()["conflict"]["fingerprint"]


def test_revision_never_overwrites_delivered_result(client: TestClient, revision_ledger) -> None:
    """修订确认后原行保持原值；若原行被外部改写，后续流转直接拒绝。"""
    original = panel(client)["original"]["fields"]
    revision_no = submit(client).json()["revision"]["revision_no"]
    assert review(client, revision_no).status_code == 200
    assert confirm(client, revision_no).status_code == 200

    from app.store import store

    row = store.find("differential_sql", 1)
    for field, value in original.items():
        assert str(row[field]) == value
    assert panel(client)["effective_result"]["来源"] == "更正结果"

    row["原结果"] = "计数=9999（被人为覆盖）"
    rewritten = panel(client)
    assert rewritten["original"]["unchanged"] is False
    response = submit(client, 1, 修订原因="第二条修订")
    assert response.status_code == 409
    assert response.json()["conflict"]["code"] == "ORIGINAL_RESULT_REWRITTEN"


def test_duplicate_submit_and_confirm_charge_once(client: TestClient) -> None:
    first = submit(client)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    revision_no = first.json()["revision"]["revision_no"]

    second = submit(client)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["revision"]["revision_no"] == revision_no

    assert review(client, revision_no).status_code == 200
    confirmed = confirm(client, revision_no).json()
    assert confirmed["charged"] is True
    assert confirmed["receipt"]["receipt_no"]

    repeated = confirm(client, revision_no).json()
    assert repeated["charged"] is False
    assert repeated["duplicate"] is True
    assert repeated["receipt"]["receipt_no"] == confirmed["receipt"]["receipt_no"]

    state = panel(client)
    assert len(state["receipts"]) == 1
    assert len(state["charges"]) == 1
    assert state["totals"]["charges"] == 1.0


def test_param_version_maps_to_exactly_one_artifact(client: TestClient) -> None:
    """同一参数版本 + 同一参数组合只能产出一个产物，第二次确认被拒。"""
    first = submit(client).json()["revision"]
    second = submit(client, 1, 修订原因="换一个理由但参数不变").json()["revision"]
    assert first["revision_no"] != second["revision_no"]

    assert review(client, first["revision_no"]).status_code == 200
    assert confirm(client, first["revision_no"]).status_code == 200
    assert review(client, second["revision_no"]).status_code == 200
    blocked = confirm(client, second["revision_no"])
    assert blocked.status_code == 409
    conflict = blocked.json()["conflict"]
    assert conflict["code"] == "PARAM_VERSION_TAKEN"
    assert conflict["artifact"] == panel(client)["artifacts"][0]["artifact_no"]

    state = panel(client)
    assert state["artifact_consistent"] is True
    keys = [artifact["param_key"] for artifact in state["artifacts"]]
    assert len(keys) == len(set(keys))


def test_version_must_match_current_config(client: TestClient) -> None:
    rotate(client, "v2.1")
    response = submit(client, 1, noise_version="v2.0")
    assert response.status_code == 409
    conflict = response.json()["conflict"]
    assert conflict["code"] == "NOISE_VERSION_ROTATED"
    assert conflict["current_version"] == "v2.1"
    assert conflict["pinned_version"] == "v2.0"


def test_rotation_conflict_surfaces_missing_and_version(client: TestClient) -> None:
    """复核通过后配置被轮换：确认报冲突，页面能看到版本，也能改绑后重新复核。"""
    revision_no = submit(client).json()["revision"]["revision_no"]
    assert review(client, revision_no).status_code == 200
    rotate(client, "v2.2")

    blocked = confirm(client, revision_no)
    assert blocked.status_code == 409
    conflict = blocked.json()["conflict"]
    assert conflict["code"] == "NOISE_VERSION_ROTATED"
    assert conflict["current_version"] == "v2.2"
    assert any("v2.2" in item for item in conflict["missing"])

    revision = next(r for r in panel(client)["revisions"] if r["revision_no"] == revision_no)
    assert revision["status"] == "已提交"
    assert revision["review"] is None
    assert revision["conflict"]["current_version"] == "v2.2"

    rebind = client.post(
        f"/api/revisions/queries/1/revisions/{revision_no}/rebind",
        json={"version": "v2.2", "reason": "轮换后重新绑定", "operator": "数据工程组"},
    )
    assert rebind.status_code == 200
    assert rebind.json()["revision"]["pinned_version"] == "v2.2"
    assert review(client, revision_no).status_code == 200
    assert confirm(client, revision_no).json()["receipt"]["参数版本"] == "v2.2"


def test_confirm_while_rotation_happens_is_rolled_back(client: TestClient, revision_ledger) -> None:
    """确认提交瞬间撞上另一路配置轮换：确认回滚、不计费、不产生产物。"""
    revision_no = submit(client).json()["revision"]["revision_no"]
    assert review(client, revision_no).status_code == 200

    from app.store import store

    def concurrent_rotation(_revision: dict) -> None:
        config = store.find("noise_engine", 3)
        config["配置版本"] = "v2.3"

    revision_ledger.before_commit_guard = concurrent_rotation
    blocked = confirm(client, revision_no)
    assert blocked.status_code == 409
    # 另一路是直接写 store 的版本变更（未走台账轮换），确认端按实际版本拦截。
    conflict = blocked.json()["conflict"]
    assert conflict["code"] == "NOISE_VERSION_ROTATED"
    assert conflict["current_version"] == "v2.3"
    assert conflict["missing"]
    revision = next(r for r in panel(client)["revisions"] if r["revision_no"] == revision_no)
    assert revision["status"] == "已提交"
    assert revision["conflicts"][-1]["code"] == "NOISE_VERSION_ROTATED"
    assert revision["conflicts"][-1]["current_version"] == "v2.3"

    state = panel(client)
    assert state["charges"] == []
    assert state["artifacts"] == []
    assert state["totals"]["charges"] == 0


def test_ledger_rotation_during_commit_reports_race(client: TestClient, revision_ledger) -> None:
    """确认提交时另一路通过台账轮换了旧配置：报 ROTATION_RACE 并告知轮换明细。"""
    revision_no = submit(client).json()["revision"]["revision_no"]
    assert review(client, revision_no).status_code == 200
    revision_ledger.before_commit_guard = lambda _revision: revision_ledger.rotate(
        3, target_version="v2.4", reason="并发轮换", operator="值班运维",
    )
    blocked = confirm(client, revision_no)
    assert blocked.status_code == 409
    conflict = blocked.json()["conflict"]
    assert conflict["code"] == "ROTATION_RACE"
    assert conflict["rotation"]["to_version"] == "v2.4"
    assert conflict["current_version"] == "v2.4"
    assert panel(client)["charges"] == []


def test_cannot_confirm_without_review(client: TestClient) -> None:
    revision_no = submit(client).json()["revision"]["revision_no"]
    blocked = confirm(client, revision_no)
    assert blocked.status_code == 409
    assert blocked.json()["conflict"]["code"] == "REVIEW_MISSING"
    assert "复核结论（通过）" in blocked.json()["conflict"]["missing"]


def test_rotation_guards(client: TestClient) -> None:
    assert rotate(client, "v2.0").json()["conflict"]["code"] == "ROTATION_VERSION_UNCHANGED"
    assert rotate(client, "v2.1").status_code == 200
    assert rotate(client, "v2.0").json()["conflict"]["code"] == "ROTATION_VERSION_REUSED"
    rotations = client.get("/api/revisions/noise-configs/3/rotations").json()
    assert [item["to_version"] for item in rotations["rotations"]] == ["v2.1"]
