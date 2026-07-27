"""
搜索节点 — 调用 Semantic Scholar 代理 API 批量搜索论文。

这是一个独立函数（非 LangGraph 图节点），由 API 路由或 CLI 在
启动逐篇论文处理流程之前调用。搜索结果直接写入 paper_status 表
（ON CONFLICT DO NOTHING 天然去重），后续处理从 DB 分块拉取。

工作流程：
  1. 从配置读取搜索关键词和年份范围
  2. 逐关键词调用 Semantic Scholar bulk search API
  3. 合并所有结果并按 paperId 去重
  4. 生成稳定 paper_id，写入 paper_status 表（幂等）
  5. 返回搜索统计和状态
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

from src.clients.semantic_scholar import SemanticScholarClient, DEFAULT_SEARCH_FIELDS
from src.config import AppConfig
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")


def _make_paper_id(title: str) -> str:
    """
    根据论文标题生成稳定的 paper_id（与 loader.py 逻辑一致）。

    格式: P_{MD5(去空格小写标题)[:10]}
    同一篇论文无论被哪个实例搜索到，paper_id 都相同。
    """
    normalized = re.sub(r'\s+', '', title).lower()
    fingerprint = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:10]
    return f"P_{fingerprint}"


def _convert_ss_paper(sp: dict) -> dict:
    """将 Semantic Scholar API 返回的论文记录转换为内部 paper dict 格式。"""
    title = sp.get("title", "")
    # SS API journal 字段可能是字符串或对象 {name, volume, pages}
    journal_raw = sp.get("journal", "")
    if isinstance(journal_raw, dict):
        journal_raw = journal_raw.get("name", "")
    return {
        "paper_id": _make_paper_id(title),
        "ss_paper_id": sp.get("paperId", ""),
        "doi": sp.get("doi", ""),
        "title": title,
        "abstract": sp.get("abstract", ""),
        "keywords": sp.get("keywords", ""),
        "publication_year": str(sp.get("publication_year") or ""),
        "journal": journal_raw or "",
        "language": "en",
    }


def search_node(
    state: PaperState,
    config: AppConfig,
    ss_client: SemanticScholarClient,
    db_conn=None,
    limit: Optional[int] = None,
) -> dict:
    """
    批量搜索论文并写入 paper_status 表（独立函数，非 LangGraph 图节点）。

    从 config.extraction.search_keywords 读取搜索关键词列表，
    从 config.extraction.search_year_range 读取可选年份范围，
    逐关键词调用 Semantic Scholar bulk search，合并去重后写入 DB。

    Args:
        state: 当前 PaperState（搜索阶段基本为空）。
        config: 应用配置，包含搜索关键词和输出路径等。
        ss_client: Semantic Scholar API 客户端实例。
        db_conn: psycopg2 连接对象。搜索结果写入 paper_status 表。
        limit: 跨关键词的总量上限（按已收集数量扣减剩余配额）。None 不限制。

    Returns:
        状态更新字典：
        - search_total: 搜索到的论文总数 (int)
        - search_new: 新入库的论文数 (int)
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
    all_papers: list[dict] = []
    seen_ids: set[str] = set()

    for kw in keywords:
        # limit 是总量上限：按已收集数量扣减剩余配额
        kw_limit = (limit - len(all_papers)) if limit else None
        if kw_limit is not None and kw_limit <= 0:
            logger.info(f"  已达 limit={limit}，跳过剩余关键词")
            break

        logger.info(f"  搜索关键词: \"{kw}\"" + (f" (剩余配额 {kw_limit})" if kw_limit else ""))
        try:
            papers = ss_client.search_all(
                kw, DEFAULT_SEARCH_FIELDS, year=year_param, limit=kw_limit,
            )
        except Exception as e:
            logger.error(f"  搜索关键词 \"{kw}\" 失败: {e}")
            papers = []

        for sp in papers:
            pid = sp.get("paperId")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_papers.append(_convert_ss_paper(sp))

        logger.info(
            f"  关键词 \"{kw}\": 返回 {len(papers)} 条, "
            f"去重后累计 {len(all_papers)} 条"
        )

    total = len(all_papers)

    # ── 处理空结果 ──
    if total == 0:
        logger.warning("搜索未返回任何结果，请检查关键词或 API 连通性")
        return {
            "search_total": 0,
            "search_new": 0,
            "status": "search_empty",
        }

    # ── 写入 paper_status 表（幂等去重）──
    search_new = 0
    if db_conn is not None:
        from src.graph.output import insert_search_results
        search_new = insert_search_results(db_conn, all_papers)
        logger.info(
            f"搜索完成: 共 {total} 篇去重论文, "
            f"新入库 {search_new} 篇, 已存在 {total - search_new} 篇"
        )
    else:
        logger.warning("未提供数据库连接，搜索结果未持久化")

    return {
        "search_total": total,
        "search_new": search_new,
        "status": "searched",
    }


