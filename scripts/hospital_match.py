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


def geo_matches(record, province="", city="", district=""):
    hints = (("p", province), ("c", city), ("d", district))
    for field, hint in hints:
        hint_norm = normalize(clean_hint(hint))
        if not hint_norm:
            continue
        value_norm = normalize(record.get(field, ""))
        if hint_norm not in value_norm and value_norm not in hint_norm:
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
        for index, record in enumerate(self.records):
            name_key = normalize(record.get("n"))
            if len(name_key) >= 4:
                self.exact[name_key].append((index, "name"))
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

    def match(self, name="", text="", province="", city="", district=""):
        hits = []
        explicit_key = normalize(name)
        explicit_keys = {explicit_key} if explicit_key in self.exact else set()
        explicit_keys.update(self._keys_ending_at_org_suffix(name))
        text_keys = self._keys_ending_at_org_suffix(text)

        for origin, keys in (("explicit", explicit_keys), ("text", text_keys)):
            for key in keys:
                for record_index, key_kind in self.exact.get(key, []):
                    record = self.records[record_index]
                    if not geo_matches(record, province, city, district):
                        continue
                    priority = {
                        ("explicit", "name"): 4,
                        ("explicit", "alias"): 3,
                        ("text", "name"): 2,
                        ("text", "alias"): 1,
                    }[(origin, key_kind)]
                    hits.append((priority, len(key), record_index, origin, key_kind, key))

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
