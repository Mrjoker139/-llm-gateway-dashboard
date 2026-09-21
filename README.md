# LLM 网关仪表盘

本地 LLM 网关 + 用量看板。把 AI 编程工具的 `base_url` 指向本服务，流量经过时自动记录
**每次请求的 token 用量、首字延迟、总耗时**，并按「厂商 + 模型」聚合性能指标——
回答"哪个模型响应最快、哪个最省钱、这次 vibecoding 烧了多少 token"。

支持厂商：DeepSeek / Kimi / Google Gemini / 火山方舟 / SiliconFlow，以及任意 OpenAI 兼容端点（UI 里添加）。

## ⚠️ 使用前请读：这算不算"不规范使用"？

这个项目做的事情是：**转发你自己发出的请求，并抄下响应里的 `usage` 字段**。具体来说：

| 行为 | 说明 |
|---|---|
| 请求次数 | **严格 1:1 转发**，不会放大、不会自动重试、不会后台轮询厂商接口 |
| API key | 还是你自己的，请求内容也没有被改写（参数原样透传） |
| 计费 | 花的是你本来就该花的额度，网关记账本身零成本 |
| 数据留存 | 只记 token 数 / 延迟 / 状态码，**不存 prompt 和回复内容** |

所以从厂商视角看，这仍然是"你本人在用自己的额度"。但以下几点请自己判断：

- **订阅制套餐（Kimi Coding Plan、火山 Coding Plan）** 的条款通常限制使用方式。
  个人自用、单机、不转售是常规做法，但**不要把这个网关公开给他人使用** ——
  那才构成事实上的"转售/反代"，是会被封号的场景。代码默认只监听 `127.0.0.1`，请保持这样。
- **网关会透传客户端的 `User-Agent`**（如 `ZCode/...`），而不是发 `python-httpx`。
  后者是扎眼的"非官方客户端"特征，透传真实工具名更接近正常流量。
- **Antigravity 相关功能**（配额查询、基准测试）走的是 Google 内部 API + 你的
  Antigravity OAuth 凭据。这是社区常见做法，但严格说属于非官方客户端访问，
  请自行评估。节点检测会真实切换代理节点，注意频率。
- **本项目与各厂商无任何关联**，仅供个人学习与自用。

## 快速开始

```bash
git clone https://gitee.com/czb_1392740286/llm-gateway-dashboard.git
cd llm-gateway-dashboard
pip install -r requirements.txt
cp .env.example .env      # 填入你的 API key (也可以启动后在网页上填)
python app.py
```

打开 <http://127.0.0.1:8787> → 进「厂商配置」页，直接在网页上填 API key（**改完立即生效，不用重启**）。

## 配置方式

三种方式，优先级从低到高：

1. `.env` 文件（适合脚本化部署）
2. **网页 UI**（推荐）— 存在 `config.json`，改完即时生效
3. 环境变量（最高优先级，空值会被忽略）

UI 里还能**添加自定义 OpenAI 兼容端点**（智谱 GLM、通义、本地 Ollama、任何中转站），
填个名字 + Base URL + Key 就行，不需要改代码。

> `.env` 和 `config.json` 都已在 `.gitignore` 里，不会被提交。

## 工作原理

```
你的工具 (Claude Code / Cline / 任何 OpenAI 客户端)
          │  base_url = http://127.0.0.1:8787/v1
          ▼
    ┌──────────────────────┐
    │  网关 (gateway.py)    │  ① 收到请求
    │                      │  ② 转发给真正的厂商
    │                      │  ③ 边转发边统计 TTFT / token / 耗时
    │                      │  ④ 写入 usage.db
    └──────────────────────┘
          │
          ▼
    厂商 API (DeepSeek / Kimi / Gemini / 火山)
```

**token 数不需要自己数**——厂商在响应的 `usage` 字段里返回；流式请求通过
`stream_options.include_usage` 让厂商在最后一帧带上用量。

## 接入方式

把任何 OpenAI 兼容客户端的 base_url 改成本网关即可：

```bash
# 环境变量方式
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
export OPENAI_API_KEY=any-value          # 真实 key 在网关的 .env 里

# 模型名带厂商前缀, 网关据此路由
#   deepseek/deepseek-chat
#   moonshot/k3
#   gemini/gemini-2.5-flash
#   volcano/doubao-seed-1-6
```

也可以不带前缀，用请求头指定：`X-Provider: deepseek`

**会话分组**：工具带 `X-Session-Id` 或 `X-Request-Id` 时按它分组；否则按
「客户端 + 小时」自动聚合，这样每次 vibecoding 时段能自动归到一个会话里。

