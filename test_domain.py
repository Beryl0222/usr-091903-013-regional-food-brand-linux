"""领域规则测试：覆盖准入、回避、动态退出、历史保留、去重与数据边界。"""

import tempfile
import unittest
from datetime import date, timedelta

from domain import (
    AUTH_ACTIVE,
    AUTH_SUSPENDED,
    AUTH_WITHDRAWN,
    BrandRegistry,
    DomainError,
    INCIDENT_INSPECTION,
    INCIDENT_IP,
    INCIDENT_SUPPLY,
    PURPOSE_EMPLOYMENT,
    PURPOSE_SURVIVAL,
    PURPOSE_VALUE_ADD,
    STORE_ACTIVE,
    STORE_APPLIED,
    STORE_EXITED,
    STORE_RECTIFYING,
)


class Clock:
    def __init__(self, day):
        self.day = day

    def today(self):
        return self.day

    def advance(self, days):
        self.day += timedelta(days=days)


class RegistryFixture:
    """搭好一个“菜品-食材产区-师傅-评审员”齐备的注册表。"""

    def __init__(self, clock=None):
        self.clock = clock or Clock(date(2026, 3, 1))
        self.r = BrandRegistry(today=self.clock.today)
        self.pepper = self.r.add_ingredient("皱皮辣椒", "贵州遵义产区")
        self.bean = self.r.add_ingredient("豆豉", "贵州毕节产区")
        self.oil = self.r.add_ingredient("菜籽油", "非指定产区", designated=False)
        self.dish = self.r.add_dish("招牌辣子鸡")
        self.other = self.r.add_dish("家常豆腐果")
        self.std = self.r.publish_standard(
            self.dish.id,
            core_steps=["腌制", "油炸", "干炒收汁"],
            ingredient_ids=[self.pepper.id, self.bean.id],
            variants=[{"name": "减辣口", "deviation_note": "辣椒减两成，工序不变"}],
            note="首版",
        )
        self.other_std = self.r.publish_standard(
            self.other.id,
            core_steps=["焖制"],
            ingredient_ids=[self.bean.id],
        )
        self.variant_id = self.std.allowed_variant_ids[0]
        self.master = self.r.add_master("周师傅", "第三代传人")
        self.teacher = self.r.add_master("陈老师傅")
        self.r.add_lineage_relation(self.master.id, self.teacher.id,
                                    "师门亲授")
        self.rev_a = self.r.add_reviewer("评审甲")
        self.rev_b = self.r.add_reviewer("评审乙")
        self.rev_c = self.r.add_reviewer("评审丙")


def submit_full_evidence(r, application_id, requests):
    """按 required_evidence 逐项提交并核实证据。"""
    for item in r.required_evidence(application_id):
        evidence = r.add_evidence(
            application_id=application_id,
            category=item["category"],
            dish_id=item["dish_id"],
            ingredient_id=item.get("ingredient_id"),
            step=item.get("step"),
            doc_ref=f"doc:{item['category']}:{item['dish_id']}:"
                    f"{item.get('ingredient_id') or item.get('step') or 'svc'}",
            supplier="遵义合作社" if item["category"] == "procurement" else "",
        )
        r.verify_evidence(evidence.id, by="secretariat")


def admit_store(fixture, name="老店", requests=None, extra_reviewer_conflict=None):
    r = fixture.r
    requests = requests or [{"dish_id": fixture.dish.id}]
    application = r.apply(
        name=name, address="老街1号", dish_requests=requests,
        master_id=fixture.master.id,
        related_person_ids=["person-owner"],
    )
    submit_full_evidence(r, application.id, requests)
    review = r.start_review(application.id,
                            [fixture.rev_a.id, fixture.rev_b.id,
                             fixture.rev_c.id])
    for reviewer in review.reviewer_ids:
        r.cast_ballot(review.id, reviewer,
                      {req["dish_id"]: "approve" for req in requests})
    result = r.finalize_review(review.id)
    return application, result


