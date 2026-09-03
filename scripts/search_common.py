#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索适配器共享的候选契约、落盘与跨来源去重。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_KEYS = {"spm", "from", "source", "track", "timestamp", "t"}
CCGP_ARTICLE_RE = re.compile(r"/t\d{8}_(\d+)\.htm$", re.I)
PLAP_NOTICE_RE = re.compile(r"/ggxx/info/\d{4}/([0-9a-f]{32})\.html$", re.I)
PLAP_HOSTS = {"plap.mil.cn", "www.plap.mil.cn"}
SOURCE_PRIORITIES = {
    "ccgp": 400,
    "plap": 400,
}
PROJECT_ID_RE = re.compile(
    r"(?:项目编号|采购项目编号|招标编号|项目编码)\s*[：:]\s*"
    r"([A-Za-z0-9][A-Za-z0-9._()（）/\-]{2,80})",
    re.I,
)
TARGET_CATEGORY_PATTERNS = [
    ("过敏原/IgE", re.compile(r"过敏原|过敏源|变应原|特异性\s*IgE|sIgE|总\s*IgE|tIgE", re.I)),
    ("自身抗体/自身免疫", re.compile(
        r"自身抗体|自身免疫|抗核抗体|\bANA\b|\bENA\b|双链\s*DNA|dsDNA|\bANCA\b|"
        r"抗磷脂抗体|心磷脂抗体|自免肝|肌炎抗体|\bPLA2R\b|细胞因子|IgG\s*亚类",
        re.I,
    )),
    ("酶联免疫", re.compile(r"酶联免疫|\bELISA\b", re.I)),
    # 化学发光 2026-09-03 移出正向信号（检索噪声过大），但**不做硬排除**：理由见下方
    # EXCLUDE_TERMS 的注释与 keywords.md §5.1。
    ("免疫荧光/免疫印迹", re.compile(r"免疫荧光|免疫印迹|免疫印迹仪", re.I)),
    ("免疫质控/校准", re.compile(r"免疫.{0,8}(?:质控品|校准品)|(?:质控品|校准品).{0,8}免疫", re.I)),
    # 酶标仪 2026-08-27 移除：业务方确认公司不做该品类（keywords.md §10.1），
    # 它却一直是正向信号，纯酶标仪公告会假阳性进核实。只从信号里摘掉、不做硬排除，
    # 「酶免仪+酶标仪+洗板机」这类混合包仍可靠酶免仪命中。
    ("免疫分析仪器", re.compile(r"免疫分析仪|全自动酶免|酶免工作站|酶免仪|洗板机", re.I)),
]

# 非本司产品域，命中即判无关，**优先级高于 TARGET_CATEGORY_PATTERNS**（keywords.md §10.1）。
# 2026-09-03 起为三个来源共用的唯一定义：此前 jrbx_search.py 与 plap_search.py 各存
# 一份副本靠注释保持同步，CCGP 候选则完全没有产品域硬排除。
#
# 化学发光**不在**本表：它当天曾被加入，实测回跑 09-02 的 33 条推送记录时拦下了一条
# 54 万元的血管炎自身抗体（ANCA）试剂盒招标——该词只出现在「可与…化学发光仪配套使用」
# 的兼容性条款里。改为「只摘正向信号、不做硬排除」：单说化学发光而无其它品类信号的公告
# 会被品类信号闸挡下，纯整机标由核实阶段按 references/verification.md 排除。
EXCLUDE_TERMS = re.compile(
    r"酶标仪|电泳|兽医|兽用|畜牧|生猪|结核|干扰素释放|免疫组化|重组蛋白|培养基|缓冲液|核酸|PCR|测序",
    re.I,
)


def excluded_domain_term(text):
    """命中的硬排除词；未命中返回空串。用于把排除理由写进 skip_reason。"""
    match = EXCLUDE_TERMS.search(text or "")
    return match.group(0) if match else ""


def compact_text(value, limit=None):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit and len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def title_fingerprint(title):
    return re.sub(r"[\s\W_]+", "", (title or "").lower(), flags=re.UNICODE)


def canonical_url(raw):
    """去跟踪参数，并规范 CCGP、PLAP 官方公告链接。"""
    try:
        parts = urlsplit(str(raw or "").strip())
        host = parts.netloc.lower()
        scheme = parts.scheme.lower()
        if host in {"ccgp.gov.cn", "www.ccgp.gov.cn", "search.ccgp.gov.cn"} | PLAP_HOSTS:
            scheme = "https"
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if (
                key.lower().startswith("utm_")
                or key.lower() in TRACKING_KEYS
                or (host in PLAP_HOSTS and key.lower() in {"noticetype", "channel"})
            ):
                continue
            query.append((key, value))
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((scheme, host, path, urlencode(query), ""))
    except ValueError:
        return str(raw or "").strip().rstrip("/")