### 在 ZCode 里接入

ZCode 的「模型设置 → 添加供应商」表单，按这样填：

| 字段 | 填什么 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| **API 格式** | **`OpenAI Chat Completions`**（不是 Anthropic Messages） |
| API Key | 随便填一个非空值，例如 `gateway`（真实 key 在网关这边） |
| 模型列表 | 手动添加，模型名要**带厂商前缀**，例如 `moonshot/k3` |

⚠️ **API 格式必须选 OpenAI**。网关只实现了 OpenAI 的 `/v1/chat/completions`，
选 Anthropic Messages 会打 `/v1/messages`，网关返回 405。

⚠️ **模型名必须带厂商前缀**。ZCode 里添加的模型名会原样作为请求的 `model` 字段，
网关靠 `厂商/模型` 这个前缀决定路由到哪家。

也可以直接改 `~/.zcode/v2/config.json` 的 `provider` 段（改前先退出 ZCode）：

```json
{
  "provider": {
    "my-gateway": {
      "name": "本地网关",
      "kind": "openai",
      "options": {
        "apiKey": "gateway",
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKeyRequired": true
      },
      "source": "custom",
      "models": {
        "moonshot/k3": {
          "limit": {"context": 1000000, "output": 131072}
        },
        "volcano/deepseek-v4-flash": {
          "limit": {"context": 256000, "output": 32768}
        }
      }
    }
  }
}
```

配好之后，在 ZCode 里选 `本地网关` 作为供应商、选 `moonshot/k3` 之类的模型，
流量就会经过网关并被记账 —— 用量总览和模型性能页立刻有数据。

## 功能

### 用量总览
- KPI 卡片：总请求数 / token 消耗 / 平均首字延迟 / 平均耗时
- Token 消耗趋势图（按时间分桶）
- 按厂商、按模型的消耗分布

### 会话分析
- 每次会话的请求数、token 总量、平均首字延迟、**会话持续时长**
- 点进去看会话内每一次请求的明细

### 模型性能（核心视图）
按「厂商 + 模型」聚合，横向比较模型。每个指标给出 **avg / P50 / P90 / P95 / P99**，
并带一条按比例的长度条，方便一眼比出快慢：

| 指标 | 含义 | 判断标准 |
|---|---|---|
| **TTFT** | 首字延迟，响应起手速度 | <1s 好 / <3s 中 / >3s 差 |
| **耗时** | 单次请求总时长 | 越短越好 |
| **TPS** | 生成吞吐量（每秒输出 token） | 越大越快 |
| **成本** | 按单价表估算的花费 | 见下方「成本估算」 |

**来源列**区分数据从哪来：

| 标签 | 含义 |
|---|---|
| `网关` | 经过网关的真实流量，有 token 量和成本 |
| `基准` | Antigravity 直连测试（走不了网关），只有延迟和速度 |
| `网关+基准` | 两种数据都有，以网关流量为准 |

看 **avg 判断整体水平，看 P90/P99 判断最差情况**——长尾差才是卡顿感的来源。
TTFT 的 P99 和 avg 差得多，说明偶发慢请求很严重。

**为什么 TTFT 和 TPS 都要看**：TTFT 慢 + TPS 高 = 起手慢但吐字快（预填充慢的大模型）；
TTFT 快 + TPS 低 = 立刻响应但吐字磨叽。单看一个指标会误判。

点任意一行展开该模型最近 100 次请求的逐条明细（含客户端来源、是否流式、状态码）。

### 成本估算
内置一份常用模型的单价表（元 / 百万 token），按账本里的 token 量估算花费。
**表里没有的模型显示 `—`，不做猜测**。要覆盖或补充单价，在项目根目录建 `pricing.json`：

```json
{
  "volcano": { "doubao-seed-1-6": {"input": 0.8, "output": 2.0} },
  "moonshot": { "kimi-k2-0905-preview": {"input": 4.0, "output": 16.0} }
}
```

### 厂商配置
- 网页直接填 API key / Base URL，**保存即生效**（热重载，不用重启）
- 一键测试连通性（列出可用模型）
- 查询额度：
  - **Kimi Coding Plan** — 走 `/usages`，显示 5 小时 / 7 天两个时间窗的已用百分比、进度条和重置时间
  - **Kimi 开放平台**（`api.moonshot.cn`）— 走 `/users/me/balance`，显示账户余额
  - **DeepSeek / SiliconFlow** — 显示账户余额
  - **火山方舟** — 只有 API Key 时只能验证有效性；配上 AK/SK 可查真实余额（见下）
- 添加/删除自定义 OpenAI 兼容端点

