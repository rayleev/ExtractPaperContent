"""
国家判定工具 — 归一化国家名称、判断是否中国、基于提取结果推断论文国家。

供 filter 节点（提取前粗筛）和 postprocess 节点（提取后复核）共用。

约定：
  - 台湾（Taiwan/TW/台湾）视为中国。
  - 空值 / Unknown 等"不确定"取值在 filter 阶段放行，提取后再复核。
"""

from __future__ import annotations
import logging
from typing import List, Tuple

logger = logging.getLogger("paper_extractor")

# 中国别名（含台湾），统一转小写后比较
CHINA_ALIASES = {
    "china", "cn", "chn", "中国", "中华人民共和国", "people's republic of china",
    "prc", "mainland china", "中国大陆", "中国大陸",
    "taiwan", "tw", "twn", "台湾", "臺灣", "中国台湾", "台湾省",
}

# 明确"不确定"的取值（提取前需放行、提取后复核）
# 注：'其他国家名'/'其他国家' 是 classify prompt 的占位符，LLM 有时会原样回填，
#     表示元数据层面无法确定具体国家，按"不确定"处理（放行→提取后按全文复核）。
UNCERTAIN_VALUES = {
    "", "unknown", "未知", "n/a", "na", "none", "null", "unspecified", "不确定",
    "其他国家名", "其他国家",
}

# 中国省级行政区关键词（用于 site_administrative_region 兜底判断）
_CHINA_PROVINCE_KEYWORDS = [
    "北京", "天津", "上海", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽",
    "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南",
    "四川", "贵州", "云南", "陕西", "甘肃", "青海", "内蒙古", "广西",
    "西藏", "宁夏", "新疆", "台湾",
]


def normalize_country(raw) -> str:
    """
    归一化国家名称。

    中国（含台湾）→ 'China'；空/不确定 → ''；其他 → 去首尾空白原样返回。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.lower() in UNCERTAIN_VALUES:
        return ""
    if s.lower() in CHINA_ALIASES:
        return "China"
    return s


def is_china(raw) -> bool:
    """判断国家取值是否为中国（含台湾）。空/不确定返回 False。"""
    if raw is None:
        return False
    return str(raw).strip().lower() in CHINA_ALIASES


def is_uncertain(raw) -> bool:
    """判断国家取值是否为空/不确定（None 也视为不确定）。"""
    if raw is None:
        return True
    return str(raw).strip().lower() in UNCERTAIN_VALUES


def _region_looks_china(region: str) -> bool:
    """行政区划/地点字符串是否呈现中国特征（含省份关键词或中文字符）。"""
    if not region:
        return False
    if any(kw in region for kw in _CHINA_PROVINCE_KEYWORDS):
        return True
    # 含中文字符（中国试验的行政区划通常为中文，如 "四川省广汉市"）
    return any("\u4e00" <= ch <= "\u9fff" for ch in region)


def infer_paper_country(studies: List[dict]) -> Tuple[bool, str]:
    """
    基于提取出的 studies 推断论文是否中国（提取后复核用）。

    判断优先级（逐 study，命中中国立即返回）：
      1. study.country 明确中国（含台湾）       → 中国
      2. study.country 明确非中国               → 记录该国家，继续看其他 study
      3. country 空/不确定时，用行政区划/地点兜底：
         含中国省份关键词或中文                 → 中国

    Returns:
        (is_china, evidence)
        - is_china=True  → evidence 为支持中国的依据（如 "study.country='CN'"）
        - is_china=False → evidence 为检测到的非中国国家名，或 'undetermined'
                           （全文仍无法确认国家）
    """
    foreign_seen = ""
    for study in studies:
        country = study.get("country")
        region = (
            study.get("site_administrative_region")
            or study.get("experimental_site_name")
            or ""
        )

        if is_china(country):
            return True, f"study.country='{country}'"
        if not is_uncertain(country):
            # 明确非中国国家，先记录，继续检查其余 study
            foreign_seen = normalize_country(country)
            continue
        # country 不确定 → 用行政区划/地点兜底
        if _region_looks_china(region):
            return True, f"site_region='{str(region)[:30]}'"

    if foreign_seen:
        return False, foreign_seen
    return False, "undetermined"
