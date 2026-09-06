#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索适配器共享的候选契约、落盘与去重。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# `sk` 是知了标讯站内链接上的会话参数，同一条公告每次调用取回的值都不同，
# 不剥掉就会让同一条公告在跨运行去重时变成两个身份。
TRACKING_KEYS = {"spm", "from", "track", "sk"}
ZLBX_CONTENT_RE = re.compile(r"/content/(\d+)(?:/[a-z0-9]+)*/?$", re.I)
ZLBX_HOSTS = {"zhiliaobiaoxun.com", "www.zhiliaobiaoxun.com"}
SOURCE_PRIORITIES = {
    "zlbx": 400,
}
PROJECT_ID_RE = re.compile(
    r"(?:项目编号|采购项目编号|招标编号|项目编码)\s*[：:]\s*"
    r"([A-Za-z0-9][A-Za-z0-9._()（）/\-]{2,80})",
    re.I,
)
def _codes(*codes):
    """把拉丁代号包成「左右都不紧邻其它拉丁字符或数字」的边界。

    不能用 \\b：中文也是 \\w，所以 \\bANCA\\b 在「抗ANCA抗体」里两侧都不成立。
    本函数的前后瞻只挡英文字母和数字，允许代号紧贴中文，同时仍不会命中 ANALYZER 里的 ANA。
    """
    return r"(?<![0-9A-Za-z])(?:" + "|".join(codes) + r")(?![0-9A-Za-z])"


def _any(*fragments):
    return re.compile("|".join(fragments), re.I)


