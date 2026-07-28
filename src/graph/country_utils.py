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

from src.core.constants import CHINA_ALIASES, UNCERTAIN_VALUES, CHINA_PROVINCE_KEYWORDS

logger = logging.getLogger("paper_extractor")


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
    """行政区划/地点字符串是否呈现中国特征。

    判断逻辑：
      1. 含中国地名关键词（省级/市级中文或拼音）
      2. 含中文字符（中文论文中的地名）
    """
    if not region:
        return False
    region_lower = region.lower()
    # 1. 关键词匹配（中文或拼音）
    if any(kw in region_lower for kw in CHINA_PROVINCE_KEYWORDS):
        return True
    # 2. 含中文字符（中文论文中的地名，如 "信阳市"）
    return any("一" <= ch <= "鿿" for ch in region)


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