class StandardAndLineageTest(unittest.TestCase):
    def setUp(self):
        self.f = RegistryFixture()

    def test_new_version_supersedes_old_but_keeps_allowed_variants(self):
        r, f = self.f.r, self.f
        self.assertEqual(f.std.status, "effective")
        v2 = r.publish_standard(
            f.dish.id, core_steps=["腌制", "低温炸", "干炒收汁"],
            ingredient_ids=[f.pepper.id, f.bean.id],
            variants=[{"name": "减辣口", "deviation_note": "同前"}],
        )
        self.assertEqual(v2.version, 2)
        self.assertEqual(f.std.status, "superseded")
        self.assertEqual(r.current_standard(f.dish.id).id, v2.id)
        # 旧变体挂在旧版本上，不能借新版本继续使用。
        self.assertNotIn(f.variant_id, v2.allowed_variant_ids)

    def test_standard_without_steps_is_refused(self):
        with self.assertRaises(DomainError):
            self.f.r.publish_standard(
                self.f.dish.id, core_steps=[],
                ingredient_ids=[self.f.pepper.id])

    def test_lineage_cannot_point_to_self(self):
        with self.assertRaises(DomainError):
            self.f.r.add_lineage_relation(
                self.f.master.id, self.f.master.id, "自封")


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self.f = RegistryFixture()

    def test_full_evidence_and_ballots_admit_store(self):
        application, result = admit_store(self.f)
        self.assertEqual(application.status, "admitted")
        self.assertEqual(result["admitted"], [self.f.dish.id])
        store = self.f.r.stores[0]
        self.assertEqual(store.status, STORE_ACTIVE)
        auth = self.f.r.authorizations[0]
        self.assertEqual(auth.standard_id, self.f.std.id)
        self.assertEqual(auth.status, AUTH_ACTIVE)

    def test_application_pins_version_and_allowed_variant(self):
        r, f = self.f.r, self.f
        application = r.apply(
            "新店", "新街2号",
            [{"dish_id": f.dish.id, "variant_ids": [f.variant_id]}])
        self.assertEqual(application.dish_requests[0]["standard_id"], f.std.id)
        with self.assertRaises(DomainError):
            r.apply("越界店", "别处",
                    [{"dish_id": f.dish.id, "variant_ids": ["var-nope"]}])

    def test_conflicted_reviewer_must_recuse(self):
        r, f = self.f.r, self.f
        application = r.apply(
            "关联店", "老街3号", [{"dish_id": f.dish.id}],
            master_id=f.master.id, related_person_ids=["person-owner"])
        submit_full_evidence(r, application.id, application.dish_requests)
        biased = r.add_reviewer("关联评审",
                                conflict_person_ids=["person-owner"])
        also_biased = r.add_reviewer(
            "门店关联评审", conflict_store_ids=[application.store_id])
        with self.assertRaises(DomainError) as ctx:
            r.start_review(application.id, [biased.id, f.rev_a.id])
        self.assertIn("回避", str(ctx.exception))
        with self.assertRaises(DomainError):
            r.start_review(application.id, [also_biased.id])
        # 换无冲突评审组后流程正常。
        review = r.start_review(
            application.id, [f.rev_a.id, f.rev_b.id, f.rev_c.id])
        self.assertEqual(len(review.reviewer_ids), 3)

    def test_missing_evidence_narrows_admission_scope_per_dish(self):
        r, f = self.f.r, self.f
        # 两菜同申请，但第二道缺“制作”证据，只能准入第一道。
        application = r.apply(
            "两菜店", "老街4号",
            [{"dish_id": f.dish.id}, {"dish_id": f.other.id}])
        for item in r.required_evidence(application.id):
            if item["dish_id"] == f.other.id and item["category"] == "preparation":
                continue
            evidence = r.add_evidence(
                application_id=application.id, category=item["category"],
                dish_id=item["dish_id"],
                ingredient_id=item.get("ingredient_id"),
                step=item.get("step"),
                doc_ref=f"doc:{item['category']}:{item['dish_id']}:x")
            r.verify_evidence(evidence.id, by="secretariat")
        coverage = r.evidence_coverage(application.id)
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["eligible_dish_ids"], [f.dish.id])
        review = r.start_review(
            application.id, [f.rev_a.id, f.rev_b.id, f.rev_c.id])
        for reviewer in review.reviewer_ids:
            r.cast_ballot(review.id, reviewer,
                          {f.dish.id: "approve", f.other.id: "approve"})
        result = r.finalize_review(review.id)
        self.assertEqual(result["admitted"], [f.dish.id])
        self.assertIsNone(r._active_auth(application.store_id, f.other.id))

    def test_rejected_evidence_does_not_count_as_coverage(self):
        r, f = self.f.r, self.f
        application = r.apply("材料存疑店", "老街5号",
                              [{"dish_id": f.dish.id}])
        items = r.required_evidence(application.id)
        first = items[0]
        ev = r.add_evidence(application_id=application.id,
                            category=first["category"],
                            dish_id=first["dish_id"],
                            ingredient_id=first.get("ingredient_id"),
                            step=first.get("step"), doc_ref="doc-suspect")
        r.reject_evidence(ev.id, by="secretariat")
        coverage = r.evidence_coverage(application.id)
        self.assertTrue(any(
            m["category"] == first["category"] for m in coverage["missing"]))


