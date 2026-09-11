"""公告查重漏斗：绝大多数结论由确定性规则给出，只有窄带才交给模型。

设计目标是**不让模型做重复劳动**。四层依次收口，前一层定了案就不进下一层：

  L1 强身份   来源公告ID / 链接 / 采购人+标题+日期 / 项目编号 —— 零模型，见 tender_identity
  L1 后续阶段 同一招标已入账时，它的更正/变更/澄清不再推：销售已在跟进这个标
  L2 字符级   裁剪归一后的标题做 3-gram Jaccard 与序列比，两端阈值直接定案
  L3 正文     标题落在中间带时，再比正文摘要（惰性读盘），两端阈值仍能直接定案
  L4 语义     只有前三层都没定下来的少数配对才写进 semantic_review.jsonl 交给模型

进入 L2 之前先过硬门（阶段、项目编号、采购人、轮次/批次/包号、日期窗口），
硬门与 tender_identity.duplicate_reason 用同一套前置条件，避免两处判重走偏。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

from tender_identity import (REPOST_DAYS, IdentityIndex, fingerprint, identity,
                             publish_date, text)

# 标题字符级相似度：高于上界直接判重，低于下界直接判新，中间带才继续。
TITLE_HIGH = 0.90
TITLE_LOW = 0.55
# 正文相似度：只在标题落中间带时才算，同样两端定案。
CONTENT_HIGH = 0.80
CONTENT_LOW = 0.25
# 每条候选最多带几个台账行去问模型。台账里真正像的行不会超过这个数，
# 超了说明是通用模板标题，多问几条也不会更准，只会更慢。
TOP_K = 3
# 一次运行里语义配对的上限。超过说明台账或检索出了问题，宁可停下来查。
MAX_SEMANTIC_PAIRS = 60
# 项目编号一致时允许的日期跨度：编号在一个项目的整个生命周期里不变，
# 但阶段与轮次仍要一致，才不会把招标和它的结果公告并成一条。
PROJECT_NUMBER_DAYS = 30
# 后续阶段压制：同一个招标已经推给销售、他们已在跟进，之后的更正就不必再推一次。
# 只压制修订类公告；二次/重新招标与不同包号是新的投标机会，靠 scope 一致挡在外面。
FOLLOWUP_PHASES = {"更正"}
FOLLOWUP_DAYS = 90
FOLLOWUP_TITLE_SIM = 0.75
GRAM = 3
SUMMARY_CHARS = 200


def ngrams(value, size=GRAM):
    value = str(value or "")
    if len(value) < size:
        return {value} if value else set()
    return {value[i:i + size] for i in range(len(value) - size + 1)}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity(a, b):
    """字符级相似度：3-gram Jaccard 与序列比取大。两者都不依赖模型。"""
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return round(max(jaccard(ngrams(a), ngrams(b)), SequenceMatcher(None, a, b).ratio()), 4)


def content_key(value):
    """正文摘要归一：去空白标点，保留中文与字母数字，长度截断到可比范围。"""
    return re.sub(r"[\s\W_]+", "", str(value or "").lower(), flags=re.UNICODE)[:1500]


def record_content(record):
    evidence = record.get("search_evidence") or {}
    for value in (record.get("内容"), evidence.get("summary"),
                  record.get("内容（检索的摘要）"), record.get("summary")):
        value = text(value)
        if value:
            return value
    return ""


def day_gap(a, b):
    if not a or not b:
        return None
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def blocked(a, b):
    """硬门：命中任一条就不是同一条公告，连相似度都不必算。"""
    if a.phase and b.phase and a.phase != b.phase:
        return "公告阶段不同"
    if a.project and b.project and a.project != b.project:
        return "项目编号不同"
    if a.buyer and b.buyer and a.buyer != b.buyer:
        return "采购人不同"
    if a.scope != b.scope and not (a.truncated or b.truncated):
        return "轮次、批次或包号不同"
    return ""


# 公告体裁词：认「同一个招标」时把首尾的体裁词剥掉，剩下的才是标的本身。
# 更正公告与它修订的原公告，差别几乎全在这些词上。
NOTICE_WORDS = (r"(?:关于|更正|变更|澄清|补充|答疑|结果|中标|成交|终止|流标|废标|合同"
                r"|采购意向|意向|需求调查|市场调研|公开招标|邀请招标|资格预审"
                r"|竞争性磋商|竞争性谈判|磋商|谈判|单一来源|询价|比选|竞价|遴选"
                r"|招标|采购|项目|公告|公示|通知|说明|事项|纪要|函|的)+")
NOTICE_LEAD = re.compile("^" + NOTICE_WORDS)
NOTICE_TAIL = re.compile(NOTICE_WORDS + "$")


def tender_core(ident):
    """剥掉首尾体裁词后的标的主体，用来认「是不是同一个招标」。"""
    core = ident.subject or ident.fp
    for _ in range(6):
        stripped = NOTICE_TAIL.sub("", NOTICE_LEAD.sub("", core))
        if stripped == core:
            break
        core = stripped
    return core


# 分包写法：更正只改其中一个包，台账里那条总招标却一个包号都不带。
PACKAGE_SCOPE = re.compile(r"(?:包|标段|批)$")
# 台账主体要认成「候选的总标」，至少得有这么长；再短就是「设备采购」这类通用写法，
# 拿子串关系判重会把同一家医院的不同标全并成一条。
SUBJECT_CONTAIN_MIN = 12


def umbrella_covers(a, b):
    """台账那行是覆盖全部包的总招标、候选是它其中一个包的更正吗？

    总招标公告的标题往往一个包号都不写，更正才点名「第四包」。此时轮次门
    （a.scope != b.scope）会把这一对踢掉，同一个标的每一个包的每一次更正都能
    重新推一遍。放行只限这一个方向，且候选带的必须是包号：
    台账行自己带包号时照旧卡住（第一包已推 ≠ 第三包的更正），候选带的是
    「次/期/重新招标」这类轮次词时也照旧卡住——重招对销售是新的投标机会。
    """
    return bool(not b.scope and a.scope
                and all(PACKAGE_SCOPE.search(token) for token in a.scope))


def subject_contains(ledger_core, candidate_core):
    """台账标的主体是候选主体的子串——更正在原标题后追加包号与标的时的常态。

    「…检验科设备采购项目」与「…检验科设备采购项目第四包全自动免疫印迹仪…」
    字符级相似度只有 0.69（越写得细越低），但前者整个包在后者里。包含关系比
    相似度更能说明「是同一个标的细化」，相似度量不出来。
    """
    return bool(ledger_core and len(ledger_core) >= SUBJECT_CONTAIN_MIN
                and ledger_core in candidate_core)


def title_evidence_ok(a, b):
    """没有采购人佐证时，只有点名医院的长标题才敢按标题判重。"""
    if a.buyer and a.buyer == b.buyer:
        return True
    return (len(a.fp) >= 16 and len(b.fp) >= 16
            and "医院" in a.title and "医院" in b.title)


def pair_id(candidate_id, ledger_id):
    return f"{candidate_id}~{ledger_id}"


@dataclass
class Pair:
    pair_id: str
    ledger: dict
    title_sim: float
    content_sim: float

    @property
    def score(self):
        return max(self.title_sim, self.content_sim)


@dataclass
class Match:
    verdict: str            # duplicate | new | semantic
    layer: str = ""         # L1 / L2 / L3 / L4 / decision
    reason: str = ""
    matched: dict = None
    pairs: list = field(default_factory=list)
    candidate_content: str = ""   # 惰性取到的候选正文，交给模型时不必再读一次盘


class LedgerMatcher:
    """按飞书台账快照判重。构造一次，整轮候选复用同一份索引。"""

    def __init__(self, records, decisions=None):
        self.records = [r for r in records if text(r.get("标题"))]
        self.index = IdentityIndex(self.records)
        self.identities = self.index.identities
        self.decisions = {d["pair_id"]: d for d in (decisions or [])}

    # ---- L1 ----

    def _project_number_match(self, a):
        """标题面目全非但项目编号、阶段、轮次一致，仍是同一条公告。"""
        if not a.project or not a.published:
            return None, ""
        for i, b in enumerate(self.identities):
            if b.project != a.project or blocked(a, b):
                continue
            if not a.phase or a.phase != b.phase or a.scope != b.scope:
                continue
            gap = day_gap(a.published, b.published)
            if gap is None or gap > PROJECT_NUMBER_DAYS:
                continue
            return self.records[i], "同项目编号、同阶段、同轮次的公告"
        return None, ""

    def _followup_match(self, a):
        """同一个招标已入账、销售已在跟进，之后的更正/变更/澄清不再推一次。

        只压制修订类公告：轮次、批次、包号必须一致，所以二次招标、重新招标和同一
        项目的其它包都不受影响——它们对销售是新的投标机会。判定锚点是项目编号，
        或者「同一采购人 + 剥掉体裁词后标的主体高度相似」。
        """
        if a.phase not in FOLLOWUP_PHASES or not a.published:
            return None, ""
        core_a = tender_core(a)
        for i, b in enumerate(self.identities):
            if a.scope != b.scope and not umbrella_covers(a, b):
                continue
            if a.project and b.project and a.project != b.project:
                continue
            gap = day_gap(a.published, b.published)
            if gap is None or gap > FOLLOWUP_DAYS:
                continue
            same_project = bool(a.project and a.project == b.project)
            core_b = tender_core(b)
            same_subject = bool(
                a.buyer and a.buyer == b.buyer and len(core_a) >= 4
                and (similarity(core_a, core_b) >= FOLLOWUP_TITLE_SIM
                     or subject_contains(core_b, core_a)))
            if not (same_project or same_subject):
                continue
            return self.records[i], (
                "同一招标已入账（{}），更正类公告不再推送".format(
                    text(self.records[i].get("_feishu_id")) or text(self.records[i].get("标题"))[:20]))
        return None, ""

    # ---- L2 / L3 ----

    def _rank(self, record, a, content_loader=None):
        # 正文只在真的要比的时候才取。检索目录里的正文是本地文件，读它不花接口调用，
        # 但绝大多数候选连一个相近的台账行都没有，没必要为它们全部读盘。
        resolved = None

        def candidate_body():
            nonlocal resolved
            if resolved is None:
                text_value = record_content(record)
                if not text_value and content_loader is not None:
                    text_value = content_loader() or ""
                resolved = (text_value, content_key(text_value))
            return resolved

        pairs = []
        for i, b in enumerate(self.identities):
            if blocked(a, b):
                continue
            gap = day_gap(a.published, b.published)
            if gap is None or gap > REPOST_DAYS:
                continue
            title_sim = similarity(a.fp, b.fp)
            if title_sim < TITLE_LOW:
                continue
            candidate_content = candidate_body()[1]
            ledger_content = content_key(record_content(self.records[i]))
            content_sim = (similarity(candidate_content, ledger_content)
                           if candidate_content and ledger_content else 0.0)
            # 自动编号可能为空（手工行或接口尚未回填）；退回 record_id，避免多条空编号
            # 台账行生成同一个 pair_id，导致语义判定互相覆盖。
            ledger_id = (self.records[i].get("_feishu_id")
                         or self.records[i].get("_record_id") or f"row-{i}")
            pairs.append(Pair(pair_id(record.get("candidate_id", ""), ledger_id),
                              self.records[i], title_sim, content_sim))
        pairs.sort(key=lambda p: p.score, reverse=True)
        return pairs, (resolved[0] if resolved else "")

    # ---- 对外 ----

    def check(self, record, content_loader=None):
        a = identity(record)
        matched, reason = self.index.find(record)
        if matched is not None:
            return Match("duplicate", "L1", reason, matched)
        matched, reason = self._project_number_match(a)
        if matched is not None:
            return Match("duplicate", "L1", reason, matched)
        matched, reason = self._followup_match(a)
        if matched is not None:
            return Match("duplicate", "L1-后续阶段", reason, matched)

        pairs, body = self._rank(record, a, content_loader)
        if not pairs:
            return Match("new", "L2", "台账中没有相近标题")

        decided = [self.decisions.get(p.pair_id) for p in pairs]
        same = next((d for d in decided if d and d.get("same") is True), None)
        if same:
            row = next(p.ledger for p in pairs if p.pair_id == same["pair_id"])
            return Match("duplicate", "decision", "人工语义核对判定为同一公告：" + same.get("note", ""), row)

        top = pairs[0]
        b = identity(top.ledger)
        if top.title_sim >= TITLE_HIGH and title_evidence_ok(a, b):
            return Match("duplicate", "L2",
                         f"标题字符级高度一致（{top.title_sim}）且采购人与发布日期吻合", top.ledger)
        if top.content_sim >= CONTENT_HIGH and title_evidence_ok(a, b):
            return Match("duplicate", "L3",
                         f"正文摘要高度一致（{top.content_sim}），标题为跨来源改写", top.ledger)

        undecided = [p for p, d in zip(pairs, decided) if not d]
        # 正文两端都排除得掉的配对不必再问模型。
        undecided = [p for p in undecided
                     if not (p.content_sim and p.content_sim <= CONTENT_LOW
                             and p.title_sim < TITLE_HIGH)]
        if not undecided:
            return Match("new", "L3", "字符级与正文相似度都不足以判重")
        return Match("semantic", "L4", "标题与正文相似度落在需要语义判断的区间",
                     pairs=undecided[:TOP_K], candidate_content=body)


def review_row(record, match):
    """写进 semantic_review.jsonl 的一行：只给模型判定必需的最小信息。"""
    a_content = match.candidate_content or record_content(record)
    return {
        "candidate_id": record.get("candidate_id", ""),
        "候选": {
            "标题": text(record.get("标题") or record.get("title")),
            "发布时间": publish_date(record.get("发布时间") or record.get("publish_time")),
            "采购人": text(record.get("单位") or (record.get("source_fields") or {}).get("单位")),
            "项目编号": text(record.get("项目编号") or (record.get("source_fields") or {}).get("项目编号")),
            "正文摘要": a_content[:SUMMARY_CHARS],
        },
        "台账候选": [
            {
                "pair_id": p.pair_id,
                "编号": p.ledger.get("_feishu_id", ""),
                "标题": text(p.ledger.get("标题")),
                "发布时间": publish_date(p.ledger.get("发布时间")),
                "采购人": text(p.ledger.get("单位") or p.ledger.get("医院全名")),
                "项目编号": text(p.ledger.get("项目编号")),
                "正文摘要": record_content(p.ledger)[:SUMMARY_CHARS],
                "标题相似度": p.title_sim,
                "正文相似度": p.content_sim,
            }
            for p in match.pairs
        ],
    }
