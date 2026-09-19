"""验证品牌准入领域核心：标准、评审、处置、去重统计与数据授权。"""

import unittest
from datetime import date, timedelta

from brand import (
    ACTION_EXIT,
    ACTION_RECTIFY,
    ACTION_SUSPEND_SCOPE,
    PURPOSE_AGRI_VALUE_ADD,
    PURPOSE_CONTINUATION,
    PURPOSE_EMPLOYMENT,
    STATUS_ACTIVE,
    STATUS_EXITED,
    STATUS_RECTIFYING,
    BrandRegistry,
    DataUseNotAuthorized,
)

TODAY = date(2026, 9, 19)


def make_registry():
    registry = BrandRegistry()
    registry.register_standard(
        "牛肉粉",
        "v1",
        spec={"broth_hours": 8},
        allowed_variants=("微辣", "加酸"),
        core_ingredients=[{"name": "黄牛后腿肉", "region": "本地山区"}],
    )
    registry.register_standard(
        "米豆腐",
        "v2",
        spec={"stone_ground": True},
        allowed_variants=("凉拌",),
        core_ingredients=[("籼米", "河谷稻区")],
    )
    return registry


def submit_full_evidence(registry, store_id, scope):
    return registry.submit_application(
        store_id,
        scope,
        [
            {"kind": "procurement", "reference": "po-2026-01"},
            {"kind": "production", "reference": "video-2026-01"},
            {"kind": "service", "reference": "svc-check-01"},
        ],
    )


def approve_store(registry, store_id="store-1", scope=None):
    scope = scope or [("牛肉粉", "v1")]
    app_id = submit_full_evidence(registry, store_id, scope)
    assert registry.review_application(app_id, {"r1": True, "r2": True, "r3": False})
    return app_id


class StandardAndLineageTest(unittest.TestCase):
    def test_standard_keeps_variants_and_regions(self):
        registry = make_registry()
        standard = registry.standard("牛肉粉", "v1")
        self.assertEqual(standard.allowed_variants, ("微辣", "加酸"))
        self.assertEqual(standard.core_ingredients[0].region, "本地山区")

    def test_core_ingredient_requires_region(self):
        registry = BrandRegistry()
        with self.assertRaises(ValueError):
            registry.register_standard(
                "米粉", "v1", spec={}, core_ingredients=[{"name": "大米", "region": ""}]
            )

    def test_lineage_records_master_apprentice(self):
        registry = make_registry()
        registry.record_lineage("张师傅", "李徒弟", "牛肉粉")
        self.assertEqual(
            registry.lineage(),
            [{"master": "张师傅", "apprentice": "李徒弟", "dish": "牛肉粉"}],
        )


class ApplicationAndReviewTest(unittest.TestCase):
    def test_application_requires_all_evidence_kinds(self):
        registry = make_registry()
        with self.assertRaises(ValueError):
            registry.submit_application(
                "store-1",
                [("牛肉粉", "v1")],
                [
                    {"kind": "procurement", "reference": "po-1"},
                    {"kind": "production", "reference": "vid-1"},
                ],
            )

    def test_application_requires_verifiable_evidence(self):
        registry = make_registry()
        with self.assertRaises(ValueError):
            registry.submit_application(
                "store-1",
                [("牛肉粉", "v1")],
                [
                    {"kind": "procurement", "reference": "po-1"},
                    {"kind": "production", "reference": "vid-1"},
                    {"kind": "service", "reference": "svc-1", "verifiable": False},
                ],
            )

    def test_application_rejects_unregistered_standard(self):
        registry = make_registry()
        with self.assertRaises(ValueError):
            submit_full_evidence(registry, "store-1", [("牛肉粉", "v9")])

    def test_review_recuses_conflicted_reviewer(self):
        registry = make_registry()
        app_id = submit_full_evidence(registry, "store-1", [("牛肉粉", "v1")])
        approved = registry.review_application(
            app_id,
            {"r1": True, "r2": True, "r3": False, "r4": False},
            conflicted={"r4"},
        )
        self.assertTrue(approved)
        self.assertEqual(
            registry.effective_scope("store-1"), frozenset({("牛肉粉", "v1")})
        )

    def test_review_waits_when_recusal_breaks_quorum(self):
        registry = make_registry()
        app_id = submit_full_evidence(registry, "store-1", [("牛肉粉", "v1")])
        decided = registry.review_application(
            app_id, {"r1": True, "r2": True, "r3": True}, conflicted={"r1"}
        )
        self.assertFalse(decided)
        self.assertEqual(registry.application(app_id).status, "pending")
        self.assertIsNone(registry.authorization("store-1"))

    def test_rejection_grants_no_scope(self):
        registry = make_registry()
        app_id = submit_full_evidence(registry, "store-1", [("牛肉粉", "v1")])
        approved = registry.review_application(
            app_id, {"r1": True, "r2": False, "r3": False}
        )
        self.assertFalse(approved)
        self.assertEqual(registry.application(app_id).status, "rejected")
        self.assertEqual(registry.effective_scope("store-1"), frozenset())