def candidate_id(url):
    return "C" + hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:12].upper()


def ccgp_article_id(url):
    match = CCGP_ARTICLE_RE.search(urlsplit(canonical_url(url)).path)
    return match.group(1) if match else ""


def plap_notice_id(url):
    match = PLAP_NOTICE_RE.search(urlsplit(canonical_url(url)).path)
    return match.group(1).lower() if match else ""


def target_category_signals(text):
    return [name for name, pattern in TARGET_CATEGORY_PATTERNS if pattern.search(text or "")]


def notice_family(value):
    text = str(value or "")
    for family, words in (
        ("更正", ("更正", "变更")),
        ("终止", ("废标", "流标", "终止", "撤销")),
        ("合同", ("合同公告", "采购合同")),
        ("结果", ("中标", "成交", "结果公告")),
        ("意向", ("采购意向", "需求调查", "市场调研")),
        ("单一来源", ("单一来源",)),
        ("公开招标", ("公开招标",)),
        ("邀请招标", ("邀请招标",)),
        ("竞争性磋商", ("竞争性磋商", "磋商公告")),
        ("竞争性谈判", ("竞争性谈判", "谈判公告")),
        ("询价", ("询价公告",)),
        ("比选", ("比选公告",)),
        ("竞价", ("竞价公告",)),
        ("遴选", ("遴选公告",)),
        ("资格预审", ("资格预审",)),
        ("采购", ("招标", "采购")),
    ):
        if any(word in text for word in words):
            return family
    return ""


def extract_project_id(text):
    match = PROJECT_ID_RE.search(str(text or ""))
    return compact_text(match.group(1)) if match else ""


def identity_keys(candidate, content=None):
    """保守身份键：同 URL、同 CCGP 公告号、同标题，或同项目号+同公告阶段。"""
    content = content or {}
    url = canonical_url(candidate.get("url") or content.get("source_url"))
    keys = {("url", url)} if url else set()
    article_id = ccgp_article_id(url)
    if article_id:
        keys.add(("ccgp", article_id))
    notice_id = plap_notice_id(url)
    if notice_id:
        keys.add(("plap", notice_id))
    fingerprint = candidate.get("title_fingerprint") or title_fingerprint(candidate.get("title"))
    if len(fingerprint) >= 8:
        keys.add(("title", fingerprint))
    source_fields = content.get("source_fields") or candidate.get("source_fields") or {}
    project_id = source_fields.get("项目编号") or extract_project_id(
        "\n".join((content.get("summary", ""), content.get("content", "")))
    )
    family = notice_family(
        source_fields.get("公告类型") or candidate.get("title") or content.get("title")
    )
    if project_id and family:
        keys.add(("project_notice", project_id.lower(), family))
    return keys


