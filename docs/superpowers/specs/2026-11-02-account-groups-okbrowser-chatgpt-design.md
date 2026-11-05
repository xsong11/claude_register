# 账号分组管理、OKBrowser 窗口关联与 ChatGPT 注册 设计

日期：2026-11-02
状态：待确认

## 背景与目标

基于现有 `claude-register` 的架构，本次演进聚焦三个核心能力：

1. **账号分组管理（单分组归属）**：每个账号可归属一个分组（空字符串表示「未分组」），支持在注册、导入、新增、编辑、批量操作及导出/领取 API 中按分组管理与筛选。
2. **账号与 OKBrowser 指纹窗口关联**：当启用 OKBrowser 且勾选「保留窗口（`delete_after=false`）」时，自动把新建的 `okbrowser_window_id` 绑定到账号；后续该账号重跑（Claude 或 ChatGPT）优先复用该窗口；面板账号列表支持手动编辑/解绑 `window_id`、一键打开/关闭 OKBrowser 窗口，以及删除账号或点按钮时同步删除 OKBrowser 窗口。
3. **新增 ChatGPT 注册（同邮箱多平台共用一条账号记录）**：同一个邮箱可同时用于注册 Claude 与 ChatGPT（一邮多用），共用邮箱凭据（AnyMail 子 Key / Outlook OAuth2）、代理与 OKBrowser 指纹窗口；ChatGPT 注册自动生成（或复用）合规密码、过邮箱 6 位验证码与姓名/生日引导页，提取并落库 ChatGPT `session_token` 与 `access_token`。

---

## 一、数据模型与数据库迁移（`server/db.py`）

沿用现有 `_ACCOUNT_EXTRA_COLS` 自动 `ALTER TABLE` 补列机制，旧 SQLite 库启动即无感迁移，不破坏已有数据。

### 1.1 `accounts` 表新增列

| 列名 | 类型 | 说明 |
|---|---|---|
| `group_name` | `TEXT` | 账号所属分组名称，默认 `""`（未分组）；去除首尾空白存储 |
| `okbrowser_window_id` | `TEXT` | 关联的 OKBrowser 指纹浏览器窗口 ID（如 `"1829304"`），未关联时为 `""` |
| `chatgpt_status` | `TEXT` | ChatGPT 注册状态：`""`（未注册）/ `"pending"` / `"running"` / `"success"` / `"needs_manual"` / `"phone_required"` / `"failed"` |
| `chatgpt_password` | `TEXT` | ChatGPT 账号登录密码（若账号本身已有 `password` 且未单独指定，可复用或独立保存） |
| `chatgpt_session_token` | `TEXT` | ChatGPT Cookie 中的 `__Secure-next-auth.session-token` |
| `chatgpt_access_token` | `TEXT` | 从 `https://chatgpt.com/api/auth/session` 提取的 `accessToken`（JWT） |
| `chatgpt_token_expires_at` | `TEXT` | `accessToken` 过期时间（ISO 8601 UTC） |
| `chatgpt_account_id` | `TEXT` | ChatGPT user/account ID（从 `/api/auth/session` 提取） |
| `chatgpt_check_status` | `TEXT` | ChatGPT 凭据存活状态：`alive` / `dead` / `blocked` / `error` / `""` |
| `chatgpt_checked_at` | `TEXT` | ChatGPT 最近检测时间（ISO 8601 UTC） |
| `chatgpt_last_run_id` | `INTEGER` | 最近一次跑 ChatGPT 注册的 `runs.id` |

> **说明**：原有的 `status`、`session_key`、`check_status`、`checked_at`、`last_run_id` 及 OAuth 字段继续代表 **Claude** 维度，完全向后兼容现有脚本与开放 API。

### 1.2 `runs` 表新增列

`runs` 表同样增加自动补列逻辑 `_migrate_runs(conn)`：

| 列名 | 类型 | 说明 |
|---|---|---|
| `platform` | `TEXT` | 本次运行的目标平台：`"claude"`（默认）或 `"chatgpt"` |
| `group_name` | `TEXT` | 本次运行指定的账号分组（入库时写入账号 `group_name`，若账号已有分组且本次未指定则保留原分组） |

