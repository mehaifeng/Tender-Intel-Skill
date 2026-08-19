# Tender Intel Skill（招标情报检索与飞书推送）

把「每天找招标信息 → 核实 → 结构化 → 推送飞书」做成一条固定管线的 Agent Skill。

面向 IVD 细分领域：**过敏原 / 自身免疫类诊断试剂**（sIgE、总 IgE、自身抗体、ELISA 酶免、化学发光、免疫荧光/印迹）与**免疫分析仪器**（化学发光免疫分析仪、全自动酶免工作站、酶标仪、洗板机等）。

核心理念：**检索要快而全，核实要分层，推送要可追溯**。标题先排除科普、营销等非招采内容；Doubao Summary/Content足以确认目标品类但源页打不开时，可作为绑定正文哈希的飞书`manual`线索。可投标与情报记录仍要求源页核实。

## 五阶段管线

```
1. 检索    doubao_search.py 直连官方 API 跑固定 49 条 query
              ↓  完整正文逐条落盘；stdout 只预览 20 条；另存轻量运行摘要
2. 排队    tender_pipeline.py 做 seen 去重、标题招采意图预筛、Content品类信号和 5 条一批
              ↓  manifest 状态机可中断续跑
3. 核实    模型只读取当前批次；优先核实 source_url 与附件
              ↓  源页证据→active/intel/manual；标题+Content证据→仅manual；品类不明→本地manual
4. 校验    创建流 26 字段 / 更新流 14 字段 + 字段证据
              ↓  校验通过才导出 payloads
5. 推送    默认 DryRun；显式 -Live 生成成功回执，核验回执后才更新 seen
```

**双流推送**，都进同一张飞书台账：

| 流 | 条件 | 用途 |
|---|---|---|
| 投标流 | `status: active` 且无指定供应商 | 可投标机会 |
| 研究流 | `status: intel` 且 `match_level` 为 full/partial/unknown | 单一来源公示、中标/合同公告、已截止公告、采购意向——供中标统计与市场扫描 |
| 人工线索 | `status: manual` 且 `requires_manual: true` | 第三方页面已核实但脱敏，或标题明确招采且Doubao Content明确品类，进入飞书待人工补充；仍走创建Webhook |

## 目录结构

| 路径 | 说明 |
|---|---|
| `SKILL.md` | 简洁的运行契约、状态机与推送权限边界 |
| `references/keywords.md` | A/B/C/D 分层词表、每日固定 49 条 Query 清单、品牌词表、判定细则、清单裁撤规则 |
| `references/schema.md` | 定稿平铺 JSON、字段字典、seen.json 专属字段、大区判定、状态枚举、更新流结构 |
| `references/verification.md` | 原文与附件核实、提示注入防护、批次结果与证据协议 |
| `scripts/doubao_search.py` | 阶段 1 检索器，直连豆包官方 API。含清单校验、去重、归因，支持 `--dry-run` |
| `scripts/tender_pipeline.py` | 状态机、轻量队列、seen过滤、批次校验和载荷导出；不发送网络请求 |
| `scripts/send_webhook.ps1` | 飞书推送门禁；默认拒绝发送，支持 `-DryRun`，生产必须显式 `-Live` |
| `config/doubao.example.json` | 检索配置模板；复制为 `config/doubao.json`（已 gitignore）填 API Key |
| `data/seen.json` | 去重表，存全量字段（更新流的原值回填依赖它） |
| `data/query_stats.json` | 每日每条 query 的 raw / unique / pushed 归因统计，裁撤零贡献 query 的依据 |
| `evals/` | 覆盖全量检索、更新流、附件提取、自动模式、第三方脱敏与Doubao证据兜底 |

## 安装

### 依赖

- Codex、Claude Code 或其他支持 `SKILL.md` 的 Agent
- Python 3.9+ 标准库（不需要安装Python第三方包）
- Windows PowerShell 5.1+ 或 PowerShell 7+（仅生产推送需要）
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

技能推送到飞书多维表格的两个 Webhook。URL 不写入仓库，运行环境分别设置 `FEISHU_CREATE_WEBHOOK_URL` 与 `FEISHU_UPDATE_WEBHOOK_URL`。生产发送必须显式传入 `-Live`和本次运行的`-ManifestPath`；没有这些门禁时脚本拒绝联网。

飞书字段有三条硬约束，违反会报字段识别错误：

