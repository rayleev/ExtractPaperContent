#!/usr/bin/env python3
"""
run.py — 论文结构化数据提取 CLI 入口

Pipeline 流程:
  1. classify  — LLM 分类（仅用元数据，无需 PDF 解析）
  2. filter    — 筛选：research_country=China + category 可提取
  3. parse     — MinerU 解析（仅解析通过筛选的论文 PDF）
  4. extract   — LLM 结构化提取 + 置信度验证

Usage:
  python run.py                       # 运行完整流程
  python run.py --step classify       # 仅分类（基于元数据）
  python run.py --step parse          # 分类+筛选+解析 PDF
  python run.py --step extract        # 分类+筛选+解析+提取
  python run.py --step all            # 完整流程（默认）
  python run.py --paper "10.14168"    # 仅处理匹配 DOI/标题的论文
"""

import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config
from src.core.loader import discover_papers
from src.core.pipeline import (
    step_parse,
    load_parsed_cache,
    load_classification_cache,
    load_extraction_cache,
)
from src.core.classifier import classify_papers, filter_papers
from src.core.extractor import extract_papers
from src.core.geocoder import Geocoder, geocode_extractions
from src.core.models import ExtractionResult
from src.clients.llm import LLMClient
from src.output.writer import generate_outputs
from src.output.statistics import generate_statistics


def setup_logging(config):
    """配置日志。"""
    log_dir = config.log_path
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "extractor.log"
    logging.basicConfig(
        level=getattr(logging, "INFO"),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("paper_extractor")


def main():
    parser = argparse.ArgumentParser(
        description="Paper structure extraction pipeline"
    )
    parser.add_argument(
        "--step",
        choices=["parse", "classify", "extract", "all"],
        default="all",
        help="Pipeline step to run",
    )
    parser.add_argument(
        "--paper",
        type=str,
        default=None,
        help="Process only paper matching this DOI or title substring",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml (default: auto-detect)",
    )
    parser.add_argument(
        "--graph",
        action="store_true",
        default=False,
        help="Use LangGraph pipeline (supports checkpoint, resume, concurrent processing)",
    )
    args = parser.parse_args()

    # 加载配置 & 初始化 run_id
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)
    config.set_run_id()

    # 确保共享目录 + 当前 run 目录存在
    config.cache_path.mkdir(parents=True, exist_ok=True)
    config.parsed_path.mkdir(parents=True, exist_ok=True)
    config.log_path.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(config)
    logger.info(f"Pipeline starting -- step={args.step}, graph={args.graph}")
    logger.info(f"Run ID: {config.run_id}")
    logger.info(f"Run dir: {config._run_path}")
    logger.info(f"Base dir: {config.base_dir}")

    # 初始化客户端
    llm = LLMClient(config.llm)

    # ── LangGraph Pipeline ──
    if args.graph:
        _run_graph_pipeline(config, llm, logger, args)
        return

    # ── Legacy Pipeline (below) ──

    # 发现论文
    papers = discover_papers(config)
    if args.paper:
        papers = [
            p for p in papers
            if args.paper.lower() in (p["doi"] + p["title"]).lower()
        ]
        logger.info(f"Filtered to {len(papers)} papers matching '{args.paper}'")

    if not papers:
        logger.error("No papers found!")
        sys.exit(1)

    parsed = {}
    classifications = []
    filtered = []
    extractions = []

    # ── Step 1: 分类（仅用元数据，不需要 PDF 解析）──
    if args.step in ("classify", "parse", "extract", "all"):
        classifications = classify_papers(papers, config, llm)

    # ── Step 2: 筛选（China + 可提取类别）──
    if args.step in ("parse", "extract", "all") and classifications:
        filtered = filter_papers(classifications, config)
    elif args.step == "classify" and classifications:
        # 分类步骤也展示筛选结果供参考
        filtered = filter_papers(classifications, config)

    # ── Step 3: 解析（仅解析通过筛选的论文）──
    if args.step in ("parse", "extract", "all") and filtered:
        # 构建通过筛选的论文子集
        from src.core.loader import get_paper_key
        filtered_ids = {cls["paper_id"] for cls in filtered}
        papers_to_parse = [p for p in papers if get_paper_key(p) in filtered_ids]
        logger.info(f"Papers to parse: {len(papers_to_parse)} (filtered from {len(papers)})")
        parsed = step_parse(papers_to_parse, config, parsed_dir=config.parsed_path)
    elif args.step in ("extract",):
        parsed = load_parsed_cache(config)

    # ── Step 4: 提取 ──
    if args.step in ("extract", "all"):
        if filtered and parsed:
            extractions = extract_papers(
                papers, parsed, filtered, config, llm, parsed_dir=config.parsed_path
            )
        else:
            logger.warning("Missing filtered classifications or parsed data, skipping extraction")

    # ── Step 5: 后处理 — 地理编码（根据地名填充 lat/lon/alt）──
    # 仅在 extract/all 步骤且有提取结果时执行
    if args.step in ("extract", "all") and extractions and config.geocoding.enabled:
        geocoder = Geocoder(config)
        geocode_extractions(extractions, geocoder)
        # 更新缓存（geocoding 修改了 extraction dict 中的 study 字段）
        from src.core.extractor import _save_cache as _save_ext_cache
        cache_file = config.cache_path / "extraction_results.json"
        _save_ext_cache(cache_file, extractions)

    # ── 生成输出 ──
    if args.step in ("extract", "all"):
        # 提取/完整流程：生成分类 + 提取 + 置信度全部输出
        if classifications:
            if not extractions:
                extractions = load_extraction_cache(config)
            generate_outputs(
                classifications,
                extractions,
                classification_dir=config.classification_path,
                extraction_dir=config.extraction_path,
                confidence_dir=config.confidence_path,
            )
            # Step 6: 统计覆盖率分析
            if extractions:
                generate_statistics(extractions, config.statistics_path)
    else:
        # classify / parse 步骤：仅输出分类结果
        if classifications:
            from src.output.writer import _write_csv, _write_json
            config.classification_path.mkdir(parents=True, exist_ok=True)
            cls_csv = config.classification_path / "classification.csv"
            cls_fields = [
                "paper_id", "doi", "title", "language", "year", "journal",
                "category", "confidence", "reasoning", "key_signals",
                "crop_species", "paper_type", "has_yield_data", "research_country",
            ]
            _write_csv(cls_csv, classifications, cls_fields)
            logger.info(f"Classification CSV: {cls_csv}")
            cls_json = config.classification_path / "classification.json"
            _write_json(cls_json, classifications)
            logger.info(f"Classification JSON: {cls_json}")

    logger.info("Pipeline complete!")