def write_candidates(candidates, out_dir, run_date):
    """按统一契约写轻量索引和逐条完整正文。"""
    out_dir = Path(out_dir)
    content_dir = out_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for item in candidates:
        url = canonical_url(item.get("url"))
        cid = candidate_id(url)
        content_rel = f"content/{cid}.json"
        full = {
            "candidate_id": cid,
            "title": item.get("title") or "",
            "source_url": url,
            "summary": item.get("summary") or "",
            "content": item.get("content") or "",
            "source_fields": item.get("source_fields") or {},
            "field_evidence": item.get("field_evidence") or {},
            "attachments": item.get("attachments") or [],
            "sources": item.get("sources") or [item.get("source") or "unknown"],
            "alternate_sources": item.get("alternate_sources") or [],
            "found_by_source_query": item.get("found_by_source_query") or [],
        }
        (out_dir / content_rel).write_text(
            json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        teaser_source = full["summary"] or full["content"]
        index.append({
            "candidate_id": cid,
            "title": full["title"],
            "title_fingerprint": title_fingerprint(full["title"]),
            "site_name": item.get("site_name") or "",
            "url": url,
            "publish_time": item.get("publish_time") or "",
            "auth_info_level": item.get("auth_info_level"),
            "auth_info_des": item.get("auth_info_des") or "",
            "rank_score": item.get("rank_score"),
            "found_by_query": sorted(set(item.get("found_by_query") or [])),
            "found_by_source_query": full["found_by_source_query"],
            "source": item.get("source") or "unknown",
            "sources": full["sources"],
            "source_fields": full["source_fields"],
            "field_evidence": full["field_evidence"],
            "attachments": full["attachments"],
            "date_authoritative": bool(item.get("date_authoritative")),
            "retrieval_verified": bool(item.get("retrieval_verified")),
            "content_access": item.get("content_access") or "unknown",
            "source_priority": int(
                item.get("source_priority")
                if item.get("source_priority") is not None
                else SOURCE_PRIORITIES.get(item.get("source"), 0)
            ),
            "alternate_sources": full["alternate_sources"],
            "teaser": compact_text(teaser_source, 240),
            "content_path": content_rel,
        })

    with (out_dir / "candidate_index.jsonl").open("w", encoding="utf-8") as handle:
        for item in index:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    (out_dir / "candidates.json").write_text(
        json.dumps(
            {"run_date": run_date, "count": len(index), "candidates": index},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return index


def load_source_candidates(source_dir):
    source_dir = Path(source_dir)
    rows = []
    index_path = source_dir / "candidate_index.jsonl"
    if not index_path.exists():
        return rows
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        candidate = json.loads(line)
        content = json.loads((source_dir / candidate["content_path"]).read_text(encoding="utf-8"))
        rows.append((candidate, content))
    return rows


class _UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left, right):
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def _preference(candidate, content):
    priority = int(
        candidate.get("source_priority")
        if candidate.get("source_priority") is not None
        else SOURCE_PRIORITIES.get(candidate.get("source"), 0)
    )
    verified = 1 if candidate.get("retrieval_verified") else 0
    access_rank = {"public_full": 2, "public_partial": 1}.get(candidate.get("content_access"), 0)
    return priority, verified, access_rank, len(content.get("content") or "")


def merge_source_dirs(source_dirs):
    """合并任意适配器目录；按来源权威级别和正文可用性选主来源。"""
    rows = []
    for source_dir in source_dirs:
        rows.extend(load_source_candidates(source_dir))
    uf = _UnionFind(len(rows))
    owner = {}
    for index, (candidate, content) in enumerate(rows):
        for key in identity_keys(candidate, content):
            if key in owner:
                uf.union(index, owner[key])
            else:
                owner[key] = index

    groups = {}
    for index, row in enumerate(rows):
        groups.setdefault(uf.find(index), []).append(row)

    merged = []
    for members in groups.values():
        members.sort(key=lambda row: _preference(*row), reverse=True)
        primary_candidate, primary_content = members[0]
        source_names = []
        source_queries = []
        found_by_query = set()
        alternates = []
        for candidate, content in members:
            for source in candidate.get("sources") or [candidate.get("source") or "unknown"]:
                if source not in source_names:
                    source_names.append(source)
            for query in candidate.get("found_by_source_query") or content.get("found_by_source_query") or []:
                if query not in source_queries:
                    source_queries.append(query)
            found_by_query.update(candidate.get("found_by_query") or [])
            if candidate is not primary_candidate:
                alternates.append({
                    "candidate_id": candidate.get("candidate_id"),
                    "source": candidate.get("source") or "unknown",
                    "site_name": candidate.get("site_name") or "",
                    "url": canonical_url(candidate.get("url")),
                })

        merged.append({
            "title": primary_candidate.get("title") or primary_content.get("title") or "",
            "site_name": primary_candidate.get("site_name") or "",
            "url": canonical_url(primary_candidate.get("url")),
            "publish_time": primary_candidate.get("publish_time") or "",
            "auth_info_level": primary_candidate.get("auth_info_level"),
            "auth_info_des": primary_candidate.get("auth_info_des") or "",
            "rank_score": primary_candidate.get("rank_score"),
            "summary": primary_content.get("summary") or "",
            "content": primary_content.get("content") or "",
            "source_fields": primary_content.get("source_fields") or primary_candidate.get("source_fields") or {},
            "field_evidence": primary_content.get("field_evidence") or primary_candidate.get("field_evidence") or {},
            "attachments": primary_content.get("attachments") or primary_candidate.get("attachments") or [],
            "found_by_query": sorted(found_by_query),
            "found_by_source_query": source_queries,
            "source": primary_candidate.get("source") or "unknown",
            "sources": source_names,
            "date_authoritative": any(c.get("date_authoritative") for c, _ in members),
            "retrieval_verified": bool(primary_candidate.get("retrieval_verified")),
            "content_access": primary_candidate.get("content_access") or "unknown",
            "source_priority": int(
                primary_candidate.get("source_priority")
                if primary_candidate.get("source_priority") is not None
                else SOURCE_PRIORITIES.get(primary_candidate.get("source"), 0)
            ),
            "alternate_sources": alternates,
        })
    return merged
