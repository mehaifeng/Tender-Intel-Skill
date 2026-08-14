---
name: tender-intel
description: 招标情报检索与飞书推送。当用户提到招标/招投标/采购公告/IVD试剂招标/免疫诊断试剂招标/过敏原试剂采购/自身抗体试剂采购/医院检验科试剂招标/免疫分析仪器采购/“跑一遍今天的招标情报”/“检索招标信息并推送到飞书”/“生成今日招标简报”等场景时，务必使用本技能。流程：豆包搜索检索→原文核实与数据清洗→按固定JSON结构化→POST到飞书Webhook。公司业务范围：过敏原/自身免疫类IVD试剂（sIgE、总IgE、自身抗体、ELISA酶免、化学发光、免疫荧光/印迹）及免疫分析仪器（化学发光免疫分析仪、全自动酶免工作站、酶标仪、洗板机等）。
---

# 招标情报检索与飞书推送（tender-intel）

把「每天找招标信息 → 核实 → 结构化 → 推送飞书」做成一条固定管线。核心理念：**检索要快而全，核实要严而准，推送只给业务能直接用的数据**。宁可零命中，也不硬凑；无法核实的，标注说明后照常推送，绝不静默丢弃。

## 流程总览

五个阶段，顺序执行：

1. **检索**：豆包搜索按 keywords.md 分层词表与每日 Query 清单检索（A/B 层固定 + C 层轮换），候选跨查询去重后进入去重
2. **去重**：对照 `data/seen.json` 去重表，跳过已推送项；识别**后续公告**（中标/废标/更正）转入更新流
3. **清洗核实**：逐条打开原文核实，过滤无效项
4. **结构化**：按定稿 JSON 组织每条有效项（创建流 / 更新流两种格式）
5. **推送**：创建流 POST 新记录、更新流 POST 状态变更，都写 seen.json，输出执行摘要

## 阶段 1：检索

使用豆包搜索 MCP（`mcp__doubao_web_search__doubao_search`），按 `references/keywords.md` 的「每日 Query 清单 + 轮换表」逐条执行（24 条/天：A/B 层固定 22 条 + 当天 C 层长尾词轮换组 1 条 + 当天 D 层意图词轮换 1 条）。query 组拼规则见该文件 §4：`[3~6 个品类词] + [意图词：招标/采购/公告] + [可选年份]`。每条固定参数：

- `version`: `custom`（信源权威度分级，正文更完整）
- `auth_level`: `1`（只保留政府/医院/军队官网等一手权威源——招投标场景信源质量压倒一切，聚合站和营销软文会直接滤掉）
- `max_age_days`: `45`（时效窗口；每天跑的话 45 天足够覆盖当月公告，且能过滤旧单重发）
- `count`: `10`，`snippet_length`: `1000`（摘要要长，直接可读）

**执行前校验（意图词）**：逐条核对当天清单，每条 query 末尾必须含一个意图词（招标 / 采购 / 公告 / 采购公告 之一），缺失的补 `采购公告` 再执行。意图词是领域开关：无意图词的 query 会被医学文献/科室科普稀释，招标页排不进 top-N 等于漏采（2026-08-14 对照检索实证）。

从每条结果中提取候选：标题、来源站点、原文 URL、发布时间、摘要。**只保留带一手原文 URL 的**；聚合站列表页（剑鱼标讯、bidcenter、招标网专题页等）只能当线索，不能作为候选。**候选收集后先按原文 URL 跨查询去重**（同一公告会被多条 query 同时命中），再进入阶段 2。

## 阶段 2：去重

每次运行前先读 `data/seen.json`（去重表），对照候选，避免同一公告重复推送——技能是每天跑的，不查重第二天就会把昨天的公告再推一遍。

**dedup_key 规则**：
- 有项目编号：`<project_code>|<purchaser>`（如 `新采招标-2026-26|新蔡县人民医院`）
- 无项目编号：用原文 `source_url`

**对照逻辑**：
- key 在表内且 `pushed: true` → 先判断新候选是否为**后续公告**（与表内记录同一项目的中标/废标/更正/合同公告）：
  - 是后续公告 → **不创建新记录**，转入阶段 5b 更新流（更新飞书已有记录 + seen.json）
  - 不是后续公告 → 跳过，摘要里记"已推送过"
