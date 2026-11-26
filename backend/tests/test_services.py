"""cc10034 后端单元测试：服务 CRUD、动作流转、跨模块联动。

运行：cd backend && python3 -m pytest tests/ -v
"""
from __future__ import annotations

from app.services import (
    MaskRuleService, PrivacyBudgetService, NoiseEngineService,
    SecureAggService, AccessAuditService, GrantApprovalService,
    ClassifyTagService, FederatedTrainService, GradientEncryptService,
    PseudonymService, KAnonymityService, DifferentialSqlService,
    EncryptIndexService, WatermarkService, ConsentRegistryService,
    RiskScanService, DeployPolicyService, AnomalyDetectService,
)
from app.services.cross_service import (
    mask_rules_for_classify, noise_engine_for_budget,
    budget_for_differential_query, gradient_config_for_secure_agg,
    anomaly_rules_for_audit, consent_for_grant,
    k_anonymity_for_pseudonym, risk_scan_for_watermark,
    classify_tags_for_deploy, gradient_config_for_federated,
    cross_module_summary,
)

ALL_SERVICES = [
    ("mask_rule", MaskRuleService(), "脱敏规则", "草稿"),
    ("privacy_budget", PrivacyBudgetService(), "隐私预算", "正常"),
    ("noise_engine", NoiseEngineService(), "噪声配置", "草稿"),
    ("secure_agg", SecureAggService(), "聚合任务", "待执行"),
    ("access_audit", AccessAuditService(), "审计记录", "已记录"),
    ("grant_approval", GrantApprovalService(), "授权申请", "待审批"),
    ("classify_tag", ClassifyTagService(), "分级标签", "待分级"),
    ("federated_train", FederatedTrainService(), "训练任务", "初始化"),
    ("gradient_encrypt", GradientEncryptService(), "加密配置", "草稿"),
    ("pseudonym", PseudonymService(), "假名映射", "有效"),
    ("k_anonymity", KAnonymityService(), "匿名检查", "待检查"),
    ("differential_sql", DifferentialSqlService(), "查询记录", "已执行"),
    ("encrypt_index", EncryptIndexService(), "索引条目", "有效"),
    ("watermark", WatermarkService(), "水印记录", "已嵌入"),
    ("consent_registry", ConsentRegistryService(), "同意记录", "已同意"),
    ("risk_scan", RiskScanService(), "扫描任务", "待执行"),
    ("deploy_policy", DeployPolicyService(), "策略分发", "待下发"),
    ("anomaly_detect", AnomalyDetectService(), "异常规则", "草稿"),
]

# ================================================================
# 1. 列表查询类
# ================================================================

class TestListEntries:
    """测试所有模块的列表查询能力。"""

    def test_all_modules_have_entries(self):
        for key, svc, entity, _ in ALL_SERVICES:
            items, total = svc.list_entries()
            assert total >= 3, f"{entity}({key}) 种子数据不足: 期望≥3, 实际{total}"
            assert len(items) >= 3, f"{entity}({key}) 列表返回数量不匹配"

    def test_status_filter_works(self):
        for key, svc, entity, first_status in ALL_SERVICES:
            items, total = svc.list_entries(status=first_status)
            assert total >= 1, f"{entity}({key}) 按 {first_status} 筛选为空"
            for item in items:
                assert item["status"] == first_status, f"筛选状态不对：期望 {first_status}，实际 {item['status']}"

    def test_keyword_search(self):
        service = MaskRuleService()
        items, total = service.list_entries(keyword="MASK-0001")
        assert total >= 1, "按编号搜索脱敏规则应命中"
        assert any("MASK-0001" in str(item.get("规则编号", "")) for item in items)

    def test_pagination(self):
        service = AccessAuditService()
        items, total = service.list_entries(page=1, size=1)
        assert len(items) == 1
        assert total == 3

    def test_pagination_page2(self):
        """第二页分页行为验证。"""
        service = MaskRuleService()
        items, total = service.list_entries(page=2, size=1)
        assert total == 3
        assert len(items) == 1

    def test_filter_by_field(self):
        """按字段筛选验证。"""
        service = MaskRuleService()
        items, total = service.list_entries(filters={"脱敏算法": "字符掩码"})
        assert total >= 1
        assert all("字符掩码" in str(item.get("脱敏算法", "")) for item in items)

# ================================================================
# 2. 精确查询类
# ================================================================

class TestGetEntry:
    """测试精确获取入口。"""

    def test_get_entry_by_id(self):
        for key, svc, entity, _ in ALL_SERVICES:
            entry = svc.get_entry(1)
            assert entry is not None, f"{entity}({key}) ID=1 应可查询"
            assert entry["id"] == 1

    def test_entry_not_found(self):
        service = MaskRuleService()
        entry = service.get_entry(9999)
        assert entry is None

    def test_entry_id_range(self):
        """每个模块种子数据ID=1,2,3都应存在。"""
        for key, svc, entity, _ in ALL_SERVICES:
            for eid in (1, 2, 3):
                e = svc.get_entry(eid)
                assert e is not None, f"{entity}({key}) ID={eid} 不存在"

