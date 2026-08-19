# 定稿 JSON 结构（飞书版）

飞书 Webhook 约束：**平铺格式、每次只接受一条记录、字段不允许出现 null**。因此：

- 全部字段平铺，无嵌套对象
- 每次推送一个对象（一条记录），多条结果逐条推送
- 无值的字符串字段填 `"null"`，无值的数字字段填 `0`，布尔默认 `false`——绝不出现 JSON null

## 标准记录（示例：新蔡县人民医院项目）

```json
{
  "title": "新蔡县人民医院流水线式全自动酶联免疫工作站项目",
  "record_id": "T20260811-K7X2P9",
  "region": "华北二区",
  "project_code": "新采招标-2026-26",
  "purchaser": "新蔡县人民医院",
  "agency": "河南钰政工程管理有限公司",
  "procurement_method": "公开招标",
  "notice_type": "招标公告",
  "category": "仪器",
  "budget": 10000,
  "tech_key_points": "★机械臂≥2；★一次性加样针；★须有医疗器械注册证；开放试剂系统",
  "publish_date": "2026-07-27",
  "doc_fetch_end": "2026-08-03T18:00",
  "deadline": "2026-08-19T09:00",
  "days_left": 13,
  "contact": "马先生 0396-2797388",
  "source_url": "https://zfcg.henan.gov.cn/zhumadian/content?channelCode=H780603&infoId=1994759",
  "attachment": "https://zfcg.henan.gov.cn/cmsweb35rc67w/xincai/rootfiles/2026/07/30/e0fd524bf97e47b6ad5769e3bc005f13.pdf",
  "match_level": "partial",
  "matched_category": "全自动酶免工作站/酶免仪",
  "status": "active",
  "requires_manual": false,
  "designated_supplier": "null",
  "winner": "null",
  "award_amount": 0,
  "notes": "文件获取已截止但PDF可直链下载，建议先电话确认参与资格"
}
```

## 字段字典

| 字段 | 类型 | 说明 |
|---|---|---|
| title | string | 项目/公告标题 |
| record_id | string | skill 端生成的记录唯一编码，格式 `T`+yyyyMMdd+`-`+6位随机大写字母数字（剔除 0/O/1/I），如 `T20260811-K7X2P9`；创建时生成、写 seen.json、永不改变，作为飞书记录定位键；必填 |
| region | string | 大区单选：按采购人所在省份判定，枚举见"大区判定"；无法判定填 `未知或非传统大区` |
| project_code | string | 项目编号；无则 `"null"` |
| purchaser | string | 采购人 |
| agency | string | 代理机构；无则 `"null"` |
| procurement_method | string | 采购方式：公开招标/竞争性谈判/竞争性磋商/询价/单一来源/院内比选等 |
| notice_type | string | 公告类型：招标公告/更正公告/单一来源公示/中标公告/合同公告/采购意向等 |
| category | string | 大类单选：`仪器` / `试剂` / `其他`（多类命中取主类，次类放 notes） |
| budget | number | 预算金额（CNY 元）；未披露填 `0` |
| tech_key_points | string | 关键技术需求要点，★项必录；无则 `"null"` |
| publish_date | string | 发布日期 YYYY-MM-DD；无则 `"null"` |
| doc_fetch_end | string/null 填 "null" | 文件获取/报名截止（ISO 8601）；无则 `"null"` |
| deadline | string | 投标/响应/公示截止（ISO 8601）；无则 `"null"` |
| days_left | number | 剩余天数；已截止填 `0` |
| contact | string | 联系人及电话（一般在公告末尾，采购人与代理机构各一段）；正文确实没有才填 `"null"`，并在 notes 注明 |
| source_url | string | 原文 URL（核实路径，必填，不填 "null"） |
| attachment | string | 主文件直链，**必须是绝对 URL**——相对路径要用 source_url 的 scheme+host 补全；跨域 OSS/CDN 链接照收；提取方法见 `references/verification.md`；多余附件放 notes；grep 零命中才填 `"null"` 并在 notes 注明 |
| match_level | string | `full` / `partial` / `unknown`；明确不匹配时使用`exclude`，不得创建记录 |
| matched_category | string | 细分类单选，枚举见 keywords.md；多类命中取最相关一个，其余放 notes |
| status | string | 单选：`active`（可投标）/ `intel`（情报）/ `closed`（已结束）/ `canceled`（取消/废标） |
| requires_manual | bool | 验证码/登录墙/反爬等无法自动核实为 `true`，否则 `false` |
| designated_supplier | string | 单一来源拟定供应商；无则 `"null"` |
| winner | string | 中标供应商；无则 `"null"` |
| award_amount | number | 中标/成交金额；无则 `0` |
| notes | string | 备注：排除原因、次类匹配、附件补充链接、待确认事项等；无则 `"null"` |

## seen.json 专属字段（**绝不进飞书载荷**）

上面的字段字典 = 创建流飞书载荷的**完整**字段集。`data/seen.json` 在此基础上额外存 5 个元字段，供去重、更新流原值回填与 query 归因使用：

| 字段 | 类型 | 说明 |
|---|---|---|
| dedup_key | string | 去重键：有项目编号用 `<project_code>\|<purchaser>`，无则用 `source_url` |
| first_seen | string | 首次命中日期 YYYY-MM-DD |
| last_seen | string | 最近一次命中日期 YYYY-MM-DD |
| pushed | bool | 是否已推送成功（HTTP 200 且 `code: 0`） |
| found_by_query | number[] | 命中该条的**全部** keywords.md §5 query 编号，如 `[3, 27]`；阶段 1 跨查询去重时采集，用途见 keywords.md §12 |