- key 不在表内 → 继续核实（走创建流）

**记录存储**：seen.json 每条记录存**全量字段**（创建流完整 JSON 载荷 + `dedup_key / first_seen / last_seen / pushed`）；更新流的"原值回填"依赖它取未变更字段的原值。

**写入时机**：
- 创建流：推送成功（HTTP 200 且返回 `code: 0`）后写入；失败不写，下次运行自动重试
- 更新流：更新推送成功后，在原记录上就地更新（status、winner、deadline 等）；失败不更新，下次运行重试

## 阶段 3：清洗核实

对每条候选**逐条打开原文核实**。抓取按四级降级，能快不快慢——**浏览器永远是最后手段**：它最重、最慢、局限性最大（以截图和可见文本为主、多步交互、无头环境偶发失败），能用 MCP 转 md 解决的绝不上浏览器：

1. **curl 抓 HTML**（首选）：多数政府采购站是服务端渲染，`curl.exe -s -L -A "<浏览器UA>"` 直接拿正文
2. **SPA 反查 API**（次选）：如果抓回来的只有 JS 空壳（特征：`<div id="app">`、Vue/React 壳、正文缺失），不要急着上浏览器——下载它的 JS bundle（`app.js`），搜接口路径（如 `getInfoById`、`/rest/`、`/api/`），直接 curl 那个 JSON 接口，又快又是结构化数据。注意：同一套系统（如 gpcms-center-web）常被多个省份共用，摸清一个接口模式能复用一整套站点族
3. **MinerU 转 md**（`mcp__mineru__parse_documents`）：curl/API 都拿不到正文时用它。把原文 URL 直接喂给 `parse_documents`（纯网页 URL 时 `model: "html"`），它抓取带渲染能力，能处理 AJAX/动态渲染站（如黑龙江 hljcg 系），返回结构化 Markdown，成本远低于浏览器。前提是已配好 `MINERU_API_TOKEN`（未配或返回 401/空解析，直接进下一级，不要反复重试）
4. **浏览器兜底**（`run_browser_task`）：仅在前三级都拿不到正文时使用。**浏览器里禁止用搜索引擎/站内搜索去检索招标信息**（给定 URL 打不开就按下面"无法核实的默认策略"处理，不要在浏览器里翻站找公告，费时且收益低）

**遇到验证码/登录墙/反爬**：不要反复尝试，直接进入"无法核实的默认策略"。

**无法核实的默认策略**：给定 URL 用 curl/API/MinerU/浏览器都拿不到正文，或打开后内容与检索摘要对不上时——**除非原始检索信息有重大漏洞（备案编号年份久远如 2019、品类明显不符、无一手原文 URL），否则默认照常推送**：`status` 维持候选应有值（多为 `active`），`match_level` 按摘要能判断的程度标（拿不准标 `unknown`），并在 `notes` 注明"原文未能核实（原因：XXX），信息来自检索摘要，建议电话确认"。有重大漏洞的才剔除。

核实每条时核对六项：

1. **公告真实有效**：原文存在、未撤销、未被更正作废（更正公告要读更正后的版本）
2. **截止时间**：投标/报名/公示截止时间是否已过；计算 `days_left`
3. **品类匹配**：采购清单里是否真的含目标品类（过敏原/自身抗体试剂、化学发光/酶免仪器等）——注意有的公告清单不公开（只写"一批"），**此时先去附件里找清单**（见下方「附件提取」：正文"拟采购货物说明"为空、清单只在 PDF 附件里的公告很常见），附件也拿不到才标 `match_level: "unknown"`，不能假设匹配
4. **参与可行性**：公告有效 ≠ 可投标。三种坑：①文件获取/报名截止和投标截止是两回事（获取已截止则参与窗口关闭）；②单一来源公示不是投标机会；③旧记录重发（看备案编号年份，如"临财采计[2019]..."）
5. **联系方式**：正文找"联系人/联系方式/联系电话"段（一般在公告末尾，采购人与代理机构各一段），取到 `contact`。正文确实没有才填 `"null"` 并在 notes 注明
6. **附件直链**：按下面的机械步骤取 `attachment`，不靠通读页面找