class EnforcementTest(unittest.TestCase):
    def test_rectify_requires_deadline(self):
        registry = make_registry()
        approve_store(registry)
        with self.assertRaises(ValueError):
            registry.apply_enforcement(
                "store-1", "spot_check_failed", ACTION_RECTIFY
            )

    def test_rectify_then_resolve(self):
        registry = make_registry()
        approve_store(registry)
        registry.apply_enforcement(
            "store-1",
            "spot_check_failed",
            ACTION_RECTIFY,
            deadline=TODAY + timedelta(days=30),
        )
        auth = registry.authorization("store-1")
        self.assertEqual(auth.status, STATUS_RECTIFYING)
        self.assertEqual(auth.rectify_deadline, TODAY + timedelta(days=30))
        registry.resolve_rectification("store-1")
        self.assertEqual(registry.authorization("store-1").status, STATUS_ACTIVE)

    def test_partial_suspension_narrows_effective_scope(self):
        registry = make_registry()
        approve_store(registry, scope=[("牛肉粉", "v1"), ("米豆腐", "v2")])
        registry.apply_enforcement(
            "store-1",
            "supply_interrupted",
            ACTION_SUSPEND_SCOPE,
            dishes=[("米豆腐", "v2")],
        )
        self.assertEqual(
            registry.effective_scope("store-1"), frozenset({("牛肉粉", "v1")})
        )

    def test_suspension_outside_granted_scope_rejected(self):
        registry = make_registry()
        approve_store(registry)
        with self.assertRaises(ValueError):
            registry.apply_enforcement(
                "store-1",
                "ip_dispute",
                ACTION_SUSPEND_SCOPE,
                dishes=[("米豆腐", "v2")],
            )

    def test_exit_keeps_history_and_public_reason(self):
        registry = make_registry()
        approve_store(registry)
        registry.ingest_transaction(
            "txn-1", "store-1", "delivery", 32.0, [("牛肉粉", "v1")], at=TODAY
        )
        registry.apply_enforcement(
            "store-1", "ip_dispute", ACTION_EXIT, reason="商标异议成立"
        )
        auth = registry.authorization("store-1")
        self.assertEqual(auth.status, STATUS_EXITED)
        self.assertEqual(registry.effective_scope("store-1"), frozenset())
        history = registry.mark_history("store-1")
        self.assertEqual(
            {record["event"] for record in history},
            {"authorized", "mark_used", ACTION_EXIT},
        )
        self.assertEqual(
            registry.public_summary()["exits"],
            [{"store_id": "store-1", "reason": "商标异议成立"}],
        )
        with self.assertRaises(ValueError):
            registry.ingest_transaction(
                "txn-2", "store-1", "retail", 20.0, [("牛肉粉", "v1")]
            )


