"""
过滤节点 — 根据分类结果和国家信息判断论文是否可提取。

可提取条件：
  1. category 在 extractable_categories 配置列表中
  2. 国家粗筛（基于摘要的 research_country）：
     - 明确中国（含台湾）        → 通过
     - 不确定（Unknown/空）      → 放行，提取后由 postprocess 基于全文复核
     - 明确非中国               → 直接 skip（节省提取成本）

注：台湾（Taiwan/TW）视为中国；Unknown 论文不在本节点丢弃，
    待提取出 study.country 后再判断是否中国。
"""

from __future__ import annotations
import logging

from src.config import AppConfig
from src.graph.state import PaperState
from src.graph.country_utils import is_china, is_uncertain

logger = logging.getLogger("paper_extractor")


def filter_node(state: PaperState, config: AppConfig) -> dict:
    """过滤节点：判断论文是否满足提取条件。"""
    pid = state["paper_id"]
    cls = state.get("classification", {})
    category = cls.get("category", "")
    country = cls.get("research_country", "")

    # 从配置读取可提取的类别列表（支持 config.yaml 动态配置）
    extractable_categories = config.extraction.extractable_categories

    # ── 1. 类别筛选 ──
    if category not in extractable_categories:
        reason = f"category '{category}' not in extractable list"
        logger.info(f"  [{pid[:25]}] Filter SKIP: {reason}")
        return {
            "is_extractable": False,
            "status": "skipped",
            "errors": [{"node": "filter", "error": reason}],
        }

    # ── 3. 作物筛选（多作物支持）──
    crops = cls.get("crops", [])
    target_crops = config.extraction.crops  # 如 ["水稻/Rice", "玉米/Maize"]

    if crops and target_crops:
        # 标准化作物名称（去除空格）后比较
        def normalize_crop(crop):
            return crop.replace(" ", "").lower()

        normalized_targets = [normalize_crop(c) for c in target_crops]
        # 检查是否有交集（论文包含任一目标作物即放行）
        has_target = any(
            normalize_crop(crop) in normalized_targets
            for crop in crops
        )
        if not has_target:
            reason = f"crops {crops} not in target list {target_crops}"
            logger.info(f"  [{pid[:25]}] Filter SKIP: {reason}")
            return {
                "is_extractable": False,
                "status": "skipped",
                "errors": [{"node": "filter", "error": reason}],
            }

    if is_china(country):
        logger.info(f"  [{pid[:25]}] Filter PASS: category={category}, country={country}")
        return {"is_extractable": True, "status": "filtered"}

    if is_uncertain(country):
        # 不确定 → 放行，提取后基于全文 study.country 复核
        logger.info(
            f"  [{pid[:25]}] Filter PASS (country '{country or 'empty'}' uncertain, "
            f"will verify after extraction): category={category}"
        )
        return {"is_extractable": True, "status": "filtered"}

    # 明确非中国 → skip
    reason = f"country '{country}' not China"
    logger.info(f"  [{pid[:25]}] Filter SKIP: {reason}")
    return {
        "is_extractable": False,
        "status": "skipped",
        "errors": [{"node": "filter", "error": reason}],
    }