### 1.3 可编辑字段与导出字段扩展

- `db.ACCOUNT_EDITABLE_FIELDS` 扩展加入：
  - `"group_name"`
  - `"okbrowser_window_id"`
  - `"chatgpt_password"`
  - `"chatgpt_session_token"`
  - `"chatgpt_access_token"`
- `server/export.py` 的 `FIELDS` 新增导出字段：
  - `group_name`（标签 `group`，中文名「分组」）
  - `okbrowser_window_id`（标签 `okbrowserId`，中文名「OKBrowser 窗口 ID」）
  - `chatgpt_status`（标签 `chatgptStatus`，中文名「ChatGPT 状态」）
  - `chatgpt_password`（标签 `chatgptPassword`，中文名「ChatGPT 密码」，`secret=True`）
  - `chatgpt_session_token`（标签 `chatgptSessionToken`，中文名「ChatGPT sessionToken」，`secret=True`）
  - `chatgpt_access_token`（标签 `chatgptAccessToken`，中文名「ChatGPT accessToken」，`secret=True`）
  - `chatgpt_token_expires_at`（标签 `chatgptExpiresAt`，中文名「ChatGPT 令牌过期时间」）
- `DEFAULT_FIELDS` 保持原五项不变，确保老脚本导出格式逐字节兼容。

---

## 二、功能设计 1：账号分组管理

### 2.1 分组模型与规则

- 采用**单字段轻量分组**（`accounts.group_name`）：
  - 空字符串 `""` 代表「未分组」。
  - 分组名去除首尾空白，长度上限 64 字符。
  - 分组列表由两部分合集自动生成：
    1. 数据库当前所有账号去重后的非空 `group_name`；
    2. `config.yaml` 中可选维护的预设分组列表 `account_groups: list[str]`（使新建的空分组在尚无账号时也能在下拉框中展示和选择）。

### 2.2 后端接口（`server/app.py` & `server/open_api.py`）

1. **分组列表与管理**：
   - `GET /api/groups`：返回所有分组及每个分组下的账号计数 `[{"name": "主力号", "count": 12}, ...]` 以及 `ungrouped_count`。
   - `POST /api/groups`（body `{"name": "新分组"}`）：将分组名加入 `config.yaml` 的 `account_groups`（若已存在则幂等返回）。
   - `PATCH /api/groups/{name}`（body `{"new_name": "..."}`）：重命名分组（同步更新 `accounts` 表中 `group_name=name` 的行与 `config.yaml`）。
   - `DELETE /api/groups/{name}`（query `clear_accounts=true`）：删除分组并将原属该分组的账号置为 `group_name=""`（不删账号本身）。
2. **批量设置分组**：
   - `POST /api/accounts/batch-group`（body `{"emails": [...], "group_name": "..."}`）：批量将所选账号移动到目标分组（传 `""` 即移出分组变为未分组）。
3. **注册、导入、新增时指定分组**：
   - `POST /api/runs`、`POST /api/v1/register`：body 新增可选 `group_name: str`。
   - `POST /api/accounts/import`：body 新增可选 `group_name: str`，导入的新增/更新账号（当 `group_name` 非空时）自动归入该分组。
   - `POST /api/accounts` / `PATCH /api/accounts/{email}`：支持读写 `group_name`。
4. **导出与领取按分组过滤**：
   - `GET/POST /api/accounts/export`、`GET /api/v1/accounts/export`、`POST /api/v1/accounts/claim`：新增可选参数 `group`（`group=__ungrouped__` 表示仅未分组，具体名称表示对应分组，不传表示不限分组）。

### 2.3 前端交互（`Accounts.jsx` / `Register.jsx` / 对话框）

- **账号列表页（`Accounts.jsx`）**：
  - 筛选栏新增**分组下拉筛选器**（全部 / 未分组 / 各分组名 + 计数）及「分组管理…」小弹窗（新建、重命名、删除分组）。
  - 勾选账号后的批量操作栏新增**「移动分组…」**下拉按钮，一键把所选账号移入某分组或设为未分组。
  - 每行账号的元信息区若有 `group_name`，展示分组 Badge（如琥珀/青色描边标签）。
  - 搜索框支持同时匹配 `group_name`。
