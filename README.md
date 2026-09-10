# IVD Bid Radar Skill

面向过敏原、自身免疫IVD试剂和相关免疫分析仪器采购情报的无人值守管线：默认检索最近72小时，完成去重、快速品类核验、医院库匹配和固定16字段写入飞书多维表格。用户明确要求离线时改用 `--mode report-only`，该模式下生产写入被门禁挡住。

## 处理流程

```text
拉取飞书多维表格台账快照（唯一防重真相源）
  → 知了标讯检索（search_bids 自适应分批 + get_bid_detail 取正文与回源链接）
  → 统一候选契约与查重（强身份 → 标题字符级 → 正文 → 语义，逐层收口）
  → 标题与内容信号预筛
  → 结构化字段直接绑定，每批10条只做产品域判断
  → 全国医院库确定性匹配
  → 固定16字段校验
  → DryRun
  → 发送前重新拉台账再查重 → 飞书接口写入与成功回执登记
```

## 关键文件

| 路径 | 用途 |
|---|---|
| `SKILL.md` | 运行契约与模式选择 |
| `references/schema.md` | 固定16字段、医院匹配和大区规则 |
| `references/verification.md` | 快速核验协议 |
| `references/keywords.md` | 业务方《过敏》《自免》两张关键词表，Query清单与筛选判据都在这里 |
| `references/zlbx.md` | 知了标讯调用约束：OR语义、分页不稳定与分批策略、字段映射、覆盖面实测 |
| `scripts/tender_search.py` | 检索入口 |
| `scripts/zlbx_search.py` | 知了标讯检索、详情正文与回源链接补全 |
| `scripts/search_common.py` | 统一候选契约、链接规范化与查重 |
| `scripts/tender_pipeline.py` | 去重、预筛、批次、字段校验和回执登记 |
| `scripts/tender_identity.py` | 全流程共用公告身份规则 |
| `scripts/tender_ledger.py` | 飞书台账拉取、运行快照与快照锁 |
| `scripts/feishu_client.py` | 飞书多维表格开放接口客户端 |
| `scripts/dedup_match.py` | 分层查重漏斗：强身份 / 字符级 / 正文 / 语义 |
| `references/dedup.md` | 去重设计、发送防重与升级约定 |
| `scripts/hospital_match.py` | 医院名称、别名、等级的本地确定性匹配 |
| `scripts/send_record.py` | 载荷 DryRun 与生产写入门禁 |
| `scripts/send_record.ps1` | Windows兼容发送入口 |
| `scripts/run_report.py` | 运行报告：只读取运行数据，生成单轮漏斗 HTML 与两轮归宿 diff |
| `data/hospitals.min.json.gz` | 50,601家医疗单位精简运行索引 |

## 配置

默认分发包不含凭据。仅使用 `scripts/build_package.py --include-secrets` 生成的本机包包含
`config/zlbx.json` 与 `config/feishu_app.json`；这两份文件含明文凭据，已被 Git 忽略，不得公开分享。

知了标讯 API Key 按环境变量`ZLBX_API_KEY` → `config/zlbx.json`的`api_key`顺序读取，模板见`config/zlbx.example.json`；该文件含凭据，已被Git忽略。Key没有到期机制，不需要定期换发。检索按调用次数计费，72h日窗一轮约25积分（列表）加每条取详情的候选1积分；2026-09-08 实跑 113 积分/轮，约¥226/月，实测明细见`references/zlbx.md`。详情里既有通过标题/标的物预筛的候选，也有「标的物是检验仪器或试剂、或医疗机构买耗材器械，而品类信号只可能在正文里」的复核批（不设条数上限，见 zlbx.md「正文里才有的信号」）。适配器把每词命中数记在`data/query_hits.json`用于装箱降低调用次数，首次运行没有该文件时会多花约一倍列表调用。飞书自建应用凭据与目标多维表格按环境变量`FEISHU_APP_ID`/`FEISHU_APP_SECRET`/`FEISHU_APP_TOKEN`/`FEISHU_TABLE_ID` → `config/feishu_app.json`的顺序读取，模板见`config/feishu_app.example.json`。应用需开通`bitable:app`，并在目标多维表格里通过「添加文档应用」加为协作者。

## 运行

检索：

```bash
python scripts/tender_search.py
```

默认窗口72小时。`--dry-run`不发请求也不读凭证，可用于校验清单与参数；`--max-details`限制常规候选详情，正数上限不截断正文复核分支，`--max-details 0`才会跳过全部详情（会失去正文、科室与回源链接，仅用于诊断）。**退出码3表示API Key缺失、被拒或积分不足**，这类失败看起来像“今天没情报”，必须当凭证故障报警。

```bash
python scripts/tender_search.py --time-range 24h --dry-run
```

建立队列：

```bash
python scripts/tender_pipeline.py prepare --search-dir .tmp/search/2026-08-23 --batch-size 10
python scripts/tender_pipeline.py next-batch --run-dir .tmp/search/2026-08-23
```

省略`--mode`时默认`daily-push`。显式调用本Skill且未限制推送时，优先执行包含推送与成功回执登记的完整流程；用户明确要求离线、不推送时改用`--mode report-only`，manifest 的 `live_push_allowed` 随之关闭。只检索时无需运行 `prepare`；只核实给定公告时也不建立生产队列。定时运行误建成 `report-only` 时用 `tender_pipeline.py authorize-unattended --run-dir <检索目录>` 升回来。

