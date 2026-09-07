"""公告身份：检索合并、历史台账与发送门禁共用。不把项目当成公告。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from urllib.parse import urlsplit, parse_qs

from search_common import canonical_url, title_fingerprint, title_is_truncated, notice_family, zlbx_bid_id

REPOST_DAYS = 3
EMPTY = {"", "null", "none", "未知", "未公开", "未披露", "无"}


def text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    return "" if value.lower() in EMPTY else value


def fingerprint(value):
    return title_fingerprint(text(value))


@lru_cache(maxsize=8192)
def buyer_key(value):
    value = text(value)
    if not value:
        return ""
    from hospital_match import get_default_index
    match = get_default_index().match(name=value)
    # 仅整名/整别名唯一匹配可以归一采购人；不接受正文或子串猜测。
    if match.get("matched") and match.get("match_method", "").startswith("explicit_"):
        value = match["hospital_name"]
    return fingerprint(value)


def publish_date(value):
    match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text(value))
    try:
        return date(*map(int, match.groups())).isoformat() if match else ""
    except ValueError:
        return ""


def normalized_title(value, published=""):
    value = text(value)
    # 台账有来源把发布日期附在标题尾部。只剥与本条发布日期一致的独立日期。
    if published:
        value = re.sub(r"\s+" + re.escape(published) + r"(?:\s+\d{2}:\d{2})?\s*$", "", value)
    return fingerprint(value)


def stage(value):
    family = notice_family(value)
    if family in {"更正", "结果", "终止", "合同", "意向", "资格预审"}:
        return family
    if re.search(r"调研|调查|参数征集|需求公示", value):
        return "意向"
    return "采购" if family else ""


def scope(value):
    """保留轮次、批次、包/标段；不删除中文数字以求相似。"""
    value = text(value)
    tokens = re.findall(
        r"第?[一二三四五六七八九十百零〇两\d]+(?:次|批次|批|期)"
        r"|重新招标|重新采购|(?:包|标段)\s*[A-Za-z\d]+"
        r"|[A-Za-z\d]+\s*(?:包|标段)", value)
    normalized = []
    for token in tokens:
        token = re.sub(r"\s+", "", token).lower().removeprefix("第").replace("批次", "批")
        number = re.match(r"([一二三四五六七八九十百零〇两\d]+)(次|批|期)$", token)
        if number:
            raw, kind = number.groups()
            if raw.isdigit():
                n = int(raw)
            else:
                digits = dict(zip("零〇一二两三四五六七八九", [0,0,1,2,2,3,4,5,6,7,8,9]))
                n = current = 0
                for char in raw:
                    if char in "十百":
                        n += (current or 1) * (10 if char == "十" else 100)
                        current = 0
                    else:
                        current = digits[char]
                n += current
            token = f"{n}{kind}"
        token = re.sub(r"^(包|标段)([a-z\d]+)$", r"\2\1", token)
        normalized.append(token)
    return tuple(sorted(set(normalized)))


def url_key(value):
    value = canonical_url(text(value))
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    # 网站首页、搜索结果页不是单条公告的稳定身份。
    if (parts.path in {"", "/"} and not parts.query and not parts.fragment
            or parts.netloc == "weixin.sogou.com" and parts.path == "/weixin"):
        return ""
    return value.split(":", 1)[1]  # 同一站点 HTTP/HTTPS 不制造新身份。


def source_ids(url):
    ids = set()
    bid = zlbx_bid_id(url)
    if bid:
        ids.add("zlbx:" + bid)
    parts = urlsplit(canonical_url(url))
    host = parts.netloc.lower()
    if host in {"www.jrbx.com", "jrbx.com"}:
        value = parse_qs(parts.query).get("id", [""])[0]
        if value:
            ids.add("jrbx:" + value.lower())
    if host == "ccgp.gov.cn" or host.endswith(".ccgp.gov.cn"):
        match = re.search(r"t\d{8}_(\d+)\.htm", parts.path)
        if match:
            ids.add("ccgp:" + match[1])
    return ids


@dataclass(frozen=True)
class Identity:
    title: str
    fp: str
    published: str
    buyer: str
    project: str
    phase: str
    scope: tuple
    truncated: bool
    urls: frozenset
    ids: frozenset
    subject: str


def identity(record):
    fields = record.get("source_fields") or {}
    title = text(record.get("标题") or record.get("title"))
    published = publish_date(record.get("发布时间") or record.get("publish_time"))
    raw_buyer = record.get("单位") or fields.get("单位") or record.get("医院全名")
    buyer = buyer_key(raw_buyer)
    project = fingerprint(record.get("项目编号") or fields.get("项目编号"))
    links = [record.get("链接"), record.get("url"), record.get("source_url")]
    links += record.get("_identity_urls", [])
    links += [a.get("url") for a in record.get("alternate_sources", [])]
    ids = set(record.get("_identity_ids", []))
    bid = record.get("bid_id") or record.get("_bid_id")
    if text(bid):
        ids.add("zlbx:" + str(bid))
    urls = set()
    for link in filter(None, links):
        key = url_key(link)
        if key:
            urls.add(key)
            ids.update(source_ids(link))
    fp = normalized_title(title, published)
    subject = fp
    for prefix in (fingerprint(raw_buyer), buyer):
        while prefix and subject.startswith(prefix) and len(subject) - len(prefix) >= 8:
            subject = subject[len(prefix):]
    return Identity(title, fp, published, buyer, project,
                    stage(title), scope(title), title_is_truncated(title), frozenset(urls), frozenset(ids), subject)


def duplicate_reason(a, b):
    # 同 URL 的更正/结果也不能吞掉原公告的另一阶段。
    if a.phase and b.phase and a.phase != b.phase:
        return ""
    strong = a.ids & b.ids or a.urls & b.urls
    if strong:
        # 固定 URL 被复用到不同批次时也视为新公告。
        if a.scope != b.scope and not (a.truncated or b.truncated):
            return ""
        return "同一来源公告ID" if a.ids & b.ids else "同一公告链接"
    if a.project and b.project and a.project != b.project:
        return ""
    if a.buyer and b.buyer and a.buyer != b.buyer:
        return ""
    if not a.published or not b.published:
        return ""
    gap = abs((date.fromisoformat(a.published) - date.fromisoformat(b.published)).days)
    if gap > REPOST_DAYS or (a.phase == "更正" and gap):
        return ""
    if a.scope != b.scope and not (a.truncated or b.truncated):
        return ""
    # 缺采购人的通用模板标题不能跨医院判重。带明确医院名的长标题兼容旧瘦台账。
    buyer_supported = bool(a.buyer and a.buyer == b.buyer)
    specific_title = len(a.fp) >= 16 and len(b.fp) >= 16 and "医院" in a.title and "医院" in b.title
    if not buyer_supported and not specific_title:
        return ""
    if (a.fp == b.fp or buyer_supported and a.subject == b.subject) and len(a.fp) >= 8:
        return "采购人、标题与发布日期一致" if not gap else "同采购人同标题的跨平台转载"
    if not gap and ((a.truncated and len(a.fp) >= 16 and b.fp.startswith(a.fp))
                    or (b.truncated and len(b.fp) >= 16 and a.fp.startswith(b.fp))):
        return "同日同采购人的来源截断标题"
    if (a.project and a.project == b.project and buyer_supported
            and a.phase and a.phase == b.phase and a.scope == b.scope):
        return "同采购人、项目编号、阶段、轮次及包号的近期公告"
    return ""


class IdentityIndex:
    """索引强身份及标题/采购人，候选判重不再全表做正则。"""
    def __init__(self, records=()):
        self.records, self.identities, self.keys = [], [], {}
        for record in records:
            self.add(record)

    def add(self, record):
        i = len(self.records)
        ident = identity(record)
        self.records.append(record)
        self.identities.append(ident)
        keys = [("url", v) for v in ident.urls] + [("id", v) for v in ident.ids]
        keys += [("title_prefix", ident.fp[:16])] if len(ident.fp) >= 16 else []
        keys += [("title", ident.fp)] if ident.fp else []
        keys += [("buyer", ident.buyer)] if ident.buyer else []
        for key in keys:
            self.keys.setdefault(key, set()).add(i)
        return i

    def find(self, record):
        a = identity(record)
        keys = [("url", v) for v in a.urls] + [("id", v) for v in a.ids]
        keys += [("title_prefix", a.fp[:16]), ("title", a.fp), ("buyer", a.buyer)]
        possible = set().union(*(self.keys.get(k, set()) for k in keys))
        for i in sorted(possible):
            reason = duplicate_reason(a, self.identities[i])
            if reason:
                return self.records[i], reason
        return None, ""

    def possible(self, record):
        """旧台账缺采购人且没有稳定链接可对照时，保留为疑似重复而非新公告。"""
        a = identity(record)
        for i in sorted(self.keys.get(("title", a.fp), set())):
            b = self.identities[i]
            if (not a.fp or not a.published or not b.published
                    or a.buyer and b.buyer or a.phase != b.phase or a.scope != b.scope
                    or a.project and b.project and a.project != b.project):
                continue
            gap = abs((date.fromisoformat(a.published) - date.fromisoformat(b.published)).days)
            if gap <= REPOST_DAYS and not (a.phase == "更正" and gap):
                return self.records[i], "标题及日期吻合，但采购人缺失且链接不同，需核对是否已入账"
        return None, ""


def remember_aliases(target, *records):
    """保留所有已核定相同公告的链接与来源ID；不覆盖原来的业务字段。"""
    urls = set(target.get("_identity_urls", []))
    ids = set(target.get("_identity_ids", []))
    for record in (target,) + records:
        ident = identity(record)
        urls.update("https:" + u for u in ident.urls)
        ids.update(ident.ids)
    target["_identity_urls"] = sorted(urls)
    target["_identity_ids"] = sorted(ids)