- **新增/编辑账号（`account-create-dialog.jsx`）与导入弹窗（`import-dialog.jsx`）**：
  - 增加「所属分组」输入框（带已有分组 `<datalist>` 自动补全，既可选已有分组也可直接输入新分组名）。
- **注册任务页（`Register.jsx`）**：
  - 注册表单增加可选「归属分组」选择/输入框，注册产出的账号直接落入该分组。

---

## 三、功能设计 2：账号与 OKBrowser 指纹浏览器窗口关联

### 3.1 核心生命周期与关联策略

当 `config.browser_mode == "okbrowser"` 时：

1. **窗口复用与新建（`claude_register/browser.py` -> `okbrowser_session`）**：
   - `okbrowser_session(settings, proxy=None, *, name="", window_id="")` 接收可选的已有 `window_id`，并把最终使用的 `window_id` 通过回调或上下文对象传回调用方：
     - **若传入了非空 `window_id`（账号已关联窗口）**：
       - 优先尝试直接调用 `client.start(window_id)` 启动该已有窗口（跳过 `client.create`）。
       - 若 OKBrowser 返回窗口不存在（用户在客户端手动删了），打印日志提示并自动回退为 `client.create(...)` 新建窗口，更新账号绑定的 `okbrowser_window_id`。
     - **若未传入 `window_id`**：
       - 调用 `client.create(name=window_name, group_id=settings.group_id, proxy=proxy)` 新建窗口。
   - **退出会话时的清理逻辑**：
     - 始终调用 `client.close(window_id)` 关闭浏览器进程。
     - 若 `settings.delete_after` 为 `True`（默认删除窗口）：
       - **仅当该窗口是本次新建且未要求保留时**调用 `client.delete(window_id)`，且落库时 `okbrowser_window_id` 置空 `""`。
       - 若该窗口是复用账号已绑定的窗口，即使全局开了 `delete_after=True`，也不主动删掉已绑定的老窗口（或者按显式策略：只有 `delete_after=False` 时保留并写入 `accounts.okbrowser_window_id`；`delete_after=True` 且新建时删窗口）。
     - 若 `settings.delete_after` 为 `False`（保留窗口）：
       - 不调用 `client.delete`，并将 `window_id` 写入返回的 `account["okbrowser_window_id"]` 及 `AccountRecord`，由 `runner.py` 持久化到 `accounts.okbrowser_window_id`（即便本次注册中途状态为 `needs_manual` 或 `phone_required`，只要窗口已创建且未删除，也把 `okbrowser_window_id` 记到账号上，方便用户点一键打开窗口继续人工处理！）。

2. **面板直接控制 OKBrowser 窗口（后端接口）**：
   - `POST /api/accounts/{email}/okbrowser/start`：
     - 检查账号是否有 `okbrowser_window_id`：
       - 若有，调 `OkBrowserClient(cfg.okbrowser_api_url).start(window_id)` 直接在宿主机唤醒打开该指纹浏览器窗口；
       - 若无，支持可选参数 `create_if_missing: bool = True`：用该账号的 `email` 和绑定的 `proxy` 在 OKBrowser 中新建窗口、绑定 `okbrowser_window_id` 落库并启动打开，同时注入已有 `session_key` / `chatgpt_session_token` Cookie（若存在）！
     - 返回 `{"ok": True, "window_id": window_id, "ws": ws, "created": created}`。
   - `POST /api/accounts/{email}/okbrowser/close`：
     - 对账号关联的 `okbrowser_window_id` 调用 `client.close(window_id)`。
   - `DELETE /api/accounts/{email}/okbrowser`（query `unbind_only: bool = False`）：
     - `unbind_only=True` 时仅清空数据库的 `okbrowser_window_id`；
     - `unbind_only=False` 时先调 `client.close(window_id)`（忽略已关闭错误）+ `client.delete(window_id)` 从 OKBrowser 客户端彻底删除该窗口，再清空数据库的 `okbrowser_window_id`。
   - **删除账号联动**：
     - `DELETE /api/accounts/{email}` 与 `POST /api/accounts/batch-delete` 增加可选参数 `delete_okbrowser: bool = False`。
     - 当 `delete_okbrowser=True` 时，删除账号前自动遍历待删账号中非空的 `okbrowser_window_id`，调用 OKBrowser `delete` 接口清理对应指纹窗口（失败记日志不阻断删库）。

