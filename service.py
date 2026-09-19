"""地域餐饮品牌准入的服务入口。

保留原有的 /health 与 --check 契约；理事会管理接口统一挂在 /api 下，
对公众只暴露 /public/summary。可选 --snapshot 将登记数据落盘，重启不丢。
"""

import argparse
import json
import os
import threading
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from domain import BrandRegistry, DomainError

SERVICE_ID = "regional-food-brand"
SERVICE_NAME = "地域餐饮品牌准入"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"无法序列化 {type(value)!r}")


class ApiState:
    """持有注册表与快照路径，串行化写操作。"""

    def __init__(self, snapshot_path=None):
        self.snapshot_path = snapshot_path
        self.lock = threading.Lock()
        if snapshot_path and os.path.exists(snapshot_path):
            self.registry = BrandRegistry.load(snapshot_path)
        else:
            self.registry = BrandRegistry()

    def mutate(self, action, payload):
        with self.lock:
            result = action(self.registry, payload or {})
            if self.snapshot_path:
                self.registry.save(self.snapshot_path)
            return result

    def read(self, action, query):
        with self.lock:
            return action(self.registry, query)


STATE = ApiState(os.environ.get("BRAND_SNAPSHOT"))


# ---- 动作表：每个理事会操作对应一个领域方法 --------------------------------


def _need(payload, key):
    if key not in payload:
        raise DomainError(f"缺少字段: {key}")
    return payload[key]


def _action(registry, payload, fn_name, **mapping):
    method = getattr(registry, fn_name)
    kwargs = {}
    for key, source in mapping.items():
        if source in payload:
            kwargs[key] = payload[source]
    return method(**kwargs)


def act_publish_standard(registry, payload):
    return registry.publish_standard(
        dish_id=_need(payload, "dish_id"),
        core_steps=_need(payload, "core_steps"),
        ingredient_ids=_need(payload, "ingredient_ids"),
        variants=payload.get("variants"),
        note=payload.get("note", ""),
        effective_on=payload.get("effective_on"),
    )


def act_apply(registry, payload):
    return registry.apply(
        name=payload.get("name"),
        address=payload.get("address"),
        dish_requests=_need(payload, "dish_requests"),
        master_id=payload.get("master_id"),
        related_person_ids=payload.get("related_person_ids"),
        store_id=payload.get("store_id"),
    )


def act_add_evidence(registry, payload):
    return registry.add_evidence(
        application_id=_need(payload, "application_id"),
        category=_need(payload, "category"),
        dish_id=_need(payload, "dish_id"),
        doc_ref=_need(payload, "doc_ref"),
        ingredient_id=payload.get("ingredient_id"),
        step=payload.get("step"),
        supplier=payload.get("supplier", ""),
        detail=payload.get("detail", ""),
    )


def act_open_incident(registry, payload):
    kwargs = dict(store_id=_need(payload, "store_id"),
                  kind=_need(payload, "kind"),
                  detail=payload.get("detail", ""))
    if "dish_ids" in payload:
        kwargs["dish_ids"] = payload["dish_ids"]
    if "rectification_days" in payload:
        kwargs["rectification_days"] = payload["rectification_days"]
    return registry.open_incident(**kwargs)


