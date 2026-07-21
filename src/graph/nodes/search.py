"""
搜索节点 — 调用 Semantic Scholar 代理 API 批量搜索论文。

这是一个独立函数（非 LangGraph 图节点），由 BatchOrchestrator 在
启动逐篇论文处理流程之前调用。搜索产生的论文列表将作为后续
per-paper graph 的输入。

工作流程：
  1. 从配置读取搜索关键词和年份范围
  2. 逐关键词调用 Semantic Scholar bulk search API
  3. 合并所有结果并按 paperId 去重
  4. 将合并结果写入 CSV 文件
  5. 返回搜索结果列表和状态
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.clients.semantic_scholar import SemanticScholarClient, DEFAULT_SEARCH_FIELDS
from src.config import AppConfig
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")

# CSV 输出列定义
_CSV_COLUMNS = [
    "paperId", "title", "doi", "pmid", "pmcid",
    "abstract", "authors", "keywords",
    "publicationYear", "journal",
]


def _serialize_field(value) -> str:
    """
    将字段值序列化为 CSV 安全的字符串。

    - 列表/字典 → JSON 字符串
    - None → 空字符串
    - 其他 → 原样转 str
    """
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _build_csv_row(paper: dict) -> list[str]:
    """从一篇论文记录构建 CSV 行。"""
    return [_serialize_field(paper.get(col)) for col in _CSV_COLUMNS]


def _write_search_csv(results: list[dict], csv_path: Path) -> None:
    """
    将搜索结果写入 CSV 文件。

    Args:
        results: 去重后的论文记录列表。
        csv_path: CSV 输出路径。
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_COLUMNS)
        for paper in results:
            writer.writerow(_build_csv_row(paper))
    logger.info(f"搜索结果已写入 CSV: {csv_path} ({len(results)} 条记录)")


def search_node(
    state: PaperState,
    config: AppConfig,
    ss_client: SemanticScholarClient,
) -> dict:
    """
    批量搜索论文（独立函数，非 LangGraph 图节点）。

    从 config.extraction.search_keywords 读取搜索关键词列表，
    从 config.extraction.search_year_range 读取可选年份范围，
    逐关键词调用 Semantic Scholar bulk search，合并去重后写入 CSV。

    该函数由 BatchOrchestrator 在逐篇论文处理之前调用，
    产出的论文列表将驱动后续的 per-paper graph 流程。

    Args:
        state: 当前 PaperState（搜索阶段基本为空）。
        config: 应用配置，包含搜索关键词和输出路径等。
        ss_client: Semantic Scholar API 客户端实例。

    Returns:
        状态更新字典：
        - search_results: 去重后的论文记录列表 (list[dict])
        - search_total: 搜索到的论文总数 (int)
        - status: "searched" | "search_empty"
    """
    # ── 读取搜索参数 ──
    keywords: list[str] = getattr(
        config.extraction, "search_keywords",
        ["水稻产量", "rice yield"],
    )
    year_range: str = getattr(
        config.extraction, "search_year_range", "",
    )
    year_param: Optional[str] = year_range if year_range else None

    logger.info(
        f"开始搜索: keywords={keywords}, year_range={year_range or '(不限)'}"
    )

    # ── 逐关键词搜索并合并 ──
    all_results: list[dict] = []
    seen_ids: set[str] = set()

    for kw in keywords:
        logger.info(f"  搜索关键词: \"{kw}\"")
        try:
            papers = ss_client.search_all(
                kw, DEFAULT_SEARCH_FIELDS, year=year_param,
            )
        except Exception as e:
            logger.error(f"  搜索关键词 \"{kw}\" 失败: {e}")
            papers = []

        for paper in papers:
            pid = paper.get("paperId")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_results.append(paper)

        logger.info(
            f"  关键词 \"{kw}\": 返回 {len(papers)} 条, "
            f"去重后累计 {len(all_results)} 条"
        )

    total = len(all_results)

    # ── 处理空结果 ──
    if total == 0:
        logger.warning("搜索未返回任何结果，请检查关键词或 API 连通性")
        return {
            "search_results": [],
            "search_total": 0,
            "status": "search_empty",
        }

    # ── 写入 CSV ──
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = config.meta_path / f"search_results_{timestamp}.csv"
    _write_search_csv(all_results, csv_path)

    logger.info(f"搜索完成: 共 {total} 篇去重论文, CSV → {csv_path}")

    return {
        "search_results": all_results,
        "search_total": total,
        "status": "searched",
    }