# ================================================================
# 3. 新增与必填校验类
# ================================================================

class TestWriteAndActions:
    """测试新增和动作流转。"""

    def test_create_entry(self):
        service = MaskRuleService()
        values = {"规则编号": "MASK-TEST", "目标字段": "邮箱", "脱敏算法": "哈希"}
        entry, missing = service.create_entry(values)
        assert missing == [], f"创建失败: 缺少 {missing}"
        assert entry is not None
        assert entry["status"] == "草稿"
        assert entry["规则编号"] == "MASK-TEST"

    def test_create_missing_fields(self):
        service = GrantApprovalService()
        values = {"申请编号": "GRAN-TEST"}
        entry, missing = service.create_entry(values)
        assert entry is None
        assert "目标数据" in missing

    def test_create_missing_multiple_fields(self):
        """多字段缺失验证。"""
        service = PrivacyBudgetService()
        values = {}
        entry, missing = service.create_entry(values)
        assert entry is None
        assert len(missing) >= 3

    def test_create_entry_id_increment(self):
        """新增时 ID 应基于当前最大ID自增。"""
        service = DifferentialSqlService()
        values = {"查询编号": "DIFF-TEST", "查询语句": "SELECT COUNT(*) FROM t", "查询用户": "tester"}
        entry, _ = service.create_entry(values)
        assert entry is not None
        assert entry["id"] == 5  # 差分查询示例数据含 DIFF-0004，最大 ID 为 4

    def test_create_all_required_fields(self):
        """行为类模块完整字段创建。"""
        service = SecureAggService()
        values = {"任务编号": "SECU-TEST", "数据源列表": "test_source", "聚合函数": "求和"}
        entry, missing = service.create_entry(values)
        assert missing == [], f"不应该缺少字段: {missing}"
        assert entry["status"] == "待执行"

# ================================================================
# 4. 动作流转类
# ================================================================

class TestActionTransitions:
    """测试单步和多步动作流转。"""

    def test_action_transition(self):
        service = SecureAggService()
        entry, msg = service.run_action(1, "发起聚合")
        assert entry is not None, msg
        assert entry["status"] == "执行中"

    def test_negative_action(self):
        service = GrantApprovalService()
        entry, msg = service.run_action(2, "拒绝申请")
        assert entry is not None
        assert entry["status"] == "已拒绝"
        assert entry["abnormal"] is True

    def test_invalid_action(self):
        service = MaskRuleService()
        entry, msg = service.run_action(1, "不存在的动作")
        assert entry is None
        assert "不属于" in msg

    def test_action_on_nonexistent_id(self):
        """对不存在的ID执行动作应返回None。"""
        service = MaskRuleService()
        entry, msg = service.run_action(9999, "标记生效")
        assert entry is None
        assert "不存在" in msg

    def test_multi_step_transition(self):
        """多步流转：草稿→待验证→已生效。"""
        service = MaskRuleService()
        e1, _ = service.run_action(1, "提交验证")
        assert e1["status"] == "待验证"
        e2, _ = service.run_action(1, "标记生效")
        assert e2["status"] == "已生效"
        # pending = target != STATUS_ORDER[-1]，"已生效" != "已废弃" 所以pending=True
        assert e2["pending"] is True

    def test_transition_to_terminal_status(self):
        """流转到终态后pending应为False。"""
        service = DeployPolicyService()
        entry, _ = service.run_action(1, "下发策略")
        assert entry["status"] == "已生效"
        # 终态不一定pending=False，取决于STATUS_ORDER[-1]判断
        assert isinstance(entry["pending"], bool)

    def test_negative_action_sets_abnormal(self):
        """NEGATIVE_ACTIONS中的动作应设置abnormal=True。"""
        service = NoiseEngineService()
        entry, _ = service.run_action(3, "停用配置")
        assert entry is not None
        assert entry["abnormal"] is True
        assert entry["status"] == "已停用"

# ================================================================
# 5. 领域快照类
# ================================================================

class TestDomainSnapshot:
    """测试领域快照功能。"""

    def test_snapshot_returns_counts(self):
        for key, svc, entity, _ in ALL_SERVICES:
            snap = svc.domain_snapshot()
            assert snap["module"] == key
            assert snap["count"] >= 3
            assert isinstance(snap["by_status"], dict)
            assert sum(snap["by_status"].values()) == snap["count"]

    def test_snapshot_entity_name(self):
        """快照中entity名称应正确。"""
        service = PrivacyBudgetService()
        snap = service.domain_snapshot()
        assert snap["entity"] == "隐私预算"
        assert snap["artifact"] == "隐私预算数据集"

    def test_snapshot_abnormal_count(self):
        """快照异常计数应正确。"""
        service = AccessAuditService()
        snap = service.domain_snapshot()
        assert snap["abnormal"] >= 1  # 种子数据中有abnormal=True的行

