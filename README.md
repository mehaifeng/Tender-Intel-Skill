# IVD Bid Radar Skill

面向过敏原、自身免疫IVD试剂和免疫分析仪器采购情报的无人值守管线：默认检索最近72小时，完成去重、快速网页核验、医院库匹配和固定16字段Webhook推送。

## 处理流程

```text
知了标讯检索（search_bids 自适应分批 + get_bid_detail 取正文与回源链接）
  → 统一候选契约与查重（同一公告的壳记录让位于完整正文记录）
  → 标题与内容信号预筛
  → 结构化字段直接绑定，每批10条只做产品域判断
  → 全国医院库确定性匹配
  → 固定16字段校验
  → DryRun
  → Webhook推送与成功回执登记
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
| `scripts/hospital_match.py` | 医院名称、别名、等级的本地确定性匹配 |
| `scripts/send_webhook.py` | 主用Webhook DryRun和生产发送门禁 |
| `scripts/send_webhook.ps1` | Windows兼容发送入口 |
| `data/hospitals.min.json.gz` | 50,599家医疗单位精简运行索引 |

## 配置

开箱包已经包含本地`config/webhook.json`，可以直接运行。该文件含凭据，已被Git忽略，请勿公开分享。

知了标讯 API Key 按环境变量`ZLBX_API_KEY` → `config/zlbx.json`的`api_key`顺序读取，模板见`config/zlbx.example.json`；该文件含凭据，已被Git忽略。Key没有到期机制，不需要定期换发。检索按调用次数计费，72h日窗一轮约27积分（列表）加通过预筛的候选每条1积分（详情），约¥132/月，实测明细见`references/zlbx.md`。适配器把每词命中数记在`data/query_hits.json`用于装箱降低调用次数，首次运行没有该文件时会多花约一倍列表调用。Webhook按环境变量`FEISHU_WEBHOOK_URL` → 旧环境变量`FEISHU_CREATE_WEBHOOK_URL` → `config/webhook.json`的顺序读取。

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
python scripts/send_webhook.py --payload <payload.json> --dry-run
python scripts/send_webhook.py --payload <payload.json> --live --manifest <manifest.json>
python scripts/tender_pipeline.py record-push --run-dir <检索目录> --receipt <成功回执>
```

## 固定Webhook字段

字段严格为：

```text
标题、项目编号、单位、地区、所属省/市、所属大区、发布时间、截止时间、预算、
采购方式、科室、命中关键词、内容（检索的摘要）、链接、医院全名、医院等级
```

所有字段都是字符串；缺失统一填`"null"`。详细示例见`references/schema.md`。

`所属省/市`只输出省级行政区或直辖市简称，例如`北京`、`河北`、`上海`、`湖南`、`新疆`、`广西`、`青海`，不输出地级市或`省/市`组合。

`地区`必须包含省份、自治区或直辖市全称，例如`安徽省凤阳县`、`北京市朝阳区`，不得只输出`凤阳县`或`朝阳区`。`科室`只取正文中明确标注的科室；`命中关键词`取实际检索Query中确实出现在候选内容里的词。

其中项目编号、单位、地区、所属省/市、截止时间、预算、采购方式由管线从知了标讯的结构化字段直接绑定（`tender_pipeline.SOURCE_BOUND_FIELDS`），模型不需要提取；覆盖须带正文证据。

## 打分发包

    python3 scripts/build_package.py              # 含凭据，仅限本机部署
    python3 scripts/build_package.py --no-secrets # 不含凭据，可外发

输出到 `dist/`（已被 Git 忽略）。打包时会在包内跑 `--dry-run` 与全量测试自检，
不通过就以非零码退出。含凭据的包里 `config/zlbx.json`、`config/webhook.json`
是明文，**不要提交版本库、不要转发**。

包里带上了 `data/query_hits.json`，所以部署后第一次运行就是热态（列表约 27 次调用），
不用先花一轮冷启动的 50 次去探路。

## 依赖

- Python 3.9+标准库；正常运行不需要Python第三方包
- Windows旧任务如继续使用`scripts/send_webhook.ps1`，需要PowerShell 5.1+或PowerShell 7+
- 医院运行索引已经内置，不需要在日常任务中读取原始15MB工作簿