提交批次结果：

```bash
python scripts/tender_pipeline.py submit-batch \
  --run-dir .tmp/search/2026-08-23 \
  --batch-id batch-0001 \
  --results .tmp/results.json
```

校验并推送单条载荷：

```bash
python scripts/send_record.py --payload <payload.json> --dry-run
python scripts/send_record.py --payload <payload.json> --live --manifest <manifest.json>
python scripts/tender_pipeline.py record-push --run-dir <检索目录> --receipt <成功回执>
```

## 运行报告与回归

每轮生成一页 HTML，把这一轮从接口返回到当前最终状态的漏斗摊开；`daily-push` 还会展示
推送确认。每一关都能展开看被它拦下的具体公告和理由：

```bash
python scripts/run_report.py --run-dir .tmp/search/2026-09-08 --open
```

它只读取运行目录里已经落盘的数据，不发请求、不改管线状态；唯一写入是报告 HTML。跑挂的轮次照样能生成——凭证故障和台账拉取失败会顶在页面最上面，不会被渲染成一次安静的“今天没情报”。

改了关键词、闸门或阈值之后要看代价，用**同一批原始候选在两个隔离的代码副本或 Git
worktree 中重放再对比**。两份运行目录必须来自同一批候选和同一份台账快照；重放只走
`prepare`，不花任何检索额度，也不要用 `git stash` 切换正在工作的目录：

```bash
python <基线代码副本>/scripts/tender_pipeline.py prepare --search-dir <基线运行副本> --mode report-only --force
python <当前代码副本>/scripts/tender_pipeline.py prepare --search-dir <当前运行副本> --mode report-only --force
python scripts/run_report.py --compare .tmp/regress-base .tmp/regress-new --open
```

两边**都要重放**：直接拿原始运行目录当基线会被台账污染——推送成功之后台账快照里就有了这些记录，重放时它们会从`create`变成`已入账`，把规则改动的真实影响盖掉。两边共用同一份快照，差异才只来自改动本身。

页面按严重程度排：`新误杀`（基线留下、这次丢了）是改动的代价，合入前必须逐条看过；`新捞回`是收益；后面几栏是判定与拦截关卡的位移。上一次排队留下的推送回执会被自动忽略，不会伪装成“这条还在推送”。

## 固定推送字段

字段严格为：

```text
标题、项目编号、单位、地区、所属省/市、所属大区、发布时间、截止时间、预算、
采购方式、科室、命中关键词、内容（检索的摘要）、链接、医院全名、医院等级
```

所有字段都是字符串；缺失统一填`"null"`。写入多维表格时`"null"`的字段直接不传，单元格保持真正的空。详细示例见`references/schema.md`。

`所属省/市`只输出省级行政区或直辖市简称，例如`北京`、`河北`、`上海`、`湖南`、`新疆`、`广西`、`青海`，不输出地级市或`省/市`组合。

`地区`必须包含省份、自治区或直辖市全称，例如`安徽省凤阳县`、`北京市朝阳区`，不得只输出`凤阳县`或`朝阳区`。`科室`只取正文中明确标注的科室；`命中关键词`取候选内容中命中的目标品类原文片段，并尽量扩成标的清单里的完整写法。

其中项目编号、单位、地区、所属省/市、截止时间、预算、采购方式由管线从知了标讯的结构化字段直接绑定（`tender_pipeline.SOURCE_BOUND_FIELDS`），模型不需要提取；覆盖须带正文证据。

`采购方式`是台账里的单选列，管线把各来源的写法收敛到九个固定选项：`公开招标`、`邀请招标`、`竞争性谈判`、`竞争性磋商`、`询价`、`单一来源`、`遴选`、`市场调研`、`其他`。对照表见`references/schema.md`。

## 打分发包

    python3 scripts/build_package.py                   # 默认不含凭据，可外发
    python3 scripts/build_package.py --include-secrets # 含明文凭据，仅限本机部署

输出到 `dist/`（已被 Git 忽略）。打包时会在包内跑 `--dry-run` 与全量测试自检；
不通过就以非零码退出且不生成压缩包。含凭据的包里 `config/zlbx.json`、
`config/feishu_app.json` 是明文，**不要提交版本库、不要转发**。

包里带上了 `data/query_hits.json`，所以部署后第一次运行就是热态（列表约 27 次调用），
不用先花一轮冷启动的 50 次去探路。

## 依赖

- Python 3.9+标准库；正常运行不需要Python第三方包
- Windows旧任务如继续使用`scripts/send_record.ps1`，需要PowerShell 5.1+或PowerShell 7+
- 医院运行索引已经内置，不需要在日常任务中读取原始15MB工作簿

## 去重与部署约定

长期台账就是飞书多维表格本身，本地没有去重库。每次检索开头拉一份快照写进运行目录，
列表阶段据此跳过已入账公告的详情调用；发送前再拉一次最新的重新查重。拉不到台账整轮停下，
绝不按空台账继续。写入结果未知时按链接回查确认，回查不到就停且不重试。
前三层定不了案的候选扣在`pipeline/semantic_review.jsonl`，语义判定后用
`tender_pipeline.py resolve-semantic`登记结论。详见 [去重与发送登记](references/dedup.md)。

只在 dist 生成分发包，不安装新技能。升级不再需要保留或合并任何本地台账。
别人手工加进多维表格的行同样进入防重，不再依赖人工导出。
