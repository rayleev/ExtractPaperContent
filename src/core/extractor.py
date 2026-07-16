"""
结构化数据提取器 — 从通过分类的论文中提取品种产量试验数据。
"""

import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.core.loader import get_paper_key
from src.core.models import ExtractionResult
from src.core.chunker import chunk_paper, build_extraction_context

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_extract_prompt() -> str:
    """加载提取 prompt 模板。"""
    path = PROMPT_DIR / "extract.txt"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_papers(
    papers: List[dict],
    parsed: dict,
    classifications: List[dict],
    config: AppConfig,
    llm: LLMClient,
    parsed_dir: Path | None = None,
) -> List[dict]:
    """
    对通过分类筛选的论文进行 LLM 结构化提取。
    支持缓存和并发。
    """
    cache_file = config.cache_path / "extraction_results.json"
    cached = _load_cache(cache_file)
    prompt_template = load_extract_prompt()
    cache_lock = threading.Lock()
    workers = config.concurrency.extract_workers

    paper_lookup = {get_paper_key(p): p for p in papers}
    extractable = {cls["paper_id"]: cls for cls in classifications}

    # 检测过时缓存（paper_id 因时间戳每次不同，旧缓存无效）
    current_pids = set(extractable.keys())
    stale_keys = [k for k in cached if k not in current_pids]
    if stale_keys:
        logger.info(f"Discarding {len(stale_keys)} stale cached extractions (paper_id changed)")
        for k in stale_keys:
            del cached[k]

    # 过滤出需要提取的论文
    to_extract = []
    for pid, cls in extractable.items():
        if pid in cached:
            logger.info(f"SKIP (cached): {cls['title'][:50]}")
            continue
        paper = paper_lookup.get(pid)
        md_text = parsed.get(pid)
        if not paper or not md_text:
            logger.warning(f"No paper/text for {pid}")
            continue
        to_extract.append((pid, cls, paper, md_text))

    results = list(cached.values())
    if not to_extract:
        logger.info(f"Extraction complete: 0 new, {len(results)} total")
        return results

    logger.info(
        f"Extracting {len(to_extract)} papers (workers={workers}, "
        f"from {len(classifications)} classified)"
    )

    def _extract_one(pid: str, cls: dict, paper: dict, md_text: str) -> dict:
        """提取单篇论文（线程内执行）。"""
        # 文档分块
        chunks = chunk_paper(md_text)
        focused_content = build_extraction_context(
            chunks, max_chars=config.extraction.max_text_chars
        )
        logger.info(
            f"  [{pid[:25]}] Content: {len(md_text)} → {len(focused_content)} chars"
        )

        # 保存分块结果
        if parsed_dir:
            safe_name = pid.replace("/", "_").replace(":", "_").replace("\\", "_")
            chunks_file = parsed_dir / f"{safe_name}_chunks.txt"
            with open(chunks_file, "w", encoding="utf-8") as cf:
                for section_type in ["abstract", "methods", "results", "tables"]:
                    text = chunks.get(section_type, "")
                    if text:
                        cf.write(f"{'='*60}\n")
                        cf.write(f"[{section_type.upper()}] ({len(text)} chars)\n")
                        cf.write(f"{'='*60}\n")
                        cf.write(text[:3000] + (
                            "\n...[truncated]..." if len(text) > 3000 else ""
                        ))
                        cf.write("\n\n")

        # LLM 提取
        json_schema = ExtractionResult.to_prompt_schema()
        prompt = prompt_template.format(
            paper_id=pid,
            doi=paper["doi"],
            title=paper["title"],
            year=paper["year"],
            journal=paper["journal"],
            content=focused_content,
            json_schema=json_schema,
        )
        raw_dict = llm.call_json(prompt, max_tokens=config.llm.max_tokens)

        # Pydantic 验证 + 后处理
        extraction = raw_dict
        if raw_dict:
            try:
                result = ExtractionResult.model_validate(raw_dict)
                result.compute_standard_yields()
                extraction = result.model_dump()
                logger.info(f"  [{pid[:25]}] Pydantic validation passed")
            except Exception as e:
                logger.warning(f"  [{pid[:25]}] Pydantic failed (using raw): {e}")

        # 多站点检测
        if extraction and "studies" in extraction:
            for study in extraction["studies"]:
                site_name = study.get("experimental_site_name", "") or ""
                if "、" in site_name or ("和" in site_name and len(site_name) > 10):
                    logger.warning(
                        f"  [{pid[:25]}] Multi-site: '{site_name}'"
                    )
                    study["notes"] = (study.get("notes", "") +
                                     " [多站点警告]").strip()

        # 置信度验证
        confidence_result = None
        if extraction:
            from src.core.validator import validate_extraction
            confidence_result = validate_extraction(paper, extraction, config, llm)

        return {
            "paper_id": pid,
            "doi": paper["doi"],
            "title": paper["title"],
            "language": paper["language"],
            "category": cls.get("category"),
            "extraction": extraction,
            "confidence": confidence_result,
            "extracted_at": datetime.now().isoformat(),
        }

    new_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_extract_one, pid, cls, paper, md): (pid, cls)
            for pid, cls, paper, md in to_extract
        }
        for future in as_completed(futures):
            pid, cls = futures[future]
            try:
                record = future.result()
                with cache_lock:
                    results.append(record)
                    new_count += 1
                    _save_cache(cache_file, results)
                logger.info(f"  Done: {cls['title'][:50]}")
            except Exception as e:
                logger.error(f"  Extract failed [{pid[:25]}]: {e}")

    _save_cache(cache_file, results)
    logger.info(f"Extraction complete: {new_count} new, {len(results)} total")
    return results


def _load_cache(cache_file: Path) -> dict:
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            cached_list = json.load(f)
            cached = {r["paper_id"]: r for r in cached_list}
        logger.info(f"Loaded {len(cached)} cached extractions")
        return cached
    return {}


def _save_cache(cache_file: Path, results: list):
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