- **平铺**：无嵌套对象
- **每次一条**：不接受数组，多条必须逐条推送
- **无 JSON null**：空字符串填 `"null"`、空数字填 `0`、布尔填 `false`

另外 `region`、`category`、`matched_category`、`status` 都是**单选字段**——`schema.md` 里列出的枚举值必须在飞书字段选项里逐一存在，否则该值写入失败。

## 使用

在会话里直接说：

```
跑一遍今天的招标情报并生成简报（不推送）
```

只有明确说“检索并推送到飞书”才启用生产推送模式。普通的“查一下”“跑今天情报”“生成简报”都不产生外部写入。

Agent不得询问“本次任务期望的执行模式”。它必须从用户措辞自动选择；措辞含糊时固定使用`report-only`继续执行。缺少API Key、Webhook或网络权限时，完成仍可完成的离线步骤并在摘要中报告阻塞，不用交互式问题暂停任务。宿主Agent自身强制的安全审批仍可能出现，Skill不会也不能绕过。

检索完成后建立小批次队列：

```bash
python scripts/tender_pipeline.py prepare --search-dir .tmp/search/2026-08-19 --mode report-only --batch-size 5
python scripts/tender_pipeline.py status --run-dir .tmp/search/2026-08-19
python scripts/tender_pipeline.py next-batch --run-dir .tmp/search/2026-08-19
```

### 单独校验载荷

推送前可以先校验，不发请求：

```bash
powershell -File scripts/send_webhook.ps1 -PayloadPath payload.json -Flow create -DryRun
```

它会检查「单条 / 平铺 / 无 JSON null」三条硬约束并打印载荷。退出码：`0` 通过、`1` 文件或 JSON 错误、`2` 校验不通过。

生产模式会把成功回执写入本次运行的`pipeline/receipts/`。回执绑定载荷哈希，随后由状态机核验并更新去重历史：

```bash
powershell -File scripts/send_webhook.ps1 -PayloadPath <pipeline/payloads/create/C....json> -Flow create -ManifestPath <pipeline/manifest.json> -Live
python scripts/tender_pipeline.py record-push --run-dir <检索落盘目录> --receipt <pipeline/receipts/create-C....json>
```

只有HTTP 200、飞书`code: 0`、manifest模式/状态、载荷路径与SHA-256全部匹配，`seen.json`和`push_ledger.json`才会分别以原子替换方式更新。

## 评估

`evals/evals.json` 供 [skill-creator](https://github.com/anthropics/claude-code) 使用，`expectations` 是 grader 的打分依据。

| # | 场景 | 前提 |
|---|---|---|
| 1 | 每日全量跑 | 使用离线夹具或显式测试配置；默认不推飞书 |
| 2 | 过敏原 sIgE 定向查 | 需要豆包 API Key |
| 3 | 更新流·中标公告 | 离线，校验更新载荷，不接触生产Webhook |
| 4 | 每日自查·截止转 closed | 离线，校验更新载荷，不接触生产Webhook |
| 5 | 附件与联系方式提取 | 完全离线，不推送 |

Eval 5 用两个真实公告页面做 fixture，覆盖附件提取的两类坑：**相对路径需 origin 补全**、**附件挂在跨域 OSS 上**。不依赖任何 MCP 与网络，任何环境都能跑，适合作为改动后的回归。

所有eval都必须使用 `-DryRun` 或本地模拟Webhook；评估流程禁止传 `-Live`。

## 已知约束

**`send_webhook.ps1` 必须是 UTF-8 with BOM。** Windows PowerShell 5.1 会把无 BOM 的 `.ps1` 按 GBK 解码，中文注释误解码后会吞掉引号，导致整个脚本解析失败——而报错位置与真实原因完全无关，极难排查。用会剥离 BOM 的编辑器改这个文件会直接把它改坏。

**附件是 `href` 属性，不是可见文字。** 把页面当纯文本读必然漏掉附件，即使读到了附近的联系方式也一样。`references/verification.md`规定了对原始HTML执行grep的机械步骤，以及三条规则：只看扩展名、相对路径必须补全为绝对URL、不做同域过滤。

**`data/seen.json` 在仓库里只保留空骨架。** 本地跑起来后累积的业务情报不再提交，靠 `git update-index --skip-worktree data/seen.json` 实现。这是本机设置、不随仓库走，新 clone 需要重新执行一次。
