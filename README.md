# Tabbit2API

把 **Tabbit 浏览器网页端**的模型能力反代成本地 **OpenAI / Claude 兼容接口**，并内置
**浏览器 OAuth 登录**导入账号。支持**国内版 / 国际版**两个板块、多账号池轮询与故障切换。

> 协议经真实环境实测验证（2026-10）：HMAC 签名、`/panel/session`、`/api/v1/chat/completion` SSE、
> `/api/commerce/quota/v1/usage` 真实额度。面板展示的会员等级 / 额度 / 模型全部来自上游真实数据，
> **不伪造任何会员标识**（见文末「合规」）。

## 功能

- **OAuth 浏览器登录导入** — 面板可选本机任意浏览器打开登录页，登录后自动捕获 `token` Cookie 并导入
- **两种启动模式**（解决 Google「此浏览器或应用可能不安全」）
  - **纯净模式（默认推荐）**：不带任何自动化标志启动真实浏览器，`navigator.webdriver=false`，
    与真人浏览器完全一致，Google / Apple 等第三方登录不再被拦截；登录后经 CDP 读取 Cookie
  - **加固模式**：Playwright 驱动，去掉 `--enable-automation` 并注入脚本抹除 webdriver 指纹
- **浏览器回退链** — 自动探测本机已安装的 Tabbit Browser / Chrome / Edge / Brave / Vivaldi 等，面板可选
- **双协议** — `POST /v1/chat/completions`（OpenAI）+ `POST /v1/messages`（Claude），均支持流式
- **国内版 / 国际版** — 两个独立板块，各自域名、账号、模型目录；模型 ID 带板块前缀区分
  - **国内版** = `web.tabbit.com`（前端 `tabbitCn`；遗留域名 `web.tabbit-ai.com` 同属国内版）
  - **国际版** = `web.tabbit.ai`（前端 `overseas`；国内网络直连返回 403 地区限制）
  - 账号按**登录域名自动归入对应板块**，不会错位
  - 模型 ID 形如 `cn/glm-5.3`、`intl/gpt-6-luna`（两板块有 14 个同名模型，必须前缀区分）
- **多账号池** — 轮询负载均衡，连续错误自动冷却，401/403 标记登录失效并切换下一账号
- **调用记录** — 记录每次调用的模型/账号/token/耗时/首字延迟/状态；统计按模型、按账号、
  按天分布；token 为本地估算，额度百分比来自上游真实接口
- **会话记忆** — 同一 API Key + 模型复用上游会话，多轮上下文连续；可在面板查看/清空
- **真实数据展示** — 会员等级、已用额度百分比、模型访问类型与倍率全部实时来自上游
- **无状态模式（默认）** — 每次请求用全新上游会话并发送完整对话 transcript，
  多轮上下文靠完整对话保持，行为可预期；如需复用上游会话，在面板把
  「会话缓存 TTL」设为 >0 即可（同一 API Key + 模型共享会话）
- **Web 管理面板** — 打开即用（无需登录）、账号增删/测活/刷新、API Key 自定义、模型目录

## 快速开始

### 方式一：双击 BAT（推荐）

直接双击 **`启动.bat`** 即可启动，双击 **`结束.bat`** 停止。

- 启动后会自动打开一个服务窗口显示实时日志，并在浏览器打开管理面板
- 端口从 **8123** 起自动探测，被占用则顺延（8124、8125…），实际端口会在启动时打印
- 关闭启动窗口不会停止服务；重复双击 `启动.bat` 不会启动第二个实例
- 启动器会记录 `.server.pid` / `.server.port`（已加入 `.gitignore`）

### 方式二：命令行

```bash
pip install -r requirements.txt
playwright install chromium        # OAuth 登录用浏览器内核
python start.py
```

启动后自动探测端口（从 **8123** 起，被占用则 +1）：

```
管理面板   http://127.0.0.1:8123/admin        （无需登录，打开即用）
OpenAI    http://127.0.0.1:8123/v1/chat/completions
Claude    http://127.0.0.1:8123/v1/messages
健康检查   http://127.0.0.1:8123/health
```

也可以直接用启动器（BAT 内部就是调用它）：

```bash
python launcher.py start       # 启动并打开面板
python launcher.py stop        # 停止
python launcher.py restart     # 重启
```

### 导入账号