# 目标品类信号 = 业务方《过敏》《自免》两张关键词表，分组即表里的谱系划分。
# 词表与取词规则见 references/keywords.md，那里是唯一来源；本表只是它的正则实现。
#
# 2026-09-03/09-05 实测各库都是「长复合词命中骤降、短片段是超集」，检索词已按最短片段
# 放宽（`红斑狼疮`→`狼疮`、`免疫印迹仪`→`印迹`、`类风湿`→`风湿`…）。**本表必须跟着放宽到
# 同一批片段**，否则宽词捞回来的公告会在预筛就被扔掉，等于白捞。宁可多留、由核实阶段的
# 模型按 references/verification.md 判掉。
TARGET_CATEGORY_PATTERNS = [
    # ---- 过敏表 ----
    ("过敏/IgE", _any(
        r"过敏", r"变态反应", r"变应",
        r"特异性\s*IgE", r"sIgE", r"总\s*IgE", r"tIgE", r"IgE\s*(?:检测|测定|定量|抗体)",
        r"(?:吸入|食入|组分|混合)[^\n]{0,6}(?:过敏原|变应原|筛查)",
    )),
    ("食物不耐受/sIgG", _any(
        r"不耐受", r"食物\s*(?:特异性)?\s*IgG", r"sIgG",
    )),
    ("印迹", _any(r"印迹")),
    # ---- 自免表·核心名词 ----
    ("自身抗体/自身免疫", _any(r"自身抗体", r"自身免疫", r"自免")),
    ("核抗体谱", _any(
        r"核抗体", r"ENA\s*谱", r"双链\s*DNA", r"核小体", r"组蛋白",
        r"核糖核蛋白", r"核糖体\s*P\s*蛋白", r"着丝点", r"拓扑异构酶抗体",
        r"狼疮", r"干燥综合", r"硬皮",
        _codes("ANA", "ENA", "dsDNA", "Nuc", "PCNA", "nRNP", "Ro-?52",
               "SS-?A", "SSA", "SS-?B", "SSB", "CENP-?B", "Scl[-A-Za-z0-9]*", "Jo-?1"),
        r"抗\s*Sm(?![0-9A-Za-z])", r"(?<![0-9A-Za-z])Sm\s*抗体",
        r"抗\s*C1q", r"(?<![0-9A-Za-z])C1q\s*抗体",
    )),
    ("血管炎/ANCA", _any(
        r"血管炎", r"胞浆抗体", r"髓过氧化物酶", r"蛋白酶\s*3",
        r"肾小球基底膜", _codes("ANCA", "MPO", "PR3", "GBM"),
    )),
    ("肌炎谱", _any(
        r"(?<!心)肌炎", r"信号识别粒子", r"苏氨酰\s*tRNA", r"丙氨酰\s*tRNA",
        _codes("Mi-?2", "MDA5", "SRP", "PL-?7", "PL-?12"),
        r"抗\s*Ku(?![0-9A-Za-z])", r"(?<![0-9A-Za-z])Ku\s*抗体",
    )),
    ("自免肝/PBC", _any(
        r"自免肝", r"自身免疫性肝", r"胆汁性",
        r"肝肾微粒体", r"肝细胞溶质抗原", r"线粒体\s*M2",
        _codes("PBC", "AMA-?M2", "gp210", "sp100", "LC-?1", "LKM-?1", "SLA/?LP"),
    )),
    ("抗磷脂谱", _any(
        r"抗磷脂抗体", r"心磷脂", r"β\s*2[^\n]{0,3}(?:糖蛋白|GP1|GPI)",
        r"beta\s*2\s*糖蛋白", r"糖蛋白\s*[ⅠI1]\s*抗体", _codes("aCL(?:-[AGM])*", "GP1"),
    )),
    ("类风湿谱", _any(
        r"类风关", r"环瓜氨酸",
        _codes("RF-[AGM]", "RA33"),
        r"抗\s*CCP(?![0-9A-Za-z])", r"(?<![0-9A-Za-z])CCP\s*抗体",
    )),
    # `风湿` 是 `类风湿` 放宽出来的宽片段，单独成组只为分层（BROAD_SIGNAL_GROUPS）：
    # 它捞回的多是大宗试剂包里以免疫比浊法跑在生化仪上的类风湿因子单项，属别的品类。
    # 仍然保留检索与筛选——`免疫印迹仪` 那两条已应标的标就是宽片段捞回来的。
    ("风湿(宽片段)", _any(r"风湿")),
    ("糖尿病自身抗体", _any(
        r"脱羧酶", r"胰岛细胞", r"酪氨酸磷酸酶抗体", r"胰岛素(?:自身)?抗体",
        r"锌转运蛋白", r"糖尿病[^\n]{0,8}(?:自身)?抗体",
        _codes("ZnT8"),
        r"抗\s*GAD(?![0-9A-Za-z])", r"(?<![0-9A-Za-z])GAD\s*抗体",
        r"抗\s*ICA(?![0-9A-Za-z])", r"(?<![0-9A-Za-z])ICA\s*抗体",
        r"抗\s*IA-?2(?![0-9A-Za-z])", r"(?<![0-9A-Za-z])IA-?2A?\s*抗体",
    )),
    ("膜性肾病/PLA2R", _any(r"膜性肾病", r"磷脂酶\s*A2\s*受体", _codes("PLA2R"))),
    ("大疱性皮肤病", _any(
        r"大疱", r"天疱疮", _codes("Dsg[-0-9]*", "BP180", "BP230"),
    )),
    ("胃肠疾病抗体", _any(
        r"胃肠疾病", r"胃壁", r"内因子", r"麦胶",
        r"抗\s*PCA(?![0-9A-Za-z])", r"(?<![0-9A-Za-z])PCA\s*抗体",
        r"抗\s*AGA(?![0-9A-Za-z])", r"(?<![0-9A-Za-z])AGA\s*抗体",
    )),
    # 「IgG1-4 亚类」是公告里的常见写法，实测 `IgG亚类` 连写反而命中 0。
    ("IgG亚类/IgG4", _any(r"IgG\s*[1-4]?\s*[-–—]?\s*[1-4]?\s*亚类", r"亚类\s*IgG", _codes("IgG4"))),
    ("25羟基维生素D", _any(
        r"羟基\s*维生素", r"25\s*\(\s*OH\s*\)\s*D", _codes("25-?OH-?VD"),
    )),
    ("细胞因子", _any(
        r"细胞因子", r"白介素", r"白细胞介素", r"肿瘤坏死因子",
        r"[αγ]\s*干扰素", r"(?<![0-9A-Za-z])IL-?\d{1,2}",
        _codes("TNF", "TNF-?α", "IFN", "IFN-?[αγ]"),
    )),
]

# 非本司产品域的排除词。词表见 references/keywords.md「排除词」。
# **只在标题域决定去留**，正文域改为打标记——原因见 screen_domain。
EXCLUDE_TERMS = re.compile(
    r"酶标仪|电泳|兽医|兽用|畜牧|生猪|结核|干扰素释放|免疫组化|重组蛋白|培养基|缓冲液|核酸|PCR|测序",
    re.I,
)