class IncidentFlowTest(unittest.TestCase):
    def setUp(self):
        self.f = RegistryFixture()
        self.application, _ = admit_store(self.f)
        self.store_id = self.application.store_id
        self.r = self.f.r

    def test_inspection_failure_starts_rectification_then_restores(self):
        r = self.r
        incident = r.open_incident(
            self.store_id, INCIDENT_INSPECTION,
            detail="炸制油温记录缺失", rectification_days=15)
        self.assertEqual(incident.due_on, "2026-03-16")
        self.assertEqual(r.stores[0].status, STORE_RECTIFYING)
        # 整改期间不属于当前有效门店。
        self.assertEqual(r.public_summary()["active_stores"], [])
        r.resolve_incident(incident.id, "rectified", detail="已补温度记录")
        self.assertEqual(r.stores[0].status, STORE_ACTIVE)
        self.assertEqual(len(r.public_summary()["active_stores"]), 1)

    def test_supply_disruption_suspends_only_affected_dish(self):
        r, f = self.r, self.f
        # 再授权第二道菜，验证停权是局部的。
        second = r.apply(
            store_id=self.store_id,
            dish_requests=[{"dish_id": f.other.id}])
        submit_full_evidence(r, second.id, second.dish_requests)
        review = r.start_review(
            second.id, [f.rev_a.id, f.rev_b.id, f.rev_c.id])
        for reviewer in review.reviewer_ids:
            r.cast_ballot(review.id, reviewer, {f.other.id: "approve"})
        r.finalize_review(review.id)

        incident = r.open_incident(
            self.store_id, INCIDENT_SUPPLY, dish_ids=[f.dish.id],
            detail="遵义辣椒供货中断")
        auth_dish = r._active_auth(self.store_id, f.dish.id,
                                   include_suspended=True)
        auth_other = r._active_auth(self.store_id, f.other.id)
        self.assertEqual(auth_dish.status, AUTH_SUSPENDED)
        self.assertEqual(auth_other.status, AUTH_ACTIVE)
        # 全店仍在，但该菜不在对公众的授权菜品里。
        summary = r.public_summary()
        dishes = summary["active_stores"][0]["dishes"]
        self.assertEqual([d["dish"] for d in dishes], ["家常豆腐果"])
        r.resolve_incident(incident.id, "restored")
        self.assertEqual(auth_dish.status, AUTH_ACTIVE)

    def test_ip_dispute_leading_to_exit_keeps_history(self):
        r, f = self.r, self.f
        usage = r.record_logo_use(
            self.store_id, medium="外卖平台", target="某平台品牌专区")
        package = r.record_logo_use(
            self.store_id, medium="零售包装", target="预包装辣子鸡")
        incident = r.open_incident(
            self.store_id, INCIDENT_IP, dish_ids=[f.dish.id],
            detail="他人主张商标权益")
        r.resolve_incident(incident.id, "exit", detail="异议成立")
        store = r.stores[0]
        self.assertEqual(store.status, STORE_EXITED)
        self.assertEqual(store.exit_reason, INCIDENT_IP)
        for auth in r.authorizations:
            self.assertEqual(auth.status, AUTH_WITHDRAWN)
        # 标识渠道在退出日截止，但历史记录仍可查。
        self.assertEqual(usage.ended_on, "2026-03-01")
        history = r.list_logo_usage(self.store_id)
        self.assertEqual({u.target for u in history},
                         {"某平台品牌专区", "预包装辣子鸡"})
        self.assertTrue(all(u.ended_on for u in history))
        # 公众侧只给克制的退出事实。
        exited = r.public_summary()["exited_stores"]
        self.assertEqual(exited, [{
            "name": store.name, "reason": "知识产权异议",
            "exited_on": "2026-03-01"}])

    def test_supply_incident_requires_affected_dishes(self):
        with self.assertRaises(DomainError):
            self.r.open_incident(self.store_id, INCIDENT_SUPPLY,
                                 detail="未指明菜品")

    def test_logo_use_requires_admission(self):
        pending = self.r.apply(
            "待审店", "未准入", [{"dish_id": self.f.dish.id}])
        with self.assertRaises(DomainError):
            self.r.record_logo_use(pending.store_id, "门头", "招牌")