3. **前端交互（`Accounts.jsx`）**：
   - 账号行元信息区：若 `a.okbrowser_window_id` 非空，显示徽章 `OKB #{a.okbrowser_window_id}`。
   - 账号行操作区：
     - 当配置为 OKBrowser 模式或账号已有 `okbrowser_window_id` 时，提供 **「打开窗口」** 按钮（调用 `/okbrowser/start` 在本地 OKBrowser 打开该账号专属环境；若账号尚无窗口但已启用 OKBrowser，可一键创建并绑定窗口打开）。
   - 编辑展开区（`AccountFields`）：
     - 可查看、手动填写或修改 `okbrowser_window_id`（支持把已有 OKBrowser 窗口 ID 手动绑给某账号）。
     - 若已绑定 `okbrowser_window_id`，在编辑区底部提供「关闭窗口」「解绑窗口」「删除 OKBrowser 窗口」快捷按钮。
   - 删除账号确认弹窗（`AlertDialog`）：
     - 若待删除账号中存在关联了 `okbrowser_window_id` 的账号，弹窗中展示勾选项 **`[x] 同时删除关联的 OKBrowser 指纹浏览器窗口（N 个）`**（默认勾选）。

---

## 四、功能设计 3：新增 ChatGPT 注册设计（一邮多用）

### 4.1 整体流程编排（`claude_register/chatgpt_flow.py` + `claude_register/chatgpt_browser.py`）

为了保持代码职责清晰、与 Claude 注册互不干扰，将浏览器公共底座（代理中继 `SocksRelay`、时区探测 `lookup_timezone`、Chromium 启动、OKBrowser CDP 会话 `okbrowser_session`、Cloudflare Turnstile 拟人点击 `click_turnstile`、截图 `screenshot`）留在 `browser.py`，新建：
- `claude_register/chatgpt_browser.py`：封装 ChatGPT（`https://chatgpt.com` / `https://auth.openai.com`）页面级 DOM 交互与凭证提取。
- `claude_register/chatgpt_flow.py`：编排邮箱准备 → 代理分配 → 浏览器注册 → 接码 → 引导页 → 提取凭证落盘。

### 4.2 一邮多用（同邮箱共用账号记录）策略

当触发 ChatGPT 注册时（无论从「注册任务」页还是「账号列表」页）：
1. **未指定邮箱**：
   - 自动通过 AnyMail 创建新邮箱（流程与 Claude 相同），入库一条新 `accounts` 记录，其 `chatgpt_status="success"`（此时 Claude `status=""`，后续可随时点「注册 Claude」复用该邮箱、代理和 OKBrowser 窗口）。
2. **指定已有邮箱（或从账号列表对已有 Claude/Outlook 账号点「注册 ChatGPT」）**：
   - 若该邮箱已在 `accounts` 表中：
     - **复用邮箱凭据**：优先使用账号已保存的 `mail_key` + `mail_base_url`（AnyMail 子 Key 或 Outlook OAuth2 凭据）；若子 Key 失效则自动用主 Key 修复。
     - **复用代理**：若本次未显式覆盖代理，自动复用账号已绑定的 `proxy`。
     - **复用 OKBrowser 窗口**：若 `browser_mode == "okbrowser"` 且账号已有 `okbrowser_window_id`，直接在同一个指纹浏览器窗口中打开 `chatgpt.com` 注册。
     - **密码生成与复用**：若账号已有 `chatgpt_password` 则使用它；否则若已有非空 `password` 且符合 OpenAI 密码要求（≥12 位）则复用，否则自动生成 16 位高强度随机密码（含大小写字母、数字、特殊字符）并写入 `chatgpt_password`（同时若原 `password` 为空也同步写入 `password`）。

