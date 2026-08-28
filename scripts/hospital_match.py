#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于本地精简索引进行保守的医疗单位名称与等级匹配。"""

import argparse
import gzip
import json
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX = ROOT / "data" / "hospitals.min.json.gz"
NULL_TEXT = {"", "null", "none", "未知", "未披露"}
ORG_SUFFIXES = (
    "妇幼保健计划生育服务中心", "疾病预防控制中心", "社区卫生服务中心",
    "妇幼保健服务中心", "妇幼保健中心", "卫生服务中心", "卫生服务站",
    "妇幼保健院", "中心血站", "疾控中心", "医疗中心", "卫生院",
    "医院", "门诊部", "诊疗中心", "诊所",
)
SUFFIX_RE = re.compile("|".join(sorted(map(re.escape, ORG_SUFFIXES), key=len, reverse=True)))
ALIAS_SPLIT_RE = re.compile(r"[、,，;/；|]+")
# 纯通用名：序号 + 机构类型，前面没有任何地名或专名。全国重名，认不出是哪一家。
# 不能改用长度阈值——「漳州市医院」5 字、「宾县人民医院」6 字都是具体医院，会被误杀。
GENERIC_ORG_RE = re.compile(
    r"^(?:第?[一二三四五六七八九十百千\d]{1,4})?"
    r"(?:人民|中心|中医|中西医结合|妇幼保健|妇幼|附属)?"
    r"(?:医院|保健院|卫生院|门诊部|诊所)$"
)


def is_generic_org_name(key):
    return bool(GENERIC_ORG_RE.match(key))