**推送前必须剥掉这 5 个字段。** 飞书字段集固定、平铺、不接受嵌套——`found_by_query` 是数组，混进载荷会直接触发字段识别报错。`scripts/send_webhook.ps1 -DryRun` 的平铺校验能拦住它，改动载荷组装逻辑后先用 DryRun 过一遍。

## 大区判定（region 单选）

`region` 按**采购人（purchaser）所在省份**判定。飞书字段为单选，只能填以下选项之一：

北京直管区、华中大区、东北一区、东南大区、华北二区、西北大区、华北一区、东北二区、西南大区、华东大区、华南大区、未知或非传统大区

| 大区 | 省市 |
|---|---|
| 北京直管区 | 北京 |
| 华北一区 | 河北、天津、山东 |
| 华北二区 | 河南、山西 |
| 西北大区 | 陕西、甘肃、青海、宁夏、西藏 |
| 东北一区 | 辽宁、吉林 |
| 东北二区 | 内蒙古、黑龙江、新疆 |
| 华东大区 | 浙江、上海、江苏 |
| 华南大区 | 广东、广西、海南 |
| 华中大区 | 湖北、安徽、湖南 |
| 东南大区 | 福建、江西 |
| 西南大区 | 四川、重庆、云南、贵州 |

判定规则：采购人所在省份在表内 → 填对应大区；省份不在表内或无法确定 → 填 `未知或非传统大区`。

## 状态枚举（与飞书单选字段一致，2026-08-11 对齐）

- `active`：公告有效、截止未过、清单匹配、可投标——投标流
- `intel`：单一来源公示 / 中标公告 / 合同公告 / 采购意向（含招标意向等探索性公告）/ **已截止的招标与采购公告**——不可投标但具情报价值，填 designated_supplier / winner / award_amount——研究流
- `closed`：已推送的 active 记录截止后经更新流转 closed、或项目处理完毕——吸收原 `expired`；**新检索发现的已截止公告不建 closed，直接走 intel 研究流**
- `canceled`：采购取消 / 废标 / 公告撤销

已移除：`manual`（职责并入 `requires_manual: true`）；`expired`（并入 `closed`）。

## 更新流 JSON 结构（飞书更新 Webhook，2026-08-11 定稿）

更新已有记录时走独立 Webhook（与创建流分开），定位用 `record_id`（skill 端生成，见字段字典）。**飞书 Webhook 只接受固定 JSON——字段集固定，每次发送完全相同，永不增删。**

约束与创建流相同：**平铺、无嵌套、无 null**（无值字符串 `"null"`、无值数字 `0`）。

**固定字段集（14 个）**：`record_id`、`change_type`、`status`、`notice_type`、`publish_date`、`deadline`、`winner`、`award_amount`、`designated_supplier`、`budget`、`contact`、`attachment`、`source_url`、`notes`

**核心规则——原值回填**：每次发送全部字段；**变更字段填新值，未变更字段填 seen.json 中存的原值**（无值按创建流规则填 `"null"`/`0`）。飞书无条件写入也安全：写回原值 = 无变化，不会破坏已有数据。代价：seen.json 必须存全量字段（实现时扩展）。

```json
{
  "record_id": "T20260811-K7X2P9",
  "change_type": "status_change",
  "status": "closed",
  "notice_type": "中标公告",
  "publish_date": "2026-08-10",
  "deadline": "2026-08-19T09:00",
  "winner": "XX医疗器械有限公司",
  "award_amount": 850000,
  "designated_supplier": "null",
  "budget": 10000,
  "contact": "马先生 0396-2797388",
  "attachment": "null",
  "source_url": "https://zfcg.henan.gov.cn/zhumadian/content?channelCode=H780603&infoId=1994759",
  "notes": "更新：中标公告发布，项目结束，中标方XX，金额85万"
}
```

**change_type 枚举与触发场景**：

| change_type | 触发场景 | 本次变更的字段 |
|---|---|---|
| `status_change` | 检索命中已推送记录的新公告：中标→closed、废标→canceled | status、notice_type、publish_date；中标再变更 winner / award_amount |
| `deadline_change` | 更正公告且截止时间变更 | deadline、notice_type、publish_date |
| `correction` | 其他信息更正（联系人、预算、附件等） | budget / contact / attachment 等 |

**固定规则**：
- `record_id` 必填，定位唯一，永不改变
- `title`、`purchaser`、`region`、`category` 等认为不可变，不进更新流（真有更名再扩展字段集）
- 更新成功（HTTP 200 且 `code: 0`）后同步更新 seen.json 对应记录（含全量字段）；失败不更新，下次运行重试

## 零命中约定

无任何有效条目时**不推送**（避免飞书堆积无意义记录），仅在执行摘要中说明"今日无新增有效条目"。

## 运行时证据（不进飞书载荷）

模型提交创建或更新判断时，必须同时提供 `evidence.source_verified=true`、核实时间和非空 `field_evidence`；格式见 `references/verification.md`。这些证据只保存在批次结果中，由 `scripts/tender_pipeline.py` 校验后剥离。原文无法核实时使用 `decision: manual`，不得生成可推送载荷。

新建 `active` 记录还必须满足：`match_level` 为 `full` 或 `partial`、`deadline` 已核实且非 `"null"`、`designated_supplier` 为 `"null"`。这些约束由脚本执行，不能靠人工绕过。