class TransactionDedupeTest(unittest.TestCase):
    def test_same_transaction_across_channels_counted_once(self):
        registry = make_registry()
        approve_store(registry)
        first = registry.ingest_transaction(
            "txn-1", "store-1", "delivery", 32.0, [("牛肉粉", "v1")], at=TODAY
        )
        second = registry.ingest_transaction(
            "txn-1", "store-1", "dine_in", 32.0, [("牛肉粉", "v1")], at=TODAY
        )
        third = registry.ingest_transaction(
            "txn-1", "store-1", "retail", 32.0, [("牛肉粉", "v1")], at=TODAY
        )
        self.assertEqual((first, second, third), (True, False, False))
        self.assertEqual(len(registry.counted_transactions("store-1")), 1)

    def test_transaction_outside_scope_rejected(self):
        registry = make_registry()
        approve_store(registry)
        with self.assertRaises(ValueError):
            registry.ingest_transaction(
                "txn-1", "store-1", "retail", 20.0, [("米豆腐", "v2")]
            )


class DataUseAndEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.registry = make_registry()
        approve_store(self.registry)
        self.registry.ingest_transaction(
            "txn-1", "store-1", "delivery", 32.0, [("牛肉粉", "v1")], at=TODAY
        )
        self.registry.ingest_transaction(
            "txn-1", "store-1", "retail", 32.0, [("牛肉粉", "v1")], at=TODAY
        )

    def test_evaluation_requires_authorized_purpose(self):
        with self.assertRaises(DataUseNotAuthorized):
            self.registry.evaluate("store-1", PURPOSE_AGRI_VALUE_ADD, TODAY)

    def test_evaluation_rejects_expired_grant(self):
        self.registry.grant_data_use(
            "store-1",
            PURPOSE_AGRI_VALUE_ADD,
            TODAY - timedelta(days=30),
            TODAY - timedelta(days=1),
        )
        with self.assertRaises(DataUseNotAuthorized):
            self.registry.evaluate("store-1", PURPOSE_AGRI_VALUE_ADD, TODAY)

    def test_evaluation_within_grant_uses_deduped_transactions(self):
        self.registry.grant_data_use(
            "store-1",
            PURPOSE_AGRI_VALUE_ADD,
            TODAY - timedelta(days=1),
            TODAY + timedelta(days=30),
        )
        result = self.registry.evaluate("store-1", PURPOSE_AGRI_VALUE_ADD, TODAY)
        self.assertEqual(result, {"unique_transactions": 1, "gross_sales": 32.0})

    def test_continuation_reflects_store_status(self):
        self.registry.grant_data_use(
            "store-1", PURPOSE_CONTINUATION, TODAY, TODAY + timedelta(days=30)
        )
        result = self.registry.evaluate("store-1", PURPOSE_CONTINUATION, TODAY)
        self.assertEqual(result, {"active": True, "unique_transactions": 1})

    def test_employment_recording_requires_authorized_period(self):
        with self.assertRaises(DataUseNotAuthorized):
            self.registry.record_employment("store-1", 6, TODAY)
        self.registry.grant_data_use(
            "store-1", PURPOSE_EMPLOYMENT, TODAY, TODAY + timedelta(days=30)
        )
        self.registry.record_employment("store-1", 6, TODAY)
        result = self.registry.evaluate("store-1", PURPOSE_EMPLOYMENT, TODAY)
        self.assertEqual(result, {"headcount": 6})


class PublicSummaryTest(unittest.TestCase):
    def test_summary_is_restrained(self):
        registry = make_registry()
        approve_store(registry)
        approve_store(registry, store_id="store-2", scope=[("米豆腐", "v2")])
        registry.apply_enforcement(
            "store-2", "spot_check_failed", ACTION_EXIT, reason="复查仍不合格"
        )
        summary = registry.public_summary()
        self.assertEqual(summary["stores"], ["store-1"])
        self.assertEqual(
            summary["standards"],
            [
                {"dish": "牛肉粉", "version": "v1"},
                {"dish": "米豆腐", "version": "v2"},
            ],
        )
        for entry in summary["standards"]:
            self.assertNotIn("spec", entry)
            self.assertNotIn("core_ingredients", entry)
        self.assertEqual(
            summary["exits"], [{"store_id": "store-2", "reason": "复查仍不合格"}]
        )


if __name__ == "__main__":
    unittest.main()