def excluded_domain_term(text):
    """命中的硬排除词；未命中返回空串。用于把排除理由写进 skip_reason。"""
    match = EXCLUDE_TERMS.search(text or "")
    return match.group(0) if match else ""


# 标题里的并列分隔符。`和` 不收：中文地名与项目名里到处是它（和田地区、和美乡村），
# 切错的代价是把一条本该丢的公告放进队列，不如不切。
TITLE_SEGMENT_SPLIT_RE = re.compile(r"[、，,；;／/｜|]+|以及|及")


def title_mixed_bundle_term(title_text):
    """标题里的排除词与本司信号分处不同并列项时返回那个排除词，否则空串。

    标题里排除词和本司品类同时出现有两种完全不同的成因，必须分开处置：

    - **限定同一个标的**：`兽用自身抗体检测试剂`、`结核分枝杆菌特异性细胞因子检测
      试剂盒`。排除词是那一个标的的定语，整条公告就不是本司的东西，照旧丢。
    - **并列两个标的**：`南医大二附院关于哥伦比亚血琼脂培养基等（7种培养基）、
      抗β2糖蛋白1IgG等（5种）试剂盒采购项目的遴选公告（第二次）`。这是标题层的
      混合包——`培养基` 和 `抗β2糖蛋白1IgG`（抗磷脂谱）是分开招的两批标的，
      连坐等于把本司那一批一起扔掉。2026-09-05 用 09-02 单日窗口实测到这一条真漏。

    判据是并列分隔符：切开后只要存在一个「有本司信号、又没有排除词」的片段，
    就按混合包处理。片段只有一个时不成立——那就是限定语的情形。
    """
    title_text = title_text or ""
    segments = [seg for seg in TITLE_SEGMENT_SPLIT_RE.split(title_text) if seg.strip()]
    if len(segments) < 2:
        return ""
    for segment in segments:
        if target_category_signals(segment) and not excluded_domain_term(segment):
            return excluded_domain_term(title_text)
    return ""


def screen_domain(title_text, body_text=""):
    """产品域预筛，返回 dict(keep/reason/signals/title_exclude_term/body_exclude_term)。

    `title_text` 是「这条公告是关于什么的」——标题，以及标题派生的产品词。
    `body_text` 是设备与试剂清单——正文、摘要，以及把全部标的拉平成一串的
    标的物清单字段。**清单属于正文域，不是标题域**：知了的 `sm_names` 会把
    「全自动体外过敏原筛查系统及其配套试剂」和「梯度pcr」并列写进同一个字段。

    硬排除只在标题域决定去留。正文域命中排除词不再丢弃候选：一份几十行的科室
    设备清单几乎必然出现 PCR、核酸或培养基，无差别连坐会把混合包整类打掉。
    2026-09-04 用 09-03~09-04 两天窗口实测，这条规则漏掉 6 条
    真候选，其中三条正文写明「全自动自身抗体检测系统」「全自动体外过敏原筛查
    系统及其配套试剂」「化学发光免疫分析仪(自身免疫检测+过敏原专用)」；同期
    实际推送只有 2 条，漏的比发的多。混合包本就该交给核实阶段判
    （references/verification.md「大宗混合包」），预筛不该替它做决定。

    这不会放宽整体口径：正文有排除词、又没有目标品类信号的候选，仍然被
    「无目标品类信号」丢掉——正文域的硬排除本来就与那一条冗余，唯一独立生效的
    场合正是上面这类误杀。正文域命中的词随候选带出，写进
    `search_evidence.body_exclude_term`，让核实阶段知道这是混合包。

    **标题域的硬排除，只在排除词与本司品类不是并列标的时才生效。** 2026-09-05 用
    09-02 单日窗口实测，`南医大二附院关于哥伦比亚血琼脂培养基等（7种培养基）、
    抗β2糖蛋白1IgG等（5种）试剂盒采购项目的遴选公告（第二次）` 被 `培养基` 杀在
    标题域——可同一个标题里就写着 `抗β2糖蛋白1IgG`（抗磷脂谱）。混合包不只发生在
    正文清单里，标题本身就可能是「A类若干、B类若干」的并列写法。

    但不能简单改成「标题有本司信号就不丢」：`兽用自身抗体检测试剂`、`结核分枝杆菌
    特异性细胞因子检测试剂盒` 里的排除词是**同一个标的的限定语**，那两条确实不是
    本司的东西。区分交给 `title_mixed_bundle_term()`：按并列分隔符切开标题，
    存在「有本司信号、无排除词」的片段才算混合包。混合包保留，命中的词写进
    `title_exclude_term` 交给核实阶段；其余照旧丢。

    纯粹的别域标题不受影响——`河南省疾病预防控制中心"华大智造基因测序仪"配套测序
    试剂采购项目` 切开后没有任何片段带本司信号，仍然被 `测序` 丢掉。
    """
    title_text = title_text or ""
    body_text = body_text or ""
    title_term = excluded_domain_term(title_text)
    mixed_bundle_term = title_mixed_bundle_term(title_text) if title_term else ""
    if title_term and not mixed_bundle_term:
        return {
            "keep": False,
            "reason": f"标题命中非本司产品域硬排除词：{title_term}",
            "signals": [],
            "title_exclude_term": "",
            "body_exclude_term": "",
        }
    signals = target_category_signals("\n".join((title_text, body_text)))
    if not signals:
        return {
            "keep": False,
            "reason": "标题、摘要和搜索正文均无目标品类信号",
            "signals": [],
            "title_exclude_term": "",
            "body_exclude_term": "",
        }
    return {
        "keep": True,
        "reason": "",
        "signals": signals,
        "title_exclude_term": mixed_bundle_term,
        "body_exclude_term": excluded_domain_term(body_text),
    }


