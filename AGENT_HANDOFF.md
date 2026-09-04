# AGENT HANDOFF — 2026-09-04

Machine-readable handoff. Not written for humans. Read fully before touching
screening, pushing, or `data/seen.json`.

## 0. STATE AT HANDOFF

- branch: `feat/rewrite-keyword-tables`
- tests: 159 pass (`python -m unittest discover -s tests`)
- committed on `feat/rewrite-keyword-tables` as `c57ea22`. **Not pushed** —
  `git push` was denied by the sandbox permission classifier; the user must run
  it (or grant the permission). Branch is 1 ahead of origin. See §5.
- pushes made this session: 3 (all HTTP 200 + feishu `code: 0`, receipts recorded,
  `data/seen.json` 78 -> 81, then 81 -> 387 by the Feishu CSV import, see §4.1)

## 1. WHAT WAS BROKEN

Commit `817fe13` (2026-09-03) introduced `EXCLUDE_TERMS` hard exclusion evaluated
over **title + summary + full body**:

```
search_text = title + summary + content
if excluded_domain_term(search_text): drop      # ran BEFORE target-category match
signals = target_category_signals(search_text)  # unreachable for those
```

`EXCLUDE_TERMS` contains `核酸|PCR|测序|免疫组化|培养基|电泳|酶标仪|结核|畜牧|...`.
A hospital equipment/reagent list of 40-90 line items almost always contains one
PCR analyzer or one nucleic-acid kit. Result: **every mixed-bundle announcement
containing our category was silently dropped** — and mixed bundles are the normal
purchasing form for 过敏原/自免 systems.

