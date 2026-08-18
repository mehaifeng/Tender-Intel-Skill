# Tender Intel Skill（招标情报检索与飞书推送）

把「每天找招标信息 → 核实 → 结构化 → 推送飞书」做成一条固定管线的 Claude Skill。

面向 IVD 细分领域：**过敏原 / 自身免疫类诊断试剂**（sIgE、总 IgE、自身抗体、ELISA 酶免、化学发光、免疫荧光/印迹）与**免疫分析仪器**（化学发光免疫分析仪、全自动酶免工作站、酶标仪、洗板机等）。

核心理念：**检索要快而全，核实要严而准，推送只给业务能直接用的数据**。宁可零命中也不硬凑；无法核实的标注说明后照常推送，绝不静默丢弃。

## 五阶段管线

```
1. 检索    doubao_search.py 直连官方 API 跑固定 49 条 query（每天完全相同，不轮换、不随机）
              ↓  脚本内完成清单校验、跨 URL 去重、found_by_query 归因；结果落盘不进上下文
2. 去重    对照 data/seen.json，跳过已推送；识别后续公告转入更新流
              ↓
3. 核实    逐条打开原文，三级降级抓取，核对六项
              ↓  正文：curl → SPA 反查 API → 浏览器兜底；PDF 附件：curl 下载后用 Read 读
4. 结构化  按 schema.md 组织平铺 JSON
              ↓
5. 推送    创建流 / 更新流两个 Webhook，逐条 POST，成功后写 seen.json
```

**双流推送**，都进同一张飞书台账：

| 流 | 条件 | 用途 |
|---|---|---|
| 投标流 | `status: active` 且无指定供应商 | 可投标机会 |
| 研究流 | `status: intel` 且 `match_level` 非 none | 单一来源公示、中标/合同公告、已截止公告、采购意向——供中标统计与市场扫描 |

## 目录结构

| 路径 | 说明 |
|---|---|
| `SKILL.md` | 管线定义。含三级降级抓取策略与附件提取的机械步骤 |
| `references/keywords.md` | A/B/C/D 分层词表、每日固定 49 条 Query 清单、品牌词表、判定细则、清单裁撤规则 |
| `references/schema.md` | 定稿平铺 JSON、字段字典、seen.json 专属字段、大区判定、状态枚举、更新流结构 |
| `scripts/doubao_search.py` | 阶段 1 检索器，直连豆包官方 API。含清单校验、去重、归因，支持 `--dry-run` |
| `scripts/send_webhook.ps1` | 飞书推送脚本，支持 `-DryRun` 校验 |
| `config/doubao.example.json` | 检索配置模板；复制为 `config/doubao.json`（已 gitignore）填 API Key |
| `data/seen.json` | 去重表，存全量字段（更新流的原值回填依赖它） |
| `data/query_stats.json` | 每日每条 query 的 raw / unique / pushed 归因统计，裁撤零贡献 query 的依据 |
| `evals/` | 5 条 eval、59 条断言、4 个 fixture |

## 安装

### 依赖

- Claude Code（或其他支持 Skill 的 Agent）
- Python 3.9+ 与 `requests`（阶段 1 检索脚本）
- Windows + PowerShell（推送脚本为 `.ps1`）
- Git Bash 或等价 POSIX shell（附件提取用到 `grep -E`、`curl.exe`）

**本技能不依赖任何 MCP 服务器**（2026-08-18 起）。阶段 1 检索改为直连官方 API 的 Python 脚本，阶段 3 的 PDF 附件解析改用 Read 工具，两个 MCP 都已移除。

### 1. 豆包搜索（阶段 1）

复制配置模板，填入 API Key：

```bash
cp config/doubao.example.json config/doubao.json
```

