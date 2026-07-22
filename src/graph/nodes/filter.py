"""
过滤节点 — 根据分类结果和国家信息判断论文是否可提取。

可提取条件：
  1. category 在 extractable_categories 配置列表中
  2. research_country 为 China 或空值
"""

from __future__ import annotations
import logging

from src.config import AppConfig
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")


def filter_node(state: PaperState, config: AppConfig) -> dict:
    """过滤节点：判断论文是否满足提取条件。"""
    pid = state["paper_id"]
    cls = state.get("classification", {})
    category = cls.get("category", "")
    country = cls.get("research_country", "")

    # 从配置读取可提取的类别列表（支持 config.yaml 动态配置）
    extractable_categories = config.extraction.extractable_categories
    is_extractable = (
        category in extractable_categories
        and country in ("China", "CN", "")
    )

    if is_extractable:
        logger.info(f"  [{pid[:25]}] Filter PASS: category={category}, country={country or 'N/A'}")
    else:
        reason = (
            f"category '{category}' not in extractable list"
            if category not in extractable_categories
            else f"country '{country}' not China"
        )
        logger.info(f"  [{pid[:25]}] Filter SKIP: {reason}")

    return {
        "is_extractable": is_extractable,
        "status": "filtered" if is_extractable else "skipped",
    }