# 宽片段组：检索词按最短片段放宽（keywords.md）时多出来的那部分召回。
# 2026-09-04 用《招标信息跟踪档案》115 条销售反馈回测，当前清单仍会召回的 45 条样本里：
# 命中核心名词或项目代号的 30 条有效率 60%，只命中宽片段的 14 条有效率仅 21%。
# **不作为丢弃依据**——这 14 条里的「中山大学孙逸仙纪念医院免疫印迹仪」等两条已应标；
# 只用来给候选分层，让核实阶段对弱候选多问一句（references/verification.md）。
BROAD_SIGNAL_GROUPS = frozenset({
    "印迹",            # 会捞到免疫印迹「成像仪」这类科研凝胶设备
    "风湿(宽片段)",     # 会捞到生化室以免疫比浊法跑的类风湿因子单项
    "25羟基维生素D",    # 业务方表里有，一线反馈「公司无相关产品」，待确认
    "细胞因子",        # 同上
})

# 采购主体不是医疗机构时，招的必然不是本司产品域的东西。
# 同一批反馈回测：血站 0/6、疾控 0/3、药检所 0/1、体检中心 0/1 全部判无效，且没有
# 任何一条有效标讯会被这条规则误杀。成因是这些主体的检验场景本就不同——血站做献血
# 筛查（乙肝/丙肝/梅毒/艾滋酶免），疾控做传染病与公卫监测，药检所检药品，体检中心
# 走常规生化免疫套餐，都不是过敏原与自身免疫的临床检验场景。
NON_HOSPITAL_BUYER_RE = re.compile(
    r"血站|血液中心|采供血|中心血库|"
    r"疾病预防控制|疾控中心|"
    r"药品检验|药检所|食品药品检验|"
    r"体检中心"
)
# 医疗机构标记，命中即视为医院采购。「XX医院体检中心」「XX医院输血科」是医院的科室，
# 不是独立的体检中心或血站，不能被上面那条误伤。
HOSPITAL_MARKER_RE = re.compile(
    r"医院|卫生院|卫生服务中心|医疗中心|医学中心|保健院|医共体|医疗集团|疗养院"
)


def non_hospital_buyer(buyer, title=""):
    """非医疗机构采购主体；是医院或判断不出来时返回空串。

    **只看采购人名称与标题，不看正文**——正文里顺带提到「送疾控复核」「血站供血」的
    医院标不该被这条规则误杀。
    """
    text = " ".join(str(value or "") for value in (buyer, title))
    if HOSPITAL_MARKER_RE.search(text):
        return ""
    match = NON_HOSPITAL_BUYER_RE.search(text)
    return match.group(0) if match else ""


def compact_text(value, limit=None):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit and len(text) > limit:
        return text[: limit - 1] + "…"
    return text