1. 打开 `http://127.0.0.1:8123/admin`（无需登录，打开即用）
2. 在「账号管理」顶部选择板块（**国内版** = `web.tabbit.com`，**国际版** = `web.tabbit.ai`）
3. 选择要用哪个浏览器打开登录页，以及启动模式（默认**纯净模式**）
4. 点 **开始浏览器登录**，在弹出的浏览器里完成 Tabbit 登录
5. 服务自动捕获登录态、校验并拉取真实会员/额度/模型，账号随即入池
   （若登录域名与所选板块不符，会**按域名自动归入正确板块**并在面板提示）

## 接入示例

```bash
curl http://127.0.0.1:8123/v1/chat/completions \
  -H "Authorization: Bearer <你的APIKey>" \
  -H "Content-Type: application/json" \
  -d '{"model":"默认","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

```bash
# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:8123
export ANTHROPIC_API_KEY=<你的APIKey>
claude
```

| 配置项 | 位置 | 说明 |
|---|---|---|
| API Key | 面板「设置」 | 留空则不鉴权；面板本身无密码 |
| 模型选择 | 请求 body 的 `model` | 用 `板块/模型` 前缀，如 `cn/glm-5.3`、`intl/gpt-6-luna` |
| 板块限定 | 请求头 / 查询参数 | `X-Tabbit-Region: cn` / `intl`，或 `GET /v1/models?region=intl`；不传则返回全部板块 |
| 默认模型 | 面板「设置」 | 缺省 `默认`（free_unlimited，不耗额度） |
| 会话缓存 TTL | 面板「设置」 | `0` = 无状态（默认，每次新会话发完整对话）；`>0` 秒 = 复用上游会话 |
| 上下文预算 | `app/message_builder.py` → `DEFAULT_BUDGET` | 默认 16000 字符；超长历史自动裁剪（保留系统指令 + 最近对话） |
| 端口 | `config.json` → `server.port` | 起始端口，冲突自动 +1 |
| 调用记录保留 | `app/usage_log.py` → `MAX_ENTRIES` | 默认 2000 条，超出丢弃最旧 |

### 模型 ID 与板块前缀

`GET /v1/models` 默认返回**所有已配置板块**的模型，共 56 个（国内 23 + 国际 33）：

```bash
curl http://127.0.0.1:8123/v1/models?region=intl   # 只看国际版（33 个）
```

两板块有 14 个同名模型（GLM-5.3、Kimi-K3、DeepSeek-V4-Flash…），因此调用时用前缀：

```bash
# 国内版
-d '{"model":"cn/glm-5.3", ...}'
# 国际版（GPT-6 / Claude-Opus-5.5 / Gemini 等仅国际版有）
-d '{"model":"intl/gpt-6-luna", ...}'
# 裸名仍兼容：落到有账号的板块
-d '{"model":"glm-5.3", ...}'
```

中文模型名已英文化：`默认` → `cn/default`；`cn/默认` 与 `cn/default` 等价。

## 长对话与内容长度上限

上游对**单次请求内容**有硬上限，实测：

| 字符数 | 结果 |
|---|---|
| 20000 | ✅ 成功 |
| 25000 | ❌ `492` |

与内容形态无关（自然文本、无空格长串、多轮结构表现一致），且**国内版与国际版一致**。

超限时的错误文案是「欢迎使用 Tabbit 浏览器（…），可免费使用最全最先进的模型。」，
**与真实原因完全无关**，极易误判为账号或权益问题。

本项目做了两层处理：

1. **自动裁剪**（`app/message_builder.py`）：按 16000 字符预算拼装，
   保留系统指令 + 从最近往回累积对话；单条超长消息保留首尾并标注「中间已省略」；
   被丢弃的更早历史会插入 `[注意：更早的对话历史因长度限制已省略]`
2. **错误归一化**（`service._explain_error`）：把上述营销文案翻译为
   「请求内容过长（约 N 字符）被上游拒绝，实测上游单次上限约 2 万字符…」

> 因此长时间运行的 Agent（如累计几百步工具调用）不会再因上下文膨胀而失败，
> 但更早的对话细节会被裁剪 —— 这是上游限制，无法绕过。

## 调用记录

「调用记录」标签页能看到每次调用的明细与汇总，回答「我调了什么模型、花了多少额度」。

| 字段 | 来源 |
|---|---|
| 模型 / 接口 / 板块 / 账号 | 本地记录 |
| tokens（输入/输出） | **本地估算**（上游 SSE 不返回 usage 事件，实测仅 ready/message_chunk/thinking/title/finish） |
| 倍率 / 折算额度 | 模型 `display_multiplier` × 估算 tokens |
| 额度百分比 | **上游真实值**（`/api/commerce/quota/v1/usage`），调用后异步刷新回填，同账号 60s 内最多刷新一次 |
| 耗时 / 首字延迟 | 本地计时 |

记录持久化到 `data/usage.json`，重启不丢；可清空、可按板块/模型/状态筛选。

## 已验证的协议细节（实测）

| 项 | 值 |
|---|---|
| 认证 Cookie | `token`（HttpOnly JWT）+ `user_id` / `managed` / `NEXT_LOCALE` / `SAPISID` |
| 签名 key | `GET /chat/sign-key` 动态获取（各域名不同，实测 web.tabbit.ai 返回 `f8d0e6a7…`，web.tabbit.com 返回 `3a9b1c8d…`） |
| 签名 | `x-nonce = HMAC-SHA256(key, "{ts}.{uuid}.{sha256hex(body)}")`，`x-signature = uuid`，`x-timestamp = ms` |
| 版本头 | `x-req-ctx = base64("{version}({build})")`，**缺失或格式不符会返回 493「浏览器版本过低」** |
| 自证头 | `unique-uuid`：**国际版必需**，缺失则聊天直接返回 492（文案是营销语，实为权益校验）。结构为 32 位 hex，第 5 位是「默认浏览器」标记位，位置 `[2,7,11,14,18,21,25,28]` 编码时间 hex。本项目**如实填写标记位**，不伪造 Pro 权益 |
| 创建会话 | `POST /panel/session`（空 body）→ `chat_session_id` |
| 对话 | `POST /api/v1/chat/completion`（SSE），v1 即可覆盖免费与付费模型 |
| `selected_model` | 必须传模型列表里的 `display_name`（如 `GLM-5.3`），传 slug 会被服务端自动切换模型 |
| 真实额度 | `GET /api/commerce/quota/v1/usage?user_id=<uid>` → `member_level` / `usage_percentage` / 周期 |
| 无权限模型 | SSE `error` code `492`（v1）/ HTTP `403`（v3），已如实透传给调用方 |
| 内容长度上限 | **实测约 2 万字符**：20000 成功、25000 必失败（与内容形态无关）。超限同样返回 `492` 且文案是「欢迎使用 Tabbit 浏览器…」，**与真实原因完全无关**。本项目默认按 16000 字符预算裁剪历史，并把该文案归一化为可读提示 |

## 目录结构

```
app/
  main.py           应用装配与启动（端口探测）
  config.py         配置/加密存储
  tabbit_client.py  上游客户端（HMAC 签名 + SSE 解析）
  accounts.py       账号存储与多账号池
  service.py        会话缓存与对话编排
  usage_log.py      调用记录（环形缓冲 + 落盘）
  login.py          OAuth 浏览器登录导入
  model_catalog.py  模型别名 → 上游 display_name（含板块前缀解析）
  message_builder.py 消息拼装与长度预算（上游单次约 2 万字符上限）
  auth.py           对外 API Key 鉴权（面板无密码）
  routes/           openai_api · claude_api · admin_api
  static/index.html 管理面板
launcher.py         启动器（进程管理/端口探测/打开面板，供 BAT 调用）
启动.bat / 结束.bat  双击启动 / 停止
tools/              开发自测脚本（非服务运行必需）
  selftest.py       用浏览器导出的 Cookie 直接驱动 TabbitClient 验证协议
  seed_account.py   跳过面板直接把 Cookie 写入账号池（自测用）
```

## 合规说明

- 本项目按账号**真实**会员等级展示额度与可用模型；付费模型无权限时返回上游 `492`。
- **不实现**伪造 `unique-uuid` 默认浏览器标记等绕过会员校验的手段。
- 上游逆向接口可能随版本变动，仅用于个人学习与本地集成，请遵守 Tabbit 服务条款。
- 令牌经加密后存于 `config.json`，请勿提交到仓库（已在 `.gitignore` 忽略）。
