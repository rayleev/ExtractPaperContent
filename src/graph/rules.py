"""
规则验证引擎 — 纯代码实现，不消耗 token。

替代原来的 LLM confidence validation，覆盖 80%+ 的数据质量检查。
"""

from __future__ import annotations
import logging
from typing import Optional

from src.core.models import _convert_yield

logger = logging.getLogger("paper_extractor")

# 不同作物的产量合理范围 (kg/ha)
YIELD_RANGES = {
    "水稻": (500, 18000),
    "玉米": (1000, 20000),
    "小麦": (800, 15000),
    "大豆": (300, 6000),
    "油菜": (200, 5000),
}
YIELD_MIN_DEFAULT = 500
YIELD_MAX_DEFAULT = 18000

# 中国经纬度范围
LAT_MIN, LAT_MAX = 18.0, 54.0
LON_MIN, LON_MAX = 73.0, 135.0


def _to_float(value):
    """安全转 float：失败（如字符串 '北纬30°'）返回 None，避免比较时抛 TypeError。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def validate_extraction(extraction: dict, paper_meta: dict, config=None, category: str = "") -> dict:
    """
    对提取结果运行规则验证。

    Args:
        extraction: 提取结果
        paper_meta: 论文元数据
        config: 配置对象
        category: 论文类别（management_yield 时跳过 CK_001 检查）

    返回:
        {
            "issues": [{"code": str, "message": str}],        # 严重问题
            "warnings": [{"code": str, "message": str}],      # 警告
            "stats": {
                "total_studies": int,
                "total_varieties": int,
                "issues_count": int,
                "warnings_count": int,
                "flagged_records": int,
            },
            "flagged_variety_indices": [(study_idx, variety_idx), ...],
        }
    """
    issues: list[dict] = []
    warnings: list[dict] = []
    flagged: list[tuple[int, int]] = []

    studies = extraction.get("studies", [])
    pub_year = paper_meta.get("year")

    # 收集所有品种名称 → yield 映射，用于跨 study 一致性检查
    variety_yields: dict[str, list[float]] = {}

    for si, study in enumerate(studies):
        prefix = f"Study[{si}] '{(study.get('study_title') or '')[:30]}'"
        varieties = study.get("varieties", [])

        # ── Study 级检查 ──

        # 对照品种存在性（management_yield 中对照处理非必须，跳过检查）
        ck_varieties = [v for v in varieties if v.get("is_check_variety")]
        if category != "management_yield":
            if not ck_varieties and varieties:
                warnings.append({"code": "CK_001", "message": f"{prefix}: 缺少对照品种"})

        # trial_year ≤ publication_year
        trial_year = study.get("trial_year", "")
        if trial_year and pub_year:
            try:
                ty = int(str(trial_year)[:4])
                if ty > int(pub_year):
                    issues.append({"code": "YEAR_001", "message": f"{prefix}: trial_year({trial_year}) > publication_year({pub_year})"})
            except ValueError:
                pass

        # 经纬度范围
        raw_lat = study.get("latitude")
        raw_lon = study.get("longitude")
        lat = _to_float(raw_lat)
        lon = _to_float(raw_lon)
        if lat is not None and lon is not None:
            if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                warnings.append({"code": "GEO_001", "message": f"{prefix}: 经纬度({lat}, {lon})超出中国范围"})
        elif (raw_lat is not None and lat is None) or (raw_lon is not None and lon is None):
            warnings.append({"code": "GEO_002", "message": f"{prefix}: 经纬度非数值（lat={raw_lat}, lon={raw_lon}），已忽略范围检查"})

        # 多站点检测
        site_name = study.get("experimental_site_name", "") or ""
        if "、" in site_name or ("和" in site_name and len(site_name) > 10):
            warnings.append({"code": "MULTI_SITE_001", "message": f"{prefix}: experimental_site_name 包含多个地点"})

        # ── Variety 级检查 ──
        for vi, v in enumerate(varieties):
            vname = v.get("variety_name", "?")
            vprefix = f"{prefix} / {vname}"

            # 1. 产量换算一致性
            raw_val = v.get("yield_raw_value")
            raw_unit = v.get("yield_raw_unit", "")
            std_val = v.get("yield_standard_value")

            if raw_val is not None and raw_unit:
                expected = _convert_yield(
                    raw_val, raw_unit,
                    mass_to_kg=config.unit_conversion.mass_to_kg if config else {},
                    area_to_ha=config.unit_conversion.area_to_ha if config else {},
                    context_plot={"plot", "小区"},
                    context_plant={"plant", "株", "pot", "盆", "ear", "穗", "hill", "穴", "棵"},
                )
                if expected is not None and std_val is not None:
                    if abs(expected - std_val) > max(1.0, expected * 0.01):
                        issues.append({"code": "YIELD_001", "message": f"{vprefix}: 产量换算不一致 (raw={raw_val} {raw_unit}, expected={expected}, got={std_val})"})
                        flagged.append((si, vi))

            # 2. 产量范围检查（按作物使用不同范围）
            if std_val is not None:
                # 从 study_title 推断作物
                study_title = study.get("study_title", "") or ""
                yield_min, yield_max = YIELD_MIN_DEFAULT, YIELD_MAX_DEFAULT
                for crop_name, (crop_min, crop_max) in YIELD_RANGES.items():
                    if crop_name in study_title:
                        yield_min, yield_max = crop_min, crop_max
                        break

                if std_val < yield_min or std_val > yield_max:
                    warnings.append({"code": "YIELD_002", "message": f"{vprefix}: 产量异常 ({std_val} kg/ha, 合理范围 {yield_min}-{yield_max})"})
                    flagged.append((si, vi))

            # 3. pct_over_check 与对照品种计算一致性
            pct = v.get("pct_over_check")
            if pct is not None and raw_val is not None and ck_varieties:
                ck_yield = ck_varieties[0].get("yield_raw_value")
                ck_unit = ck_varieties[0].get("yield_raw_unit", "")
                if ck_yield and raw_unit == ck_unit:
                    computed_pct = (raw_val - ck_yield) / ck_yield * 100
                    if abs(computed_pct - pct) > 1.0:
                        warnings.append({"code": "YIELD_003", "message": f"{vprefix}: 增产率偏差 (reported={pct}%, computed={computed_pct:.1f}%)"})
                        flagged.append((si, vi))

            # 4. yield_raw_unit 为 % 的异常
            if raw_unit == "%":
                issues.append({"code": "YIELD_004", "message": f"{vprefix}: yield_raw_unit 为 %（增产比例，非实际产量）"})
                flagged.append((si, vi))

            # 5. source_location 为空
            if not (v.get("source_location") or "").strip():
                warnings.append({"code": "SOURCE_001", "message": f"{vprefix}: source_location 为空"})

            # 6. management_yield 论文缺少 treatment_name
            if category == "management_yield" and not (v.get("treatment_name") or "").strip():
                warnings.append({"code": "TREATMENT_001", "message": f"{vprefix}: management_yield 论文缺少 treatment_name"})

            # 7. NPK 施肥处理完整性：treatment_name 存在但 N/P/K raw 全空
            treatment_name = (v.get("treatment_name") or "").strip()
            if treatment_name:
                n_raw = v.get("n_raw_value")
                p_raw = v.get("p_raw_value")
                k_raw = v.get("k_raw_value")
                if n_raw is None and p_raw is None and k_raw is None:
                    warnings.append({
                        "code": "NUTRIENT_001",
                        "message": f"{vprefix}: treatment_name='{treatment_name}' 但 N/P/K raw 全空（该处理声称是处理但未抄到任何养分量）"
                    })

            # 收集品种产量用于跨 study 检查
            if std_val is not None:
                variety_yields.setdefault(vname, []).append(std_val)

    # ── 跨 study 一致性检查 ──
    for vname, yields in variety_yields.items():
        if len(yields) >= 2:
            avg = sum(yields) / len(yields)
            for y in yields:
                if avg > 0 and abs(y - avg) / avg > 0.5:
                    warnings.append({"code": "CONSISTENCY_001", "message": f"品种 '{vname}' 跨 study 产量波动 >50%: {[round(y, 0) for y in yields]}"})
                    break

    # 去重 flagged
    flagged = list(set(flagged))

    total_varieties = sum(len(s.get("varieties", [])) for s in studies)

    report = {
        "issues": issues,
        "warnings": warnings,
        "stats": {
            "total_studies": len(studies),
            "total_varieties": total_varieties,
            "issues_count": len(issues),
            "warnings_count": len(warnings),
            "flagged_records": len(flagged),
        },
        "flagged_variety_indices": flagged,
    }

    return report
