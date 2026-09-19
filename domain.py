"""地域餐饮品牌准入的理事会后端领域规则。

只依赖标准库。核心立场：

* 一块牌匾不管原料与工序——准入绑定到“菜品标准版本 + 证据覆盖”；
* 统一配方不抹掉技艺差异——标准显式登记“允许变体”，逐店逐菜授权；
* 评审先回避利益冲突，再决定准入范围（可只准入部分菜品）；
* 抽查不合格、供应中断、知识产权异议分别触发整改、局部停权或退出；
* 标识使用历史与退出事实不删除；
* 门店经营数据仅在门店授权的用途与期限内使用，跨渠道同一交易只计一次。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

# ---- 常量 -----------------------------------------------------------------

PURPOSE_SURVIVAL = "survival"      # 存续评估
PURPOSE_VALUE_ADD = "value_add"    # 农产品增值
PURPOSE_EMPLOYMENT = "employment"  # 就业带动
PURPOSES = (PURPOSE_SURVIVAL, PURPOSE_VALUE_ADD, PURPOSE_EMPLOYMENT)

RECORD_SALES = "sales"
RECORD_PROCUREMENT = "procurement"
RECORD_EMPLOYMENT = "employment"
RECORD_KIND_PURPOSE = {
    RECORD_SALES: PURPOSE_SURVIVAL,
    RECORD_PROCUREMENT: PURPOSE_VALUE_ADD,
    RECORD_EMPLOYMENT: PURPOSE_EMPLOYMENT,
}

INCIDENT_INSPECTION = "inspection_failure"  # 抽查不合格
INCIDENT_SUPPLY = "supply_disruption"       # 供应中断
INCIDENT_IP = "ip_dispute"                  # 知识产权异议
INCIDENT_KINDS = (INCIDENT_INSPECTION, INCIDENT_SUPPLY, INCIDENT_IP)
EXIT_REASON_LABEL = {
    INCIDENT_INSPECTION: "抽查不合格",
    INCIDENT_SUPPLY: "供应中断",
    INCIDENT_IP: "知识产权异议",
}

STORE_APPLIED = "applied"
STORE_ACTIVE = "active"
STORE_RECTIFYING = "rectifying"
STORE_EXITED = "exited"

AUTH_ACTIVE = "active"
AUTH_SUSPENDED = "suspended"
AUTH_WITHDRAWN = "withdrawn"

EVIDENCE_SUBMITTED = "submitted"
EVIDENCE_VERIFIED = "verified"
EVIDENCE_REJECTED = "rejected"


class DomainError(ValueError):
    """请求不满足领域规则。"""


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _d(value) -> str | None:
    """把入参统一成 ISO 日期字符串。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    # 尽早拦住无法解释的日期，避免授权期限静默失真。
    datetime.strptime(text, "%Y-%m-%d")
    return text


# ---- 数据结构 -------------------------------------------------------------


@dataclass
class Ingredient:
    id: str
    name: str
    origin_region: str          # 核心食材产区
    designated: bool = True     # 是否为受保护的指定产区食材


@dataclass
class Variant:
    id: str
    standard_id: str
    name: str
    deviation_note: str         # 允许偏离标准的边界说明


@dataclass
class Dish:
    id: str
    name: str
    description: str = ""


@dataclass
class DishStandard:
    """代表性菜品的一个标准版本。新版本生效后旧版本自动转为 superseded。"""

    id: str
    dish_id: str
    version: int
    status: str                 # effective / superseded
    effective_on: str
    core_steps: list[str]
    ingredient_ids: list[str]
    allowed_variant_ids: list[str]
    note: str = ""


@dataclass
class Master:
    id: str
    name: str
    lineage_note: str = ""


@dataclass
class MasterRelation:
    id: str
    master_id: str
    teacher_id: str
    style: str
    note: str = ""


@dataclass
class Reviewer:
    id: str
    name: str
    conflict_person_ids: list[str] = field(default_factory=list)
    conflict_store_ids: list[str] = field(default_factory=list)


@dataclass
class Store:
    id: str
    name: str
    address: str
    master_id: str | None
    related_person_ids: list[str]
    status: str = STORE_APPLIED
    exit_reason: str | None = None
    exited_on: str | None = None