# 聚合站给标题加的栏目/来源标签，不属于公告身份：柳铁那条被存成
# 「【调查公告】柳州市柳铁中心医院…」，人工台账里同一条没有这个壳；白鸟湖那条在
# 新疆公共资源网叫「[政采云]…」，在政府采购网就没有前缀。不剥这层壳，同一条
# 公告在两个平台上就是两个身份，去重必漏（2026-09-05 实测）。
# 只认方头/中文方头括号：圆括号在中文标题里常常是内容的一部分（「（第二次）」），
# 剥掉会把不同轮次的公告混成一条。
TITLE_TAG_RE = re.compile(r"^\s*[【\[][^】\]]{1,10}[】\]]\s*")
# 聚合来源会把长标题截断并以省略号收尾，台账里那批历史记录也有。指纹里去掉省略号，
# 截断标题的指纹才会是完整标题指纹的**前缀**，tender_pipeline 的前缀判重才有意义。
TITLE_ELLIPSIS_RE = re.compile(r"(?:\.{3,}|。{3,}|[…⋯]+)\s*$")


def strip_title_tags(title):
    """剥掉标题开头的一层层栏目标签，直到没有为止。"""
    text = str(title or "").strip()
    while True:
        stripped = TITLE_TAG_RE.sub("", text, count=1)
        if stripped == text or not stripped:
            return text
        text = stripped


def title_is_truncated(title):
    """标题被来源截断（以省略号收尾），指纹只能当前缀用。"""
    return bool(TITLE_ELLIPSIS_RE.search(str(title or "")))


def title_fingerprint(title):
    text = TITLE_ELLIPSIS_RE.sub("", strip_title_tags(title))
    return re.sub(r"[\s\W_]+", "", text.lower(), flags=re.UNICODE)


def canonical_url(raw):
    """去跟踪参数并统一 scheme。

    回源到的原始站点五花八门（政府采购网、省级平台、医院自建站、公众号文章），
    这里只做通用规范化，不为某个站点写专用规则。
    """
    try:
        parts = urlsplit(str(raw or "").strip())
        host = parts.netloc.lower()
        scheme = parts.scheme.lower()
        if host in ZLBX_HOSTS:
            scheme = "https"
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower().startswith("utm_") or key.lower() in TRACKING_KEYS:
                continue
            query.append((key, value))
        path = parts.path.rstrip("/") or "/"
        # SPA 公告把详情 ID 放在 #/route?uuid=... 中，不能像普通页内锚点一样删掉。
        fragment = parts.fragment if parts.fragment.startswith(("/", "!")) else ""
        return urlunsplit((scheme, host, path, urlencode(sorted(query)), fragment))
    except ValueError:
        return str(raw or "").strip().rstrip("/")


def candidate_id(url, discriminator=""):
    value = canonical_url(url) + ("\n" + discriminator if discriminator else "")
    return "C" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()


def zlbx_bid_id(url):
    """知了标讯站内链接里的标讯 ID，用作去重身份键。"""
    parts = urlsplit(canonical_url(url))
    if parts.netloc.lower() not in ZLBX_HOSTS:
        return ""
    match = ZLBX_CONTENT_RE.search(parts.path)
    return match.group(1) if match else ""


# 「详情请求成功」不等于「正文足以核验」。知了的详情有时只回标题与几个结构化字段，
# 正文写一句「完整信息请查看原文」；实质内容在附件里——平台自己解析了附件才把这条
# 召回，我们照着链接打开也看不到。此时不能让核实阶段以为手上是完整正文。
# 每类指向词自带长度上限：越弱的信号越只在短正文里才说明问题。实测 638 字的
# 招标代理选取公告里，「登录后查看」挡的是咨询电话而不是正文，不该判成壳。
_BODY_STUB_RULES = (
    (re.compile(r"完整信息请查看|请查看原文|详见原文|查看全文|内容详见原文"), 800,
     "正文只给了原文链接，实质内容不在返回的正文里"),
    (re.compile(r"详情请?见附件|详见附件|内容见附件|见公告附件|请下载附件|附件下载"), 400,
     "正文把内容指向附件，而附件我们取不到"),
    (re.compile(r"登录后查看|注册后查看|会员可见|请登录查看"), 200,
     "正文需登录才能看全"),
)
# 2026-09-04 实测 37 条正文：两条壳分别 73/101 字且都写着「完整信息请查看原文」；
# 145/148 字的两条竞价公告反而是完整的（正文里就是那张商品明细表），所以不能只看长度。
_BODY_SHELL_MAX = 120


