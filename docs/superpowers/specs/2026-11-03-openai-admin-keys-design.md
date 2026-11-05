# OpenAI Admin Key 管理（第一批：费用、剩余额度、项目 Key）设计

日期：2026-11-03
状态：已实现

## 背景与目标

在面板中新增独立页面「OpenAI」，录入多个 OpenAI Platform 的 Admin API Key（`sk-admin-...`，每个 Key 对应一个组织）。第一批只做三件事：

1. **多 Key 管理**：增、删、改、校验。
2. **费用与剩余额度**：每个 Key 显示本月费用、起算日至今费用、剩余额度。
3. **项目 Key 管理**：列出组织下所有项目的 API Key，新建、删除。

本页面独立，不与账号、注册、sub2api 等模块联动。

---

## 一、剩余额度的算法

OpenAI 没有任何 API Key 可用的余额接口（实测旧计费接口返回 403，只能用浏览器登录态调用），所以剩余额度用本地数据计算：

```
剩余 = 额度 − 起算日至今的费用（Costs API）
```

- 每个 Key 可填「额度（USD）」和「起算日期」（默认为录入当天）。比如充值了 $50，就填 50 和充值日期。
- 不填额度则只显示费用。
- 费用数据有数小时延迟，按 UTC 自然日统计，界面标注「以 OpenAI 后台为准」。

---

## 二、数据模型（`server/db.py`）

新增一张表 `openai_admin_keys`：

| 列名 | 类型 | 说明 |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | 本地 ID |
| `name` | `TEXT` | 备注名，必填 |
| `admin_key` | `TEXT` | 明文，接口只回显掩码 `sk-admin-…abcd` |
| `org_id` | `TEXT` | 校验时从响应头 `openai-organization` 读取（个人组织为 `user-...`，响应头里是全小写） |
| `credit_usd` | `REAL` | 额度，可空 |
| `credit_since` | `TEXT` | 起算日期 `YYYY-MM-DD`（UTC） |
| `status` | `TEXT` | `ok` / `invalid` / `error` / `""` |
| `last_error` | `TEXT` | 最近错误（不含 Key） |
| `cost_month_usd` | `REAL` | 最近一次刷新得到的本月费用 |
| `cost_since_usd` | `REAL` | 最近一次刷新得到的起算日至今费用 |
| `refreshed_at` | `TEXT` | 最近刷新时间 |
| `created_at` | `TEXT` | 录入时间 |

生成的项目 Key 不在本地保存，只在创建时显示一次。

---

## 三、OpenAI 客户端（`server/openai_admin.py`，新增）

- 基于 `httpx.Client`，`base_url="https://api.openai.com/v1/organization"`，`Authorization: Bearer <admin_key>`。直连，不做代理。
- 模块级 `TRANSPORT = None`，测试时注入 `httpx.MockTransport`。
- 错误统一抛 `OpenAIAdminError`，消息不含 Key：
  - 401 → 「Admin Key 无效或已被撤销」（不透传原文，原文里带有 Key 掩码）
  - 其他非 2xx → `HTTP {code}：{error.message}`（截断 200 字）
  - 网络异常 → 「连接 api.openai.com 失败」

用到的接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/projects?limit=1` | 校验 Key，读响应头里的组织 ID |
| `GET` | `/costs` | 费用：`start_time`、`bucket_width=1d`、`limit=180`，跟 `next_page` 翻页，累加 `amount.value` |
| `GET` | `/projects` | 项目列表（不含已归档） |
| `GET` | `/projects/{id}/api_keys` | 项目 Key 列表 |
| `POST` | `/projects/{id}/service_accounts` | 新建 Key（OpenAI 只能通过服务账号用 API 生成 Key），响应里有一次性明文 |
| `DELETE` | `/projects/{id}/api_keys/{key_id}` | 删除用户创建的 Key |
| `DELETE` | `/projects/{id}/service_accounts/{sa_id}` | 删除服务账号 Key（连同服务账号一起删） |

实测得到的注意点：

- Costs 不传 `limit` 默认只返回 7 天，必须显式传并翻页；`limit` 上限 180；只支持 `bucket_width=1d`。
- 不传 `end_time`（传了必须比 `start_time` 晚至少 1 天）。
- 起算日超过 180 天时自动翻页拉全。

---

## 四、面板接口（`server/app.py` 注册 `register_openai_routes`）

全部要求面板登录。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/openai/keys` | 列表：名称、掩码、组织 ID、状态、本月费用、起算日至今费用、额度、剩余、刷新时间（读库，不请求 OpenAI） |
| `POST` | `/api/openai/keys` | 新增 `{name, admin_key, credit_usd?, credit_since?}`；先校验，401 拒绝保存，成功后顺带刷新一次费用 |
| `PATCH` | `/api/openai/keys/{id}` | 修改；`admin_key` 传空 = 不修改 |
| `DELETE` | `/api/openai/keys/{id}` | 删除本地记录 |
| `POST` | `/api/openai/keys/{id}/refresh` | 校验并刷新费用 |
| `GET` | `/api/openai/keys/{id}/api-keys` | 遍历所有项目，返回扁平列表：项目名、Key 名、`redacted_value`、类型（用户 / 服务账号）、创建时间、最近使用时间 |
| `POST` | `/api/openai/keys/{id}/api-keys` | `{project_id, name}` → 返回明文 Key（仅此一次） |
| `DELETE` | `/api/openai/keys/{id}/api-keys/{key_id}?project_id=` | 按类型调对应删除接口 |

