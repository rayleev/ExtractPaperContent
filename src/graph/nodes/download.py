"""
下载节点 — 从 Semantic Scholar 下载论文 PDF。

仅对通过分类筛选的论文下载。下载路径按年份组织：
  {base_dir}/docs/PDF/{year}/{paper_id}.pdf

下载失败时不中断 pipeline，设置 pdf_missing 标记并路由到 END。
"""

from __future__ import annotations
import logging
from pathlib import Path

from src.config import AppConfig
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")


def download_node(state: PaperState, config: AppConfig, ss_client) -> dict:
    """
    下载论文 PDF。

    优先使用本地缓存（PDF 已存在时跳过下载）。
    下载失败时返回 status="no_pdf"，不抛出异常。

    Args:
        state: 当前论文状态。
        config: 全局配置。
        ss_client: SemanticScholarClient 实例。

    Returns:
        更新后的状态字段字典。
    """
    paper_id = state["paper_id"]
    paper_meta = state.get("paper_meta", {})

    # ── 解析 paper identifier（paperId 或 DOI）──
    s2_paper_id = (
        paper_meta.get("paperId")
        or paper_meta.get("paper_id")
        or ""
    )
    doi = paper_meta.get("doi", "")

    # 用于 API 调用的标识符
    download_id = s2_paper_id
    if not download_id and doi:
        download_id = f"doi:{doi}"

    if not download_id:
        msg = f"No paperId or DOI available for {paper_id}, cannot download PDF"
        logger.warning(f"  [{paper_id[:25]}] {msg}")
        return {
            "paper_meta": paper_meta,
            "pdf_missing": True,
            "status": "no_pdf",
            "errors": state.get("errors", []) + [{
                "node": "download",
                "error": msg,
            }],
        }

    # ── 确定保存路径 ──
    year = (
        paper_meta.get("publicationYear")
        or paper_meta.get("publication_year")
        or paper_meta.get("year")
        or ""
    )
    year_str = str(year) if year else "unknown"
    save_path = config.pdf_path / year_str / f"{paper_id}.pdf"

    # ── 已存在则跳过 ──
    if save_path.exists():
        logger.debug(f"  [{paper_id[:25]}] PDF already exists: {save_path}")
        paper_meta["pdf_path"] = str(save_path)
        return {"paper_meta": paper_meta}

    # ── 下载 ──
    try:
        success = ss_client.download_pdf(download_id, save_path)
    except Exception as e:
        logger.error(f"  [{paper_id[:25]}] PDF download exception: {e}")
        success = False

    if success:
        logger.info(f"  [{paper_id[:25]}] PDF downloaded: {save_path}")
        paper_meta["pdf_path"] = str(save_path)
        return {
            "paper_meta": paper_meta,
            "status": state.get("status", "downloaded"),
        }

    # ── 下载失败 ──
    msg = f"PDF download failed for {paper_id} (id={download_id})"
    logger.warning(f"  [{paper_id[:25]}] {msg}")
    return {
        "paper_meta": paper_meta,
        "pdf_missing": True,
        "status": "no_pdf",
        "errors": state.get("errors", []) + [{
            "node": "download",
            "error": msg,
        }],
    }
