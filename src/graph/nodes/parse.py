"""
解析节点 — 获取论文 Markdown 全文并构建文档树。

优先级：
  1. docs/ 下预置的 MD 文件
  2. output/parsed/ 下已有的解析结果（按标题模糊匹配）
  3. MinerU OCR 解析 PDF
"""

from __future__ import annotations
import logging
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

from src.config import AppConfig
from src.clients.mineru import MinerUClient
from src.core.chunker import (
    build_document_tree,
    get_section_outline,
    find_nodes_by_type,
)
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")


def parse_node(
    state: PaperState,
    config: AppConfig,
    mineru_client: Optional[MinerUClient],
) -> dict:
    """
    解析节点：获取论文全文并构建文档树。

    返回 parsed_text（MD 全文）、tree_outline（标题大纲）、
    abstract_text（摘要）、methods_text（方法部分）。
    """
    paper_meta = state["paper_meta"]
    pid = state["paper_id"]
    md_text = None

    # ── 优先级 1: docs/ 下预置的 MD 文件 ──
    md_path = paper_meta.get("md_path")
    if md_path and Path(md_path).exists():
        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
        logger.info(f"  [{pid[:25]}] Read MD: {Path(md_path).name}")

    # ── 优先级 2: 已有解析结果（按标题子串匹配）──
    if not md_text:
        title = paper_meta.get("title", "")
        if title:
            title_norm = re.sub(r'\s+', '', title)
            parsed_dir = config.parsed_path
            if parsed_dir.exists():
                for md_file in parsed_dir.glob("*.md"):
                    if "_chunks" in md_file.stem:
                        continue
                    try:
                        with open(md_file, "r", encoding="utf-8") as f:
                            head = f.read(2000)
                        head_norm = re.sub(r'\s+', '', head)
                        if len(title_norm) >= 5 and title_norm in head_norm:
                            with open(md_file, "r", encoding="utf-8") as f:
                                md_text = f.read()
                            logger.info(f"  [{pid[:25]}] Reused MD: {md_file.name}")
                            break
                    except Exception:
                        pass

    # ── 优先级 3: MinerU 解析 PDF ──
    if not md_text and mineru_client:
        pdf_path = paper_meta.get("pdf_path")
        if pdf_path and Path(pdf_path).exists():
            logger.info(f"  [{pid[:25]}] Parsing PDF via MinerU...")
            md_text = mineru_client.parse_pdf(Path(pdf_path))
            if md_text:
                # 保存到 parsed 目录供后续复用
                safe_name = pid.replace("/", "_").replace(":", "_").replace("\\", "_")
                save_path = config.parsed_path / f"{safe_name}.md"
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(md_text)
        elif pdf_path:
            logger.warning(f"  [{pid[:25]}] PDF not found at: {pdf_path}")
        else:
            logger.warning(f"  [{pid[:25]}] No pdf_path in paper_meta, cannot parse")
    elif not md_text and not mineru_client:
        logger.warning(f"  [{pid[:25]}] MinerU client not available, skipping PDF parse")

    if not md_text:
        logger.warning(f"  [{pid[:25]}] Parse failed: no text available")
        return {
            "errors": state.get("errors", []) + [
                {"node": "parse", "error": "No text available", "timestamp": datetime.now().isoformat()}
            ],
            "status": "failed",
        }

    # ── 构建文档树 + 收集各部分文本 ──
    tree = build_document_tree(md_text)
    outline = get_section_outline(tree, max_level=3)

    abstract_nodes = find_nodes_by_type(tree, "abstract")
    abstract_text = "\n\n".join(
        n.content.strip() for n in abstract_nodes if n.content.strip()
    ) or "(摘要未识别)"

    methods_nodes = find_nodes_by_type(tree, "methods")
    methods_text = "\n\n".join(
        n.content.strip() for n in methods_nodes if n.content.strip()
    ) or "(方法部分未识别)"

    return {
        "parsed_text": md_text,
        "tree_outline": outline,
        "abstract_text": abstract_text[:5000],
        "methods_text": methods_text[:15000],
        "status": "parsed",
    }
