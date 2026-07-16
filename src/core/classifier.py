"""
论文分类器 — 使用 LLM 按细化标准对论文进行五类分类。
"""

import json
import logging
import threading
from pathlib import Path
from typing import List
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.core.loader import get_paper_key

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_classify_prompt() -> str:
    """加载分类 prompt 模板。"""
    path = PROMPT_DIR / "classify.txt"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def classify_papers(
    papers: List[dict],
    config: AppConfig,
    llm: LLMClient,
) -> List[dict]:
    """
    对每篇论文进行 LLM 分类（仅使用元数据，无需解析文本），返回分类结果列表。
    支持缓存和并发：已分类的论文不会重复处理。
    """
    cache_file = config.cache_path / "classification_results.json"
    cached = _load_cache(cache_file)
    prompt_template = load_classify_prompt()

    # 检测过时缓存（paper_id 因时间戳每次不同，旧缓存无效）
    current_pids = {get_paper_key(p) for p in papers}
    stale_keys = [k for k in cached if k not in current_pids]
    if stale_keys:
        logger.info(f"Discarding {len(stale_keys)} stale cached classifications (paper_id changed)")
        for k in stale_keys:
            del cached[k]

    results = list(cached.values())
    cache_lock = threading.Lock()
    workers = config.concurrency.classify_workers

    # 过滤出需要分类的论文
    to_classify = []
    for paper in papers:
        pid = get_paper_key(paper)
        if pid not in cached:
            to_classify.append(paper)
        else:
            logger.info(f"SKIP (cached): {paper['title'][:50]}")

    if not to_classify:
        logger.info(f"Classification complete: 0 new, {len(results)} total")
        return results

    logger.info(f"Classifying {len(to_classify)} papers (workers={workers})...")

    def _classify_one(paper: dict) -> dict:
        """分类单篇论文（线程内执行）。"""
        pid = get_paper_key(paper)
        prompt = prompt_template.format(
            title=paper["title"],
            abstract=paper["abstract"][:2000],
            keywords=paper["keywords"],
            journal=paper["journal"],
            language="中文" if paper["language"] == "zh" else "English",
        )
        result = llm.call_json(prompt, max_tokens=1000)
        if result is None:
            result = {
                "category": "unclear",
                "confidence": 0.0,
                "reasoning": "LLM classification failed",
            }

        record = {
            "paper_id": pid,
            "doi": paper["doi"],
            "title": paper["title"],
            "language": paper["language"],
            "year": paper["year"],
            "journal": paper["journal"],
            **result,
        }
        logger.info(f"  Classified: {paper['title'][:50]} → {record.get('category')}")
        return record

    new_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_classify_one, p): p for p in to_classify}
        for future in as_completed(futures):
            try:
                record = future.result()
                with cache_lock:
                    results.append(record)
                    new_count += 1
                    _save_cache(cache_file, results)
            except Exception as e:
                paper = futures[future]
                logger.error(f"  Classification failed: {paper['title'][:50]}: {e}")

    cats = Counter(r.get("category", "unknown") for r in results)
    logger.info(f"Classification complete: {new_count} new, {len(results)} total")
    logger.info(f"  Distribution: {dict(cats)}")
    return results


def filter_papers(
    classifications: List[dict],
    config: AppConfig,
) -> List[dict]:
    """
    根据 research_country 和 category 筛选可提取的论文。
    规则：research_country == China 且 category 在 extractable_categories 中。
    返回通过筛选的分类结果列表（含所有已分类论文，但日志标记哪些通过）。
    """
    extractable_cats = set(config.extraction.extractable_categories)
    passed = []
    skipped_reasons = Counter()

    for cls in classifications:
        country = cls.get("research_country", "Unknown")
        category = cls.get("category", "unknown")
        pid = cls.get("paper_id", "")
        title = cls.get("title", "")[:50]

        # 先检查国家
        if country != "China":
            skipped_reasons[f"non-China ({country})"] += 1
            logger.info(f"  SKIP [{pid[:30]}] research_country={country}: {title}")
            continue

        # 再检查分类
        if category not in extractable_cats:
            skipped_reasons[f"category={category}"] += 1
            logger.info(f"  SKIP [{pid[:30]}] category={category}: {title}")
            continue

        passed.append(cls)

    logger.info(f"Filter result: {len(passed)} passed out of {len(classifications)}")
    if skipped_reasons:
        logger.info(f"  Skipped reasons: {dict(skipped_reasons)}")
    return passed


def _load_cache(cache_file: Path) -> dict:
    """加载分类缓存。"""
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            cached_list = json.load(f)
            cached = {r["paper_id"]: r for r in cached_list}
        logger.info(f"Loaded {len(cached)} cached classifications")
        return cached
    return {}


def _save_cache(cache_file: Path, results: list):
    """保存分类缓存。"""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