SIMPLE_POST = {
    "/api/ingredients": lambda r, p: r.add_ingredient(
        _need(p, "name"), _need(p, "origin_region"), p.get("designated", True)),
    "/api/dishes": lambda r, p: r.add_dish(
        _need(p, "name"), p.get("description", "")),
    "/api/standards": act_publish_standard,
    "/api/masters": lambda r, p: r.add_master(
        _need(p, "name"), p.get("lineage_note", "")),
    "/api/lineage": lambda r, p: r.add_lineage_relation(
        _need(p, "master_id"), _need(p, "teacher_id"),
        _need(p, "style"), p.get("note", "")),
    "/api/reviewers": lambda r, p: r.add_reviewer(
        _need(p, "name"), p.get("conflict_person_ids"),
        p.get("conflict_store_ids")),
    "/api/applications": act_apply,
    "/api/evidence": act_add_evidence,
    "/api/evidence/verify": lambda r, p: r.verify_evidence(
        _need(p, "evidence_id"), _need(p, "by")),
    "/api/evidence/reject": lambda r, p: r.reject_evidence(
        _need(p, "evidence_id"), _need(p, "by")),
    "/api/reviews": lambda r, p: r.start_review(
        _need(p, "application_id"), _need(p, "reviewer_ids")),
    "/api/reviews/ballot": lambda r, p: r.cast_ballot(
        _need(p, "review_id"), _need(p, "reviewer_id"), _need(p, "votes")),
    "/api/reviews/finalize": lambda r, p: r.finalize_review(
        _need(p, "review_id")),
    "/api/incidents": act_open_incident,
    "/api/incidents/resolve": lambda r, p: r.resolve_incident(
        _need(p, "incident_id"), _need(p, "resolution"),
        p.get("detail", "")),
    "/api/logo-usage": lambda r, p: r.record_logo_use(
        _need(p, "store_id"), _need(p, "medium"), _need(p, "target"),
        p.get("started_on")),
    "/api/logo-usage/end": lambda r, p: r.end_logo_use(
        _need(p, "usage_id"), p.get("ended_on")),
    "/api/consents": lambda r, p: r.grant_consent(
        _need(p, "store_id"), _need(p, "purposes"),
        _need(p, "expires_on"), p.get("granted_on")),
    "/api/consents/revoke": lambda r, p: r.revoke_consent(
        _need(p, "consent_id")),
    "/api/records": lambda r, p: r.ingest_record(
        store_id=_need(p, "store_id"), kind=_need(p, "kind"),
        occurred_on=_need(p, "occurred_on"), channel=p.get("channel"),
        amount=p.get("amount"), headcount=p.get("headcount"),
        ingredient_id=p.get("ingredient_id"), order_no=p.get("order_no"),
        dedup_key=p.get("dedup_key")),
}


class Handler(BaseHTTPRequestHandler):
    """健康检查、公众摘要与理事会 /api 管理接口。"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._write_json(200, health_payload())
            return
        if path == "/public/summary":
            on = parse_qs(parsed.query).get("on", [None])[0]
            self._write_json(200, STATE.read(lambda r, q: r.public_summary(q),
                                            on))
            return
        if path == "/api/metrics":
            on = parse_qs(parsed.query).get("on", [None])[0]
            self._write_json(200, STATE.read(lambda r, q: r.effectiveness_metrics(q),
                                            on))
            return
        if path == "/api/logo-usage":
            query = parse_qs(parsed.query)
            store_id = query.get("store_id", [None])[0]
            include_ended = query.get("include_ended", ["true"])[0] != "false"
            self._write_json(200, STATE.read(
                lambda r, q: r.list_logo_usage(q[0], q[1]),
                (store_id, include_ended)))
            return
        if path.startswith("/api/applications/") and path.endswith("/coverage"):
            application_id = path.rsplit("/", 2)[1]
            self._write_json(200, STATE.read(
                lambda r, q: r.evidence_coverage(q), application_id))
            return
        if path.startswith("/api/stores/"):
            store_id = path[len("/api/stores/"):]
            self._write_json(200, STATE.read(lambda r, q: r.store_view(q),
                                            store_id))
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        action = SIMPLE_POST.get(path)
        if action is None:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise DomainError("请求体必须是 JSON 对象")
            result = STATE.mutate(action, payload)
        except DomainError as error:
            self._write_json(400, {"error": str(error)})
            return
        except json.JSONDecodeError:
            self._write_json(400, {"error": "请求体不是合法 JSON"})
            return
        if result is None:
            self._write_json(200, {"ok": True})
        else:
            self._write_json(200, {"ok": True, "data": result})

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False,
                          default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--snapshot", default=os.environ.get("BRAND_SNAPSHOT"),
                        help="可选：登记数据 JSON 快照路径")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert BrandRegistry().public_summary()["active_stores"] == []
        print("基础检查通过")
        return
    global STATE
    STATE = ApiState(args.snapshot)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
