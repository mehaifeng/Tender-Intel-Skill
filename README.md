# Tender Intel Skill

面向过敏原、自身免疫IVD试剂和免疫分析仪器采购情报的无人值守管线：默认检索最近72小时，完成去重、快速网页核验、医院库匹配和固定13字段Webhook推送。

## 处理流程

```text
Doubao全网检索 + CCGP官方HTTP检索
  → 统一候选契约与跨来源查重
  → 标题与内容信号预筛
  → 每批10条快速核验（CCGP缺字段时可按需读取直链附件）
  → 全国医院库确定性匹配
  → 固定13字段校验
  → DryRun
  → Webhook推送与成功回执登记
```

## 关键文件

| 路径 | 用途 |
|---|---|
| `SKILL.md` | 运行契约与模式选择 |
| `references/schema.md` | 固定13字段、医院匹配和大区规则 |
| `references/verification.md` | 快速核验协议 |
| `references/keywords.md` | 固定49条检索Query和品类词 |
| `references/ccgp.md` | CCGP单词Query、普通HTTP约束和来源优先级 |
| `scripts/tender_search.py` | 可插拔多来源统一检索入口 |
| `scripts/doubao_search.py` | Doubao官方API检索 |
| `scripts/ccgp_search.py` | 中国政府采购网普通HTTP检索与详情字段提取 |
| `scripts/search_common.py` | 统一候选契约、链接规范化与跨来源查重 |
| `scripts/tender_pipeline.py` | 去重、预筛、批次、字段校验和回执登记 |
| `scripts/hospital_match.py` | 医院名称、别名、等级的本地确定性匹配 |
| `scripts/send_webhook.py` | 主用Webhook DryRun和生产发送门禁 |
| `scripts/send_webhook.ps1` | Windows兼容发送入口 |
| `data/hospitals.min.json.gz` | 50,599家医疗单位精简运行索引 |

## 配置

开箱包已经包含本地`config/doubao.json`和`config/webhook.json`，可以直接运行。两个文件含凭据，已被Git忽略，请勿公开分享。

如需覆盖配置，Doubao Key可使用环境变量`DOUBAO_SEARCH_API_KEY`。Webhook按环境变量`FEISHU_WEBHOOK_URL` → 旧环境变量`FEISHU_CREATE_WEBHOOK_URL` → `config/webhook.json`的顺序读取。

## 运行

检索：

```bash
python scripts/tender_search.py
```

默认同时运行`doubao,ccgp`。可用`--sources doubao`或`--sources ccgp`单独诊断某个适配器。CCGP不需要账号或浏览器；搜索页、完整详情正文和附件直链均由普通HTTP读取。

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
标题、单位、地区、所属省/市、所属大区、发布时间、截止时间、预算、采购方式、
内容（检索的摘要）、链接、医院全名、医院等级
```

所有字段都是字符串；缺失统一填`"null"`。详细示例见`references/schema.md`。

`所属省/市`只输出省级行政区或直辖市简称，例如`北京`、`河北`、`上海`、`湖南`、`新疆`、`广西`、`青海`，不输出地级市或`省/市`组合。

## 依赖

- Python 3.9+标准库；正常运行不需要Python第三方包
- Windows旧任务如继续使用`scripts/send_webhook.ps1`，需要PowerShell 5.1+或PowerShell 7+
- 医院运行索引已经内置，不需要在日常任务中读取原始15MB工作簿