def import_papers_by_ids(
    ss_paper_ids: list,
    config: AppConfig,
    ss_client: SemanticScholarClient,
    db_conn=None,
    batch_size: int = 500,
) -> dict:
    """
    按 Semantic Scholar paperId 批量导入论文到 paper_status 表（独立函数）。

    与搜索不同，导入已知晓确切的论文 ID，因此直接按 ID 拉取元数据，
    无需关键词搜索。拉取到的论文以 status='pending' 写入 paper_status 表
    （ON CONFLICT DO NOTHING 幂等去重），后续走 process 步骤处理。

    Args:
        ss_paper_ids: SS paperId 列表（可含重复/空值，内部会清洗去重）。
        config: 应用配置。
        ss_client: Semantic Scholar API 客户端实例。
        db_conn: psycopg2 连接对象。
        batch_size: 每批拉取的 ID 数量（默认 500，SS batch 端点上限）。

    Returns:
        导入统计字典：
        - total: 去重后的 ID 总数 (int)
        - fetched: 成功拉到元数据的数量 (int)
        - new: 新入库的数量 (int)
        - existed: 已存在被跳过的数量 (int)
        - failed: 未查到元数据的 ID 数量 (int)
        - failed_ids: 未查到的 paperId 列表 (list)
    """
    # ── 清洗 + 去重（保持顺序）──
    seen: set = set()
    unique_ids: list = []
    for raw in ss_paper_ids:
        pid = (raw or "").strip()
        if pid and pid not in seen:
            seen.add(pid)
            unique_ids.append(pid)

    total = len(unique_ids)
    logger.info(f"开始按 ID 导入: {total} 个 SS paperId (batch_size={batch_size})")

    all_papers: list = []
    failed_ids: list = []

    # ── 分批拉取元数据 ──
    for start in range(0, total, batch_size):
        batch = unique_ids[start:start + batch_size]
        batch_num = start // batch_size + 1
        logger.info(f"  批次 {batch_num}: 拉取 {len(batch)} 个 ID 的元数据")
        try:
            results = ss_client.get_papers_batch(batch, DEFAULT_SEARCH_FIELDS)
        except Exception as e:
            logger.error(f"  批次 {batch_num} 拉取失败: {e}")
            results = []

        found = {sp.get("paperId"): sp for sp in results if sp.get("paperId")}
        for bid in batch:
            if bid in found:
                all_papers.append(_convert_ss_paper(found[bid]))
            else:
                failed_ids.append(bid)

        logger.info(
            f"  批次 {batch_num}: 命中 {len(found)} 个, "
            f"累计 {len(all_papers)} 个, 未查到 {len(failed_ids)} 个"
        )

    # ── title 为空的论文用 ss_paper_id 生成 paper_id 兜底 ──
    # （否则所有空标题论文会共用同一个 MD5，互相覆盖）
    for p in all_papers:
        if not p.get("title"):
            ss_pid = p.get("ss_paper_id", "")
            p["paper_id"] = f"P_{hashlib.md5(ss_pid.encode('utf-8')).hexdigest()[:10]}"

    # ── 写入 paper_status 表（幂等去重，status='pending'）──
    new = 0
    if db_conn is not None and all_papers:
        from src.graph.output import insert_search_results
        new = insert_search_results(db_conn, all_papers)
        logger.info(
            f"导入完成: 共 {len(all_papers)} 篇, 新入库 {new} 篇, "
            f"已存在 {len(all_papers) - new} 篇, 未查到 {len(failed_ids)} 个 ID"
        )
    else:
        logger.warning("未提供数据库连接或无可导入论文，结果未持久化")

    return {
        "total": total,
        "fetched": len(all_papers),
        "new": new,
        "existed": len(all_papers) - new,
        "failed": len(failed_ids),
        "failed_ids": failed_ids,
    }