---

## 五、前端（`web/src/pages/OpenAI.jsx`，新增）

侧边栏新增「OpenAI」一项。页面只有一个列表：

- 每行：名称、组织 ID、掩码 Key、状态、本月费用、剩余 / 额度（进度条，剩余不足 10% 标红）、刷新时间；操作：刷新、编辑、删除、展开。
- 顶部：「新增 Admin Key」。新增 / 编辑弹窗字段：名称、Admin Key、额度、起算日期。
- 展开行：该组织的项目 Key 表格，可删除；「新建 Key」选项目、填名称，成功后弹窗显示明文并提示「关闭后无法再查看」。

---

## 六、测试计划

1. `tests/test_openai_admin.py`（`httpx.MockTransport`，假响应按第八节真实结构构造）：
   - 401 / 其他错误 / 网络异常的消息映射，不含 Key。
   - Costs 请求带 `limit=180`、不带 `end_time`，多页累加正确；空 `results` 计为 0。
   - 从小写响应头读取组织 ID。
   - 新建 Key 解析出明文；删除按类型调对应接口。
2. `tests/test_db.py`：新表建表与增删改查。
3. `tests/test_app.py`：接口增删改查；响应中不出现完整 Key；校验失败拒绝保存；剩余额度计算。

---

## 七、后续可选（第一批不做）

- 用量统计（token、请求数，按模型 / 项目 / Key 拆分）。
- 费用按日、按项目明细与日期区间查询。
- 项目创建 / 归档、限速查看、组织 Admin Key 列表。
- 在本地留存生成的 Key 明文并导出。
- 每个 Key 单独配置代理。
- 定时刷新与余额不足提醒。
- 用浏览器登录 platform.openai.com 读取官方余额。
- 开放 API（`/api/v1`）。

---

## 八、实测记录（2026-11-03，真实 Admin Key，只读）

测试组织为个人组织（`openai-organization: user-...`），1 个项目（`Default project`），近 30 天无消费。

| 请求 | 结果 |
|---|---|
| `GET /organization/projects` | 200；响应头带 `openai-organization`（小写）；项目字段 `id`、`name`、`status`、`archived_at` |
| `GET /organization/costs` | 200；无消费的日期为 `results: []`；不传 `limit` 默认 7 个桶，`has_more` + `next_page` 翻页正常 |
| `GET /organization/costs?limit=181` | 400：上限 180 |
| `GET /organization/costs?bucket_width=1h` | 400：只支持 `1d` |
| `GET /organization/costs`（`end_time` < `start_time`） | 400：结束时间必须晚于开始时间且至少相差 1 天 |
| `GET /organization/projects/{id}/api_keys`、`/service_accounts` | 200；当前均为空列表 |
| `GET /v1/dashboard/billing/credit_grants`、`/subscription` | 403：只能用浏览器登录态调用 |
| `GET /v1/models` | 403：缺少 `api.model.read`，Admin Key 不能用于推理 |
| 无效 Key | 401，`error.code=invalid_api_key`，`message` 回显 Key 掩码 |
| 网络 | 本机直连可用 |
| `POST /organization/projects/{id}/service_accounts` | 200；`api_key.value` 为 `sk-svcacct-...` 明文；服务账号 ID 前缀是 `user-` |
| `GET /organization/projects/{id}/api_keys`（建完后） | 新 Key 的 `owner.type=service_account`，`redacted_value` 约 150 字符（中间全是 `*`），界面需截短 |
| `DELETE /organization/projects/{id}/service_accounts/{sa_id}` | 200；之后项目 Key 列表为空 |

写操作已通过面板接口端到端实测（建测试 Key 后立即删除）。