def body_completeness(body):
    """返回 (content_access, 原因)。区分没取到、取到但是壳、取到完整正文三种。"""
    body = (body or "").strip()
    if not body:
        return "metadata_only", "详情未取到正文"
    for pattern, limit, reason in _BODY_STUB_RULES:
        if len(body) < limit and pattern.search(body):
            return "public_partial", reason
    if len(body) < _BODY_SHELL_MAX:
        return "public_partial", f"正文仅{len(body)}字，不足以核验"
    return "public_full", ""


# 聚合来源会把「一篇覆盖多家医院」的公众号推文也收进来。此时接口的 `caller_name`
# 只是其中一家，`province`/`city` 描述的是发文方，标的清单是所有医院的并集——
# 采购人、品类、采购方式各来自不同子公告，任何一个字段照常绑定都可能张冠李戴。
# 实测样本：「【扫院行动】9月4日医院采购清单」被绑成了单位=宁海县城关医院。
_AGGREGATE_TITLE_RE = re.compile(
    r"扫院行动|采购清单|采购(?:信息)?汇总|招标(?:信息)?汇总|信息汇总|汇总公告"
    r"|每日(?:采购|招标|标讯)"
    r"|[一二三四五六七八九十\d]{1,2}月[一二三四五六七八九十\d]{1,3}日[^\n]{0,8}"
    r"(?:清单|汇总|速览|一览|盘点)"
)
_MEDICAL_ORG_RE = re.compile(r"[一-鿿]{2,12}?(?:医院|卫生院|保健院|卫生服务中心|医学中心)")
# 2026-09-04 实测 24 条队列：真汇总页数出 458 家，其余最多 4 家——而那 4 家是正则
# 连着前文吃进来的变体（`受许昌市中心医院`、`影响医院`），不是真的多家单位。门槛落在
# 这道口子中间，宁可漏判也不误伤医共体联合采购这类确实牵涉几家医院的单一公告。
_AGGREGATE_MIN_ORGS = 8


def aggregate_notice(title, text):
    """多家单位合成的汇总页；返回判定理由，不是汇总页时返回空串。"""
    names = {match.group(0) for match in _MEDICAL_ORG_RE.finditer(text or "")}
    if len(names) >= _AGGREGATE_MIN_ORGS:
        return f"正文出现{len(names)}家医疗机构，字段无法归属到其中某一家"
    return "标题是多家单位的采购汇总" if _AGGREGATE_TITLE_RE.search(title or "") else ""


def target_category_signals(text):
    return [name for name, pattern in TARGET_CATEGORY_PATTERNS if pattern.search(text or "")]


def target_category_matches(text, per_group=3):
    """返回 [(组名, 公告里命中的原文片段)]，按组内首次出现顺序去重。

    与 `target_category_signals` 的差别是保留**命中的原文**而不只是组名：
    `膜性肾病/PLA2R` 这个组名对业务方没有意义，公告里写的 `磷脂酶A2受体` 才有。
    """
    matches = []
    for name, pattern in TARGET_CATEGORY_PATTERNS:
        spans = []
        for found in pattern.finditer(text or ""):
            span = found.group(0).strip()
            if span and span not in spans:
                spans.append(span)
            if len(spans) >= per_group:
                break
        matches.extend((name, span) for span in spans)
    return matches


def signal_tier(signals):
    """候选的目标信号强度。

    `core` 命中过任一核心名词组或项目代号组；`broad` 只命中宽片段组；`none` 无信号。
    分层不决定去留，只进 search_evidence 供核实阶段调整盘问力度。
    """
    signals = list(signals or [])
    if not signals:
        return "none"
    return "broad" if all(name in BROAD_SIGNAL_GROUPS for name in signals) else "core"


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
    """仅公开强身份键；完整公告比较统一由 tender_identity 负责。"""
    from tender_identity import identity
    ident = identity({**(content or {}), **candidate})
    return {("url", u) for u in ident.urls} | {("id", i) for i in ident.ids}