Callers affected: `tender_pipeline.py` (unified layer, CCGP's only screen),
`jrbx_search.passes_prefilter` (silent `return False`, no log),
`plap_search.screen_row` / `matched_target_terms`.

### Measured damage, window 2026-09-03..2026-09-04

Measurement method: monkeypatched `excluded_domain_term` to a no-op, re-ran
`jrbx.collect` over the same window; CCGP measured offline from the saved source dir.

| source | dropped-with-target-signal | of those, real candidates |
| --- | --- | --- |
| jrbx | 11 | 5 |
| ccgp | 4 | 1 |
| total | 15 | **6** |

Same window actually pushed only 2. Missed > pushed by 3x.

The 5 correct jrbx kills (结核 x2, 疾控核酸 x2, 科研培养基, 畜牧) are **all also
caught by another gate** (title-scope exclude term / no target signal /
non-hospital buyer). So body-scope hard exclusion was redundant except for the
false kills.

Regression proof: `呼和浩特市第一医院以检验试剂为主的医用耗材采购招标公告`
(2900万, contains 过敏原特异性IgE抗体检测试剂盒) was **successfully pushed
2026-09-02**, one day before `817fe13`. Under `817fe13` code it is dropped by `PCR`.
It survived this run only because `already_seen` matched first.

Note `8aa8ec0` had already reverted `化学发光` out of `EXCLUDE_TERMS` for the same
class of reason. This was the second occurrence.

## 2. THE FIX

### 2.1 `search_common.screen_domain(title_text, body_text)` — new single entry point

Returns `{"keep": bool, "reason": str, "signals": [...], "body_exclude_term": str}`.

Rule:
1. exclude term in **title scope** -> drop.
2. no target category signal in title+body -> drop.
3. otherwise keep; an exclude term found in **body scope** is returned as
   `body_exclude_term`, a marker only, never a drop reason.

`product` / `product_list` (the flattened goods list) is **body scope, not title
scope** — jrbx puts `全自动体外过敏原筛查系统及其配套试剂` and `梯度pcr` in the
same field.

Callers rewritten: `tender_pipeline` unified layer, `jrbx.passes_prefilter`
(now returns the dict, not bool), `plap.screen_row`.
`plap.matched_target_terms` no longer self-excludes (all call sites are already
behind `screen_row`).

### 2.2 Second gap found while verifying the fix: `product_list` was not persisted

`jrbx.build_candidate` used `item["product"]` for its own attribution but never
wrote it to the candidate. When a body says `详见附件` / `下载`, the goods list
exists ONLY in that field. Consequence: the candidate survived the adapter, then
died at the unified layer with `无目标品类信号`, and `命中关键词` came out empty.

Fixed by threading `product_list` through every hop:
- `jrbx.build_candidate` -> emits `product_list`
- `search_common.write_candidates` -> persists it in `content/<id>.json`
- `search_common.merge_source_dirs` -> carries it to the unified candidate
- `tender_pipeline` -> includes it in (a) `screen_domain` body scope,
  (b) `retrieved_text` for `matched_keywords`/`departments`,
  (c) the pushed `内容（检索的摘要）` as a trailing `【标的清单】...` segment

(c) matters: without it the push showed no product at all for such notices and
sales could not judge relevance.

### 2.3 Observability

jrbx prefilter drops were invisible (`return False`, no record). The 11 drops were
only discoverable by human cross-check. Now `search_summary.json` carries
`prefilter_dropped: [{notice_id, stage, title, reason}]`, `stage` in
`{listing, detail}`.

### 2.4 Tests

`tests/test_search_common.py::MixedBundleScreeningTests` — pins all 6 real misses
by their actual body fragments, plus: title-scope exclusion still drops;
body exclude term without target signal still drops; clean candidate has empty
`body_exclude_term`; `product_list`-only signal survives.

`tests/test_jrbx_search.py::PrefilterTests` — updated for the dict return, plus
`test_exclude_term_only_in_product_list_keeps_candidate`.

`ProductDomainScreenTests.test_exclude_terms_beat_target_terms` renamed to
`test_exclude_terms_are_recognised`; the old name asserted a precedence that no
longer holds.

### 2.5 Docs updated

`SKILL.md` (screening paragraph under 完成条件), `references/keywords.md`
(「排除词」rewritten with the measured table + new 「预筛丢弃必须留痕」),
`references/verification.md` (new 「`body_exclude_term` 非空 = 混合包」 section;
fixed the stale 「排除词命中就丢」 sentence in 产品域判断).

## 3. PUSHES MADE THIS SESSION

Run `.tmp/search/2026-09-04` (main; 54 candidates, 19 queued, 2 batches,
2 create / 17 exclude / 0 manual):

1. `C0A36E9EC9AA4` 新疆维吾尔自治区人民医院白鸟湖医院 检验科抗核抗体IgG及相关试剂
   — budget corrected to 100000. jrbx structured `budget` said 10000000, body says
   `预算金额（元）：100000` twice. **jrbx budget field is not trustworthy alone.**
2. `C40362EB96824` 浙江大学医学院附属儿童医院 白介素6(IL-6)快速测定试剂及设备租赁

Run `.tmp/search/2026-09-04-recovered` (rebuilt from a known notice id; no new search):

3. `C5D6E48CC2A7F` 国家康复辅具研究中心附属康复医院 2026年医用耗材试剂遴选
   — recovered from the bug. `body_exclude_term: PCR`, tier `broad`, signal `细胞因子`.

## 4. OPEN ITEMS — ACT ON THESE

### 4.1 RESOLVED — Feishu ledger imported

Was: the 4 announcements the user posted to Feishu by hand were absent from
`data/seen.json` and would have been re-pushed as duplicates.

Resolved by importing `招标信息实时监测智能体_招标信息跟踪档案.csv` (374 rows, the
full Feishu archive, every row `是否已推送=1`). `data/seen.json` 81 -> 387:
+304 CSV rows, +2 aliases, 70 skipped as already-known or intra-CSV duplicates.

Imported records carry only the three fields `historical_identity_keys` actually
uses (`标题`, `链接`, `发布时间`) plus `_pushed: true`, `_source:
feishu_csv_import_2026-09-04`, `_feishu_id`. Deliberately minimal: `record_push`
treats a WEBHOOK_FIELD missing from a duplicate as equal
(`field not in duplicate or ...`), so a thin record can never trip
`seen中已存在同链接但内容不同的记录`.

**Two traps hit during the import — read before importing another export:**

1. **73 of the 374 Feishu links are not announcement pages.** They are aggregator
   tag/search/list pages (`https://www.120bid.com/tag/53_4785_mianyifenxiyi.html`
   backs 7 different notices) or SPA links whose id lives in the fragment
   (`https://ctbpsp.com/#/bulletinDetail?uuid=...` normalizes to the bare domain
   `https://ctbpsp.com/`). 21 normalized URLs covered 56 rows. Importing those as
   url keys would falsely suppress every future candidate on those domains.
   Handled by `url_is_trustworthy`: a link earns a url key only if it is unique
   within the import, has a real path or query, and is not a tag/search/list path.
   The other 73 rows are stored with `_untrusted_link` (kept for humans, not a
   dedup key) and dedup on `title_date` alone.

2. **`normalize_url("")` returns `"/"`**, so a record with no link used to emit a
   real-looking `("url", "/")` key. First blank-link record claimed it and the next
   72 were discarded as duplicates. Fixed in
   `tender_pipeline.historical_identity_keys` (skip the url key when normalized is
   `"/"`), pinned by `tests/test_search_common.py::HistoricalIdentityKeyTests`.

**Two alias records added** (`_source: manual_feishu_alias_2026-09-04`) because the
Feishu title/link and the pipeline-side title/link differ enough that no key would
match. Both verified by hand this session:

| Feishu side | pipeline side |
| --- | --- |
| `【院务公开】阿拉善盟中心医院实验室设备配置项目招标公告` + mp.weixin link | `阿拉善盟中心医院实验室设备配置项目招标公告` + ccgp.gov.cn link |
| `长沙市医健建设发展有限公司检验科…` (single prefix) + ctbpsp link | `长沙市医健建设发展有限公司长沙市医健建设发展有限公司检验科…` (doubled prefix, jrbx full-body record) |

Verified after import: all 5 pipeline-side identities (both 长沙 records, 阿拉善盟,
仙居, 甘肃) are blocked; 0 duplicate url keys and 0 bare-domain keys in the ledger;
the 3 real pushes from this session survive. A `prepare` re-run over the main
search dir moved `already_seen` 7 -> 10 and left 0 unjudged candidates in the queue.

If another Feishu export is imported later, reuse `url_is_trustworthy` — do not
trust the `链接` column blindly.

### 4.2 MEDIUM — `变应原皮肤点刺液` product scope unresolved

`彭州市人民医院2026年第二十次医用耗材临时采购遴选项目（第二次挂网）`
(jrbx `E0BC8E8018F8CC8DD43E0D3054E282D0`): 包件1 = 悬铃木花粉 / 德国小蠊 /
猫毛皮屑 变应原皮肤点刺液, 600元/个, 国产, 挂网. 包件2 = 科研试剂 (DMEM培养基,
DNA ladder, 内切酶, HRP二抗) -> clearly out.

Judged **not pushed**, on the grounds that 皮肤点刺液 is in-vivo diagnostic while
the 过敏 table in `keywords.md` is in-vitro only (sIgE / 总IgE / 食物不耐受 sIgG /
免疫印迹仪). **This is a product-scope question, not a screening question**: if
浩欧博 sells or intends to sell 点刺液, the 过敏 table needs that row and this
decision flips. Ask 业务方. Until then the current tables are authoritative and
the exclusion stands.

### 4.3 MEDIUM — `25羟基维生素D` and `细胞因子` lines still unconfirmed

`verification.md` records that frontline sales repeatedly report 公司无相关产品 for
both, while both remain in the 自免 table. Protocol says judge normally until
业务方 confirms. Two of the three pushes this session (`C40362EB96824`,
`C5D6E48CC2A7F`) are 细胞因子/IL-6. If 业务方 confirms there is no product, delete
those two rows from the tables in `keywords.md`; `BROAD_SIGNAL_GROUPS` in
`search_common.py` already lists them as low-yield. That would also remove a large
share of the remaining broad-tier noise.

### 4.4 MEDIUM — `截止时间` comes out `null` for 院内遴选 / 院内自行采购

`verification.md` 易错字段 forbids using 报名 / 文件获取 deadlines. These notice
types often publish **no** separate 投标/响应文件提交截止 — only a 报名 window that
functionally is the response deadline (`逾期提交资料将不再接收`). Applied strictly
this session, so `C40362EB96824` (浙大儿院, 报名 9/3-9/14) and `C5D6E48CC2A7F`
(康复辅具, 报名 9/7-9/11 16:00) both shipped with `截止时间: "null"`, losing the
only actionable date from that field. The date does survive inside
`内容（检索的摘要）`.

Consider a narrow amendment: allow 报名截止 as `截止时间` **only when** the notice
publishes no later submission step. Do not change this silently — it alters what
sales sees in a fixed field. jrbx exposes `registerDeadline` for these
(康复辅具: 1789113600000 = 2026-09-11 16:00 CST), so it is mechanically available.

### 4.5 LOW — jrbx stores duplicate records for one announcement

`长沙市医健` exists twice in jrbx: `DA2B49D904B85A77DD50F654DC76A832` is a 149-char
stub (title + 发布时间 + 信息来源 link only, `product` empty), while
`E179B6410FDE95D1A9DCB64B89BB5DC7` carries the full 86-row equipment list. Recall
correctness depends on which one a query returns. The stub yields no target signal
and is dropped as noise — correct in isolation, but it means a real announcement
can end up represented only by its stub. No fix attempted. If this recurs,
de-duplicate jrbx records by title fingerprint inside the adapter and prefer the
one with the longer body.

### 4.6 LOW — CCGP transient timeouts

Main run had 4 CCGP query timeouts (`狼疮`, `硬皮`, `肌炎`, `gp210`). Retried
separately at `--delay 3`; all 4 succeeded and yielded 1 extra candidate that was
merged back in. `ccgp_search.py` has no internal retry. Cheap improvement: one
retry with backoff on `read operation timed out`.

### 4.7 INFO — jrbx origin-url quota exhausted 2026-09-04

`original_url` returned the quota code, so the recovered candidate uses the jrbx
article permalink (`link_kind: jrbx_article`, `source_priority` 250), which needs a
jrbx login to open. Expected behaviour; resets daily.

## 5. WHAT WENT INTO THE COMMIT

Committed together on `feat/rewrite-keyword-tables` as `c57ea22`, after confirming
the pre-existing working-tree changes do not conflict with this fix. The push to
`origin` was blocked by the sandbox permission classifier and is still pending.

Two independent workstreams share the commit:

1. **Pre-existing (not authored this session)** — jrbx multi-account pool and 1403
   rate-limit rotation: `JrbxClient` (account retire + in-place retry on the next
   account), credential pool loading, `check_token` per-account output,
   `config/jrbx.example.json` `accounts[]` schema, `tender_search.py`
   `AUTH_ERROR_EXIT_CODES` (3 = 登录态失效, 5 = 池空频控), `README.md`,
   `references/jrbx.md`.
2. **This session** — the screening fix (§2), `product_list` plumbing,
   `prefilter_dropped` logging, the `normalize_url("")` identity-key fix, the
   Feishu ledger import (§4.1), and this document.

Conflict check performed: workstream 1 touches `jrbx_search.py` lines ~99-630
(credentials / client / `main`); workstream 2 touches `passes_prefilter` (~754),
`build_candidate` (~882), `collect` (~948). No overlapping hunks; 159 tests pass
with both applied.

`_scratch/` was left untracked and out of the commit.

## 6. HOW TO RE-VERIFY WITHOUT A NEW SEARCH

```
python -m unittest tests.test_search_common.MixedBundleScreeningTests -v
python -m unittest tests.test_jrbx_search.PrefilterTests -v
```

To re-measure live damage on any window: monkeypatch
`jrbx_search.excluded_domain_term` to return `""`, run
`jrbx.collect(..., max_origin_lookups=0)` (costs no origin-url quota), and diff the
candidate count against a normal run. Record which drops carried a non-empty
`target_category_signals` — those are the false kills.
