"""地域餐饮品牌准入的领域核心。

品牌理事会在后端维护代表性菜品的标准版本、允许变体、核心食材产区、
师傅传承关系与门店授权；申请方提交可验证的采购、制作与服务证据，
评审回避利益冲突后决定准入范围。抽查不合格、供应中断或知识产权异议
可触发限期整改、局部停权或退出，历史标识使用记录保留。成效统计对
外卖、堂食与零售重复汇入的同一交易去重；门店依法授权经营数据的用途
与期限，协会仅在授权范围内评估存续、农产品增值与就业带动，并对公众
提供克制摘要。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# 证据类型：采购、制作、服务
EVIDENCE_KINDS = ("procurement", "production", "service")

# 处置触发原因：抽查不合格、供应中断、知识产权异议
TRIGGERS = ("spot_check_failed", "supply_interrupted", "ip_dispute")

# 处置动作：限期整改、局部停权、退出
ACTION_RECTIFY = "rectify"
ACTION_SUSPEND_SCOPE = "suspend_scope"
ACTION_EXIT = "exit"
ACTIONS = (ACTION_RECTIFY, ACTION_SUSPEND_SCOPE, ACTION_EXIT)

# 门店授权状态
STATUS_ACTIVE = "active"
STATUS_RECTIFYING = "rectifying"
STATUS_EXITED = "exited"

# 交易渠道：外卖、堂食、零售
CHANNELS = ("delivery", "dine_in", "retail")

# 经营数据授权用途：存续、农产品增值、就业带动
PURPOSE_CONTINUATION = "continuation"
PURPOSE_AGRI_VALUE_ADD = "agri_value_add"
PURPOSE_EMPLOYMENT = "employment"
PURPOSES = (PURPOSE_CONTINUATION, PURPOSE_AGRI_VALUE_ADD, PURPOSE_EMPLOYMENT)

DEFAULT_QUORUM = 3


class DataUseNotAuthorized(PermissionError):
    """经营数据的用途或期限未获门店授权。"""


@dataclass(frozen=True)
class CoreIngredient:
    """核心食材及其产区。"""

    name: str
    region: str


@dataclass
class DishStandard:
    """代表性菜品的标准版本。"""

    dish: str
    version: str
    spec: dict
    allowed_variants: tuple
    core_ingredients: tuple
    active: bool = True


@dataclass(frozen=True)
class Evidence:
    """申请方提交的可验证证据。"""

    kind: str
    reference: str
    verifiable: bool = True


@dataclass
class Application:
    """门店准入申请。"""

    app_id: str
    store_id: str
    requested_scope: frozenset
    evidence: tuple
    status: str = "pending"  # pending | approved | rejected
    granted_scope: frozenset = frozenset()


@dataclass
class StoreAuthorization:
    """门店授权及其处置状态。"""

    store_id: str
    scope: frozenset
    status: str = STATUS_ACTIVE
    suspended_scope: frozenset = frozenset()
    rectify_deadline: date | None = None
    exit_reason: str | None = None


@dataclass(frozen=True)
class DataUseGrant:
    """门店对经营数据用途与期限的授权。"""

    store_id: str
    purpose: str
    valid_from: date
    valid_to: date

    def covers(self, on: date) -> bool:
        return self.valid_from <= on <= self.valid_to


class BrandRegistry:
    """品牌理事会的后端管理台。"""

    def __init__(self):
        self._standards = {}
        self._lineage = []
        self._applications = {}
        self._app_seq = 0
        self._stores = {}
        self._transactions = {}
        self._grants = []
        self._employment = []
        self._history = []

    # ---- 标准与传承 ----

    def register_standard(
        self, dish, version, spec, allowed_variants=(), core_ingredients=()
    ):
        """登记代表性菜品的标准版本、允许变体与核心食材产区。"""
        if not dish or not version:
            raise ValueError("菜品与版本不能为空")
        ingredients = tuple(
            CoreIngredient(item["name"], item["region"])
            if isinstance(item, dict)
            else CoreIngredient(*item)
            for item in core_ingredients
        )
        for ingredient in ingredients:
            if not ingredient.name or not ingredient.region:
                raise ValueError("核心食材必须标注名称与产区")
        standard = DishStandard(
            dish, version, dict(spec), tuple(allowed_variants), ingredients
        )
        self._standards[(dish, version)] = standard
        return standard

    def standard(self, dish, version):
        return self._standards.get((dish, version))

    def record_lineage(self, master, apprentice, dish):
        """登记师傅与徒弟之间的传承关系。"""
        if not master or not apprentice or not dish:
            raise ValueError("师承关系需包含师傅、徒弟与菜品")
        entry = {"master": master, "apprentice": apprentice, "dish": dish}
        self._lineage.append(entry)
        return entry

    def lineage(self):
        return list(self._lineage)

    # ---- 申请与评审 ----

    def submit_application(self, store_id, requested_scope, evidence):
        """提交准入申请：范围须为已登记标准，证据须覆盖三类且可验证。"""
        scope = frozenset((dish, version) for dish, version in requested_scope)
        if not scope:
            raise ValueError("准入范围不能为空")
        unknown = [
            key
            for key in scope
            if key not in self._standards or not self._standards[key].active
        ]
        if unknown:
            raise ValueError(f"申请的标准版本未登记或已停用: {sorted(unknown)}")
        items = tuple(
            item if isinstance(item, Evidence) else Evidence(**item)
            for item in evidence
        )
        kinds = {item.kind for item in items}
        missing = [kind for kind in EVIDENCE_KINDS if kind not in kinds]
        if missing:
            raise ValueError(f"证据缺少类型: {missing}")
        if any(not item.verifiable or not item.reference for item in items):
            raise ValueError("证据必须可验证并带有出处")
        self._app_seq += 1
        app_id = f"app-{self._app_seq}"
        self._applications[app_id] = Application(app_id, store_id, scope, items)
        return app_id

    def review_application(self, app_id, votes, conflicted=(), quorum=DEFAULT_QUORUM):
        """评审申请：利益冲突者回避，其余评审过半同意方决定准入范围。

        返回是否批准；回避后不足法定人数时暂缓决定，申请保持待审。
        """
        app = self._applications.get(app_id)
        if app is None:
            raise ValueError(f"未知申请: {app_id}")
        if app.status != "pending":
            raise ValueError("申请已完成评审")
        recused = set(conflicted)
        counted = {r: v for r, v in dict(votes).items() if r not in recused}
        if len(counted) < quorum:
            return False
        approvals = sum(1 for v in counted.values() if v)
        approved = approvals * 2 > len(counted)
        app.status = "approved" if approved else "rejected"
        if approved:
            app.granted_scope = app.requested_scope
            auth = self._stores.get(app.store_id)
            if auth is None:
                auth = StoreAuthorization(app.store_id, frozenset())
                self._stores[app.store_id] = auth
            auth.scope = auth.scope | app.granted_scope
            self._record_history(
                app.store_id, "authorized", {"scope": sorted(app.granted_scope)}
            )
        return approved

    def application(self, app_id):
        return self._applications.get(app_id)

    # ---- 授权与处置 ----

    def authorization(self, store_id):
        return self._stores.get(store_id)

    def effective_scope(self, store_id):
        """当前有效授权范围：退出后为空，局部停权的菜品被剔除。"""
        auth = self._stores.get(store_id)
        if auth is None or auth.status == STATUS_EXITED:
            return frozenset()
        return auth.scope - auth.suspended_scope

    def apply_enforcement(
        self, store_id, trigger, action, *, dishes=None, deadline=None, reason=""
    ):
        """对抽查不合格、供应中断或知识产权异议执行处置。"""
        if trigger not in TRIGGERS:
            raise ValueError(f"未知触发原因: {trigger}")
        if action not in ACTIONS:
            raise ValueError(f"未知处置动作: {action}")
        auth = self._stores.get(store_id)
        if auth is None:
            raise ValueError(f"未知门店: {store_id}")
        if auth.status == STATUS_EXITED:
            raise ValueError("门店已退出，无法再次处置")
        detail = {"trigger": trigger}
        if action == ACTION_RECTIFY:
            if deadline is None:
                raise ValueError("限期整改必须给定期限")
            auth.status = STATUS_RECTIFYING
            auth.rectify_deadline = deadline
            detail["deadline"] = deadline.isoformat()
        elif action == ACTION_SUSPEND_SCOPE:
            targets = frozenset(dishes or ())
            if not targets:
                raise ValueError("局部停权必须指定菜品范围")
            if not targets <= auth.scope:
                raise ValueError("停权范围超出已授权菜品")
            auth.suspended_scope = auth.suspended_scope | targets
            detail["dishes"] = sorted(targets)
        else:  # ACTION_EXIT
            auth.status = STATUS_EXITED
            auth.exit_reason = reason or trigger
            auth.rectify_deadline = None
            detail["reason"] = auth.exit_reason
        self._record_history(store_id, action, detail)

    def resolve_rectification(self, store_id):
        """整改完成，恢复正常状态。"""
        auth = self._stores.get(store_id)
        if auth is None or auth.status != STATUS_RECTIFYING:
            raise ValueError("门店不在整改期")
        auth.status = STATUS_ACTIVE
        auth.rectify_deadline = None
        self._record_history(store_id, "rectification_resolved", {})

    # ---- 标识使用历史 ----

    def mark_history(self, store_id=None):
        """标识使用历史：退出后仍可追溯过去的渠道与商品记录。"""
        if store_id is None:
            return list(self._history)
        return [r for r in self._history if r["store_id"] == store_id]

    def _record_history(self, store_id, event, detail, at=None):
        self._history.append(
            {
                "store_id": store_id,
                "event": event,
                "detail": detail,
                "at": (at or date.today()).isoformat(),
            }
        )

    # ---- 成效统计：同一交易跨渠道去重 ----

    def ingest_transaction(self, txn_id, store_id, channel, amount, dishes, at=None):
        """汇入一笔交易；同一交易编号重复汇入时不重复计入成效。"""
        if channel not in CHANNELS:
            raise ValueError(f"未知交易渠道: {channel}")
        auth = self._stores.get(store_id)
        if auth is None or auth.status == STATUS_EXITED:
            raise ValueError("门店未获授权或已退出")
        dishes = frozenset(tuple(dish) for dish in dishes)
        if not dishes <= self.effective_scope(store_id):
            raise ValueError("交易菜品超出当前有效授权范围")
        if txn_id in self._transactions:
            return False
        record = {
            "txn_id": txn_id,
            "store_id": store_id,
            "channel": channel,
            "amount": amount,
            "dishes": sorted(dishes),
            "at": (at or date.today()).isoformat(),
        }
        self._transactions[txn_id] = record
        self._record_history(
            store_id,
            "mark_used",
            {"channel": channel, "dishes": sorted(dishes), "txn_id": txn_id},
            at=at,
        )
        return True

    def counted_transactions(self, store_id=None):
        records = list(self._transactions.values())
        if store_id is not None:
            records = [r for r in records if r["store_id"] == store_id]
        return records

    # ---- 经营数据授权与评估 ----

    def grant_data_use(self, store_id, purpose, valid_from, valid_to):
        """门店依法授权经营数据的用途与期限。"""
        if purpose not in PURPOSES:
            raise ValueError(f"未知数据用途: {purpose}")
        if valid_to < valid_from:
            raise ValueError("授权期限无效")
        grant = DataUseGrant(store_id, purpose, valid_from, valid_to)
        self._grants.append(grant)
        return grant

    def _require_grant(self, store_id, purpose, on):
        if purpose not in PURPOSES:
            raise ValueError(f"未知数据用途: {purpose}")
        for grant in self._grants:
            if (
                grant.store_id == store_id
                and grant.purpose == purpose
                and grant.covers(on)
            ):
                return grant
        raise DataUseNotAuthorized(
            f"门店未授权 {purpose} 用途在 {on.isoformat()} 使用其经营数据"
        )

    def record_employment(self, store_id, headcount, at):
        """在就业带动用途授权期限内登记就业人数。"""
        if headcount < 0:
            raise ValueError("就业人数不能为负")
        self._require_grant(store_id, PURPOSE_EMPLOYMENT, at)
        record = {"store_id": store_id, "headcount": headcount, "at": at.isoformat()}
        self._employment.append(record)
        return record

    def evaluate(self, store_id, purpose, on):
        """协会仅在授权用途与期限内评估存续、农产品增值或就业带动。"""
        self._require_grant(store_id, purpose, on)
        txns = [t for t in self._transactions.values() if t["store_id"] == store_id]
        if purpose == PURPOSE_CONTINUATION:
            auth = self._stores.get(store_id)
            return {
                "active": auth is not None and auth.status != STATUS_EXITED,
                "unique_transactions": len(txns),
            }
        if purpose == PURPOSE_AGRI_VALUE_ADD:
            return {
                "unique_transactions": len(txns),
                "gross_sales": sum(t["amount"] for t in txns),
            }
        records = [r for r in self._employment if r["store_id"] == store_id]
        latest = max(records, key=lambda r: r["at"], default=None)
        return {"headcount": latest["headcount"] if latest else 0}

    # ---- 公众摘要 ----

    def public_summary(self):
        """面向公众的克制摘要：当前有效门店、标准版本与退出原因。"""
        stores = sorted(
            store_id
            for store_id in self._stores
            if self._stores[store_id].status != STATUS_EXITED
            and self.effective_scope(store_id)
        )
        standards = [
            {"dish": standard.dish, "version": standard.version}
            for _, standard in sorted(self._standards.items())
            if standard.active
        ]
        exits = [
            {"store_id": auth.store_id, "reason": auth.exit_reason}
            for _, auth in sorted(self._stores.items())
            if auth.status == STATUS_EXITED
        ]
        return {"stores": stores, "standards": standards, "exits": exits}