class ConsentAndDedupTest(unittest.TestCase):
    def setUp(self):
        self.f = RegistryFixture()
        self.application, _ = admit_store(self.f)
        self.store_id = self.application.store_id
        self.r = self.f.r

    def grant(self, purposes, expires_on="2026-12-31", granted_on="2026-01-01"):
        return self.r.grant_consent(self.store_id, purposes, expires_on,
                                    granted_on=granted_on)

    def test_ingest_requires_consent_for_purpose_and_period(self):
        r = self.r
        with self.assertRaises(DomainError):
            r.ingest_record(self.store_id, "sales", "2026-02-01",
                            channel="堂食", amount=100, order_no="O1")
        self.grant([PURPOSE_SURVIVAL])
        with self.assertRaises(DomainError):
            # 早于授权起始日的数据不得使用。
            r.ingest_record(self.store_id, "sales", "2025-12-31",
                            channel="堂食", amount=100, order_no="O0")
        ok = r.ingest_record(self.store_id, "sales", "2026-02-01",
                             channel="堂食", amount=100, order_no="O1")
        self.assertFalse(ok["duplicate"])

    def test_same_order_across_channels_counts_once(self):
        r = self.f.r
        self.grant([PURPOSE_SURVIVAL, PURPOSE_VALUE_ADD, PURPOSE_EMPLOYMENT])
        first = r.ingest_record(
            self.store_id, "sales", "2026-02-01",
            channel="堂食", amount=88, order_no="ORD-9")
        # 外卖/零售侧重复汇入同一订单（金额还因平台费用略有差异）。
        dup1 = r.ingest_record(
            self.store_id, "sales", "2026-02-01",
            channel="外卖平台", amount=84.5, order_no="ORD-9")
        dup2 = r.ingest_record(
            self.store_id, "sales", "2026-02-01",
            channel="零售自提", amount=88, order_no="ORD-9")
        self.assertTrue(dup1["duplicate"])
        self.assertTrue(dup2["duplicate"])
        self.assertEqual(dup1["record"].id, first["record"].id)
        metrics = r.effectiveness_metrics(on="2026-03-01")["stores"][0]
        survival = metrics["purposes"][PURPOSE_SURVIVAL]
        self.assertEqual(survival["unique_transactions"], 1)
        self.assertEqual(survival["sales_amount"], 88)

    def test_distinct_orders_and_explicit_keys_are_not_merged(self):
        r = self.r
        self.grant([PURPOSE_SURVIVAL])
        r.ingest_record(self.store_id, "sales", "2026-02-01",
                        channel="堂食", amount=30, order_no="A")
        r.ingest_record(self.store_id, "sales", "2026-02-01",
                        channel="外卖平台", amount=30, order_no="B")
        # 无订单号的两笔同额交易按渠道区分，不误并。
        r.ingest_record(self.store_id, "sales", "2026-02-02",
                        channel="堂食", amount=50)
        r.ingest_record(self.store_id, "sales", "2026-02-02",
                        channel="外卖平台", amount=50)
        survival = r.effectiveness_metrics()["stores"][0]["purposes"][
            PURPOSE_SURVIVAL]
        self.assertEqual(survival["unique_transactions"], 4)

    def test_expired_or_revoked_consent_excludes_data_from_metrics(self):
        r = self.r
        consent = self.grant([PURPOSE_SURVIVAL], expires_on="2026-02-28")
        r.ingest_record(self.store_id, "sales", "2026-02-10",
                        channel="堂食", amount=100, order_no="O1")
        # 授权到期后：历史数据不再用于存续评估。
        self.assertFalse(r.consent_active(self.store_id, PURPOSE_SURVIVAL,
                                          on="2026-03-01"))
        self.assertEqual(r.effectiveness_metrics(on="2026-03-01")["stores"], [])
        # 续期后恢复可见。
        self.grant([PURPOSE_SURVIVAL], expires_on="2027-01-01")
        self.assertEqual(
            r.effectiveness_metrics(on="2026-03-01")["stores"][0]["purposes"]
            [PURPOSE_SURVIVAL]["sales_amount"], 100)
        # 撤销立即生效。
        r.revoke_consent(consent.id)
        newest = [c for c in r.consents if not c.revoked][0]
        r.revoke_consent(newest.id)
        self.assertEqual(r.effectiveness_metrics()["stores"], [])

    def test_metrics_stay_within_authorized_purposes(self):
        r, f = self.r, self.f
        self.grant([PURPOSE_VALUE_ADD, PURPOSE_EMPLOYMENT])
        r.ingest_record(self.store_id, "procurement", "2026-02-01",
                        amount=500, ingredient_id=f.pepper.id)
        r.ingest_record(self.store_id, "procurement", "2026-02-01",
                        amount=200, ingredient_id=f.oil.id)  # 非指定产区
        r.ingest_record(self.store_id, "employment", "2026-02-01",
                        headcount=6)
        # 未授权存续用途：销售数据进不来。
        with self.assertRaises(DomainError):
            r.ingest_record(self.store_id, "sales", "2026-02-01",
                            channel="堂食", amount=999, order_no="X")
        metrics = r.effectiveness_metrics()["stores"][0]["purposes"]
        self.assertNotIn(PURPOSE_SURVIVAL, metrics)
        # 农产品增值只计指定产区食材。
        self.assertEqual(metrics[PURPOSE_VALUE_ADD]
                         ["designated_origin_procurement"], 500)
        self.assertEqual(metrics[PURPOSE_EMPLOYMENT]["latest_headcount"], 6)

    def test_consent_period_must_be_positive(self):
        with self.assertRaises(DomainError):
            self.r.grant_consent(self.store_id, [PURPOSE_SURVIVAL],
                                 expires_on="2026-01-01",
                                 granted_on="2026-01-01")