### 火山方舟查余额（可选）
火山**没有**给 Coding Plan 用户提供额度查询接口（`api/coding/v3` 下所有路径都返回 404），
只有控制台能看。但如果你有火山主账号的 AK/SK，可以查账单余额：

```bash
# .env 里加上 (AK/SK 在火山控制台「访问控制」里创建)
ARK_ACCESS_KEY=AKLT...
ARK_SECRET_KEY=...
```

配上之后，余额查询会走费用中心的 `QueryBalanceAcct`（HMAC-SHA256 签名）。
注意这个查的是**账户现金余额**，不是 Coding Plan 的套餐剩余额度。

### Antigravity 配额（Google Pro 会员的用法）
- 复用 Antigravity 的 OAuth 凭据，查 33 个模型的剩余配额百分比和重置时间
- 对模型做流式基准测试（TTFT / 输出速度）

**需要先配置 OAuth 客户端凭据**（`.env` 里，见 `.env.example`）：

```bash
ANTIGRAVITY_CLIENT_ID=...
ANTIGRAVITY_CLIENT_SECRET=...
```

这两个是 **Antigravity 应用自带的安装型应用凭据**，不是你的个人凭据
（从安装目录或抓一次令牌刷新请求就能看到）。代码里不硬编码是为了避免被滥用。
另外需要 `~/.gemini/oauth_creds.json`（Antigravity 登录后自动生成）。

不配这两项的话，Antigravity 和节点检测功能会提示凭据未配置，**其他功能不受影响**。

**为什么它不经过网关**：Antigravity 走的是 Google 内部 API（OAuth 凭据 + 私有端点），
不是 OpenAI 兼容协议，所以没法像 Kimi/火山那样挂到网关后面。它的实测数据会**单独存下来**，
再合并进「模型性能」页，用紫色
<span>基准</span> 标签区分 —— 这类行没有 token 量和成本（不经过网关就统计不到），
但 TTFT 和 TPS 是真测出来的。

### Gemini 节点可用性检测
Google 对 Antigravity 的调用有地区风控：部分出口 IP 会被
`User location is not supported` 拒掉（**同一个国家不同 IP 段也可能一个通一个不通**）。

在「厂商配置 → Gemini 节点可用性」里**勾选要用的节点**（不勾就不测，避免无谓的节点切换），
点「检测勾选的节点」。每个节点默认测 3 次 —— 单次失败可能只是网络抖动，不作数。
**测完会自动切回你原来的节点**，不会改掉你的选择。

检测结论会标在「节点测速」页的节点名后面：

| 标记 | 含义 |
|---|---|
| <span>可用</span> | 每次都通 |
| <span>不稳</span> | 有时通有时不通（网络抖动或节点不稳） |
| <span>禁用</span> | 每次都因 Google 地区限制被拒，换节点才有用 |

结论存在 SQLite 的 `node_checks` 表里，**重启不丢**，不用重复测同一个节点。

### 节点测速 / Gemini 节点检测的兼容范围

这两块功能驱动的是**代理内核的控制 API**，用的是 Clash 风格协议：
`GET /proxies`（列节点）、`GET /proxies/{name}/delay`（单节点测速）、
`PUT /proxies/{group}`（切换节点）。所以**只要客户端提供兼容的 Clash 控制 API 就能用**：

| 客户端 | 控制 API | 代理端口 | 能否用 |
|---|---|---|---|
| **SakuraCat**（sing-box 内核） | `12440` | `12450` | ✅ 默认值 |
| **Clash Verge / mihomo** | `9090` | `7890` | ✅ 改环境变量 |
| **ClashX / Clash for Windows** | `9090` | `7890` | ✅ 改环境变量 |
| **V2Ray / Xray / Shadowsocks** | 无 | — | ❌ 没有控制 API |
| **WireGuard / 系统 VPN** | 无 | — | ❌ 没有节点概念 |

换客户端时，启动网关前设环境变量（默认值就是 SakuraCat）：

```bash
CLASH_API=http://127.0.0.1:9090 CLASH_PROXY=http://127.0.0.1:7890 python app.py
```

连不上时「节点测速」页会直接显示当前在连哪个地址、以及该怎么改。

**注意**：厂商请求（Kimi / 火山 / DeepSeek）**不走代理，全部直连**。
只有 Antigravity 相关功能（配额查询、基准测试、节点检测）才通过 `CLASH_PROXY` 出去 ——
因为 Google 有地区风控。所以代理挂了不影响正常聊天，只影响 Gemini 那些功能。

### 拥堵分析 — 什么时段该换模型
「模型性能」页底部按**本地小时**聚合，回答"这个模型有没有明显的拥堵时段"：