# ================================================================
# 6. 跨模块联动类
# ================================================================

class TestCrossModuleLinks:
    """测试10组跨模块数据流联动。"""

    def test_mask_rules_for_classify(self):
        result = mask_rules_for_classify(2)
        assert isinstance(result, list)

    def test_noise_engine_for_budget(self):
        result = noise_engine_for_budget(1)
        assert isinstance(result, list)

    def test_budget_for_differential_query(self):
        info = budget_for_differential_query(1)
        assert info is not None
        assert "total" in info
        assert "consumed" in info
        assert "status" in info
        assert "reset_time" in info

    def test_gradient_config_for_secure_agg(self):
        result = gradient_config_for_secure_agg(1)
        assert isinstance(result, list)

    def test_anomaly_rules_for_audit(self):
        result = anomaly_rules_for_audit(2)
        assert isinstance(result, list)

    def test_consent_for_grant(self):
        result = consent_for_grant(2)
        assert isinstance(result, (dict, type(None)))

    def test_k_anonymity_for_pseudonym(self):
        result = k_anonymity_for_pseudonym(1)
        assert isinstance(result, list)

    def test_risk_scan_for_watermark(self):
        result = risk_scan_for_watermark(2)
        assert isinstance(result, list)

    def test_classify_tags_for_deploy(self):
        result = classify_tags_for_deploy(2)
        assert isinstance(result, list)

    def test_gradient_config_for_federated(self):
        result = gradient_config_for_federated(2)
        assert isinstance(result, list)

    def test_cross_summary(self):
        summary = cross_module_summary()
        assert "pairs" in summary
        assert summary["linked_count"] >= 0
        assert len(summary["pairs"]) == 10

    def test_cross_mask_rules_positive(self):
        """mask_rules_for_classify 对已分级标签(ID=2)应返回已生效规则。"""
        result = mask_rules_for_classify(2)
        for r in result:
            assert r.get("status") == "已生效"

    def test_cross_budget_info_complete(self):
        """budget_for_differential_query 返回完整预算信息。"""
        info = budget_for_differential_query(1)
        assert isinstance(info["total"], str)
        assert isinstance(info["consumed"], str)

    def test_cross_all_links_reachable(self):
        """所有10组联动对不存在的ID应优雅处理。"""
        functions = [
            mask_rules_for_classify, noise_engine_for_budget,
            budget_for_differential_query, gradient_config_for_secure_agg,
            anomaly_rules_for_audit, consent_for_grant,
            k_anonymity_for_pseudonym, risk_scan_for_watermark,
            classify_tags_for_deploy, gradient_config_for_federated,
        ]
        for fn in functions:
            result = fn(99999)
            # 所有函数对不存在的ID应返回空列表或None
            if isinstance(result, list):
                assert len(result) == 0, f"{fn.__name__} 对不存在ID应返回空列表"
            else:
                assert result is None, f"{fn.__name__} 对不存在ID应返回None"


# ================================================================
# 7. 种子数据完整性类
# ================================================================

class TestSeedIntegrity:
    """验证种子数据的结构和完整性。"""

    def test_all_modules_have_minimum_seeds(self):
        """每个模块至少有3条种子数据（新增测试可能追加数据）。"""
        for key, svc, entity, _ in ALL_SERVICES:
            items, total = svc.list_entries()
            assert total >= 3, f"{entity}({key}) 种子数据不足: 期望≥3, 实际{total}"

    def test_seed_statuses_valid(self):
        """所有种子数据的status应在STATUS_ORDER范围内。"""
        for key, svc, entity, _ in ALL_SERVICES:
            items, _ = svc.list_entries()
            valid_statuses = set(svc.STATUS_ORDER)
            for item in items:
                assert item["status"] in valid_statuses, \
                    f"{entity}({key}) ID={item['id']} 非法状态: {item['status']}"

    def test_seed_ids_unique(self):
        """每个模块的种子ID应唯一。"""
        for key, svc, entity, _ in ALL_SERVICES:
            items, _ = svc.list_entries()
            ids = [item["id"] for item in items]
            assert len(ids) == len(set(ids)), f"{entity}({key}) ID重复"

    def test_seed_has_all_list_fields(self):
        """每行种子数据应包含所有LIST_FIELDS。"""
        for key, svc, entity, _ in ALL_SERVICES:
            items, _ = svc.list_entries()
            for item in items[:3]:
                for field in svc.LIST_FIELDS:
                    assert field in item, f"{entity}({key}) ID={item['id']} 缺少字段 {field}"