class PublicSummaryTest(unittest.TestCase):
    def test_summary_is_restrained_and_current(self):
        f = RegistryFixture()
        application, _ = admit_store(f, name="可公开老店")
        r = f.r
        summary = r.public_summary()
        self.assertEqual(summary["as_of"], "2026-03-01")
        standard = next(s for s in summary["current_standards"]
                        if s["dish"] == "招牌辣子鸡")
        self.assertEqual(standard["current_version"], 1)
        self.assertIn("皱皮辣椒:贵州遵义产区",
                      standard["core_ingredient_origins"])
        self.assertEqual(standard["allowed_variants"], ["减辣口"])
        store = summary["active_stores"][0]
        self.assertEqual(set(store), {"name", "dishes"})  # 无地址、数据等
        self.assertEqual(store["dishes"][0]["standard_version"], 1)
        self.assertEqual(summary["exited_stores"], [])


class SnapshotTest(unittest.TestCase):
    def test_save_load_roundtrip_keeps_state(self):
        f = RegistryFixture()
        admit_store(f)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            path = tmp.name
        try:
            f.r.save(path)
            restored = BrandRegistry.load(path, today=f.clock.today)
            self.assertEqual(len(restored.stores), 1)
            self.assertEqual(restored.stores[0].status, STORE_ACTIVE)
            # 计数器续写不撞 id。
            new_dish = restored.add_dish("续载后的菜")
            self.assertTrue(new_dish.id.startswith("dish-"))
            self.assertNotIn(new_dish.id, {d.id for d in restored.dishes[:-1]})
            view = restored.store_view(restored.stores[0].id)
            self.assertEqual(len(view["authorizations"]), 1)
        finally:
            import os
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
