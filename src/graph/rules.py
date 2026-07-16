"""
规则验证引擎 — 纯代码实现，不消耗 token。

替代原来的 LLM confidence validation，覆盖 80%+ 的数据质量检查。
"""

from __future__ import annotations
import logging
from typing import Optional

from src.core.models import _convert_yield

logger = logging.getLogger("paper_extractor")

# 水稻产量合理范围 (kg/ha)
YIELD_MIN = 500
YIELD_MAX = 18000

# 中国经纬度范围
LAT_MIN, LAT_MAX = 18.0, 54.0
LON_MIN, LON_MAX = 73.0, 135.0


def validate_extraction(extraction: dict, paper_meta: dict) -> dict:
    """
    对提取结果运行规则验证。

    返回:
        {
            "issues": [str],        # 严重问题（数据可能错误）
            "warnings": [str],      # 警告（数据可能有问题）
            "stats": {
                "total_studies": int,
                "total_varieties": int,
                "issues_count": int,
                "warnings_count": int,
                "flagged_records": int,  # 需要 LLM 验证的记录数
            },
            "flagged_variety_indices": [(study_idx, variety_idx), ...],
        }
    """
    issues: list[str] = []
    warnings: list[str] = []
    flagged: list[tuple[int, int]] = []

    studies = extraction.get("studies", [])
    pub_year = paper_meta.get("year")

    # 收集所有品种名称 → yield 映射，用于跨 study 一致性检查
    variety_yields: dict[str, list[float]] = {}

    for si, study in enumerate(studies):
        prefix = f"Study[{si}] '{(study.get('study_title') or '')[:30]}'"
        varieties = study.get("varieties", [])

        # ── Study 级检查 ──

        # 对照品种存在性
        ck_varieties = [v for v in varieties if v.get("is_check_variety")]
        if not ck_varieties and varieties:
            warnings.append(f"{prefix}: 缺少对照品种")

        # trial_year ≤ publication_year
        trial_year = study.get("trial_year", "")
        if trial_year and pub_year:
            try:
                ty = int(str(trial_year)[:4])
                if ty > int(pub_year):
                    issues.append(f"{prefix}: trial_year({trial_year}) > publication_year({pub_year})")
            except ValueError:
                pass

        # 经纬度范围
        lat = study.get("latitude")
        lon = study.get("longitude")
        if lat is not None and lon is not None:
            if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                warnings.append(f"{prefix}: 经纬度({lat}, {lon})超出中国范围")

        # 多站点检测
        site_name = study.get("experimental_site_name", "") or ""
        if "、" in site_name or ("和" in site_name and len(site_name) > 10):
            warnings.append(f"{prefix}: experimental_site_name 包含多个地点")

        # ── Variety 级检查 ──
        for vi, v in enumerate(varieties):
            vname = v.get("variety_name", "?")
            vprefix = f"{prefix} / {vname}"

            # 1. 产量换算一致性
            raw_val = v.get("yield_raw_value")
            raw_unit = v.get("yield_raw_unit", "")
            std_val = v.get("yield_standard_value")

            if raw_val is not None and raw_unit:
                expected = _convert_yield(raw_val, raw_unit)
                if expected is not None and std_val is not None:
                    if abs(expected - std_val) > max(1.0, expected * 0.01):
                        issues.append(
                            f"{vprefix}: 产量换算不一致 "
                            f"(raw={raw_val} {raw_unit}, expected={expected}, got={std_val})"
                        )
                        flagged.append((si, vi))

            # 2. 产量范围检查
            if std_val is not None:
                if std_val < YIELD_MIN or std_val > YIELD_MAX:
                    warnings.append(
                        f"{vprefix}: 产量异常 ({std_val} kg/ha, "
                        f"合理范围 {YIELD_MIN}-{YIELD_MAX})"
                    )
                    flagged.append((si, vi))

            # 3. pct_over_check 与对照品种计算一致性
            pct = v.get("pct_over_check")
            if pct is not None and raw_val is not None and ck_varieties:
                ck_yield = ck_varieties[0].get("yield_raw_value")
                ck_unit = ck_varieties[0].get("yield_raw_unit", "")
                if ck_yield and raw_unit == ck_unit:
                    computed_pct = (raw_val - ck_yield) / ck_yield * 100
                    if abs(computed_pct - pct) > 1.0:
                        warnings.append(
                            f"{vprefix}: 增产率偏差 "
                            f"(reported={pct}%, computed={computed_pct:.1f}%)"
                        )
                        flagged.append((si, vi))

            # 4. yield_raw_unit 为 % 的异常
            if raw_unit == "%":
                issues.append(f"{vprefix}: yield_raw_unit 为 %（增产比例，非实际产量）")
                flagged.append((si, vi))

            # 5. source_location 为空
            if not (v.get("source_location") or "").strip():
                warnings.append(f"{vprefix}: source_location 为空")

            # 收集品种产量用于跨 study 检查
            if std_val is not None:
                variety_yields.setdefault(vname, []).append(std_val)

    # ── 跨 study 一致性检查 ──
    for vname, yields in variety_yields.items():
        if len(yields) >= 2:
            avg = sum(yields) / len(yields)
            for y in yields:
                if avg > 0 and abs(y - avg) / avg > 0.5:
                    warnings.append(
                        f"品种 '{vname}' 跨 study 产量波动 >50%: "
                        f"{[round(y, 0) for y in yields]}"
                    )
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
