# IVD Bid Radar Skill

面向过敏原、自身免疫IVD试剂和免疫分析仪器采购情报的无人值守管线：默认检索最近72小时，完成去重、快速品类核验、医院库匹配和固定16字段写入飞书多维表格。

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
| `data/hospitals.min.json.gz` | 50,601家医疗单位精简运行索引 |

## 配置

开箱包已经包含本地`config/feishu_app.json`，可以直接运行。该文件含凭据，已被Git忽略，请勿公开分享。

知了标讯 API Key 按环境变量`ZLBX_API_KEY` → `config/zlbx.json`的`api_key`顺序读取，模板见`config/zlbx.example.json`；该文件含凭据，已被Git忽略。Key没有到期机制，不需要定期换发。检索按调用次数计费，72h日窗一轮约25积分（列表）加每条取详情的候选1积分；2026-09-08 实跑 113 积分/轮，约¥226/月，实测明细见`references/zlbx.md`。详情里既有通过标题/标的物预筛的候选，也有「标的物是检验仪器或试剂、或医疗机构买耗材器械，而品类信号只可能在正文里」的复核批（不设条数上限，见 zlbx.md「正文里才有的信号」）。适配器把每词命中数记在`data/query_hits.json`用于装箱降低调用次数，首次运行没有该文件时会多花约一倍列表调用。飞书自建应用凭据与目标多维表格按环境变量`FEISHU_APP_ID`/`FEISHU_APP_SECRET`/`FEISHU_APP_TOKEN`/`FEISHU_TABLE_ID` → `config/feishu_app.json`的顺序读取，模板见`config/feishu_app.example.json`。应用需开通`bitable:app`，并在目标多维表格里通过「添加文档应用」加为协作者。

## 运行

检索：

```bash
python scripts/tender_search.py
```

默认窗口72小时。`--dry-run`不发请求也不读凭证，可用于校验清单与参数；`--max-details 0`跳过详情（会失去正文、科室与回源链接，仅用于诊断）。**退出码3表示API Key缺失、被拒或积分不足**，这类失败看起来像“今天没情报”，必须当凭证故障报警。

```bash
python scripts/tender_search.py --time-range 24h --dry-run
```

建立队列：

```bash
python scripts/tender_pipeline.py prepare --search-dir .tmp/search/2026-08-23 --batch-size 10
python scripts/tender_pipeline.py next-batch --run-dir .tmp/search/2026-08-23
```

省略`--mode`时默认`daily-push`。显式调用本Skill且未限制推送时，优先执行包含推送与成功回执登记的完整流程；用户明确要求离线、不推送、只检索或只核实时，必须改用`report-only`、`search-only`或`verify-only`。

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

## 固定推送字段

字段严格为：

```text
标题、项目编号、单位、地区、所属省/市、所属大区、发布时间、截止时间、预算、
采购方式、科室、命中关键词、内容（检索的摘要）、链接、医院全名、医院等级
```

所有字段都是字符串；缺失统一填`"null"`。写入多维表格时`"null"`的字段直接不传，单元格保持真正的空。详细示例见`references/schema.md`。

`所属省/市`只输出省级行政区或直辖市简称，例如`北京`、`河北`、`上海`、`湖南`、`新疆`、`广西`、`青海`，不输出地级市或`省/市`组合。

`地区`必须包含省份、自治区或直辖市全称，例如`安徽省凤阳县`、`北京市朝阳区`，不得只输出`凤阳县`或`朝阳区`。`科室`只取正文中明确标注的科室；`命中关键词`取实际检索Query中确实出现在候选内容里的词。

其中项目编号、单位、地区、所属省/市、截止时间、预算、采购方式由管线从知了标讯的结构化字段直接绑定（`tender_pipeline.SOURCE_BOUND_FIELDS`），模型不需要提取；覆盖须带正文证据。

## 打分发包

    python3 scripts/build_package.py              # 含凭据，仅限本机部署
    python3 scripts/build_package.py --no-secrets # 不含凭据，可外发

输出到 `dist/`（已被 Git 忽略）。打包时会在包内跑 `--dry-run` 与全量测试自检，
不通过就以非零码退出。含凭据的包里 `config/zlbx.json`、`config/feishu_app.json`
是明文，**不要提交版本库、不要转发**。

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