### 4.3 ChatGPT 浏览器注册状态机（`chatgpt_browser.py`）

ChatGPT 当前注册链路（`https://chatgpt.com/` → 点击 `Sign up` / 直接打开注册入口 → `auth.openai.com`）：

1. **打开入口与过 Cloudflare 盾**：
   - 打开 `https://chatgpt.com/auth/login`（或首页识别 `Sign up` / `Create account` 按钮点击跳转 `auth.openai.com`）。
   - 复用 `cloudflare_challenge_visible` + `click_turnstile` 自动处理 Cloudflare 验证。
2. **填写邮箱（Email Step）**：
   - 定位邮箱输入框（`input[name='email']` / `input[type='email']` / `get_by_label("Email address")`），拟人按键输入邮箱并点击 `Continue`。
   - 若检测到账号已存在（直接跳登录密码框且 URL 指示 login），则用保存的密码尝试登录；若无密码则报错提示。
3. **设置密码（Password Step）**：
   - 定位密码输入框（`input[type='password']` / `input[name='password']`），填入生成的 16 位密码（例如 `secrets` 生成确保包含大小写+数字+符号，长度 16 位，满足 OpenAI ≥12 字符要求），点击 `Continue`。
4. **邮箱验证码轮询与填入（Verification Code Step）**：
   - 等待验证码输入框出现（`input[name='code']` / `input[autocomplete='one-time-code']` / 6 位数字输入框）。
   - 调用 `poll_client.poll_code(to=email, since=since, timeout=code_timeout)` 轮询 OpenAI 发来的 6 位数字验证码（OpenAI 邮件标题通常为 `Your ChatGPT code is XXXXXX` 或正文含 6 位码，现有 `DEFAULT_CODE_REGEX` + `FALLBACK_CODE_REGEX` 已原生支持，同时在 `chatgpt_flow.py` 中支持匹配主题中的 6 位数字）。
   - 填入 6 位验证码并提交。
5. **Onboarding 引导页（About You / 姓名与生日）**：
   - 循环探测引导页状态（类似 Claude 的 `finish_after_auth` 表驱动状态机）：
     - **姓名与生日页（Tell us about you / What's your name? / Birthday）**：
       - 填入 `Full name`（复用 `default_display_name(email)` 生成拟真英文名，如 `Alex Morgan`）。
       - 填入 `Birthday`：随机生成 1990–2001 年之间的合法生日（兼容 React Aria / 分段式 `month`/`day`/`year` spinbutton 输入、文本 `MM/DD/YYYY` 输入及 `<select>` 下拉框三种 DOM 形态）。
       - 点击 `Continue` / `Agree` / `Okay, let's go`。
     - **手机号验证检测**：若出现手机号绑定要求，截图 `chatgpt_phone_verify.png` 并抛 `PhoneVerificationRequired`，记为 `chatgpt_status="phone_required"`。
     - **欢迎弹窗 / 引导提示（Okay, let's go / Stay logged out 等）**：自动关闭或点继续，直到进入 ChatGPT 主聊天界面（`#prompt-textarea` 可见）。
6. **提取凭证（Session Token & Access Token）**：
   - **提取 `chatgpt_session_token`**：从 `page.context.cookies()` 读取 `__Secure-next-auth.session-token`（兼容非 HTTPS 下 `next-auth.session-token`）。
   - **提取 `chatgpt_access_token`**：在页面上下文请求或导航读取 `https://chatgpt.com/api/auth/session` 返回的 JSON：
     - `accessToken` → `chatgpt_access_token`
     - `expires` → `chatgpt_token_expires_at`
     - `user.id` / `account.id` → `chatgpt_account_id`
   - 只要拿到 `chatgpt_session_token` 或 `chatgpt_access_token` 即判定 ChatGPT 注册成功（`chatgpt_status="success"`）。

### 4.4 ChatGPT 存活检测（`claude_register/chatgpt_check.py`）

