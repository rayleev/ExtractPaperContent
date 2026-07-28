"""
后处理工具函数 — 数据清洗、过滤、回填。

从 extractor.py 迁移而来，所有函数均为纯代码，不依赖 LLM。
"""

from __future__ import annotations
import logging
from typing import List

logger = logging.getLogger("paper_extractor")


def backfill_variety_codes(studies: List[dict]) -> int:
    """
    品种审定编号回填：同一品种名在不同 study 间共享 variety_code。

    收集所有非空 name→code 映射，回填空的 variety_code。
    返回回填数量。
    """
    code_map: dict = {}
    for study in studies:
        for v in study.get("varieties", []):
            name = v.get("variety_name", "")
            code = v.get("variety_code", "")
            if name and code and name not in code_map:
                code_map[name] = code

    backfill_count = 0
    if code_map:
        for study in studies:
            for v in study.get("varieties", []):
                name = v.get("variety_name", "")
                if name and not v.get("variety_code") and name in code_map:
                    v["variety_code"] = code_map[name]
                    backfill_count += 1

    if backfill_count:
        logger.info(f"  Variety code backfill: {backfill_count} records")
    return backfill_count


def filter_non_field_experiments(studies: List[dict]) -> int:
    """
    剔除非大田试验（盆栽、温室、单株计产等）。

    综合判断信号：
      1. experimental_design_description / growth_facility_description 含盆栽关键词
      2. yield_raw_unit 为 g/株（单株计产，通常非大田）
      3. measurement_method 含"盆栽"/"单株"等关键词

    返回剔除数量。
    """
    pot_keywords = [
        "盆栽", "pot experiment", "greenhouse", "温室",
        "人工气候室", "growth chamber", "培养箱",
        "水培", "hydroponic", "营养液",
        "模拟试验", "室内试验", "箱栽", "桶栽",
    ]

    kept_studies = []
    removed_count = 0

    for study in studies:
        design = (study.get("experimental_design_description") or "").lower()
        facility = (study.get("growth_facility_description") or "").lower()
        combined_text = f"{design} {facility}"

        # 检查试验描述中的盆栽关键词
        is_pot = any(kw in combined_text for kw in pot_keywords)

        # 检查产量单位: g/株 通常表示单株计产
        is_single_plant = False
        varieties = study.get("varieties", [])
        if varieties:
            units = [v.get("yield_raw_unit", "") or "" for v in varieties]
            g_per_plant = sum(1 for u in units if "g/株" in u or "g·株" in u)
            if g_per_plant > len(units) * 0.5:
                is_single_plant = True

        # 检查测定方法
        methods = [v.get("measurement_method", "") or "" for v in varieties]
        method_text = " ".join(methods).lower()
        is_pot_method = any(kw in method_text for kw in ["盆栽", "单株", "pot"])

        if is_pot or (is_single_plant and is_pot_method):
            title = study.get("study_title", "")[:40]
            logger.info(f"  Filtered non-field: '{title}'")
            removed_count += 1
        else:
            kept_studies.append(study)

    if removed_count:
        studies[:] = kept_studies
        logger.info(f"  Non-field filter: removed {removed_count} studies")
    return removed_count


def filter_no_yield_studies(studies: List[dict]) -> tuple[int, int]:
    """
    剔除无产量数据的 study 和无效产量记录。

    处理：
      1. yield_raw_value 和 yield_raw_unit 都为空，且 pct_over_check 为空 → 无效记录
      2. study 过滤后无品种 → 删除该 study

    返回 (removed_studies, removed_varieties)。
    """
    removed_studies = 0
    removed_varieties = 0
    kept_studies = []

    for study in studies:
        varieties = study.get("varieties", [])
        kept_varieties = []

        for v in varieties:
            unit = (v.get("yield_raw_unit") or "").strip()
            value = v.get("yield_raw_value")
            pct = v.get("pct_over_check")

            # 只有产量值和增产比例都为空时，才视为无效记录
            if value is None and not unit and pct is None:
                removed_varieties += 1
                continue

            kept_varieties.append(v)

        if kept_varieties:
            study["varieties"] = kept_varieties
            kept_studies.append(study)
        else:
            title = study.get("study_title", "")[:40]
            logger.info(f"  Filtered no-yield study: '{title}'")
            removed_studies += 1

    if removed_studies or removed_varieties:
        studies[:] = kept_studies
        logger.info(
            f"  Yield filter: removed {removed_studies} studies, "
            f"{removed_varieties} variety records"
        )
    return removed_studies, removed_varieties


def normalize_site_info(studies: List[dict]) -> int:
    """
    同一论文内试验地点信息一致性回填。

    如果只有一个非空的 site_administrative_region / experimental_site_name，
    用它回填所有空值。多地点试验不回填。

    返回回填字段数。
    """
    if len(studies) <= 1:
        return 0

    # 收集非空的 region 和 site
    regions = set()
    sites = set()
    for study in studies:
        r = (study.get("site_administrative_region") or "").strip()
        s = (study.get("experimental_site_name") or "").strip()
        if r:
            regions.add(r)
        if s:
            sites.add(s)

    backfill_count = 0

    # 回填 region：只有一个非空值时
    if len(regions) == 1:
        fill_region = next(iter(regions))
        for study in studies:
            if not (study.get("site_administrative_region") or "").strip():
                study["site_administrative_region"] = fill_region
                backfill_count += 1

    # 回填 site_name：只有一个非空值时
    if len(sites) == 1:
        fill_site = next(iter(sites))
        for study in studies:
            if not (study.get("experimental_site_name") or "").strip():
                study["experimental_site_name"] = fill_site
                backfill_count += 1

    if backfill_count:
        logger.info(f"  Site info backfill: {backfill_count} fields")
    return backfill_count
