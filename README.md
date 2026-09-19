# 地域餐饮品牌准入

服务用于维护地域餐饮品牌的标准、门店授权与动态退出，让技艺差异和质量底线同时得到保留。

运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可确认服务身份。加 `--snapshot data.json`（或环境变量 `BRAND_SNAPSHOT`）可把全部登记数据落盘，重启后恢复。

测试：`npm test`（内部运行 `python3 -m unittest -v service_contract`，含领域规则与 HTTP 契约共 28 项）。

## 需求如何落到规则

只发牌匾管不住原料与工序、统一配方又会抹平门店技艺——领域模型把准入建立在五套可核验事实上，而不是一块牌子：

| 关切 | 规则落点（`domain.py`） |
| --- | --- |
| 代表菜品的标准版本 | `publish_standard`：必须写明关键工序与核心食材；同菜发布新版本时旧版本自动 `superseded`，门店申请钉住具体 `standard_id` 与版本号 |
| 允许变体、保留技艺差异 | 变体随每个标准版本登记（`Variant.standard_id`），申请逐菜声明使用哪些变体，越界变体直接拒绝；门店授权按“店 × 菜 × 版本 × 变体”发放 |
| 核心食材产区 | `Ingredient.origin_region` + `designated`；采购证据须逐项覆盖，农产品增值成效只统计指定产区食材 |
| 师傅传承关系 | `masters` 与师承关系（师傅—老师—流派），禁止自指；申请可挂传承师傅 |
| 门店授权 | 评审通过后生成 `Authorization`，可按菜分别生效、停权、撤回 |
| 可验证证据 | 采购 / 制作（逐关键工序）/ 服务三类证据，必须给 `doc_ref`；只有被秘书处核实（`verified`）的证据才计入覆盖度，被驳回的不算 |
| 评审回避利益冲突 | `start_review` 比对评审员登记的关联人、关联门店与申请门店的关联人/师傅；冲突即整组拒绝并要求换人；回避后的评审组逐菜投票，多数通过 |
| 准入范围由证据决定 | 证据缺哪道菜，就准入不了哪道菜——证据覆盖度与投票逐菜取交集，可只准入部分菜品 |

### 动态存续：整改、局部停权、退出

- **抽查不合格** → 门店进入 `rectifying` 限期整改（默认 15 天，带到期日），期间不出现在公众“当前有效门店”；整改通过恢复。
- **供应中断 / 知识产权异议** → 先对受影响菜品**局部停权**（其他菜不受影响，必须指明菜品）；供应恢复可恢复授权。
- 整改失败、供应无法恢复或异议成立 → **退出**：全店授权撤回，在用标识在退出日截止；退出原因（抽查不合格 / 供应中断 / 知识产权异议）对公众克制披露。
- **历史不删除**：标识在各渠道与商品上的使用记录（含已退出店、已截止渠道）始终可查，`/api/logo-usage` 默认返回全部历史。

### 数据边界与去重

- 门店依法**逐用途、逐期限**授权（`survival` 存续评估 / `value_add` 农产品增值 / `employment` 就业带动）；早于授权日、授权过期或撤销后的数据一律不得汇入或用于评估。
- 同一 `order_no` 从外卖、堂食、零售重复汇入只计一次——去重键刻意不含渠道与金额（平台费用微调不会让同一交易被多算）；无订单号时保留渠道区分，避免误并不同交易。
- 成效汇总只输出已授权用途；对公众的 `/public/summary` 只含当前有效门店（名称 + 授权菜品及标准版本/变体）、当前标准版本（含产区与允许变体）和退出原因，不含地址、证据、经营数据。

## HTTP 接口

所有理事会写操作是 `POST /api/...`，成功返回 `{"ok": true, "data": ...}`，违反领域规则返回 `400 {"error": ...}`。

- 标准与传承：`/api/ingredients`、`/api/dishes`、`/api/standards`、`/api/masters`、`/api/lineage`、`/api/reviewers`
- 准入：`/api/applications`（已准入门店加菜传 `store_id`）、`/api/evidence`、`/api/evidence/verify`、`/api/evidence/reject`、`GET /api/applications/{id}/coverage`、`/api/reviews`、`/api/reviews/ballot`、`/api/reviews/finalize`
- 存续：`/api/incidents`（`kind`: `inspection_failure` / `supply_disruption` / `ip_dispute`）、`/api/incidents/resolve`（`rectified` / `restored` / `exit`）
- 标识与数据：`/api/logo-usage`、`/api/logo-usage/end`、`GET /api/logo-usage`、`/api/consents`、`/api/consents/revoke`、`/api/records`、`GET /api/metrics`、`GET /api/stores/{id}`
- 公众：`GET /public/summary`；健康：`GET /health`