- 新增 `check_chatgpt_session(session_token, access_token, proxy)`：
  - 使用 `curl_cffi`（`impersonate="chrome"`，走账号绑定代理）请求 `https://chatgpt.com/api/auth/session`（附带 `__Secure-next-auth.session-token` Cookie 或 `Authorization: Bearer <access_token>`）：
    - HTTP 200 且返回有效 `accessToken` / `user` → `("alive", "有效")`，并顺便刷新最新的 `accessToken` 与 `expires` 落库！
    - HTTP 401 / 返回空 JSON `{}` → `("dead", "会话已失效")`
    - Cloudflare 403 盾 → `("blocked", "被 Cloudflare 拦截")`
    - 网络/代理异常 → `("error", ...)`
- 接口：`POST /api/accounts/{email}/check-chatgpt`，与 Claude 的 `/check` 对称。

### 4.5 面板与 API 的多平台整合

1. **注册任务页（`Register.jsx`）**：
   - 顶部增加**「目标平台」切换（Claude / ChatGPT / 同时注册 Claude + ChatGPT）**：
     - 选 `Claude`：走现有 Claude 注册。
     - 选 `ChatGPT`：走 ChatGPT 注册。
     - 选 `Claude + ChatGPT`：先建好邮箱/代理/OKBrowser 窗口跑完 Claude，紧接着复用同一邮箱、同一代理、同一 OKBrowser 窗口自动跑 ChatGPT（一次任务产出双平台账号）。
   - 运行历史列表每条记录显示平台徽章（`Claude` / `ChatGPT`）。
2. **账号列表页（`Accounts.jsx`）**：
   - 每行账号同时展示 Claude 状态徽章与 ChatGPT 状态徽章（如 `Claude: 成功 · 有效`、`ChatGPT: 成功 · AT`）。
   - 筛选器新增：
     - `chatgpt_success`（ChatGPT 已注册）
     - `chatgpt_pending`（ChatGPT 未注册）
     - `chatgpt_alive` / `chatgpt_dead`
   - 行操作与批量操作：
     - 支持单行点「注册 ChatGPT」或勾选一批已有 Claude/Outlook 账号点「批量注册 ChatGPT」。
     - 支持单行/批量「检测 ChatGPT」。
   - 编辑展开区：
     - 展示并可编辑/一键复制 `chatgpt_password`、`chatgpt_session_token`、`chatgpt_access_token`。
3. **开放 API（`server/open_api.py`）**：
   - `POST /api/v1/register`：body 新增可选 `platform: "claude" | "chatgpt" | "both"`（默认 `"claude"`）、`group_name: str`。
   - `GET /api/v1/accounts/export` 与 `POST /api/v1/accounts/claim`：新增可选 `platform: "claude" | "chatgpt"`（当 `platform="chatgpt"` 时，`claim` 按 `chatgpt_status='success'` 且有 `chatgpt_session_token` / `chatgpt_access_token` 的条件领取）。

---

## 五、测试计划

1. `tests/test_db.py`：
   - 验证旧库迁移补齐 `group_name`、`okbrowser_window_id`、`chatgpt_*` 列与 `runs.platform`。
   - 验证分组重命名、批量改分组、按分组查询与按平台 `claim_account`。
2. `tests/test_okbrowser.py`：
   - 验证 `okbrowser_session` 传入已有 `window_id` 时直接 `start` 不 `create`；窗口失效时回退 `create`。
   - 验证 `delete_after=False` 时保留窗口并回传 `window_id`；面板 `/okbrowser/start`、`/okbrowser/close`、`DELETE /okbrowser` 及删号联动清理窗口。
3. `tests/test_chatgpt.py`（新增）：
   - 密码生成器合规性（长度、字符集）。
   - OpenAI 验证码邮件提取（主题与正文）。
   - `chatgpt_browser.py` 引导页状态机（邮箱 → 密码 → 验证码 → 姓名生日 → 提取 sessionToken / accessToken）假页面单测。
   - `runner.py` 同邮箱先跑 Claude 再跑 ChatGPT（或单独跑 ChatGPT）不覆盖对方平台字段。
4. `tests/test_app.py` & `tests/test_open_api.py`：
   - 分组 CRUD、批量移动分组、导出/领取按 `group` 与 `platform` 过滤接口测试。
