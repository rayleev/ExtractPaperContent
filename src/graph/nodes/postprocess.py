"""
后处理节点 — Pydantic 验证 + 产量换算 + 数据清洗。

处理步骤（按顺序）：
  1. Pydantic 模型校验 + 产量单位换算
  2. 多站点检测标记
  3. 品种审定编号回填
  4. 非大田试验过滤（盆栽/温室）
  5. 无产量数据过滤
  6. 试验地点信息一致性回填
"""

from __future__ import annotations
import logging

from src.config import AppConfig
from src.core.models import ExtractionResult
from src.graph.postprocess_utils import (
    filter_non_field_experiments,
    filter_no_yield_studies,
    normalize_site_info,
    backfill_variety_codes,
)
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")


def postprocess_node(state: PaperState, config: AppConfig) -> dict:
    """
    后处理节点：合并 Phase 1 + Phase 2 结果并执行数据清洗。

    所有步骤均为纯代码，不消耗 LLM token。
    """
    pid = state["paper_id"]
    phase1 = state.get("phase1_result", {})
    phase2 = state.get("phase2_results", [])

    paper_info = phase1.get("paper", {})
    combined = {
        "paper": paper_info,
        "studies": phase2,
    }

    # ── Pydantic 验证 + 产量换算（yield_raw → yield_standard）──
    try:
        result = ExtractionResult.model_validate(combined)
        result.compute_standard_yields()
        extraction = result.model_dump()
    except Exception as e:
        logger.warning(f"  [{pid[:25]}] Pydantic failed: {e}")
        extraction = combined

    # ── 后处理流水线（纯代码）──
    if "studies" in extraction:
        studies = extraction["studies"]

        # 1. 多站点检测：site_name 含"、"的标记警告
        for study in studies:
            site_name = study.get("experimental_site_name", "") or ""
            if "、" in site_name or ("和" in site_name and len(site_name) > 10):
                study["notes"] = ((study.get("notes") or "") + " [多站点警告]").strip()

        # 2. 品种审定编号回填（同一品种名在不同 study 间共享 code）
        backfill_variety_codes(studies)

        # 3. 非大田试验过滤（盆栽、温室、单株计产等）
        filter_non_field_experiments(studies)

        # 4. 无产量数据过滤（yield_raw_unit 为 % 或空）
        filter_no_yield_studies(studies)

        # 5. 试验地点信息一致性回填
        normalize_site_info(studies)

    return {
        "extraction": extraction,
        "status": "postprocessed",
    }