@dataclass
class Evidence:
    """可验证的采购、制作与服务证据。"""

    id: str
    application_id: str
    store_id: str
    category: str               # procurement / preparation / service
    dish_id: str
    ingredient_id: str | None
    step: int | None
    doc_ref: str                # 单据/报告/影像的可核验引用
    supplier: str
    detail: str
    status: str = EVIDENCE_SUBMITTED
    verified_by: str | None = None


@dataclass
class Application:
    id: str
    store_id: str
    dish_requests: list[dict]   # [{dish_id, standard_id, variant_ids}]
    status: str = "pending"     # pending / admitted / rejected
    decided_on: str | None = None


@dataclass
class Review:
    id: str
    application_id: str
    reviewer_ids: list[str]
    ballots: dict = field(default_factory=dict)  # reviewer_id -> {dish_id: vote}
    decided: bool = False


@dataclass
class Authorization:
    """某门店对某菜品某标准版本（含允许变体）的授权。"""

    id: str
    store_id: str
    dish_id: str
    standard_id: str
    version: int
    variant_ids: list[str]
    status: str = AUTH_ACTIVE
    granted_on: str = ""


@dataclass
class Incident:
    id: str
    store_id: str
    kind: str
    dish_ids: list[str]
    detail: str
    opened_on: str
    due_on: str | None
    status: str = "open"        # open / resolved
    resolution: str | None = None
    resolved_on: str | None = None


@dataclass
class LogoUsage:
    """标识在渠道或商品上的使用记录。退出后记录仍保留。"""

    id: str
    store_id: str
    medium: str                 # 如 外卖平台 / 堂食门头 / 零售包装
    target: str                 # 渠道名或商品名
    started_on: str
    ended_on: str | None = None


@dataclass
class Consent:
    id: str
    store_id: str
    purposes: list[str]
    granted_on: str
    expires_on: str
    revoked: bool = False


@dataclass
class EffectivenessRecord:
    """经营数据记录。同一交易跨外卖/堂食/零售只保留一条。"""

    id: str
    store_id: str
    kind: str
    channel: str | None
    occurred_on: str
    amount: float | None
    headcount: int | None
    ingredient_id: str | None
    order_no: str | None
    dedup_key: str


# ---- 注册表（所有规则的入口） ----------------------------------------------


