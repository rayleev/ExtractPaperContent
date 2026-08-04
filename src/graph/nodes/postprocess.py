"""
后处理节点 — Pydantic 验证 + 产量换算 + 数据清洗。

处理步骤（按顺序）：
  0. 国家复核（基于提取出的 study.country 判断是否中国，非中国 skip 不入库）
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
from src.graph.country_utils import infer_paper_country, is_china, is_uncertain
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
    phase1_studies = phase1.get("studies", [])

    # ── 合并 Phase 1（study 级字段）+ Phase 2（varieties）──
    # Phase 1 输出 study 级字段（study_title, trial_year, site 等）
    # Phase 2 输出 varieties 列表（品种级字段）
    combined_studies = []
    for i, phase1_study in enumerate(phase1_studies):
        # 复制 phase1 的 study 级字段
        study = dict(phase1_study)
        # 查找对应的 phase2 varieties
        phase2_varieties = []
        for phase2_item in phase2:
            if phase2_item.get("study_index") == i:
                phase2_varieties = phase2_item.get("varieties", [])
                break
        study["varieties"] = phase2_varieties
        combined_studies.append(study)

    # phase2 中可能有 phase1 未覆盖的 study（兜底）
    for phase2_item in phase2:
        idx = phase2_item.get("study_index", -1)
        if idx >= len(phase1_studies):
            combined_studies.append({
                "varieties": phase2_item.get("varieties", [])
            })

    # ── 去重：移除无效 study（空壳 study 或无 varieties）──
    # 当 Phase 2 产生幽灵 study（无 study_title 且无有效 varieties）时，在此剔除
    valid_studies = []
    for study in combined_studies:
        has_title = bool(study.get("study_title"))
        has_varieties = bool(study.get("varieties"))
        # 有 study_title 或有 varieties 的才算有效 study
        if has_title or has_varieties:
            valid_studies.append(study)
        else:
            logger.info(f"  [{pid[:25]}] Dedup: removing empty study (no title, no varieties)")

    if len(valid_studies) < len(combined_studies):
        logger.info(
            f"  [{pid[:25]}] Dedup: {len(combined_studies)} → {len(valid_studies)} studies "
            f"(removed {len(combined_studies) - len(valid_studies)} empty)"
        )

    combined = {
        "paper": paper_info,
        "studies": valid_studies,
    }

    # ── Step 0: 国家复核（提取后基于全文判断，仅放行中国论文）──
    # filter 阶段对 Unknown（不确定国家）论文放行，这里用提取出的 study.country
    # （兜底用行政区划）复核：
    #   中国（含台湾）        → study.country 归一化为 'CN'，继续后续处理
    #   非中国 / 无法确认     → status='skipped'，不写入结果表（batch 层标记 paper_status）
    is_cn, evidence = infer_paper_country(combined_studies)
    if not is_cn:
        if evidence == "undetermined":
            reason = "study country undetermined after extraction (no China signal in full text)"
        else:
            reason = f"study country '{evidence}' not China (judged from full text), extraction discarded"
        logger.info(f"  [{pid[:25]}] Postprocess SKIP (country): {reason}")
        return {
            "extraction": combined,
            "status": "skipped",
            "errors": state.get("errors", []) + [
                {"node": "country_judge", "error": reason}
            ],
        }

    # 中国论文：归一化 study.country 为 'CN'（台湾/空值也统一为 CN）
    for study in combined_studies:
        c = study.get("country")
        if is_china(c) or is_uncertain(c):
            study["country"] = "CN"
    logger.info(f"  [{pid[:25]}] Country check PASS ({evidence})")

    # ── Pydantic 验证 + 产量换算（yield_raw → yield_standard）──
    try:
        result = ExtractionResult.model_validate(combined)
        result.compute_standard_yields(config)  # 传入 config 以使用配置的换算表
        extraction = result.model_dump()
        n_studies = len(extraction.get("studies", []))
        n_varieties = sum(len(s.get("varieties", [])) for s in extraction.get("studies", []))
        logger.info(f"  [{pid[:25]}] Postprocess: validated {n_studies} studies, {n_varieties} varieties")
    except Exception as e:
        logger.warning(f"  [{pid[:25]}] Pydantic failed: {e}")
        extraction = combined

    # ── 后处理流水线（纯代码）──
    if "studies" in extraction:
        studies = extraction["studies"]
        before_count = len(studies)

        # 1. 多站点检测：site_name 含"、"的标记警告
        multi_site = 0
        for study in studies:
            site_name = study.get("experimental_site_name", "") or ""
            if "、" in site_name or ("和" in site_name and len(site_name) > 10):
                study["notes"] = ((study.get("notes") or "") + " [多站点警告]").strip()
                multi_site += 1

        # 2. 品种审定编号回填（同一品种名在不同 study 间共享 code）
        backfill_variety_codes(studies)

        # 3. 非大田试验过滤（盆栽、温室、单株计产等）
        filter_non_field_experiments(studies)
        after_field = len(studies)

        # 4. 无产量数据过滤（yield_raw_unit 为 % 或空）
        filter_no_yield_studies(studies)
        after_yield = len(studies)

        # 5. 试验地点信息一致性回填
        normalize_site_info(studies)

        removed_field = before_count - after_field
        removed_yield = after_field - after_yield
        if removed_field or removed_yield or multi_site:
            logger.info(
                f"  [{pid[:25]}] Filters: -{removed_field} non-field, "
                f"-{removed_yield} no-yield, {multi_site} multi-site warnings, "
                f"{len(studies)} studies remaining"
            )

    return {
        "extraction": extraction,
        "status": "postprocessed",
    }