def normalize(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def clean_hint(value):
    value = str(value or "").strip()
    return "" if value.lower() in NULL_TEXT else value


def alias_variants(value):
    value = str(value or "").strip().strip("()（）[]【】")
    if not value:
        return []
    parts = [part.strip().strip("()（）[]【】") for part in ALIAS_SPLIT_RE.split(value)]
    return [part for part in parts if len(normalize(part)) >= 4]


def loose_key(value):
    """去掉行政区划通名的键，用于「撤县设市／设区」后的新旧名互认。

    弥勒 2013 年撤县设市，公告写「弥勒市人民医院」而索引里是「弥勒县人民医院」，
    精确键对不上。去掉市/县/区后两者都归到「弥勒人民医院」。
    只在精确匹配全无命中时兜底，且撞车（朝阳区 vs 朝阳市）会被后续分组判为歧义。
    """
    return re.sub(r"[市县区]", "", normalize(value))


# 名字打头的地名（故城县、呼和浩特市、山东省…）
# 「省」在列，是因为「山东省南山医院」这类全称打头的记录同样会被错编码
# （该条被编到四川内江市中区）。代价是「武警云南省总队医院」会截出「武警云」
# 而被误标，那是安全方向——最多不回填地理，不会填错。
NAME_LOCALITY_RE = re.compile(r"^([一-鿿]{2,4})[市县区州盟旗省]")


def _name_locality(record):
    """(名字打头的地名, 记录自身地理字段) 归一化后的一对；截不出地名时为 None。"""
    match = NAME_LOCALITY_RE.match(str(record.get("n") or ""))
    if not match:
        return None
    geo = normalize("".join(str(record.get(field) or "") for field in ("p", "c", "d")))
    if not geo:
        return None
    return normalize(match.group(1)), geo


def geo_is_suspect(record):
    """记录自身的地理字段和名字里的地名对不上时为真。

    索引是多来源拼的，其中一路的地理编码把音近地名搞混了。实例：
        故城县中医院      → 河北省衡水市故城县   （对）
        故城县中医医院    → 云南省丽江古城       （错，故城被编码成古城）
        故城县医院        → 云南省丽江古城       （错）
    这种记录一旦在无省份提示时被匹上，会把省/地区/大区一路填错，直接发错人。
    标记出来，只禁止它回填地理，名称和等级仍然可用。
    异体字（滕冲/腾冲）也会被标上，那是安全方向的误判——最多不回填，不会填错。
    """
    locality = _name_locality(record)
    return bool(locality) and locality[0] not in locality[1]


def geo_is_confirmed(record):
    """名字打头的地名确实出现在记录自身的地理字段里——地理正面自洽。

    严格于 `not geo_is_suspect(...)`：名字里根本没有地名的记录两者都不是，
    它只是无从判断。消歧必须押在正面自洽上，不能押在「没被标记」上——
    否则一条无地名的脏记录会白捡一次胜出。
    """
    locality = _name_locality(record)
    return bool(locality) and locality[0] in locality[1]


def geo_matches(record, province="", city="", district=""):
    """提示只要命中记录的任一地理层级就算通过。

    不按层级对号入座，是因为调用方给的层级本来就不可靠：CCGP 的
    source_fields["所属省/市"] 存的往往是「宾县」「漳州市」这类地名而非省份，
    照层级严格比对会把「宾县人民医院」判成与「黑龙江省」矛盾，误杀真匹配。
    跨省的错配仍然拦得住——「天津市滨海新区」在「浙江省宁波市北仑区」的
    任何一级里都找不到。
    """
    values = [normalize(record.get(field, "")) for field in ("p", "c", "d")]
    values = [value for value in values if value]
    for hint in (province, city, district):
        hint_norm = normalize(clean_hint(hint))
        if not hint_norm:
            continue
        if not any(hint_norm in value or value in hint_norm for value in values):
            return False
    return True


class HospitalIndex:
    def __init__(self, path=DEFAULT_INDEX):
        self.path = Path(path)
        with gzip.open(self.path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.records = payload.get("records", [])
        if not isinstance(self.records, list):
            raise ValueError("医院索引必须含 records 数组")

        self.exact = defaultdict(list)
        self.loose = defaultdict(list)
        for index, record in enumerate(self.records):
            name_key = normalize(record.get("n"))
            if len(name_key) >= 4:
                self.exact[name_key].append((index, "name"))
                relaxed = loose_key(record.get("n"))
                if len(relaxed) >= 4 and relaxed != name_key:
                    self.loose[relaxed].append((index, "name"))
            for alias in alias_variants(record.get("a")):
                alias_key = normalize(alias)
                if len(alias_key) >= 4:
                    self.exact[alias_key].append((index, "alias"))

    def _keys_ending_at_org_suffix(self, text):
        compact = normalize(text)
        found = set()
        for suffix_match in SUFFIX_RE.finditer(compact):
            end = suffix_match.end()
            max_length = min(60, end)
            matches = []
            for length in range(4, max_length + 1):
                key = compact[end - length:end]
                if key in self.exact:
                    matches.append(key)
            if matches:
                longest = max(map(len, matches))
                found.update(key for key in matches if len(key) == longest)
        return found

    def _disambiguate_same_name(self, grouped, province, city, district):
        """在同名候选之间挑一组，挑不出就原样返回。返回 (分组, 是否靠提示挑的)。

        先用调用方的地理提示。整名命中不过 geo 门禁是为了「不否决匹配」（平坝区 vs
        平坝县这类改名会误杀），而这里是在几个已确定同名的候选之间挑一个：挑得出
        就是它，挑不出就退回，不会挑错。
        """
        hinted = {group_key: group_hits for group_key, group_hits in grouped.items()
                  if any(geo_matches(self.records[hit[2]], province, city, district)
                         for hit in group_hits)}
        if len(hinted) == 1:
            return hinted, True

        # 没有提示或提示裁决不了，就剔掉地理与自身名字矛盾的那些组。
        # 幸存者还必须正面自洽，光是「没被标记」不够（见 geo_is_confirmed）：
        # 2026-08-28 全量实测 912 次触发全部满足，这层约束当下零代价，挡的是
        # 将来索引换源后冒出来的风险形态——一条无地名的脏记录白捡一次胜出。
        trusted = {group_key: group_hits for group_key, group_hits in grouped.items()
                   if not all(geo_is_suspect(self.records[hit[2]]) for hit in group_hits)}
        if len(trusted) == 1:
            survivor = next(iter(trusted.values()))
            if any(geo_is_confirmed(self.records[hit[2]]) for hit in survivor):
                return trusted, False
        return grouped, False

    def match(self, name="", text="", province="", city="", district=""):
        hits = []
        explicit_key = normalize(name)
        whole_keys = {explicit_key} if explicit_key in self.exact else set()
        # 单位名里截出来的后缀键（「…第三人民医院」→「第三人民医院」）只是子串命中，
        # 确定性远低于整名命中，按 text 一样对待。
        # 子串键还得有辨识度。「第三人民医院」全国无数家都叫这个，拿它去认一个
        # 14 字的全名纯属巧合，地理提示缺失时更没有东西能纠正它。
        partial_keys = {key for key in self._keys_ending_at_org_suffix(name) - whole_keys
                        if not is_generic_org_name(key)}
        # 自由文本里截出来的通用名更不可信，同样挡掉
        text_keys = {key for key in self._keys_ending_at_org_suffix(text)
                     if not is_generic_org_name(key)}

        for origin, keys in (("explicit", whole_keys), ("partial", partial_keys),
                             ("text", text_keys)):
            for key in keys:
                for record_index, key_kind in self.exact.get(key, []):
                    record = self.records[record_index]
                    # 只有整名/整别名命中才是确定性匹配，geo hint 不得否决它——行政区域
                    # 新旧名差异（平坝区 vs 平坝县）会误杀。同名消歧交给下方 grouped 分组。
                    #
                    # 子串命中必须过 geo。2026-08-27 实测：岳阳县血防医院（湖南）的别名
                    # 就叫「第三人民医院」，于是「新疆维吾尔自治区第三人民医院」的三条公告
                    # 全被它劫持，省份提示是新疆也挡不住——因为当时子串走的是 explicit 通道。
                    if origin != "explicit" and not geo_matches(record, province, city, district):
                        continue
                    priority = {
                        ("explicit", "name"): 6,
                        ("explicit", "alias"): 5,
                        ("partial", "name"): 4,
                        ("partial", "alias"): 3,
                        ("text", "name"): 2,
                        ("text", "alias"): 1,
                    }[(origin, key_kind)]
                    hits.append((priority, len(key), record_index, origin, key_kind, key))

        if not hits:
            # 精确键全无命中时，才允许「撤县设市」这类通名差异兜底，且只认显式单位名——
            # 拿自由文本做宽松匹配噪声太大。
            relaxed = loose_key(name)
            if len(relaxed) >= 4:
                for record_index, key_kind in self.loose.get(relaxed, []):
                    hits.append((0, len(relaxed), record_index, "loose", key_kind, relaxed))

        if not hits:
            return {"matched": False, "ambiguous": False, "candidate_count": 0}

        top_priority = max(hit[0] for hit in hits)
        hits = [hit for hit in hits if hit[0] == top_priority]
        top_length = max(hit[1] for hit in hits)
        hits = [hit for hit in hits if hit[1] == top_length]

        grouped = defaultdict(list)
        for hit in hits:
            record = self.records[hit[2]]
            group_key = (
                normalize(record.get("n")), normalize(record.get("p")),
                normalize(record.get("c")), normalize(record.get("d")),
            )
            grouped[group_key].append(hit)

        # 索引里有 1983 组同名但地理冲突的重复记录（多为音近地名被编错：临湘→临翔、
        # 故城→古城、保山→宝山），它们会把一次本该唯一的匹配堵成歧义。整名命中按
        # 设计不过 geo 门禁（见上），拦不住这种，于是连带上正确的省份提示都匹不上。
        #
        # 只在候选同名时消歧。名字不同（朝阳区 vs 朝阳市人民医院）是两家真医院，
        # 那时 suspect 只说明其地理不可信，不足以判定它不是要找的那一家。
        geo_disambiguated = False
        geo_from_hint = False
        if len(grouped) > 1 and len({group_key[0] for group_key in grouped}) == 1:
            grouped, geo_from_hint = self._disambiguate_same_name(
                grouped, province, city, district)
            geo_disambiguated = len(grouped) == 1

        if len(grouped) != 1:
            return {
                "matched": False,
                "ambiguous": True,
                "candidate_count": len(grouped),
                "reason": "同名或别名对应多个医疗单位，地区信息不足",
            }

        group_hits = next(iter(grouped.values()))
        records = [self.records[hit[2]] for hit in group_hits]
        base = records[0]
        names = {record.get("n", "") for record in records if record.get("n")}
        levels = {record.get("l", "") for record in records if record.get("l")}
        aliases = {record.get("a", "") for record in records if record.get("a")}
        method_hit = max(group_hits, key=lambda hit: (hit[0], hit[1]))
        return {
            "matched": True,
            "ambiguous": False,
            "candidate_count": len(records),
            "hospital_name": next(iter(names)) if len(names) == 1 else base.get("n", ""),
            "hospital_alias": next(iter(aliases)) if len(aliases) == 1 else "",
            "hospital_level": next(iter(levels)) if len(levels) == 1 else "",
            "province": base.get("p", ""),
            "city": base.get("c", ""),
            "district": base.get("d", ""),
            "match_method": f"{method_hit[3]}_{method_hit[4]}",
            "match_key": method_hit[5],
            "level_conflict": len(levels) > 1,
            # False = 不得用来回填省份/地区。两种情形：
            # 一是记录的地理字段与自身名字矛盾；
            # 二是它本来就是「因为和调用方提示吻合」才从同名候选里被选出来的——
            # 再拿它的地理回填是循环论证。好的情况只是复述提示，坏的情况会把错提示
            # 洗成确信字段：索引里那条挂在四川内江的「山东中医药大学附属眼科医院」
            # 名字不带行政区划前缀，geo_is_suspect 看不见它，提示写四川就会选中它。
            # 名称与等级不受影响，那才是这次匹配真正的增量。
            "geo_trusted": not geo_from_hint and not any(geo_is_suspect(record)
                                                         for record in records),
            # True = 曾有同名不同地理的候选，靠提示或剔除脏记录才收敛到这一家
            "geo_disambiguated": geo_disambiguated,
        }


@lru_cache(maxsize=1)
def get_default_index():
    return HospitalIndex(DEFAULT_INDEX)


def main():
    parser = argparse.ArgumentParser(description="查询本地全国医疗单位精简索引")
    parser.add_argument("--name", default="", help="公告中的单位名或医院名")
    parser.add_argument("--text", default="", help="标题或摘要等辅助文本")
    parser.add_argument("--province", default="")
    parser.add_argument("--city", default="")
    parser.add_argument("--district", default="")
    parser.add_argument("--index", default=str(DEFAULT_INDEX))
    args = parser.parse_args()
    index = HospitalIndex(args.index)
    result = index.match(args.name, args.text, args.province, args.city, args.district)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("matched") else 1


if __name__ == "__main__":
    raise SystemExit(main())