class BrandRegistry:
    def __init__(self, today=None):
        self.today = today or _today_utc
        self._counters: dict[str, int] = {}
        self.ingredients: list[Ingredient] = []
        self.dishes: list[Dish] = []
        self.variants: list[Variant] = []
        self.standards: list[DishStandard] = []
        self.masters: list[Master] = []
        self.relations: list[MasterRelation] = []
        self.reviewers: list[Reviewer] = []
        self.stores: list[Store] = []
        self.applications: list[Application] = []
        self.evidences: list[Evidence] = []
        self.reviews: list[Review] = []
        self.authorizations: list[Authorization] = []
        self.incidents: list[Incident] = []
        self.logo_usages: list[LogoUsage] = []
        self.consents: list[Consent] = []
        self.records: list[EffectivenessRecord] = []

    def _id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}-{n}"

    # ---- 标准、食材、变体 ------------------------------------------------

    def add_ingredient(self, name, origin_region, designated=True):
        ingredient = Ingredient(self._id("ing"), name, origin_region, bool(designated))
        self.ingredients.append(ingredient)
        return ingredient

    def add_dish(self, name, description=""):
        dish = Dish(self._id("dish"), name, description)
        self.dishes.append(dish)
        return dish

    def publish_standard(self, dish_id, core_steps, ingredient_ids,
                         variants=None, note="", effective_on=None):
        """发布菜品标准新版本；同菜旧版本自动失效，允许变体随版本登记。"""
        dish = self._get(self.dishes, dish_id, "菜品")
        if not core_steps:
            raise DomainError("标准必须写明关键工序，不能只发一块牌匾")
        for ingredient_id in ingredient_ids:
            self._get(self.ingredients, ingredient_id, "核心食材")
        version = 1 + max(
            (s.version for s in self.standards if s.dish_id == dish_id), default=0
        )
        standard = DishStandard(
            id=self._id("std"),
            dish_id=dish_id,
            version=version,
            status="effective",
            effective_on=_d(effective_on) or self.today().isoformat(),
            core_steps=list(core_steps),
            ingredient_ids=list(ingredient_ids),
            allowed_variant_ids=[],
            note=note,
        )
        for old in self.standards:
            if old.dish_id == dish_id and old.status == "effective":
                old.status = "superseded"
        for spec in variants or []:
            variant = Variant(
                id=self._id("var"),
                standard_id=standard.id,
                name=spec["name"],
                deviation_note=spec.get("deviation_note", ""),
            )
            self.variants.append(variant)
            standard.allowed_variant_ids.append(variant.id)
        self.standards.append(standard)
        return standard

    def current_standard(self, dish_id):
        for standard in reversed(self.standards):
            if standard.dish_id == dish_id and standard.status == "effective":
                return standard
        raise DomainError("该菜品尚无生效标准版本")

    # ---- 师傅传承 --------------------------------------------------------

    def add_master(self, name, lineage_note=""):
        master = Master(self._id("master"), name, lineage_note)
        self.masters.append(master)
        return master

    def add_lineage_relation(self, master_id, teacher_id, style, note=""):
        master = self._get(self.masters, master_id, "师傅")
        teacher = self._get(self.masters, teacher_id, "师承师傅")
        if master_id == teacher_id:
            raise DomainError("师承关系不能指向自己")
        relation = MasterRelation(
            id=self._id("rel"),
            master_id=master_id,
            teacher_id=teacher_id,
            style=style,
            note=note,
        )
        self.relations.append(relation)
        return relation

    # ---- 评审员 ----------------------------------------------------------

    def add_reviewer(self, name, conflict_person_ids=None, conflict_store_ids=None):
        reviewer = Reviewer(
            id=self._id("rev"),
            name=name,
            conflict_person_ids=list(conflict_person_ids or []),
            conflict_store_ids=list(conflict_store_ids or []),
        )
        self.reviewers.append(reviewer)
        return reviewer

    # ---- 申请与证据 ------------------------------------------------------

    def apply(self, name=None, address=None, dish_requests=None, master_id=None,
              related_person_ids=None, store_id=None):
        if not dish_requests:
            raise DomainError("申请至少包含一道代表菜品")
        if store_id is not None:
            # 已准入门店就新增菜品再次申请（如恢复供应后扩菜）。
            store = self._get(self.stores, store_id, "门店")
            if store.status == STORE_EXITED:
                raise DomainError("已退出门店不能再提交申请")
            already = {a.dish_id for a in self.authorizations
                       if a.store_id == store.id
                       and a.status in (AUTH_ACTIVE, AUTH_SUSPENDED)}
        else:
            if not name or not address:
                raise DomainError("新门店申请须提供名称与地址")
            if master_id is not None:
                self._get(self.masters, master_id, "传承师傅")
            store = None
            already = set()
        normalized = []
        for request in dish_requests:
            dish_id = request["dish_id"]
            if dish_id in already:
                raise DomainError("该菜品授权仍有效，无需重复申请")
            standard_id = request.get("standard_id")
            standard = (
                self._get(self.standards, standard_id, "标准版本")
                if standard_id else self.current_standard(dish_id)
            )
            if standard.dish_id != dish_id:
                raise DomainError("标准版本与菜品不匹配")
            if standard.status != "effective":
                raise DomainError("只能按当前生效标准版本申请")
            variant_ids = list(request.get("variant_ids", []))
            for variant_id in variant_ids:
                variant = self._get(self.variants, variant_id, "允许变体")
                if variant.id not in standard.allowed_variant_ids:
                    raise DomainError("所请变体不在该标准版本允许范围内")
            normalized.append(
                {"dish_id": dish_id,
                 "standard_id": standard.id,
                 "variant_ids": variant_ids}
            )
        if store is None:
            store = Store(
                id=self._id("store"),
                name=name,
                address=address,
                master_id=master_id,
                related_person_ids=list(related_person_ids or []),
            )
            self.stores.append(store)
        application = Application(
            id=self._id("app"), store_id=store.id, dish_requests=normalized
        )
        self.applications.append(application)
        return application

    def add_evidence(self, application_id, category, dish_id, doc_ref,
                     ingredient_id=None, step=None, supplier="", detail=""):
        application = self._get(self.applications, application_id, "申请")
        if application.status != "pending":
            raise DomainError("已决申请不能再补证据")
        if category not in ("procurement", "preparation", "service"):
            raise DomainError("证据类别只能是 procurement/preparation/service")
        if not doc_ref:
            raise DomainError("证据必须给出可核验引用 doc_ref")
        if not any(r["dish_id"] == dish_id for r in application.dish_requests):
            raise DomainError("证据对应的菜品不在申请范围内")
        evidence = Evidence(
            id=self._id("ev"),
            application_id=application_id,
            store_id=application.store_id,
            category=category,
            dish_id=dish_id,
            ingredient_id=ingredient_id,
            step=step,
            doc_ref=doc_ref,
            supplier=supplier,
            detail=detail,
        )
        self.evidences.append(evidence)
        return evidence

    def verify_evidence(self, evidence_id, by):
        evidence = self._get(self.evidences, evidence_id, "证据")
        evidence.status = EVIDENCE_VERIFIED
        evidence.verified_by = by
        return evidence

    def reject_evidence(self, evidence_id, by):
        evidence = self._get(self.evidences, evidence_id, "证据")
        evidence.status = EVIDENCE_REJECTED
        evidence.verified_by = by
        return evidence

    def required_evidence(self, application_id):
        """按申请的标准版本算出应提交的证据清单。"""
        application = self._get(self.applications, application_id, "申请")
        required = []
        for request in application.dish_requests:
            standard = self._get(self.standards, request["standard_id"], "标准版本")
            for ingredient_id in standard.ingredient_ids:
                required.append({"category": "procurement",
                                 "dish_id": request["dish_id"],
                                 "ingredient_id": ingredient_id})
            for index in range(len(standard.core_steps)):
                required.append({"category": "preparation",
                                 "dish_id": request["dish_id"],
                                 "step": index})
            required.append({"category": "service",
                             "dish_id": request["dish_id"]})
        return required

    def evidence_coverage(self, application_id):
        """返回已核实覆盖情况；缺项决定了评审最多能准入哪些菜。"""
        application = self._get(self.applications, application_id, "申请")
        verified = [e for e in self.evidences
                    if e.application_id == application_id
                    and e.status == EVIDENCE_VERIFIED]

        def covered(category, dish_id, ingredient_id=None, step=None):
            return any(
                e.category == category and e.dish_id == dish_id
                and e.ingredient_id == ingredient_id and e.step == step
                for e in verified
            )

        missing = []
        eligible_dish_ids = set()
        for item in self.required_evidence(application_id):
            ok = covered(**item)
            if not ok:
                missing.append(item)
            else:
                eligible_dish_ids.add(item["dish_id"])
        requested = {r["dish_id"] for r in application.dish_requests}
        # 一道菜只有在它自身的全部证据项都被核实时才具备准入资格。
        missing_dish_ids = {m["dish_id"] for m in missing}
        eligible = requested - missing_dish_ids
        return {"complete": not missing, "missing": missing,
                "eligible_dish_ids": sorted(eligible)}

    # ---- 评审回避与准入决定 ----------------------------------------------

    def start_review(self, application_id, reviewer_ids):
        application = self._get(self.applications, application_id, "申请")
        if application.status != "pending":
            raise DomainError("该申请已评审")
        if not reviewer_ids:
            raise DomainError("评审组不能为空")
        store = self._get(self.stores, application.store_id, "门店")
        related = set(store.related_person_ids)
        if store.master_id:
            related.add(store.master_id)
        blocked = []
        for reviewer_id in reviewer_ids:
            reviewer = self._get(self.reviewers, reviewer_id, "评审员")
            persons = sorted(related & set(reviewer.conflict_person_ids))
            if store.id in reviewer.conflict_store_ids or persons:
                blocked.append({"reviewer_id": reviewer_id,
                                "conflict_person_ids": persons,
                                "conflict_with_store": store.id
                                in reviewer.conflict_store_ids})
        if blocked:
            raise DomainError(f"存在利益冲突，相关评审员必须回避: {blocked}")
        if len(set(reviewer_ids)) != len(reviewer_ids):
            raise DomainError("评审员不能重复登记")
        review = Review(self._id("revw"), application_id, list(reviewer_ids))
        self.reviews.append(review)
        return review

    def cast_ballot(self, review_id, reviewer_id, votes):
        """逐菜投票；准入范围由证据资格与多数意见共同决定。"""
        review = self._get(self.reviews, review_id, "评审")
        if review.decided:
            raise DomainError("评审已作出决定")
        if reviewer_id not in review.reviewer_ids:
            raise DomainError("该评审员不在本次回避后的评审组内")
        application = self._get(self.applications, review.application_id, "申请")
        dish_ids = {r["dish_id"] for r in application.dish_requests}
        if set(votes) != dish_ids:
            raise DomainError("必须对申请范围内每道菜逐一表态")
        for vote in votes.values():
            if vote not in ("approve", "reject"):
                raise DomainError("表决只能是 approve/reject")
        review.ballots[reviewer_id] = dict(votes)
        return review

    def finalize_review(self, review_id):
        review = self._get(self.reviews, review_id, "评审")
        if review.decided:
            raise DomainError("评审已作出决定")
        application = self._get(self.applications, review.application_id, "申请")
        if set(review.ballots) != set(review.reviewer_ids):
            raise DomainError("评审组尚未完成回避后表决")
        coverage = self.evidence_coverage(application.id)
        admitted = []
        for request in application.dish_requests:
            dish_id = request["dish_id"]
            if dish_id not in coverage["eligible_dish_ids"]:
                continue  # 证据未核实的菜不进入准入范围
            approvals = sum(1 for ballot in review.ballots.values()
                            if ballot[dish_id] == "approve")
            if approvals * 2 > len(review.reviewer_ids):
                standard = self._get(self.standards, request["standard_id"],
                                     "标准版本")
                admitted.append((request, standard))
        review.decided = True
        today = self.today().isoformat()
        if not admitted:
            application.status = "rejected"
            application.decided_on = today
            return {"application": application, "admitted": []}
        for request, standard in admitted:
            self.authorizations.append(Authorization(
                id=self._id("auth"),
                store_id=application.store_id,
                dish_id=request["dish_id"],
                standard_id=standard.id,
                version=standard.version,
                variant_ids=request["variant_ids"],
                granted_on=today,
            ))
        store = self._get(self.stores, application.store_id, "门店")
        store.status = STORE_ACTIVE
        application.status = "admitted"
        application.decided_on = today
        return {"application": application,
                "admitted": [r["dish_id"] for r, _ in admitted]}

    # ---- 抽查、供应、知识产权：整改 / 局部停权 / 退出 ---------------------

    def open_incident(self, store_id, kind, dish_ids=None, detail="",
                      rectification_days=15):
        store = self._get(self.stores, store_id, "门店")
        if kind not in INCIDENT_KINDS:
            raise DomainError("未知的触发事项类型")
        if store.status == STORE_EXITED:
            raise DomainError("已退出门店不能再立案")
        dish_ids = list(dish_ids or [])
        today = self.today()
        due = None
        if kind == INCIDENT_INSPECTION:
            # 抽查不合格：先限期整改，期间不计入“当前有效门店”。
            store.status = STORE_RECTIFYING
            due = (today + timedelta(days=rectification_days)).isoformat()
        else:
            # 供应中断或知识产权异议：先对相关菜品局部停权。
            if not dish_ids:
                raise DomainError("供应中断/知识产权异议须指明受影响的菜品")
            for dish_id in dish_ids:
                self._suspend_dish(store_id, dish_id)
        incident = Incident(
            id=self._id("inc"),
            store_id=store_id,
            kind=kind,
            dish_ids=dish_ids,
            detail=detail,
            opened_on=today.isoformat(),
            due_on=due,
        )
        self.incidents.append(incident)
        return incident

    def resolve_incident(self, incident_id, resolution, detail=""):
        incident = self._get(self.incidents, incident_id, "事项")
        if incident.status != "open":
            raise DomainError("事项已结案")
        if resolution not in ("rectified", "restored", "exit"):
            raise DomainError("结案方式只能是 rectified/restored/exit")
        store = self._get(self.stores, incident.store_id, "门店")
        today = self.today().isoformat()
        if resolution == "exit":
            # 整改失败、供应无法恢复或知识产权异议成立：退出。
            store.status = STORE_EXITED
            store.exit_reason = incident.kind
            store.exited_on = today
            for auth in self.authorizations:
                if auth.store_id == store.id and auth.status != AUTH_WITHDRAWN:
                    auth.status = AUTH_WITHDRAWN
            # 正在使用的标识在退出日截止，但历史记录保留。
            for usage in self.logo_usages:
                if usage.store_id == store.id and usage.ended_on is None:
                    usage.ended_on = today
        else:
            if incident.kind == INCIDENT_INSPECTION:
                store.status = STORE_ACTIVE
            for dish_id in incident.dish_ids:
                auth = self._active_auth(store.id, dish_id, include_suspended=True)
                if auth is not None and auth.status == AUTH_SUSPENDED:
                    auth.status = AUTH_ACTIVE
        incident.status = "resolved"
        incident.resolution = resolution
        incident.resolved_on = today
        incident.detail = (incident.detail + f" | 结案: {detail}").strip(" |")
        return incident

    def _suspend_dish(self, store_id, dish_id):
        auth = self._active_auth(store_id, dish_id, include_suspended=True)
        if auth is None:
            raise DomainError("门店未取得该菜品授权，无法停权")
        auth.status = AUTH_SUSPENDED

    def _active_auth(self, store_id, dish_id, include_suspended=False):
        statuses = (AUTH_ACTIVE, AUTH_SUSPENDED) if include_suspended else (AUTH_ACTIVE,)
        for auth in self.authorizations:
            if (auth.store_id == store_id and auth.dish_id == dish_id
                    and auth.status in statuses):
                return auth
        return None

    # ---- 标识使用历史 ----------------------------------------------------

    def record_logo_use(self, store_id, medium, target, started_on=None):
        store = self._get(self.stores, store_id, "门店")
        if store.status == STORE_APPLIED:
            raise DomainError("未准入门店没有标识使用记录可登记")
        usage = LogoUsage(
            id=self._id("logo"),
            store_id=store_id,
            medium=medium,
            target=target,
            started_on=_d(started_on) or self.today().isoformat(),
        )
        self.logo_usages.append(usage)
        return usage

    def end_logo_use(self, usage_id, ended_on=None):
        usage = self._get(self.logo_usages, usage_id, "标识使用记录")
        if usage.ended_on is not None:
            raise DomainError("该记录已截止")
        usage.ended_on = _d(ended_on) or self.today().isoformat()
        return usage

    def list_logo_usage(self, store_id=None, include_ended=True):
        """历史可查：默认包含已退出门店与已截止渠道/商品。"""
        rows = self.logo_usages
        if store_id is not None:
            rows = [u for u in rows if u.store_id == store_id]
        if not include_ended:
            rows = [u for u in rows if u.ended_on is None]
        return list(rows)

    # ---- 门店数据授权 ----------------------------------------------------

    def grant_consent(self, store_id, purposes, expires_on, granted_on=None):
        store = self._get(self.stores, store_id, "门店")
        purposes = list(purposes)
        if not purposes or any(p not in PURPOSES for p in purposes):
            raise DomainError(f"用途必须取自 {PURPOSES}")
        granted = _d(granted_on) or self.today().isoformat()
        expires = _d(expires_on)
        if expires <= granted:
            raise DomainError("授权期限必须晚于授权日")
        consent = Consent(self._id("consent"), store.id, purposes,
                          granted, expires)
        self.consents.append(consent)
        return consent

    def revoke_consent(self, consent_id):
        consent = self._get(self.consents, consent_id, "数据授权")
        consent.revoked = True
        return consent

    def consent_active(self, store_id, purpose, on=None):
        day = _d(on) or self.today().isoformat()
        for consent in self.consents:
            if (consent.store_id == store_id and not consent.revoked
                    and purpose in consent.purposes
                    and consent.granted_on <= day <= consent.expires_on):
                return True
        return False

    # ---- 经营数据汇入与去重 ----------------------------------------------

    def ingest_record(self, store_id, kind, occurred_on, channel=None,
                      amount=None, headcount=None, ingredient_id=None,
                      order_no=None, dedup_key=None):
        store = self._get(self.stores, store_id, "门店")
        if kind not in RECORD_KIND_PURPOSE:
            raise DomainError("数据类别只能是 sales/procurement/employment")
        purpose = RECORD_KIND_PURPOSE[kind]
        if not self.consent_active(store_id, purpose, on=occurred_on):
            raise DomainError(f"门店未就 {purpose} 用途在该日期授权数据使用")
        if kind == RECORD_PROCUREMENT:
            if ingredient_id is None:
                raise DomainError("采购数据须指明食材")
            self._get(self.ingredients, ingredient_id, "食材")
        if kind == RECORD_EMPLOYMENT and headcount is None:
            raise DomainError("就业数据须包含在岗人数 headcount")
        key = dedup_key or self._dedup_key(
            kind, occurred_on, order_no=order_no, amount=amount,
            ingredient_id=ingredient_id, headcount=headcount, channel=channel,
        )
        for existing in self.records:
            if existing.store_id == store_id and existing.dedup_key == key:
                # 外卖、堂食、零售重复汇入的同一交易：不重复计数。
                return {"record": existing, "duplicate": True}
        record = EffectivenessRecord(
            id=self._id("rec"),
            store_id=store_id,
            kind=kind,
            channel=channel,
            occurred_on=_d(occurred_on),
            amount=None if amount is None else float(amount),
            headcount=headcount,
            ingredient_id=ingredient_id,
            order_no=order_no,
            dedup_key=key,
        )
        self.records.append(record)
        return {"record": record, "duplicate": False}

    @staticmethod
    def _dedup_key(kind, occurred_on, *, order_no, amount, ingredient_id,
                   headcount, channel):
        if order_no:
            # 同一订单号跨外卖/堂食/零售汇入视为同一交易；刻意不含金额与渠道，
            # 避免平台费用微调或重复上报造成同一交易被多算。
            basis = [kind, occurred_on, order_no]
        else:
            # 没有订单号时不误并：保留渠道区分。
            basis = [kind, occurred_on, channel, amount]
        if ingredient_id:
            basis.append(ingredient_id)
        if headcount is not None:
            basis.append(headcount)
        return sha256("|".join("" if b is None else str(b)
                               for b in basis).encode("utf-8")).hexdigest()

    def effectiveness_metrics(self, on=None):
        """仅按门店当前有效授权用途汇总；撤销或过期即不评估。"""
        day = _d(on) or self.today().isoformat()
        result = []
        for store in self.stores:
            purposes = {}
            mine = [r for r in self.records if r.store_id == store.id]
            if self.consent_active(store.id, PURPOSE_SURVIVAL, day):
                sales = [r for r in mine if r.kind == RECORD_SALES]
                purposes[PURPOSE_SURVIVAL] = {
                    "unique_transactions": len(sales),
                    "sales_amount": round(sum(r.amount or 0 for r in sales), 2),
                }
            if self.consent_active(store.id, PURPOSE_VALUE_ADD, day):
                designated = {i.id for i in self.ingredients if i.designated}
                procured = [r for r in mine
                            if r.kind == RECORD_PROCUREMENT
                            and r.ingredient_id in designated]
                purposes[PURPOSE_VALUE_ADD] = {
                    "designated_origin_procurement": round(
                        sum(r.amount or 0 for r in procured), 2),
                }
            if self.consent_active(store.id, PURPOSE_EMPLOYMENT, day):
                staff_rows = [r for r in mine if r.kind == RECORD_EMPLOYMENT]
                latest = max(staff_rows, key=lambda r: r.occurred_on,
                             default=None)
                purposes[PURPOSE_EMPLOYMENT] = {
                    "latest_headcount": latest.headcount if latest else 0,
                }
            if purposes:
                result.append({"store_id": store.id, "name": store.name,
                               "purposes": purposes})
        return {"as_of": day, "stores": result}

    # ---- 公众摘要：克制披露 ----------------------------------------------

    def public_summary(self, on=None):
        day = _d(on) or self.today().isoformat()
        standards_out = []
        for dish in self.dishes:
            current = next((s for s in reversed(self.standards)
                            if s.dish_id == dish.id and s.status == "effective"),
                           None)
            if current is None:
                continue
            standards_out.append({
                "dish": dish.name,
                "current_version": current.version,
                "effective_on": current.effective_on,
                "allowed_variants": [
                    self._get(self.variants, v, "允许变体").name
                    for v in current.allowed_variant_ids
                ],
                "core_ingredient_origins": [
                    f"{self._get(self.ingredients, i, '食材').name}:"
                    f"{self._get(self.ingredients, i, '食材').origin_region}"
                    for i in current.ingredient_ids
                ],
            })
        active_stores = []
        for store in self.stores:
            if store.status != STORE_ACTIVE:
                continue  # 整改中、已退出均不列入当前有效门店
            dishes_out = []
            for auth in self.authorizations:
                if (auth.store_id == store.id
                        and auth.status == AUTH_ACTIVE):
                    dish = self._get(self.dishes, auth.dish_id, "菜品")
                    dishes_out.append({
                        "dish": dish.name,
                        "standard_version": auth.version,
                        "variants": [
                            self._get(self.variants, v, "允许变体").name
                            for v in auth.variant_ids
                        ],
                    })
            if not dishes_out:
                continue  # 全部菜品局部停权时不作为有效门店对外展示
            active_stores.append({"name": store.name, "dishes": dishes_out})
        exited = [
            {"name": store.name,
             "reason": EXIT_REASON_LABEL.get(store.exit_reason, store.exit_reason),
             "exited_on": store.exited_on}
            for store in self.stores if store.status == STORE_EXITED
        ]
        return {"as_of": day,
                "current_standards": standards_out,
                "active_stores": active_stores,
                "exited_stores": exited}

    # ---- 查询与快照 ------------------------------------------------------

    @staticmethod
    def _get(rows, entity_id, label):
        for row in rows:
            if row.id == entity_id:
                return row
        raise DomainError(f"{label}不存在: {entity_id}")

    def store_view(self, store_id):
        """理事会内部视图：含授权、事项、标识历史等完整事实。"""
        store = self._get(self.stores, store_id, "门店")
        return {
            "store": asdict(store),
            "authorizations": [asdict(a) for a in self.authorizations
                               if a.store_id == store_id],
            "incidents": [asdict(i) for i in self.incidents
                          if i.store_id == store_id],
            "logo_usage": [asdict(u) for u in self.logo_usages
                           if u.store_id == store_id],
            "consents": [asdict(c) for c in self.consents
                         if c.store_id == store_id],
        }

    _LISTS = {
        "ingredients": Ingredient, "dishes": Dish, "variants": Variant,
        "standards": DishStandard, "masters": Master,
        "relations": MasterRelation, "reviewers": Reviewer, "stores": Store,
        "applications": Application, "evidences": Evidence,
        "reviews": Review, "authorizations": Authorization,
        "incidents": Incident, "logo_usages": LogoUsage,
        "consents": Consent, "records": EffectivenessRecord,
    }

    def to_snapshot(self):
        snapshot = {"counters": dict(self._counters)}
        for name in self._LISTS:
            snapshot[name] = [asdict(item) for item in getattr(self, name)]
        return snapshot

    @classmethod
    def from_snapshot(cls, snapshot, today=None):
        registry = cls(today=today)
        for prefix, value in snapshot.get("counters", {}).items():
            registry._counters[prefix] = int(value)
        for name, kind in cls._LISTS.items():
            setattr(registry, name,
                    [kind(**row) for row in snapshot.get(name, [])])
        return registry

    def save(self, path):
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.to_snapshot(), handle, ensure_ascii=False)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, today=None):
        with open(path, encoding="utf-8") as handle:
            return cls.from_snapshot(json.load(handle), today=today)