**附件提取（必做，不靠眼力）**：`href` 是标记属性，不是可见文字——**把页面当文本读必然漏掉附件**。实测 chinablood 某公告：联系方式在第 742 行、附件在第 772 行，只隔 9 行，读到了联系方式照样漏附件。所以正文核实完、HTML 还在手上时，对**原始 HTML** 执行一次：

```bash
grep -o -i -E 'href="[^"]*\.(pdf|doc|docx|xls|xlsx|zip|rar|7z)"' <抓到的HTML文件>
```

三条规则：

1. **只看扩展名**，不看目录名和锚文本——附件常躺在 `/Uploads/Picture/` 这类误导性目录里、和十几个装饰图同目录，锚文本也常只是"详见附件"四个字
2. **相对路径必须补全**：`/Uploads/xxx.pdf` 要用 source_url 的 scheme+host 拼成绝对 URL 再填。相对路径在页面里是字面存在的、绝对 URL 不是，照抄等于填了个打不开的值
3. **不做同域过滤**：附件常挂在 OSS/CDN 上（如政采云的 `zcy-gov-open-doc.oss-*.aliyuncs.com`），与 source_url 不同域，照收

多个命中时取招标/采购文件主体（标题含"招标文件/采购文件/需求"或体积最大的）填 `attachment`，其余进 notes。**零命中才允许填 `"null"`**，并在 notes 注明"正文无附件直链"。

SPA/API 站（上面第 2 级）同理：在返回的 JSON 里找 `fileId` / `fileName` / `filePath` / `attachmentUrl` 一类字段，按站点模式拼直链。

**排除规则**（命中任一即剔除，不留候选）：旧记录重发；清单明确不含目标品类。其余按性质分流：**已截止的招标/采购公告、单一来源公示、中标/合同公告、采购意向一律转入研究流**（见推送范围），不再因"截止已过"剔除。

**推送范围（重要）**：双流推送，都进同一张飞书台账表：
- **投标流**：`status: "active"` 且 `designated_supplier` 为空的记录 → 创建流推送
- **研究流**：`status: "intel"` 且 `match_level` 非 `none` 的记录 → 创建流推送，供中标统计与市场扫描。包括：
  - 单一来源公示（带 designated_supplier）
  - 中标公告 / 合同公告（带 winner / award_amount，竞品与自家品牌中标均收）
  - **已截止的招标/采购公告**（投标窗口已关闭仍入库；winner 未知填 `"null"`/0，notes 注明已截止）
  - **采购意向 / 招标意向等探索性公告**（未正式招标、无投标窗口，作市场前瞻情报）
- 与已推送记录同项目的**后续公告**（中标/废标/更正）：不创建新记录，走更新流（阶段 5b）

## 阶段 4：结构化

按 `references/schema.md` 的定稿 JSON 组织每条有效项。飞书约束：**平铺、无嵌套、无 null、每次一条**。要点：

- 所有字段平铺；`items` 清单已移除，清单要点并入 `tech_key_points`，明细可写进 `notes`
- `region` 单选：按采购人所在省份查 `references/schema.md` 的"大区判定"表；省份不在表内或无法确定填 `未知或非传统大区`
- `category` 单选：`仪器` / `试剂` / `其他`，多类命中取主类，次类进 notes
- `matched_category` 单选：多条命中取最相关一个，其余进 notes
- **无值的字符串填 `"null"`，无值的数字填 `0`，布尔默认 `false`**——绝不出现 JSON null，否则飞书字段识别报错
- `intel` 已展开平铺：`designated_supplier` / `winner` / `award_amount` 是顶层字段，仅情报类有值，其余按规则填 "null"/0
- `attachment` 取主文件；多余附件塞进 `notes`
- `days_left` 推送时算好；已截止填 0

## 阶段 5：推送

飞书 Webhook **每次只接受一条平铺记录**，多条结果必须逐条推送。分**创建流**（新记录）和**更新流**（已有记录的后续公告）两个 Webhook。

### 5a. 创建流（新记录）