Key 来自[火山引擎控制台 → 联网搜索 → API Key 管理](https://console.volcengine.com/search-infinity/api-key)。也可改用环境变量 `DOUBAO_SEARCH_API_KEY`，优先级高于配置文件。`config/doubao.json` 已在 `.gitignore` 中。

验证配置（不发请求、不花钱）：

```bash
python scripts/doubao_search.py --dry-run
```

> 2026-08-18 前这一步走第三方 MCP `huashu-doubao-search`，现已移除：它把 `Count` 卡在 20（官方 50）、把官方只有 0/1 的 `AuthInfoLevel` 自造成 1~4 分级、不暴露 `TimeRange` 闭区间与 `Sites`/`BlockHosts`，且以 `npx -y github:...` 每次会话拉 HEAD 执行并持有 API Key。

**调用量与配额**：49 条/天 × 30 天 = 1470 次/月，而火山账号每月免费额度 500 次——本管线在付费区间运行。账号维度默认 5 QPS，脚本按 4 QPS 限流，全量跑一轮约 30 秒。

### 2. 飞书

技能推送到飞书多维表格的 Webhook。需要在飞书侧准备两个 Webhook（创建流 / 更新流），地址写入 `SKILL.md` 与 `send_webhook.ps1`。

飞书字段有三条硬约束，违反会报字段识别错误：

- **平铺**：无嵌套对象
- **每次一条**：不接受数组，多条必须逐条推送
- **无 JSON null**：空字符串填 `"null"`、空数字填 `0`、布尔填 `false`

另外 `region`、`category`、`matched_category`、`status` 都是**单选字段**——`schema.md` 里列出的枚举值必须在飞书字段选项里逐一存在，否则该值写入失败。

## 使用

在会话里直接说：

```
跑一遍今天的招标情报
```

或者更具体的：检索招标信息并推送到飞书 / 生成今日招标简报 / 最近有没有过敏原 sIgE 试剂的招标。

### 单独校验载荷

推送前可以先校验，不发请求：

```bash
powershell -File scripts/send_webhook.ps1 -PayloadPath payload.json -DryRun
```

它会检查「单条 / 平铺 / 无 JSON null」三条硬约束并打印载荷。退出码：`0` 通过、`1` 文件或 JSON 错误、`2` 校验不通过。

## 评估

`evals/evals.json` 供 [skill-creator](https://github.com/anthropics/claude-code) 使用，`expectations` 是 grader 的打分依据。

| # | 场景 | 前提 |
|---|---|---|
| 1 | 每日全量跑 | 需要豆包 API Key，**会真推飞书** |
| 2 | 过敏原 sIgE 定向查 | 需要豆包 API Key |
| 3 | 更新流·中标公告 | 离线，**会真推飞书更新流** |
| 4 | 每日自查·截止转 closed | 离线，**会真推飞书更新流** |
| 5 | 附件与联系方式提取 | 完全离线，不推送 |

Eval 5 用两个真实公告页面做 fixture，覆盖附件提取的两类坑：**相对路径需 origin 补全**、**附件挂在跨域 OSS 上**。不依赖任何 MCP 与网络，任何环境都能跑，适合作为改动后的回归。

Eval 3、4 使用的 `record_id` 在真实飞书表里不存在，跑完可能留下孤儿更新记录。

## 已知约束

**`send_webhook.ps1` 必须是 UTF-8 with BOM。** Windows PowerShell 5.1 会把无 BOM 的 `.ps1` 按 GBK 解码，中文注释误解码后会吞掉引号，导致整个脚本解析失败——而报错位置与真实原因完全无关，极难排查。用会剥离 BOM 的编辑器改这个文件会直接把它改坏。

**附件是 `href` 属性，不是可见文字。** 把页面当纯文本读必然漏掉附件，即使读到了附近的联系方式也一样。SKILL.md 阶段 3 规定了对原始 HTML 执行 grep 的机械步骤，以及三条规则：只看扩展名、相对路径必须补全为绝对 URL、不做同域过滤。

**`data/seen.json` 在仓库里只保留空骨架。** 本地跑起来后累积的业务情报不再提交，靠 `git update-index --skip-worktree data/seen.json` 实现。这是本机设置、不随仓库走，新 clone 需要重新执行一次。
