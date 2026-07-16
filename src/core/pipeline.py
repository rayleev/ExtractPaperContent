"""
流程编排器 — 串联解析、分类、提取三个阶段的完整 pipeline。
"""

import json
import logging
import threading
from pathlib import Path
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import AppConfig
from src.clients.mineru import MinerUClient
from src.clients.llm import LLMClient
from src.core.loader import get_paper_key

logger = logging.getLogger("paper_extractor")


def step_parse(papers: List[dict], config: AppConfig, parsed_dir: Path | None = None) -> dict:
    """批量获取论文的 Markdown 文本，返回 {paper_key: markdown_text}。

    优先级:
      1. 缓存（parsed_pdfs.json）
      2. docs/ 下的 .md 文件（直接读取）
      3. PDF 文件（通过 MinerU 并发解析）
    """
    cache_file = config.cache_path / "parsed_pdfs.json"
    parsed = _load_json_cache(cache_file, "parsed PDFs")
    cache_lock = threading.Lock()
    workers = config.concurrency.parse_workers

    # 分离: 已缓存 / MD文件(快) / PDF文件(需要MinerU)
    pdf_papers = []
    for paper in papers:
        pid = get_paper_key(paper)
        name = paper.get("pdf_name", paper.get("title", "?")[:40])

        if pid in parsed:
            logger.info(f"SKIP (cached): {name}")
            continue

        md_path = paper.get("md_path")
        if md_path and Path(md_path).exists():
            logger.info(f"Reading MD: {name}")
            with open(md_path, "r", encoding="utf-8") as f:
                md = f.read()
            parsed[pid] = md
            continue

        pdf_path = paper.get("pdf_path")

        # Fallback: look for existing MD files in parsed dir matching by title
        # Build title→file index on first use, then match
        if pdf_path and not hasattr(step_parse, '_md_title_index'):
            parsed_dir = config.parsed_path
            if parsed_dir.exists():
                step_parse._md_title_index = {}
                for md_file in parsed_dir.glob("*.md"):
                    if "_chunks" in md_file.stem:
                        continue
                    try:
                        with open(md_file, "r", encoding="utf-8") as f:
                            head = f.read(2000)
                        step_parse._md_title_index[md_file.name] = head
                    except Exception:
                        pass

        if pdf_path and hasattr(step_parse, '_md_title_index'):
            paper_title = paper.get("title", "")
            if paper_title:
                # Normalize whitespace for matching (MinerU may add spaces)
                import re as _re
                title_norm = _re.sub(r'\s+', '', paper_title)
                for md_name, head_text in step_parse._md_title_index.items():
                    head_norm = _re.sub(r'\s+', '', head_text)
                    if len(title_norm) >= 5 and title_norm in head_norm:
                        md_file = config.parsed_path / md_name
                        logger.info(f"Reusing MD: {md_name} → {name}")
                        with open(md_file, "r", encoding="utf-8") as f:
                            parsed[pid] = f.read()
                        break
                if pid in parsed:
                    continue

        if pdf_path and Path(pdf_path).exists():
            pdf_papers.append(paper)
        else:
            logger.warning(f"No file found for: {name}")

    # 先保存 MD 结果
    if parsed:
        _save_json(cache_file, parsed)

    # 并发解析 PDF
    if pdf_papers:
        logger.info(f"Parsing {len(pdf_papers)} PDFs (workers={workers})...")
        client = MinerUClient(config.mineru)

        def _parse_one(paper: dict) -> tuple:
            """解析单篇 PDF（线程内执行），返回 (pid, md_text_or_None)。"""
            pid = get_paper_key(paper)
            name = paper.get("pdf_name", "?")
            logger.info(f"  Submitting: {name}")
            md = client.parse_pdf(Path(paper["pdf_path"]))

            # 保存 markdown 到磁盘
            if md and parsed_dir:
                safe_name = pid.replace("/", "_").replace(":", "_").replace("\\", "_")
                save_path = parsed_dir / f"{safe_name}.md"
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as mf:
                    mf.write(md)
                logger.info(f"  Parsed: {name} ({len(md)} chars) → {save_path.name}")

            return pid, md

        fail_count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_parse_one, p): p for p in pdf_papers}
            for future in as_completed(futures):
                try:
                    pid, md = future.result()
                    with cache_lock:
                        if md:
                            parsed[pid] = md
                        else:
                            parsed[pid] = None
                            fail_count += 1
                        _save_json(cache_file, parsed)
                except Exception as e:
                    paper = futures[future]
                    logger.error(f"  Parse failed: {paper.get('pdf_name', '?')}: {e}")
                    fail_count += 1

        logger.info(f"PDF parsing: {len(pdf_papers) - fail_count} ok, {fail_count} failed")

    return parsed


def load_parsed_cache(config: AppConfig) -> dict:
    """加载已解析的 PDF 缓存。如果 JSON 缓存不存在，从 output/parsed/*.md 自动重建。"""
    cache_file = config.cache_path / "parsed_pdfs.json"

    # 优先从 JSON 缓存加载
    if cache_file.exists():
        return _load_json_cache(cache_file, "parsed PDFs")

    # JSON 缓存不存在 → 尝试从 parsed 目录的 md 文件重建
    parsed_dir = config.parsed_path
    if not parsed_dir.exists():
        return {}

    md_files = list(parsed_dir.glob("*.md"))
    if not md_files:
        return {}

    parsed = {}
    for md_file in md_files:
        # 跳过 chunks 文件（分块输出，不是原始 markdown）
        if md_file.name.endswith("_chunks.md") or "_chunks" in md_file.stem:
            continue

        # 文件名格式: {doi_with_/_replaced_by__}.md
        # 还原 DOI: 将 _ 替换回 /（只替换最后一个 _ 之后的部分中的 _）
        stem = md_file.stem
        # 反向还原: 先用占位符保护已知前缀，再替换
        doi = stem.replace("_", "/")
        # 如果结果不像 DOI（不含 /），直接用文件名
        if "/" not in doi or not any(c.isdigit() for c in doi[:10]):
            doi = stem

        with open(md_file, "r", encoding="utf-8") as f:
            parsed[doi] = f.read()

    if parsed:
        _save_json(cache_file, parsed)
        logger.info(f"Rebuilt parsed cache from {len(parsed)} md files in {parsed_dir}")

    return parsed


def load_classification_cache(config: AppConfig) -> list:
    """加载分类结果缓存。"""
    cache_file = config.cache_path / "classification_results.json"
    return _load_json_cache(cache_file, "classifications", default=[])


def load_extraction_cache(config: AppConfig) -> list:
    """加载提取结果缓存。"""
    cache_file = config.cache_path / "extraction_results.json"
    return _load_json_cache(cache_file, "extractions", default=[])


def _load_json_cache(path: Path, label: str, default=None):
    """通用 JSON 缓存加载。"""
    if default is None:
        default = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = len(data)
        logger.info(f"Loaded {count} cached {label}")
        return data
    return default


def _save_json(path: Path, data):
    """通用 JSON 保存。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