1. 把每条有效项按定稿 JSON 组织成**单个对象**（不是数组）；只推 `active` 和 `intel`（见阶段 3 推送范围）
2. 每条生成 `record_id`（格式见 schema.md：`T`+yyyyMMdd+`-`+6位随机大写字母数字，剔除 0/O/1/I，如 `T20260812-A3B9C7`），写入载荷并存入 seen.json——后续更新的定位键，永不改变
3. 逐条推送：每条写一个 JSON 文件，调用 `scripts/send_webhook.ps1 -PayloadPath <该条文件>` POST 到创建流 Webhook（默认 URL 已内置，可用参数覆盖）
4. **零命中不推送**：无有效条目时跳过推送，仅在执行摘要说明（避免飞书堆积无意义记录）
5. 每条**推送成功后**（HTTP 200 且 `code: 0`），把该条**全量字段**写入 `data/seen.json`（见阶段 2 记录存储）；失败不写，下次运行自动重试

### 5b. 更新流（已有记录的后续公告）

触发条件（阶段 2 识别）：检索命中已推送记录的后续公告；或每日自查 seen.json 发现记录截止已过、状态仍为 `active`。

| 场景 | change_type | 本次变更字段 |
|---|---|---|
| 后续公告为中标公告 | `status_change` | status→closed、notice_type、publish_date、winner、award_amount |
| 后续公告为废标公告 | `status_change` | status→canceled、notice_type、publish_date |
| 更正公告且截止时间变更 | `deadline_change` | deadline、notice_type、publish_date |
| 其他更正（联系人/预算/附件等） | `correction` | budget、contact、attachment 等 |
| 每日自查：截止已过仍 active | `status_change` | status→closed |

发送规则（schema.md「更新流 JSON 结构」定稿）：

1. **固定 14 字段**：`record_id`、`change_type`、`status`、`notice_type`、`publish_date`、`deadline`、`winner`、`award_amount`、`designated_supplier`、`budget`、`contact`、`attachment`、`source_url`、`notes`——每次全发，永不增删
2. **原值回填**：变更字段填新值，未变更字段填 seen.json 里存的原值（无值字符串 `"null"`、无值数字 `0`）
3. 每条写一个 JSON 文件，调用 `scripts/send_webhook.ps1 -PayloadPath <该条文件> -WebhookUrl <更新流地址>` POST 到更新流 Webhook
4. 更新成功后（HTTP 200 且 `code: 0`），就地更新 seen.json 对应记录（status、winner、deadline 等全量）；失败不更新，下次运行重试

### 5c. 执行摘要

给用户输出：执行了多少条 query（固定 + 轮换组）、候选几条（去重后）、去重跳过几条（其中后续公告几条）、核实后有效几条、剔除几条及原因、创建流逐条推送结果（HTTP 状态码 + 飞书返回）、更新流逐条更新结果；推送记录按 status 分布（active / intel）列明。

## 参考资料

- `references/keywords.md` — 分层词表、每日 Query 清单与 C/D 层轮换表、品类枚举、判定细则（阶段1、4 必读）
- `references/schema.md` — 定稿 JSON 结构、字段字典、状态枚举（阶段4 必读）
- `scripts/send_webhook.ps1` — 飞书推送脚本（阶段5 使用；`-WebhookUrl` 参数可指定更新流地址）。加 `-DryRun` 只校验不发送：解析 JSON、检查"单条 / 平铺 / 无 JSON null"三条硬约束并打印载荷。退出码 0 通过、1 文件或 JSON 错误、2 校验不通过。调试与跑 eval 时用它，避免往台账里塞测试数据
- `data/seen.json` — 去重表，全量字段存储（阶段2、5 读写）
- 创建流 Webhook：`https://cp-pharm.feishu.cn/base/workflow/webhook/event/BiHYar0YcwkDhYhjVF4cVFMgnJf`（send_webhook.ps1 内置默认）
- 更新流 Webhook：`https://cp-pharm.feishu.cn/base/workflow/webhook/event/DQFOamcvvwzRTDhct41cw9xznMe`（2026-08-12 用户确认；发送时用 `-WebhookUrl` 参数指定）