def group_candidates(candidates):
    """不做裸标题或意向汇总前缀的合并；每个成员都必须互相兼容。"""
    from tender_identity import IdentityIndex, identity, duplicate_reason
    groups = []
    lookup = IdentityIndex()
    owner = {}
    for candidate in candidates:
        known, _ = lookup.find(candidate)
        group = owner.get(id(known)) if known is not None else None
        ident = identity(candidate)
        if group is None or not all(duplicate_reason(ident, identity(m)) for m in group):
            group = []
            groups.append(group)
        group.append(candidate)
        owner[id(candidate)] = group
        lookup.add(candidate)
    return groups


def write_candidates(candidates, out_dir, run_date):
    """按统一契约写轻量索引和逐条完整正文。"""
    out_dir = Path(out_dir)
    content_dir = out_dir / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for item in candidates:
        url = canonical_url(item.get("url"))
        discriminator = str(item.get("bid_id") or "") or (
            title_fingerprint(item.get("title")) + "|" + str(item.get("publish_time") or "")
        )
        cid = candidate_id(url, discriminator)
        content_rel = f"content/{cid}.json"
        full = {
            "_identity_urls": item.get("_identity_urls", []),
            "_identity_ids": item.get("_identity_ids", []),
            "candidate_id": cid,
            "identity_discriminator": discriminator,
            "bid_id": item.get("bid_id") or "",
            "title": item.get("title") or "",
            "source_url": url,
            "summary": item.get("summary") or "",
            "content": item.get("content") or "",
            # 来源自带的标的清单（知了的 sm_names + brand_names）。正文写「详见附件」
            # 「下载」时，这是唯一能定品类的字段——不落盘统一层就只能看到一篇没有清单的公告。
            "product_list": item.get("product_list") or "",
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
            "identity_discriminator": discriminator,
            "_identity_urls": full["_identity_urls"],
            "_identity_ids": full["_identity_ids"],
            "candidate_id": cid,
            "bid_id": full["bid_id"],
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
            "content_access_reason": item.get("content_access_reason") or "",
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
    """合并候选并按正文可用性选代表。

    知了会为同一条公告存两份记录：一份只有标题的壳（正文写「完整信息请查看原文」），
    一份带完整正文。2026-09-05 实测长沙市医健那条，壳 165 字、完整记录 65,351 字，
    而目标品类信号「自身抗体」只在完整记录里。`_preference` 按 content_access 与
    正文长度取代表，正好让完整记录赢过壳——这是本函数在单信源下的主要价值。
    """
    rows = []
    for source_dir in source_dirs:
        rows.extend(load_source_candidates(source_dir))
    combined = [{**content, **candidate} for candidate, content in rows]
    originals = {id(c): row for c, row in zip(combined, rows)}
    groups = [[originals[id(c)] for c in group] for group in group_candidates(combined)]

    merged = []
    for members in groups:
        members.sort(key=lambda row: _preference(*row), reverse=True)
        primary_candidate, primary_content = members[0]
        source_names = []
        source_queries = []
        found_by_query = set()
        alternates = []
        for candidate, content in members:
            for alternate in candidate.get("alternate_sources") or []:
                if alternate not in alternates:
                    alternates.append(alternate)
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

        from tender_identity import remember_aliases
        aliases = {}
        remember_aliases(aliases, *[{**content, **candidate} for candidate, content in members])
        merged.append({
            **aliases,
            "bid_id": primary_candidate.get("bid_id") or primary_content.get("bid_id") or "",
            "title": primary_candidate.get("title") or primary_content.get("title") or "",
            "site_name": primary_candidate.get("site_name") or "",
            "url": canonical_url(primary_candidate.get("url")),
            "publish_time": primary_candidate.get("publish_time") or "",
            "auth_info_level": primary_candidate.get("auth_info_level"),
            "auth_info_des": primary_candidate.get("auth_info_des") or "",
            "rank_score": primary_candidate.get("rank_score"),
            "summary": primary_content.get("summary") or "",
            "content": primary_content.get("content") or "",
            # 合并时同样要带上：清单丢在这一步，统一层照样看不到品类。
            "product_list": primary_content.get("product_list") or "",
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
            "content_access_reason": primary_candidate.get("content_access_reason") or "",
            "source_priority": int(
                primary_candidate.get("source_priority")
                if primary_candidate.get("source_priority") is not None
                else SOURCE_PRIORITIES.get(primary_candidate.get("source"), 0)
            ),
            "alternate_sources": alternates,
        })
    return merged
