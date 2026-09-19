"""验证服务契约：健康检查与理事会端到端流程。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import Handler, SERVICE_ID, SERVICE_NAME, health_payload


def request_json(base_url, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        body = json.load(error)
        error.close()
        return error.code, body


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    def setUp(self):
        # 每个用例使用全新注册表，避免相互污染。
        service.STATE = service.ApiState()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(
            health_payload(),
            {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME},
        )

    def test_health_endpoint_returns_json(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def post(self, path, payload):
        status, body = request_json(self.base_url, "POST", path, payload)
        self.assertEqual(status, 200, body)
        return body["data"]

    def get(self, path):
        status, body = request_json(self.base_url, "GET", path)
        self.assertEqual(status, 200, body)
        return body

    def _bootstrap(self):
        ingredient = self.post("/api/ingredients",
                               {"name": "皱皮辣椒", "origin_region": "遵义产区"})
        dish = self.post("/api/dishes", {"name": "辣子鸡"})
        standard = self.post("/api/standards", {
            "dish_id": dish["id"],
            "core_steps": ["腌制", "油炸", "收汁"],
            "ingredient_ids": [ingredient["id"]],
            "variants": [{"name": "减辣口"}],
        })
        master = self.post("/api/masters", {"name": "周师傅"})
        return ingredient, dish, standard, master

    def _admit(self, dish, standard, ingredient, master,
               reviewer_specs=None):
        reviewers = []
        for spec in reviewer_specs or [{"name": n} for n in ("甲", "乙", "丙")]:
            reviewers.append(self.post("/api/reviewers", spec))
        application = self.post("/api/applications", {
            "name": "老街老店", "address": "老街1号",
            "master_id": master["id"],
            "related_person_ids": ["owner-1"],
            "dish_requests": [{"dish_id": dish["id"]}],
        })
        coverage = self.get(
            f"/api/applications/{application['id']}/coverage")
        for missing in coverage["missing"]:
            payload = {"application_id": application["id"],
                       "category": missing["category"],
                       "dish_id": missing["dish_id"],
                       "doc_ref": f"doc-{missing['category']}-"
                                  f"{missing.get('step') if 'step' in missing else 'x'}"}
            if missing["category"] == "procurement":
                payload["ingredient_id"] = ingredient["id"]
            if "step" in missing:
                payload["step"] = missing["step"]
            evidence = self.post("/api/evidence", payload)
            self.post("/api/evidence/verify",
                      {"evidence_id": evidence["id"], "by": "秘书处"})
        review = self.post("/api/reviews", {
            "application_id": application["id"],
            "reviewer_ids": [r["id"] for r in reviewers],
        })
        for reviewer in reviewers:
            self.post("/api/reviews/ballot", {
                "review_id": review["id"], "reviewer_id": reviewer["id"],
                "votes": {dish["id"]: "approve"},
            })
        result = self.post("/api/reviews/finalize",
                           {"review_id": review["id"]})
        self.assertEqual(result["admitted"], [dish["id"]])
        return application

    def test_end_to_end_admission_public_summary_and_exit(self):
        ingredient, dish, standard, master = self._bootstrap()
        application = self._admit(dish, standard, ingredient, master)
        summary = self.get("/public/summary")
        self.assertEqual(summary["active_stores"], [{
            "name": "老街老店",
            "dishes": [{"dish": "辣子鸡", "standard_version": 1,
                        "variants": []}],
        }])
        # 知识产权异议成立 -> 退出，公众侧只看到克制的退出原因。
        incident = self.post("/api/incidents", {
            "store_id": application["store_id"], "kind": "ip_dispute",
            "dish_ids": [dish["id"]], "detail": "商标异议"})
        self.post("/api/incidents/resolve",
                  {"incident_id": incident["id"], "resolution": "exit",
                   "detail": "异议成立"})
        summary = self.get("/public/summary")
        self.assertEqual(summary["active_stores"], [])
        self.assertEqual(summary["exited_stores"], [{
            "name": "老街老店", "reason": "知识产权异议",
            "exited_on": summary["as_of"]}])

    def test_conflicted_reviewer_is_blocked_over_http(self):
        ingredient, dish, standard, master = self._bootstrap()
        biased = self.post("/api/reviewers", {
            "name": "关联评审", "conflict_person_ids": ["owner-1"]})
        neutral = self.post("/api/reviewers", {"name": "中立评审"})
        application = self.post("/api/applications", {
            "name": "关联店", "address": "老街2号",
            "master_id": master["id"], "related_person_ids": ["owner-1"],
            "dish_requests": [{"dish_id": dish["id"]}]})
        status, body = request_json(
            self.base_url, "POST", "/api/reviews",
            {"application_id": application["id"],
             "reviewer_ids": [biased["id"], neutral["id"]]})
        self.assertEqual(status, 400)
        self.assertIn("回避", body["error"])

    def test_consent_gates_duplicate_free_effectiveness(self):
        ingredient, dish, standard, master = self._bootstrap()
        application = self._admit(dish, standard, ingredient, master)
        store_id = application["store_id"]
        # 未授权用途：数据汇入被拒绝。
        status, body = request_json(
            self.base_url, "POST", "/api/records",
            {"store_id": store_id, "kind": "sales",
             "occurred_on": "2026-03-01", "channel": "堂食",
             "amount": 88, "order_no": "ORD-9"})
        self.assertEqual(status, 400)
        self.post("/api/consents", {
            "store_id": store_id, "purposes": ["survival"],
            "granted_on": "2026-01-01", "expires_on": "2026-12-31"})
        first = self.post("/api/records", {
            "store_id": store_id, "kind": "sales",
            "occurred_on": "2026-03-01", "channel": "堂食",
            "amount": 88, "order_no": "ORD-9"})
        dup = self.post("/api/records", {
            "store_id": store_id, "kind": "sales",
            "occurred_on": "2026-03-01", "channel": "外卖平台",
            "amount": 84.5, "order_no": "ORD-9"})
        self.assertFalse(first["duplicate"])
        self.assertTrue(dup["duplicate"])
        metrics = self.get("/api/metrics")
        survival = metrics["stores"][0]["purposes"]["survival"]
        self.assertEqual(survival["unique_transactions"], 1)
        self.assertEqual(survival["sales_amount"], 88)

    def test_malformed_json_is_rejected(self):
        request = Request(
            f"{self.base_url}/api/dishes", data=b"{not json",
            method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
