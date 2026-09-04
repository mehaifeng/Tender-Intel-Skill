#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""睿销（jrbx）聚合站检索适配器。

本技能唯一携带用户登录态的来源：凭证只从环境变量读取，绝不落盘、不进候选、不进日志。
回源原始公告 URL 对免费账号是每日配额（实测约10次/天），因此只花在通过目标品类
预筛的候选上；配额耗尽后退回睿销主站正文永久链接，两种链接都拼不出时才丢弃，
任何情况下不伪造链接。细节见 references/jrbx.md。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from search_common import (
    canonical_url,
    compact_text,
    extract_project_id,
    screen_domain,
    write_candidates,
)


ROOT = Path(__file__).resolve().parent.parent
REFERENCE_FILE = ROOT / "references" / "keywords.md"
BASE_URL = "https://www.jrbx360.cn"
CREDENTIALS_FILE = ROOT / "config" / "jrbx.json"

WEB_ORIGIN = "https://www.jrbx.com"
SEARCH_ENDPOINT = "/integrated-search/v1/search"
DETAIL_ENDPOINT = "/integrated-search/v1/verify/noticeDetail"
ORIGIN_URL_ENDPOINT = "/integrated-search/v1/verify/getNoticeOriginalUrl"
# 睿销主站的公告正文永久链接；id 与 year 两个参数缺一不可（前端 811 模块 `pp`）。
ARTICLE_URL_TEMPLATE = WEB_ORIGIN + "/article/detail?id={notice_id}&year={year}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 只保留仍可行动的公告阶段；已有结论的八类在服务端就排除（references/jrbx.md）。
ACTIONABLE_NOTICE_TYPES = ["20100", "20300", "20400", "20600"]
NOTICE_TYPE_NAMES = {
    "20100": "招标", "20200": "结果", "20201": "中标", "20203": "废标",
    "20205": "终止", "20206": "入围", "20300": "预告", "20400": "变更",
    "20500": "合同验收", "20501": "合同", "20502": "验收",
    "20600": "资格预审", "20700": "其他",
}

# 需要中止全流程的登录态故障，不得当成空结果吞掉。
FATAL_CODES = {
    "05": "token 无效，需重新扫码登录",
    "06": "登录已过期，需重新扫码登录",
    "08": "账号在别处登录被顶号，需重新扫码登录",
    "40": "账号未关注公众号，网页端完成关注后重试",
}
SUCCESS_CODE = "00"
NOT_FOUND_CODE = "04"
QUOTA_CODE = "07"
RATE_LIMIT_CODE = "1403"

SHORT_TO_FULL_PROVINCE = {
    "北京": "北京市", "天津": "天津市", "上海": "上海市", "重庆": "重庆市",
    "河北": "河北省", "山西": "山西省", "辽宁": "辽宁省", "吉林": "吉林省",
    "黑龙江": "黑龙江省", "江苏": "江苏省", "浙江": "浙江省", "安徽": "安徽省",
    "福建": "福建省", "江西": "江西省", "山东": "山东省", "河南": "河南省",
    "湖北": "湖北省", "湖南": "湖南省", "广东": "广东省", "广西": "广西壮族自治区",
    "海南": "海南省", "四川": "四川省", "贵州": "贵州省", "云南": "云南省",
    "西藏": "西藏自治区", "陕西": "陕西省", "甘肃": "甘肃省", "青海": "青海省",
    "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区", "内蒙古": "内蒙古自治区",
}
PROCUREMENT_METHODS = (
    "公开招标", "邀请招标", "竞争性磋商", "竞争性谈判", "询价", "单一来源",
    "框架协议", "比选", "比价", "竞价", "遴选", "询比",
)


class JrbxError(Exception):
    """可继续处理其他任务的普通失败。"""


class JrbxAuthError(JrbxError):
    """登录态失效，必须中止并报警。"""


class JrbxRateLimitError(JrbxAuthError):
    """账号池里每个账号都被判了频控，无号可换，必须中止并报警。

    实测撞上 1403 的账号即废，跟「token 过期」不是一回事，因此单列一个退出码（5），
    定时任务才分得清该重新扫码还是该把节流参数调松。
    """