- 每小时一根柱子，越高越慢，颜色从绿到红
- **拥堵倍数** = 最慢时段 ÷ 最快时段，超过 2 倍说明有明显时段差异
- 卡片上直接标出最快/最慢是几点，以及样本数
- 每个模型**单独统计**，多个模型混用不会互相污染

**算法要点（避免误判）**：TTFT 里很大一部分是"预填充"——prompt 越长，首字延迟越大。
如果直接比较各时段的原始 TTFT，就会把"那个时段正好在处理大文件"误判成"那个时段模型拥堵"。
所以代码先对每个模型拟合 `TTFT ≈ a + b × 输入token`，把各请求折算到该模型的典型 prompt 长度
再比较（用中位数而非均值，抗离群值）。鼠标悬停能看到原始值。

实测对照：构造"模型本身没变慢、但 14–18 点 prompt 从 5.7k 涨到 57k token"的场景，
原始 TTFT 显示 5 倍差异（假警报），归一化后是 1.03 倍（正确识别为无拥堵）；
反过来构造"prompt 长度稳定、模型真慢 3 倍"的场景，仍能检出。

### 关于走网关的合规性

网关做的事是：**把你自己发出的请求转发给厂商，并抄下 `usage` 字段**。
你的 API key 还是你自己的，请求内容也没被改写（参数原样透传），所以
从厂商视角看仍然是"你本人在用自己的额度"。

不过有两点值得知道：

- **网关会透传客户端的 `User-Agent`**（比如 `ZCode/...`），而不是发
  `python-httpx`。后者是个很扎眼的"非官方客户端"特征 —— 透传真实工具名更像正常流量。
  如果你在意这点，可以确认厂商条款里对"本地代理/网关"的规定。
- **Kimi/火山的 Coding Plan 是订阅制**，厂商有权限制使用方式。
  自用、单机、不转售是常规做法，但**不要把这个网关暴露到公网给他人用** ——
  那才是真正会被判定为"反代/转售"的场景。代码里 `app.run(host="127.0.0.1")`
  只监听本机，请保持这样。

### 节点测速
- 通过 SakuraCat 控制 API 批量测速代理节点、切换、查出口 IP

**网关是怎么用你的 VPN 的**：它不启动也不控制 SakuraCat，只是**连接 SakuraCat 已经开着的
本地代理端口**（`http://127.0.0.1:12450`）和它的控制 API（`12440`）。
所以前提是 SakuraCat 在运行（托盘/后台进程活着就行，不必开系统代理开关）。
如果你关掉 SakuraCat，网关的普通请求不受影响（走直连），但 Antigravity 相关功能会失败。

## 关于成本

| 操作 | 是否消耗 token |
|---|---|
| 余额查询、模型列表、配额查询 | **否**，免费 |
| 通过网关的实际对话 | 是（本来就要花的） |
| 基准测试（TTFT） | 是，每次约 120 token（≈ ¥0.001） |

网关本身的记账**零成本**——它只是转发你原本就要发的请求，顺手把 `usage` 抄下来。

## 文件结构

```
llm-gateway-dashboard/
├── app.py             # Flask 入口: 网关路由 + 看板 API + 节点测速 API
├── gateway.py         # 网关核心: 转发 + 打点 + 记账
├── providers.py       # 多厂商适配层 (统一接口 + 额度查询)
├── usage_db.py        # SQLite 账本 + 查询 + 分位数/成本估算
├── antigravity.py     # Antigravity OAuth / 配额 / 基准测试
├── selftest.py        # 自测脚本 (假上游验证记账链路)
├── static/index.html  # 前端: 卡片网格仪表盘
├── requirements.txt   # 依赖 (flask, httpx)
├── pricing.json       # 可选: 覆盖内置单价表
├── .env.example       # key 配置模板
└── usage.db           # 账本 (自动生成, 已 gitignore)
```

## API

### 网关（给工具用）
| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容入口，自动记账 |
| `GET /v1/models` | 聚合所有已配置厂商的模型列表 |