def _run_graph_pipeline(config, llm, logger, args):
    """运行 LangGraph pipeline。"""
    from src.core.loader import discover_papers
    from src.core.classifier import classify_papers, filter_papers
    from src.core.geocoder import Geocoder
    from src.clients.mineru import MinerUClient
    from src.graph.batch import BatchOrchestrator
    from src.output.statistics import generate_statistics

    # 发现论文
    papers = discover_papers(config)
    if args.paper:
        papers = [
            p for p in papers
            if args.paper.lower() in (p["doi"] + p["title"]).lower()
        ]
        logger.info(f"Filtered to {len(papers)} papers matching '{args.paper}'")

    if not papers:
        logger.error("No papers found!")
        sys.exit(1)

    logger.info(f"Found {len(papers)} papers for LangGraph pipeline")

    # 分类（快速步骤，不走 graph）
    logger.info("Step 1: Classifying papers...")
    classifications = classify_papers(papers, config, llm)
    filtered = filter_papers(classifications, config)
    filtered_ids = {cls["paper_id"] for cls in filtered}

    # 构建通过筛选的论文子集
    from src.core.loader import get_paper_key
    extractable_papers = [p for p in papers if get_paper_key(p) in filtered_ids]
    logger.info(f"Extractable papers: {len(extractable_papers)} (filtered from {len(papers)})")

    # 初始化 MinerU（仅当有 PDF 需要解析时）
    mineru_client = None
    has_pdf = any(
        p.get("pdf_path") and not p.get("md_path")
        for p in extractable_papers
    )
    if has_pdf:
        mineru_client = MinerUClient(config.mineru)
        logger.info("MinerU client initialized for PDF parsing")

    # 初始化 Geocoder
    geocoder = Geocoder(config) if config.geocoding.enabled else None

    # 初始化 BatchOrchestrator
    orchestrator = BatchOrchestrator(
        config=config,
        llm=llm,
        geocoder=geocoder,
        mineru_client=mineru_client,
        max_concurrent=config.concurrency.extract_workers,
    )

    # 运行
    logger.info("Step 2: Running LangGraph batch processing...")
    stats = orchestrator.process_batch(
        papers=extractable_papers,
        classifications=classifications,
    )

    # 输出统计
    logger.info(f"LangGraph pipeline complete!")
    logger.info(f"  Completed: {stats['completed']}")
    logger.info(f"  Failed: {stats['failed']}")
    logger.info(f"  Skipped: {stats['skipped']}")

    # 生成覆盖率统计（从输出的 CSV 读取）
    csv_path = config.extraction_path / "full_flat.csv"
    if csv_path.exists():
        logger.info(f"Output CSV: {csv_path}")


if __name__ == "__main__":
    main()