class _TextParser(HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value):
    parser = _TextParser()
    try:
        parser.feed(str(value or ""))
        raw = "".join(parser.parts)
    except Exception:
        raw = re.sub(r"<[^>]+>", " ", str(value or ""))
    lines = [compact_text(unescape(line)) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def mask_user_id(value):
    """userId 是登录态三字段之一，进日志、摘要和报告时一律只留前四位。"""
    value = str(value or "")
    return (value[:4] + "…") if value else ""


def _account_fields(node):
    """从一个账号对象里取三字段；不全时返回 {}——半个凭证发出去只会白撞一次频控。"""
    if not isinstance(node, dict):
        return {}
    got = {k: str(node.get(k) or "").strip() for k in ("userId", "token", "openid")}
    return got if all(got.values()) else {}


def read_credential_pool_file(path=None):
    """config/jrbx.json 的账号池，按文件里的顺序返回；文件不存在时返回 []。

    现行格式是 `{"accounts": [{userId, token, openid}, ...]}`；旧的三字段平铺格式
    仍然读得动，等价于只有一个账号的池。
    """
    path = Path(path or CREDENTIALS_FILE)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JrbxAuthError(f"{path} 不是合法 JSON：{exc}") from exc
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        nodes = data["accounts"]
    elif isinstance(data, list):
        nodes = data
    else:
        nodes = [data]
    pool = []
    for node in nodes:
        account = _account_fields(node)
        if account and account not in pool:
            pool.append(account)
    return pool


def load_credential_pool(env=None, path=None):
    """环境变量优先（只表达单账号），其次 config/jrbx.json 的账号池。

    该文件在 .gitignore 中，与 config/webhook.json 同等对待：**只允许留在本机**，
    不得提交、不得进候选目录、日志或 Webhook 载荷。命令行仍然不接受凭证明文，
    避免进入进程列表和 shell 历史。

    多账号只解决一件事：撞上 1403 之后还能不能把这一趟检索跑完。适配器按池的顺序
    串行使用，同一时刻只有一个账号在发请求，不并发、不做负载分摊。
    """
    env = env if env is not None else os.environ
    names = ("JRBX_USER_ID", "JRBX_TOKEN", "JRBX_OPENID")
    if all(env.get(name) for name in names):
        return [{
            "userId": env["JRBX_USER_ID"].strip(),
            "token": env["JRBX_TOKEN"].strip(),
            "openid": env["JRBX_OPENID"].strip(),
        }]
    from_file = read_credential_pool_file(path)
    if from_file:
        return from_file
    missing = [name for name in names if not env.get(name)]
    raise JrbxAuthError(
        "缺少睿销凭证：环境变量 " + "、".join(missing)
        + f" 未设置，且 {CREDENTIALS_FILE} 不存在或字段不全"
        + "；用 `python scripts/jrbx_search.py --set-token` 写入，取值方法见 references/jrbx.md「凭证」"
    )


def load_credentials(env=None, path=None):
    """账号池里的第一个账号，供只关心单账号的调用方使用。"""
    return load_credential_pool(env, path)[0]


def credentials_from_user_info(raw):
    """从浏览器 IndexedDB 的 USER_INFO#1 整段 JSON 里解出三个字段。

    该对象在不同版本里可能多包一层（`data` / `userInfo`），逐层找齐为止。
    """
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise JrbxAuthError(f"粘贴的内容不是合法 JSON：{exc}") from exc

    def dig(node):
        if isinstance(node, dict):
            if node.get("userId") and (node.get("accessToken") or node.get("token")):
                return node
            for value in node.values():
                found = dig(value)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = dig(value)
                if found:
                    return found
        return None

    node = dig(data)
    if not node:
        raise JrbxAuthError(
            "在粘贴的 JSON 里找不到 userId / accessToken；"
            "请确认拷的是 USER_INFO#1 的完整值（见 references/jrbx.md「凭证」）"
        )
    creds = {
        "userId": str(node.get("userId") or "").strip(),
        "token": str(node.get("accessToken") or node.get("token") or "").strip(),
        "openid": str(node.get("openid") or "").strip(),
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        raise JrbxAuthError("USER_INFO#1 里缺少字段：" + "、".join(missing))
    return creds


def write_credentials_file(credentials, path=None):
    """把一个账号 upsert 进 config/jrbx.json 的账号池，权限收到仅当前用户可读写。

    同 `userId` 就地覆盖（重新扫码换发，不打乱轮换顺序），新 `userId` 追加到池尾。
    返回写入路径。
    """
    path = Path(path or CREDENTIALS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    account = {
        "userId": credentials["userId"],
        "token": credentials["token"],
        "openid": credentials["openid"],
    }
    pool = read_credential_pool_file(path)
    for position, existing in enumerate(pool):
        if existing["userId"] == account["userId"]:
            pool[position] = account
            break
    else:
        pool.append(account)
    payload = {
        "_说明": "睿销登录态账号池，按 accounts 顺序串行使用。该文件已在 .gitignore 中，"
                 "禁止提交或外发；环境变量 JRBX_USER_ID / JRBX_TOKEN / JRBX_OPENID 优先级更高"
                 "（设了就只用那一个账号）。token 20 天固定窗口，过期只能微信重新扫码，"
                 "用 --set-token 逐个写入；撞上 1403 的账号即废，用 --check-token 查出来后手动删掉。",
        "accounts": pool,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def token_expires_at(token):
    """解析 JWT 的 exp，仅用于到期预警；解析失败返回 None，不影响主流程。"""
    try:
        payload = str(token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return datetime.fromtimestamp(int(claims["exp"]))
    except Exception:
        return None


def check_token(credentials, warn_days=3, probe=True, client_factory=None):
    """预检凭证健康度，不做检索也不消耗回源配额。

    只解析 JWT 的 exp 无法发现「被别处重新扫码顶掉」的 token（返回码 08），
    因此默认再打一次最小检索（pageSize=1）验证服务端是否仍然接受该凭证。
    """
    now = datetime.now()
    expires_at = token_expires_at(credentials["token"])
    report = {
        "user_id": mask_user_id(credentials["userId"]),
        "token_expires_at": expires_at.isoformat(timespec="seconds") if expires_at else None,
        "days_remaining": None,
        "warn_days": warn_days,
        "expired": False,
        "expiring_soon": False,
        "server_accepted": None,
        "status": "ok",
        "message": "",
    }
    if expires_at:
        remaining = expires_at - now
        report["days_remaining"] = round(remaining.total_seconds() / 86400, 2)
        report["expired"] = remaining.total_seconds() <= 0
        report["expiring_soon"] = 0 < remaining.total_seconds() <= warn_days * 86400
    if report["expired"]:
        report["status"] = "expired"
        report["message"] = f"token 已于 {expires_at:%Y-%m-%d %H:%M} 过期，需重新扫码登录"
        return report

    if probe:
        window_end = now
        window_start = now - timedelta(hours=1)
        client = (client_factory or JrbxClient)(credentials, delay=0.0)
        try:
            client.search(["试剂"], window_start, window_end, 1, 1, ACTIONABLE_NOTICE_TYPES)
            report["server_accepted"] = True
        except JrbxRateLimitError:
            # 撞过 1403 的账号实测即废，但 token 本身没到期，JWT 解析看不出来。
            report["server_accepted"] = False
            report["status"] = "rate_limited"
            report["message"] = "该账号被判频控（1403），实测触发即废，需重新扫码换发"
            return report
        except JrbxAuthError as exc:
            report["server_accepted"] = False
            report["status"] = "rejected"
            report["message"] = str(exc)
            return report
        except JrbxError as exc:
            # 网络或接口异常不等于凭证失效，单独标注，不误报为需要重新扫码。
            report["status"] = "probe_failed"
            report["message"] = f"凭证有效期正常，但探测请求失败：{exc}"
            return report

    if report["expiring_soon"]:
        report["status"] = "expiring_soon"
        report["message"] = (
            f"token 将于 {expires_at:%Y-%m-%d %H:%M} 过期"
            f"（剩余 {report['days_remaining']} 天），请重新扫码换发"
        )
    elif expires_at:
        report["message"] = f"token 正常，{report['days_remaining']} 天后到期"
    else:
        report["status"] = "unknown_expiry"
        report["message"] = "无法解析 token 有效期，但服务端接受该凭证"
    return report


# --check-token 的退出码：0 正常 / 3 需重新扫码 / 4 即将到期 / 2 探测失败
CHECK_TOKEN_EXIT_CODES = {
    "ok": 0,
    "unknown_expiry": 0,
    "expiring_soon": 4,
    "expired": 3,
    "rejected": 3,
    "rate_limited": 3,
    "probe_failed": 2,
}

# 从「最可用」到「最不可用」；池的状态取最可用的那个账号，因为只要还有一个能跑，
# 检索就不该停——但不可用的账号仍要在报告里点名，否则池会悄悄耗光。
STATUS_SEVERITY = [
    "ok", "unknown_expiry", "expiring_soon", "probe_failed",
    "rate_limited", "rejected", "expired",
]


def check_credential_pool(pool, warn_days=3, probe=True, client_factory=None):
    """逐个预检账号池，退出码按「这个池今天还能不能跑完一趟检索」取。"""
    accounts = [
        check_token(account, warn_days=warn_days, probe=probe, client_factory=client_factory)
        for account in pool
    ]
    best = min(accounts, key=lambda report: STATUS_SEVERITY.index(report["status"]))
    unusable = [
        report for report in accounts
        if report["status"] in ("expired", "rejected", "rate_limited")
    ]
    message = best["message"]
    if unusable:
        message += "；需重新扫码换发的账号：" + "、".join(
            f"{report['user_id']}（{report['status']}）" for report in unusable
        )
    return {
        "account_count": len(accounts),
        "usable_account_count": len(accounts) - len(unusable),
        "status": best["status"],
        "message": message,
        "accounts": accounts,
    }


def probe_keyword_syntax(credentials, client_factory=None, terms=("过敏原", "自身抗体")):
    """实测 `keywords` 支持哪种拼接语义，用来决定清单能不能合并成更少的 query。

    已知 `keywords` 数组是 AND（交集）。未知的是「能不能在一个字符串里表达 OR」——
    如果可以，一行一条的项目代号清单就能按谱系合并，检索次数直接降一个量级。

    做法是打 7 次 `pageSize=1` 的最小检索（不消耗回源配额）：两个单词各一次拿基线，
    数组双词一次验 AND，四种候选 OR 写法各一次。判据是命中总数——OR 成立时结果应当
    **大于两个基线中的较大者**（并集），AND 成立时应当小于较小者。
    """
    first, second = terms
    factory = client_factory or (lambda: JrbxClient(credentials, delay=1.2))
    client = factory()
    end = datetime.now()
    start = end - timedelta(days=30)

    variants = [
        ("单词A", [first]),
        ("单词B", [second]),
        ("数组双词(已知AND)", [first, second]),
        ("单串空格", [f"{first} {second}"]),
        ("单串竖线", [f"{first}|{second}"]),
        ("单串OR", [f"{first} OR {second}"]),
        ("单串加号", [f"{first}+{second}"]),
    ]
    results = []
    for name, keywords in variants:
        row = {"variant": name, "keywords": keywords, "total": None, "error": ""}
        try:
            content = client.search(keywords, start, end, 1, 1, [])
            # 列表响应的计数字段是 totalCount（totalPage 会随 pageSize 变），别用 total。
            row["total"] = content.get("totalCount")
        except JrbxError as exc:
            row["error"] = str(exc)
        results.append(row)

    totals = {row["variant"]: row["total"] for row in results if isinstance(row["total"], int)}
    base = max((totals.get("单词A") or 0), (totals.get("单词B") or 0))
    union_like = sorted(
        name for name, total in totals.items()
        if name.startswith("单串") and total > base
    )
    return {
        "window": f"{start:%Y-%m-%d}..{end:%Y-%m-%d}",
        "results": results,
        "or_supported_by": union_like,
        "verdict": (
            f"可用 OR 写法：{'、'.join(union_like)}——清单可按谱系合并，检索次数大幅下降"
            if union_like else
            "没有一种单串写法返回并集，判定不支持 OR；清单保持一行一条"
        ),
    }


def parse_queries():
    text = REFERENCE_FILE.read_text(encoding="utf-8")
    marker = "## 检索 Query 清单"
    if marker not in text:
        raise JrbxError(f"{REFERENCE_FILE} 缺少“{marker}”")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    queries = [query.strip() for _, query in re.findall(r"^(\d+)\.\s+(\S.*?)\s*$", section, re.M)]
    if not queries:
        raise JrbxError("睿销默认 Query 清单为空")
    return queries


def split_terms(query):
    """`过敏原+试剂` -> ['过敏原', '试剂']，对应睿销 keywords 的 AND 语义。"""
    return [term.strip() for term in str(query or "").split("+") if term.strip()]


def parse_time_range(value, now=None):
    value = str(value or "72h").strip()
    now = now or datetime.now().replace(microsecond=0)
    match = re.fullmatch(r"(\d+)h", value)
    if match:
        if int(match.group(1)) < 1:
            raise JrbxError("--time-range 小时数必须大于0")
        return now - timedelta(hours=int(match.group(1))), now
    match = re.fullmatch(r"(\d+)d", value)
    if match:
        if int(match.group(1)) < 1:
            raise JrbxError("--time-range 天数必须大于0")
        return now - timedelta(days=int(match.group(1))), now
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", value)
    if match:
        start = datetime.fromisoformat(match.group(1))
        end = datetime.fromisoformat(match.group(2)) + timedelta(days=1) - timedelta(seconds=1)
        if start > end:
            raise JrbxError(f"时间范围起点晚于终点：{value}")
        return start, end
    raise JrbxError("--time-range 支持 72h、3d 或 YYYY-MM-DD..YYYY-MM-DD")


def to_millis(moment):
    return int(moment.timestamp() * 1000)


def from_millis(value):
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value / 1000) if value > 0 else None


class JrbxClient:
    """按账号池顺序串行发请求，同一时刻只有一个账号在用。

    1403「操作过于频繁」实测是一次性的：撞上之后该账号即废，退避重试无济于事。
    因此这里不重试，而是把当前账号退池、换下一个**原地重发同一请求**——请求序列
    既不丢也不重跑，等价于在被打断的那一步续上。账号全部退池才抛
    JrbxRateLimitError 中止。
    """

    def __init__(self, credentials, delay=1.2, timeout=30, max_bytes=20 * 1024 * 1024,
                 jitter=1.8, pause_every=25, pause_seconds=20.0):
        pool = [credentials] if isinstance(credentials, dict) else list(credentials)
        if not pool:
            raise JrbxAuthError("睿销账号池为空")
        self.pool = pool
        self.account_index = 0
        self.retired = []
        self.delay = max(0.0, float(delay))
        self.jitter = max(0.0, float(jitter))
        self.pause_every = max(0, int(pause_every))
        self.pause_seconds = max(0.0, float(pause_seconds))
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.last_request_at = 0.0
        self.request_count = 0

    @property
    def credentials(self):
        """当前在用的账号。"""
        return self.pool[self.account_index]

    def _next_gap(self):
        """请求间隔：固定下限 + 随机抖动，每 pause_every 次插一次长停顿。

        固定间隔是很明确的机器指纹，而 1403 一撞账号就废，宁可跑慢也别撞。
        delay 为 0 表示调用方要的是不节流的单次探测（--check-token），一并关掉。
        """
        if self.delay <= 0:
            return 0.0
        if self.pause_every and self.request_count and self.request_count % self.pause_every == 0:
            return self.pause_seconds * random.uniform(0.75, 1.5)
        return self.delay + random.uniform(0.0, self.jitter)

    def _retire(self, reason, detail):
        """当前账号退池，返回是否还有下一个可用账号。

        只记 userId 前四位：三字段都是登录态，不得进日志、摘要或候选目录。
        """
        self.retired.append({
            "user_id": mask_user_id(self.credentials["userId"]),
            "reason": reason,
            "detail": detail,
            "after_requests": self.request_count,
        })
        self.account_index += 1
        if self.account_index >= len(self.pool):
            return False
        if reason == "rate_limited" and self.delay > 0:
            # 换号了，但出口 IP 没变，站点刚说过太快——先把这一拍让出去再续。
            time.sleep(self.pause_seconds * random.uniform(0.75, 1.5))
        return True

    def post(self, path, body):
        wait = self._next_gap() - (time.monotonic() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        payload = dict(body)
        payload.update(self.credentials)
        request = Request(
            BASE_URL + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": WEB_ORIGIN,
                "Referer": WEB_ORIGIN + "/business/search",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise JrbxError(f"响应超过{self.max_bytes // 1024 // 1024}MB：{path}")
                document = json.loads(raw.decode("utf-8", errors="replace"))
        except HTTPError as exc:
            raise JrbxError(f"HTTP {exc.code}: {path}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise JrbxError(f"网络错误：{exc}") from exc
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise JrbxError(f"返回内容不是有效JSON：{exc}") from exc
        finally:
            self.last_request_at = time.monotonic()
            self.request_count += 1

        code = str(document.get("code"))
        if code == RATE_LIMIT_CODE:
            # 实测触发即废，重试只是多送一次违规；换号原地重发，不丢这一步。
            if self._retire("rate_limited", "1403 操作过于频繁"):
                return self.post(path, body)
            raise JrbxRateLimitError(
                f"睿销账号池全部被判频控（code={RATE_LIMIT_CODE}）："
                f"{len(self.retired)} 个账号已退池，需重新扫码换发；"
                "见 references/jrbx.md「多账号轮换」"
            )
        if code in FATAL_CODES:
            if self._retire("auth_failed", f"code={code} {FATAL_CODES[code]}"):
                return self.post(path, body)
            raise JrbxAuthError(f"睿销登录态失效（code={code}）：{FATAL_CODES[code]}")
        return code, document.get("content")

    def search(self, terms, start, end, page, page_size, notice_types):
        body = {
            "keywords": list(terms),
            "timeType": "custom",
            "startTime": to_millis(start),
            "endTime": to_millis(end),
            "pageNum": page,
            "pageSize": page_size,
        }
        if notice_types:
            body["noticeTypes"] = list(notice_types)
        code, content = self.post(SEARCH_ENDPOINT, body)
        if code != SUCCESS_CODE or not isinstance(content, dict):
            raise JrbxError(f"列表接口异常：code={code!r}")
        return content

    def notice_detail(self, notice_id):
        # 只传 noticeId：额外传 year 会返回 04（references/jrbx.md）。
        code, content = self.post(DETAIL_ENDPOINT, {"noticeId": notice_id})
        if code == NOT_FOUND_CODE:
            return None
        if code != SUCCESS_CODE or not isinstance(content, dict):
            raise JrbxError(f"详情接口异常：code={code!r} noticeId={notice_id}")
        return content

    def original_url(self, notice_id):
        """返回 (url, quota_exhausted)；配额耗尽时由调用方停止后续回源。"""
        code, content = self.post(ORIGIN_URL_ENDPOINT, {"noticeId": notice_id})
        if code == QUOTA_CODE:
            return "", True
        if code != SUCCESS_CODE or not isinstance(content, str) or not content.strip():
            return "", False
        return content.strip(), False


def matched_terms(text, terms):
    lowered = (text or "").lower()
    return [term for term in terms if term and term.lower() in lowered]


# 注意不能把「州」整体当后缀：温州、亳州、杭州都是地级市，名字自带「州」，
# 只有「自治州」才是真正的行政区后缀。
DIVISION_SUFFIXES = ("市", "自治州", "地区", "盟", "区", "县", "旗")


def with_division_suffix(name):
    """睿销的 city/county 有时省掉行政区后缀（如「南昌」「温州」），补「市」；
    自治州、地区、盟等已带后缀的原样保留，避免造出「黔西南布依族苗族自治州市」。"""
    name = compact_text(name)
    if not name or name.endswith(DIVISION_SUFFIXES):
        return name
    return name + "市"


def region_fields(item):
    province = compact_text(item.get("province"))
    city = with_division_suffix(item.get("city"))
    county = compact_text(item.get("county"))
    if province in {"", "全国"}:
        return "", ""
    full = SHORT_TO_FULL_PROVINCE.get(province, province)
    return full + city + county, province


def normalize_budget(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return str(int(number)) if float(number).is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def pick_deadline(item):
    """投标/响应截止，不取报名与文件获取截止（schema.md 明确区分）。"""
    for key, label in (
        ("bidDeadline", "投标截止时间"),
        ("deliverBidDocDeadline", "响应文件提交截止时间"),
        ("quoteDeadline", "报价截止时间"),
    ):
        moment = from_millis(item.get(key))
        if moment:
            return moment.strftime("%Y-%m-%dT%H:%M"), f"睿销结构化字段 {key}（{label}）"
    return "", ""


def extract_procurement_method(title, bid_type, text):
    bid_type = compact_text(bid_type)
    if bid_type in PROCUREMENT_METHODS:
        return bid_type, f"睿销结构化字段 bidType：{bid_type}"
    for method in PROCUREMENT_METHODS:
        if method in (title or ""):
            return method, f"标题出现：{method}"
    match = re.search(
        r"(?:采购方式|招标方式)\s*[：:]\s*(" + "|".join(PROCUREMENT_METHODS) + r")", text or "", re.I
    )
    return (match.group(1), compact_text(match.group(0))) if match else ("", "")


def extract_labeled_field(text, labels, limit=120):
    pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{pattern})\s*[：:]\s*([^\n；;]{{2,{limit}}})", text or "", re.I)
    return (compact_text(match.group(1)), compact_text(match.group(0))) if match else ("", "")


def normalize_attachments(value):
    rows = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        url = compact_text(entry.get("originUrl") or entry.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            continue
        row = {"url": canonical_url(url)}
        name = compact_text(entry.get("name"))
        if name:
            row["name"] = name
        if row not in rows:
            rows.append(row)
    return rows


def passes_prefilter(item, detail=None):
    """目标品类预筛，返回 search_common.screen_domain 的结果 dict。

    标题域只放标题：`product` 是把公告里全部标的拉平成的一串，属于清单，和正文
    同级——睿销会把「全自动体外过敏原筛查系统及其配套试剂」和「梯度pcr」并列写进
    这一个字段，按标题域处理就会连坐（screen_domain 的注释有实测数）。
    """
    detail = detail or {}
    body_text = "\n".join((
        compact_text(item.get("product")),
        compact_text(item.get("titleProduct")),
        html_to_text(detail.get("simpleContent")),
        html_to_text(detail.get("content")),
    ))
    return screen_domain(compact_text(item.get("title")), body_text)


def article_url(item, detail=None):
    """睿销主站正文永久链接，回源配额耗尽时作为 source_url 兜底。"""
    detail = detail or {}
    notice_id = compact_text(item.get("id") or detail.get("id"))
    year = compact_text(item.get("year") or detail.get("year"))
    if not notice_id or not year:
        return ""
    return ARTICLE_URL_TEMPLATE.format(notice_id=notice_id, year=year)


def build_candidate(item, detail, origin_url, hits, all_terms):
    """把睿销列表项 + 详情正文映射为统一候选契约。

    `source_url` 优先用回源到原始站点的链接：它匿名可访问，且能与 CCGP/PLAP 在
    规范 URL 层直接命中同一身份键。回源配额耗尽时退回睿销主站正文永久链接——
    该链接需登录睿销才能打开，因此这类候选的 `source_priority` 再降一档。
    """
    detail = detail or {}
    title = compact_text(item.get("title") or detail.get("title"))
    content = html_to_text(detail.get("content"))
    summary = html_to_text(detail.get("simpleContent")) or compact_text(content, 2000)
    search_text = "\n".join((title, compact_text(item.get("product")), summary, content))

    source_hits = list(hits)
    for term in matched_terms(search_text, all_terms):
        hit = {"source": "jrbx", "query": term, "query_mode": "local_content_filter"}
        if hit not in source_hits:
            source_hits.append(hit)

    fields = {}
    evidence = {}
    published = from_millis(item.get("publishTime"))
    publish_time = published.strftime("%Y-%m-%d") if published else ""
    if publish_time:
        fields["发布时间"] = publish_time
        evidence["发布时间"] = f"睿销结构化字段 publishTime：{published.isoformat(timespec='minutes')}"

    organization = compact_text(item.get("organization") or detail.get("organization"))
    if organization:
        fields["单位"] = organization
        evidence["单位"] = f"睿销结构化字段 organization：{organization}"
    else:
        unit, unit_evidence = extract_labeled_field(
            search_text, ("采购人", "采购单位", "招标人", "采购人名称")
        )
        if unit:
            fields["单位"] = unit
            evidence["单位"] = unit_evidence

    region, province = region_fields(item)
    if region:
        fields["地区"] = region
        evidence["地区"] = f"睿销结构化字段 province/city/county：{region}"
    if province:
        fields["所属省/市"] = province
        evidence["所属省/市"] = f"睿销结构化字段 province：{province}"

    budget = normalize_budget(item.get("budget") or detail.get("budget"))
    if budget:
        fields["预算"] = budget
        evidence["预算"] = f"睿销结构化字段 budget：{budget}"

    deadline, deadline_evidence = pick_deadline({**item, **detail})
    if deadline:
        fields["截止时间"] = deadline
        evidence["截止时间"] = deadline_evidence

    method, method_evidence = extract_procurement_method(
        title, item.get("bidType") or detail.get("bidType"), search_text
    )
    if method:
        fields["采购方式"] = method
        evidence["采购方式"] = method_evidence

    notice_type = compact_text(item.get("noticeType")) or NOTICE_TYPE_NAMES.get(
        compact_text(item.get("noticeTypeCode")), ""
    )
    if notice_type:
        fields["公告类型"] = notice_type
        evidence["公告类型"] = f"睿销公告类型码 {item.get('noticeTypeCode')}：{notice_type}"

    project_id = extract_project_id(search_text)
    if project_id:
        fields["项目编号"] = project_id
        evidence["项目编号"] = f"正文项目编号：{project_id}"

    if origin_url:
        url = canonical_url(origin_url)
        site_name = urlsplit(url).netloc
        auth_info_des = "商业聚合库转载，链接已回源原始站点"
        # 低于 CCGP/PLAP 官方一手（400），高于无归属的泛搜结果（0）。
        source_priority = 300
        link_kind = "origin"
    else:
        # canonical_url("") 会返回 "/"，直接用会让缺 id/year 的候选带着假链接进队列。
        raw_article_url = article_url(item, detail)
        url = canonical_url(raw_article_url) if raw_article_url else ""
        site_name = "睿销"
        auth_info_des = "商业聚合库正文永久链接，需登录睿销账号打开"
        # 再降一档：同一公告若同时有回源链接版本，合并时优先保留可匿名访问的那条。
        source_priority = 250
        link_kind = "jrbx_article"
    return {
        "title": title,
        "site_name": site_name,
        "url": url,
        "publish_time": publish_time,
        "auth_info_level": 3,
        "auth_info_des": auth_info_des,
        "link_kind": link_kind,
        "rank_score": item.get("score"),
        "summary": summary,
        # 睿销把公告里全部标的拉平成一串。正文写「详见附件」「下载」时，这是唯一
        # 能定品类的字段，必须随候选落盘给统一层和核实阶段看。
        "product_list": compact_text(item.get("product")),
        "content": content,
        "source_fields": fields,
        "field_evidence": evidence,
        "attachments": normalize_attachments(detail.get("attachments")),
        "found_by_query": [],
        "found_by_source_query": source_hits,
        "source": "jrbx",
        "sources": ["jrbx"],
        "source_priority": source_priority,
        "date_authoritative": True,
        "retrieval_verified": bool(content),
        "content_access": "public_full" if content else "metadata_only",
    }


def collect_listings(client, queries, start, end, page_size, max_pages_per_query, notice_types):
    """第一阶段：只跑列表接口（不计配额），按公告去重并累计 query 归因。"""
    by_notice = {}
    failures = []
    raw_count = 0
    for query in queries:
        terms = split_terms(query)
        if not terms:
            continue
        page = 1
        total_pages = 1
        while page <= total_pages and page <= max_pages_per_query:
            try:
                content = client.search(terms, start, end, page, page_size, notice_types)
            except JrbxAuthError:
                raise
            except JrbxError as exc:
                failures.append({"query": query, "page": page, "error": str(exc)})
                break
            items = content.get("items") or []
            total_pages = int(content.get("totalPage") or 0)
            raw_count += len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                notice_id = compact_text(item.get("id"))
                if not notice_id:
                    continue
                slot = by_notice.setdefault(notice_id, {"item": item, "hits": []})
                hit = {"source": "jrbx", "query": query, "query_mode": "keywords_and"}
                if hit not in slot["hits"]:
                    slot["hits"].append(hit)
            if not items:
                break
            page += 1
    return by_notice, failures, raw_count


def collect(client, queries, start, end, page_size=100, max_pages_per_query=20,
            notice_types=None, max_origin_lookups=10):
    """两阶段：列表+正文不限量取，回源 URL 只花在预筛幸存者上。"""
    notice_types = notice_types if notice_types is not None else ACTIONABLE_NOTICE_TYPES
    by_notice, failures, raw_count = collect_listings(
        client, queries, start, end, page_size, max_pages_per_query, notice_types
    )

    all_terms = sorted({term for query in queries for term in split_terms(query)})
    # 先用列表元数据粗筛，再取正文复筛：正文不计配额，但省下的请求同样降低被限频的概率。
    # 预筛丢弃必须留痕：2026-09-04 之前这里静默丢弃，两天窗口吞掉 11 条有目标品类
    # 信号的公告，只能靠人工比对才发现。丢弃明细写进 search_summary.json。
    prefilter_dropped = []
    shortlisted = []
    for notice_id, slot in by_notice.items():
        screen = passes_prefilter(slot["item"])
        if screen["keep"]:
            shortlisted.append((notice_id, slot))
        else:
            prefilter_dropped.append({
                "notice_id": notice_id,
                "stage": "listing",
                "title": compact_text(slot["item"].get("title"))[:120],
                "reason": screen["reason"],
            })
    shortlisted.sort(key=lambda row: int(row[1]["item"].get("publishTime") or 0), reverse=True)
    prefilter_excluded = len(prefilter_dropped)

    detailed = []
    for notice_id, slot in shortlisted:
        try:
            detail = client.notice_detail(notice_id)
        except JrbxAuthError:
            raise
        except JrbxError as exc:
            failures.append({"notice_id": notice_id, "stage": "detail", "error": str(exc)})
            continue
        if detail is None:
            prefilter_excluded += 1
            prefilter_dropped.append({
                "notice_id": notice_id, "stage": "detail",
                "title": compact_text(slot["item"].get("title"))[:120],
                "reason": "详情接口未返回正文",
            })
            continue
        screen = passes_prefilter(slot["item"], detail)
        if not screen["keep"]:
            prefilter_excluded += 1
            prefilter_dropped.append({
                "notice_id": notice_id, "stage": "detail",
                "title": compact_text(slot["item"].get("title"))[:120],
                "reason": screen["reason"],
            })
            continue
        detailed.append((notice_id, slot, detail))

    candidates = []
    dropped_no_url = 0
    fallback_article_url = 0
    quota_exhausted = False
    origin_lookups = 0
    for notice_id, slot, detail in detailed:
        origin_url = ""
        # 配额耗尽或预算用完后不再空烧请求，直接走主站正文永久链接兜底。
        if not quota_exhausted and origin_lookups < max_origin_lookups:
            try:
                origin_url, quota_exhausted = client.original_url(notice_id)
                origin_lookups += 1
            except JrbxAuthError:
                raise
            except JrbxError as exc:
                failures.append({"notice_id": notice_id, "stage": "origin_url", "error": str(exc)})
        candidate = build_candidate(slot["item"], detail, origin_url, slot["hits"], all_terms)
        if not candidate or not candidate["url"]:
            # 连 id/year 都缺，拼不出永久链接：没有任何可用 source_url 才丢弃。
            dropped_no_url += 1
            continue
        if candidate["link_kind"] == "jrbx_article":
            fallback_article_url += 1
        candidates.append(candidate)

    stats = {
        "raw_result_count": raw_count,
        "unique_notice_count": len(by_notice),
        "prefilter_excluded": prefilter_excluded,
        "prefilter_dropped": prefilter_dropped,
        "detail_fetched": len(detailed),
        "origin_lookups": origin_lookups,
        "origin_quota_exhausted": quota_exhausted,
        "origin_url_count": len(candidates) - fallback_article_url,
        "fallback_article_url_count": fallback_article_url,
        "dropped_no_url": dropped_no_url,
    }
    return candidates, failures, stats


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="睿销（jrbx）聚合站检索适配器")
    parser.add_argument("--query", help="单个即席 Query；`A+B` 表示 AND")
    parser.add_argument("--queries", help="逗号分隔的 Query；默认读取 references/jrbx.md")
    parser.add_argument("--time-range", default="72h", help="72h / 3d / YYYY-MM-DD..YYYY-MM-DD")
    parser.add_argument("--out-dir", help="输出目录；默认 .tmp/search/<日期>/.sources/jrbx")
    parser.add_argument(
        "--delay", type=float, default=1.2,
        help="请求间隔下限秒数，默认1.2；实际间隔再叠加 --delay-jitter 的随机抖动",
    )
    parser.add_argument(
        "--delay-jitter", type=float, default=1.8,
        help="叠加在 --delay 之上的随机抖动上限秒数，默认1.8（实际间隔 1.2~3.0s）；0 表示等距发包",
    )
    parser.add_argument(
        "--pause-every", type=int, default=25,
        help="每发多少次请求插一次长停顿，默认25；0 表示关闭",
    )
    parser.add_argument(
        "--pause-seconds", type=float, default=20.0,
        help="长停顿基准秒数，实际按 ±50%% 随机，默认20",
    )
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages-per-query", type=int, default=20)
    parser.add_argument(
        "--max-origin-lookups", type=int, default=10,
        help="回源URL调用上限；免费账号实测约10次/天，超出一律返回07",
    )
    parser.add_argument(
        "--notice-types", default=",".join(ACTIONABLE_NOTICE_TYPES),
        help="公告类型码，逗号分隔；留空表示不做服务端类型过滤",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--check-token", action="store_true",
        help="只预检凭证健康度并退出：0 正常 / 4 即将到期 / 3 需重新扫码 / 2 探测失败",
    )
    parser.add_argument("--warn-days", type=int, default=3, help="--check-token 的到期告警阈值")
    parser.add_argument(
        "--set-token", action="store_true",
        help="从 stdin 读取浏览器 USER_INFO#1 的整段 JSON，解出三字段写入 config/jrbx.json",
    )
    parser.add_argument(
        "--probe-keyword-syntax", action="store_true",
        help="实测 keywords 支持哪种拼接语义（7 次最小检索），用于判断清单能否合并",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="--check-token 时只解析 JWT 有效期，不发探测请求（查不出被顶号）",
    )
    args = parser.parse_args()

    try:
        if args.set_token:
            if sys.stdin.isatty():
                print(
                    "请粘贴浏览器 USER_INFO#1 的整段 JSON，粘完按 Ctrl+Z 回车（Windows）"
                    "或 Ctrl+D（macOS/Linux）：",
                    file=sys.stderr,
                )
            credentials = credentials_from_user_info(sys.stdin.read())
            path = write_credentials_file(credentials)
            pool = read_credential_pool_file(path)
            expires = token_expires_at(credentials["token"])
            report = {
                "written": str(path),
                "userId": mask_user_id(credentials["userId"]),
                "expires_at": expires.isoformat(timespec="seconds") if expires else None,
                "days_left": (expires - datetime.now()).days if expires else None,
                "account_count": len(pool),
                "accounts": [mask_user_id(account["userId"]) for account in pool],
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print(
                f"已写入 {path}，账号池现有 {len(pool)} 个账号"
                f"（该文件在 .gitignore 中，禁止提交或外发）",
                file=sys.stderr,
            )
            return 0

        if args.probe_keyword_syntax:
            report = probe_keyword_syntax(load_credential_pool())
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        if args.check_token:
            report = check_credential_pool(
                load_credential_pool(), warn_days=args.warn_days, probe=not args.offline
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            exit_code = CHECK_TOKEN_EXIT_CODES.get(report["status"], 2)
            if exit_code:
                print(f"睿销凭证预检：{report['message']}", file=sys.stderr)
            return exit_code

        start, end = parse_time_range(args.time_range)
        if args.query:
            queries = [args.query.strip()]
        elif args.queries:
            queries = [item.strip() for item in args.queries.split(",") if item.strip()]
        else:
            queries = parse_queries()
        if not queries:
            raise JrbxError("检索 Query 为空")
        if not 1 <= args.page_size <= 100:
            raise JrbxError("--page-size 必须在1到100之间")
        if args.max_pages_per_query < 1:
            raise JrbxError("--max-pages-per-query 必须大于0")
        if args.max_origin_lookups < 0:
            raise JrbxError("--max-origin-lookups 不能为负")
        if args.delay_jitter < 0 or args.pause_every < 0 or args.pause_seconds < 0:
            raise JrbxError("--delay-jitter / --pause-every / --pause-seconds 都不能为负")
        notice_types = [code.strip() for code in args.notice_types.split(",") if code.strip()]

        if args.dry_run:
            # 干跑不读凭证也不发请求，便于在没有 token 的环境校验配置。
            print(json.dumps({
                "source": "jrbx",
                "queries": queries,
                "query_count": len(queries),
                "terms_per_query": {query: split_terms(query) for query in queries},
                "notice_types": [
                    {"code": code, "name": NOTICE_TYPE_NAMES.get(code, "?")} for code in notice_types
                ],
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "max_origin_lookups": args.max_origin_lookups,
                "pacing": {
                    "delay": args.delay,
                    "delay_jitter": args.delay_jitter,
                    "pause_every": args.pause_every,
                    "pause_seconds": args.pause_seconds,
                },
                "authentication": "user_token_from_env",
                "credentials_present": all(
                    os.environ.get(name) for name in ("JRBX_USER_ID", "JRBX_TOKEN", "JRBX_OPENID")
                ),
            }, ensure_ascii=False, indent=2))
            return 0

        # 已过期的账号先在本地筛掉：JWT 的 exp 是离线可判的，没必要拿一次请求去撞。
        pool = []
        expiries = []
        for account in load_credential_pool():
            expires_at = token_expires_at(account["token"])
            if expires_at and expires_at <= datetime.now():
                print(
                    f"警告：睿销账号 {mask_user_id(account['userId'])} 的 token 已于 "
                    f"{expires_at:%Y-%m-%d %H:%M} 过期，本次跳过，请重新扫码换发",
                    file=sys.stderr,
                )
                continue
            pool.append(account)
            if expires_at:
                expiries.append(expires_at)
        if not pool:
            raise JrbxAuthError(
                "睿销账号池里没有未过期的 token，需重新扫码登录；用 --check-token 查明细"
            )
        # 到期预警按池里最早到期的那个报——它决定了下一次必须去扫码的时间。
        expires_at = min(expiries) if expiries else None
        if expires_at:
            remaining = expires_at - datetime.now()
            if remaining.days <= 3:
                print(
                    f"警告：睿销账号池中最早的 token 将于 {expires_at:%Y-%m-%d %H:%M} 过期"
                    f"（剩余 {remaining.days} 天），请及时重新扫码",
                    file=sys.stderr,
                )

        out_dir = (
            Path(args.out_dir) if args.out_dir
            else ROOT / ".tmp" / "search" / date.today().isoformat() / ".sources" / "jrbx"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        client = JrbxClient(
            pool, delay=args.delay, jitter=args.delay_jitter,
            pause_every=args.pause_every, pause_seconds=args.pause_seconds,
        )
        candidates, failures, stats = collect(
            client, queries, start, end,
            page_size=args.page_size,
            max_pages_per_query=args.max_pages_per_query,
            notice_types=notice_types,
            max_origin_lookups=args.max_origin_lookups,
        )
        index = write_candidates(candidates, out_dir, date.today().isoformat())
        summary = {
            "schema_version": 1,
            "source": "jrbx",
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "time_range": f"{start.isoformat(timespec='seconds')}..{end.isoformat(timespec='seconds')}",
            "query_count": len(queries),
            "query_failed": len(failures),
            "notice_types": notice_types,
            "request_count": client.request_count,
            "candidate_count": len(index),
            "failures": failures,
            "account_count": len(client.pool),
            # 只留 userId 前四位：三字段都是登录态，摘要会进候选目录，不能落明文。
            "accounts_retired": client.retired,
        }
        summary.update(stats)
        if expires_at:
            summary["token_expires_at"] = expires_at.isoformat(timespec="seconds")
        (out_dir / "search_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"睿销：{len(queries)} 条 Query，原始 {stats['raw_result_count']} 条，"
            f"去重 {stats['unique_notice_count']} 条，预筛排除 {stats['prefilter_excluded']} 条，"
            f"取正文 {stats['detail_fetched']} 条，回源 {stats['origin_lookups']} 次，"
            f"最终 {len(index)} 条（回源链接 {stats['origin_url_count']} 条，"
            f"主站正文链接 {stats['fallback_article_url_count']} 条），"
            f"无可用链接丢弃 {stats['dropped_no_url']} 条，"
            f"失败 {len(failures)} 项，耗时 {time.time() - started:.1f}s"
        )
        if client.retired:
            # 退池不是“今天没情报”，是账号在缩水；无人值守时必须喊出来。
            print(
                "警告：本次有 " + str(len(client.retired)) + " 个睿销账号退池（"
                + "、".join(f"{row['user_id']} {row['reason']}" for row in client.retired)
                + f"），池中剩余 {len(client.pool) - len(client.retired)} 个；"
                "被判频控（1403）的账号实测即废，请重新扫码换发后用 --set-token 覆盖",
                file=sys.stderr,
            )
        if stats["fallback_article_url_count"]:
            print(
                f"提示：{stats['fallback_article_url_count']} 条候选使用睿销主站正文链接"
                f"（回源配额已耗尽或超出 --max-origin-lookups）；这些链接需登录睿销账号才能打开",
                file=sys.stderr,
            )
        print(f"落盘：{out_dir}")
        return 0 if candidates or not failures else 2
    except JrbxRateLimitError as exc:
        # 与“需重新扫码”分开：定时任务据此判断是该换号还是该把节流参数调松。
        print(f"睿销频控：{exc}", file=sys.stderr)
        return 5
    except JrbxAuthError as exc:
        # 登录态问题必须以独立退出码暴露，不能被当成“今天没有新公告”。
        print(f"睿销登录态错误：{exc}", file=sys.stderr)
        return 3
    except JrbxError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