### 看板
| 端点 | 说明 |
|---|---|
| `GET /api/usage/summary?hours=24` | 总览统计 |
| `GET /api/usage/by_provider` | 按厂商 |
| `GET /api/usage/by_model` | 按模型 |
| `GET /api/usage/sessions` | 会话列表 |
| `GET /api/usage/session/<id>` | 会话详情 |
| `GET /api/usage/timeline` | 时间线（画图用） |
| `GET /api/providers` | 厂商配置状态 |
| `POST /api/providers/<id>/config` | 保存厂商 key/base_url（热生效） |
| `POST /api/providers/custom` | 添加自定义端点 |
| `DELETE /api/providers/custom/<id>` | 删除自定义端点 |
| `POST /api/providers/<id>/balance` | 查余额 |
| `POST /api/providers/<id>/models` | 查模型列表（也用于连通性测试） |
| `GET /api/usage/model_stats?hours=168` | 按厂商+模型聚合性能（含 avg/P50-P99 分位数、成本、来源） |
| `GET /api/usage/model_detail?provider=x&model=y` | 单模型逐条明细 |
| `GET /api/usage/benchmark_detail?provider=x&model=y` | 单模型基准测试明细 |
| `GET /api/usage/congestion?hours=168` | 模型时段拥堵画像 |
| `GET /api/usage/congestion_hourly?hours=168&model=x` | 按小时的性能数据 |
| `GET /api/models/quota` | Antigravity 配额 |
| `POST /api/models/benchmark` | 模型基准测试（结果会写进账本） |
| `POST /api/node/gemini_check` | 批量检测勾选节点能否用 Gemini（测完自动切回） |
| `GET /api/node/gemini_check/<job_id>` | 检测进度 |
| `GET /api/node/checks` | 检测记录（持久化，键为节点名） |
| `GET /api/status` | 节点信息 |
| `POST /api/test` | 节点批量测速 |

## 已知限制

- **只支持 OpenAI 协议**：网关实现了 `/v1/chat/completions`，没有 `/v1/messages`。
  客户端（含 ZCode）的 API 格式要选 OpenAI / OpenAI-compatible，选 Anthropic 会 405
- **请求参数原样透传**：网关不替客户端补默认值。这是刻意的 —— Kimi 的 k3 系列
  只接受 `temperature=1`，网关若自作主张填 0.7 会直接 400。代价是客户端得自己带全参数
- **别把网关暴露到公网**：代码只监听 `127.0.0.1`。自用没问题，公开给他人用会构成
  事实上的"转售/反代"，那是会被厂商封号的场景
- **节点检测会切换节点**：虽然测完自动切回，但测试期间你的网络会短暂经过其他节点。
  默认每个节点测 3 次，请只勾选你真正打算用的节点
- **火山方舟没有额度查询接口**：`api/coding/v3` 下所有路径都是 404，套餐剩余额度只能去控制台看。
  配 AK/SK 能查账户现金余额（`QueryBalanceAcct`），但那不是套餐额度
- **Kimi 有两种计费方式**，接口不通用：Coding Plan 用 `/usages`（套餐窗口），
  开放平台用 `/users/me/balance`（余额）。代码按 base_url 自动选，并互相兜底
- **成本是估算值**，基于内置单价表；实际以账单为准。单价表可用 `pricing.json` 覆盖
- **TPS 会剔除退化样本**：生成窗口 <10ms 或输出 <10 token 的请求不参与统计，
  否则除出来的数字会把 avg / P99 带偏
- **拥堵分析需要样本量**：每个时段至少 3 次请求、且至少 2 个时段才参与比较；
  样本少时结果仅供参考。TTFT 已按 prompt 长度归一化，但若某时段的请求特征
  （如流式/非流式比例）差异极大，仍可能有偏差
- **节点检测会真实切换节点**：测完自动切回，但测试期间你的网络会短暂经过其他节点。
  默认每节点测 3 次，请只勾选真正打算用的节点。**同时只允许一个检测任务**，
  并发请求会被拒（409）—— 两个任务同时切节点会互相切乱
- **网关本身没有鉴权**：它只监听 `127.0.0.1`，所以本机之外访问不到。
  **不要改成 `0.0.0.0`** —— 那等于把你所有厂商的 key 和账本暴露到局域网
- **密钥存储**：`config.json` 和 `.env` 里是明文 key（这是本机自用工具的常规做法）。
  所有 API 返回的 key 都是脱敏的（`sk-kim***2LZE`），`server.log` 里也不记 key。
  但这两个文件本身请勿提交到公开仓库（本项目不是 git 仓库，不会误提交）
- **自定义端点的 base_url 可指向任意地址**：这是设计如此（要支持本地 Ollama、
  各种中转站）。但意味着**谁能访问这个网页，谁就能让你去请求任意 URL** ——
  所以又回到上一条：别把服务暴露出去
- Gemini API key 没有余额接口；订阅配额走 Antigravity 凭据
- 会话分组依赖工具是否发送会话标识头，否则按「客户端+小时」粗粒度聚合
- 数据存在本地 SQLite，默认保留全部（`usage_db.purge_older_than(days)` 可清理）
- 网关用 Flask 开发服务器，单机自用足够；如需高并发换 waitress/gunicorn
