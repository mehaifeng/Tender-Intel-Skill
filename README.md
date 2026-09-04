# IVD Bid Radar Skill

面向过敏原、自身免疫IVD试剂和免疫分析仪器采购情报的无人值守管线：默认检索最近72小时，完成去重、快速网页核验、医院库匹配和固定16字段Webhook推送。

## 处理流程

```text
睿销（jrbx）聚合库检索 + CCGP官方HTTP检索 + PLAP匿名公开检索
  → 统一候选契约与跨来源查重
  → 标题与内容信号预筛
  → 每批10条快速核验（CCGP缺字段时可按需读取直链附件）
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
| `references/keywords.md` | 业务方《过敏》《自免》两张关键词表，三个信源的Query清单与筛选判据都在这里 |
| `references/jrbx.md` | 睿销调用约束：登录态账号池与1403轮换、keywords的AND语义、回源URL配额 |
| `references/ccgp.md` | CCGP的普通HTTP约束、单词Query限制和来源优先级 |
| `references/plap.md` | 军队采购网匿名公开检索、混合策略和正文降级规则 |
| `scripts/tender_search.py` | 可插拔多来源统一检索入口 |
| `scripts/jrbx_search.py` | 睿销聚合库检索、回源链接补全与凭证维护 |
| `scripts/ccgp_search.py` | 中国政府采购网普通HTTP检索与详情字段提取 |
| `scripts/plap_search.py` | 军队采购网匿名公开列表检索与部分正文提取 |
| `scripts/search_common.py` | 统一候选契约、链接规范化与跨来源查重 |
| `scripts/tender_pipeline.py` | 去重、预筛、批次、字段校验和回执登记 |
| `scripts/hospital_match.py` | 医院名称、别名、等级的本地确定性匹配 |
| `scripts/send_webhook.py` | 主用Webhook DryRun和生产发送门禁 |
| `scripts/send_webhook.ps1` | Windows兼容发送入口 |
| `data/hospitals.min.json.gz` | 50,599家医疗单位精简运行索引 |

## 配置

开箱包已经包含本地`config/webhook.json`，可以直接运行。该文件含凭据，已被Git忽略，请勿公开分享。

睿销登录态按环境变量`JRBX_USER_ID`、`JRBX_TOKEN`、`JRBX_OPENID`（只表达得了一个账号）→ `config/jrbx.json`的`accounts`账号池的顺序读取；后者用`python scripts/jrbx_search.py --set-token`逐个写入，已被Git忽略。token有效期20天，过期需微信重新扫码，用`--check-token`逐个查剩余天数。返回码`1403`实测撞上即废，适配器会退池换下一个账号原地重发同一请求，池空才以退出码5中止，因此多备几个账号能让一趟检索跑完。Webhook按环境变量`FEISHU_WEBHOOK_URL` → 旧环境变量`FEISHU_CREATE_WEBHOOK_URL` → `config/webhook.json`的顺序读取。

## 运行

检索：

```bash
python scripts/tender_search.py
```

默认同时运行`jrbx,ccgp,plap`。可用`--sources jrbx`、`--sources ccgp`或`--sources plap`单独诊断某个适配器。CCGP不需要账号或浏览器；搜索页、完整详情正文和附件直链均由普通HTTP读取。

PLAP 默认启用，只读取军队采购网匿名公开信息，不登录，也不补全登录后字段。若详情页要求登录，适配器仍保留搜索结果中可见的标题、日期、公告类型和链接，并将正文访问状态标记为受限：

```bash
python scripts/tender_search.py --sources plap --time-range 24h
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

`地区`必须包含省份、自治区或直辖市全称，例如`安徽省凤阳县`、`北京市朝阳区`，不得只输出`凤阳县`或`朝阳区`。`科室`只取检索正文中明确标注的科室；`命中关键词`取实际检索Query中确实出现在候选内容里的词。

## 依赖

- Python 3.9+标准库；正常运行不需要Python第三方包
- Windows旧任务如继续使用`scripts/send_webhook.ps1`，需要PowerShell 5.1+或PowerShell 7+
- 医院运行索引已经内置，不需要在日常任务中读取原始15MB工作簿
