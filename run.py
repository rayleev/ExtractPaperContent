#!/usr/bin/env python3
"""
run.py — 论文结构化数据提取 CLI 入口（LangGraph Pipeline）

基于 LangGraph 的有向图流程：
  classify → filter → parse → extract_phase1 → extract_phase2
    → postprocess → geocode → validate → targeted_llm_validate

特性：
  - SQLite checkpoint 断点续跑
  - 多论文并发处理
  - 逐篇追加 CSV（实时查看进度）
  - 批次完成后生成验证报告 + 覆盖率统计
  - 分步执行（--step classify/parse/extract）+ 自动补全前置步骤
  - 步骤级注册表（记录每篇论文完成的最高步骤，避免重复处理）

Usage:
  python run.py                          # 运行完整流程（默认）
  python run.py --step classify          # 仅分类（快速检查分类结果）
  python run.py --step parse             # 分类 + 解析 PDF（自动补全分类）
  python run.py --step extract           # 完整流程（自动补全前置步骤）
  python run.py --paper "10.14168"       # 仅处理匹配 DOI/标题的论文
  python run.py --config path.yaml       # 指定配置文件
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
from src.clients.llm import LLMClient


def setup_logging(config):
    """配置日志（CLI 模式）。"""
    import os
    log_dir = config.log_path
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "extractor.log"
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("paper_extractor")


def main():
    parser = argparse.ArgumentParser(
        description="Paper structure extraction — LangGraph Pipeline"
    )
    parser.add_argument(
        "--step",
        choices=["search", "classify", "download", "parse", "extract", "all"],
        default="all",
        help="Pipeline step to run: search/classify/download/parse/extract/all",
    )
    parser.add_argument(
        "--paper",
        type=str,
        default=None,
        help="Process only papers matching this DOI or title substring",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml (default: auto-detect)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        default=False,
        help="Start HTTP API server (FastAPI + uvicorn)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="HTTP server port (default: 8000, used with --serve)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="HTTP server host (default: 0.0.0.0, used with --serve)",
    )
    args = parser.parse_args()

    # ── HTTP 服务模式 ──
    if args.serve:
        import uvicorn
        print(f"Starting Paper Extractor API server on {args.host}:{args.port}")
        print(f"Swagger UI: http://localhost:{args.port}/docs")
        uvicorn.run(
            "src.api.main:app",
            host=args.host,
            port=args.port,
            reload=False,
            log_level="info",
        )
        return

    # 将 --step 映射为 graph 的 stop_after 节点名
    # classify → 分类后停止, parse → 解析后停止, extract/all → 完整流程
    step_to_stop = {
        "search": "search",
        "classify": "classify",
        "download": "download",
        "parse": "parse",
        "extract": "",   # 完整流程
        "all": "",       # 完整流程
    }
    stop_after = step_to_stop.get(args.step, "")

    # ── 加载配置 & 初始化 ──
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)
    config.set_run_id()

    config.cache_path.mkdir(parents=True, exist_ok=True)
    config.parsed_path.mkdir(parents=True, exist_ok=True)
    config.log_path.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(config)
    logger.info(f"Pipeline starting (LangGraph) — step={args.step}")
    logger.info(f"Run ID: {config.run_id}")
    logger.info(f"Run dir: {config._run_path}")
    logger.info(f"Base dir: {config.base_dir}")
    logger.info(f"Crops: {config.extraction.crops}")
    logger.info(f"Extractable categories: {config.extraction.extractable_categories}")

    # ── 初始化客户端 ──
    llm = LLMClient(config.llm)

    # ── 发现论文 ──
    papers = discover_papers(config)
    if args.paper:
        papers = [
            p for p in papers
            if args.paper.lower() in (p.get("doi", "") + p.get("title", "")).lower()
        ]
        logger.info(f"Filtered to {len(papers)} papers matching '{args.paper}'")

    if not papers:
        logger.error("No papers found!")
        sys.exit(1)

    logger.info(f"Papers to process: {len(papers)}")

    # ── 初始化 MinerU（仅当有 PDF 需要解析时）──
    from src.clients.mineru import MinerUClient
    from src.core.geocoder import Geocoder
    from src.graph.batch import BatchOrchestrator

    mineru_client = None
    has_pdf = any(
        p.get("pdf_path") and not p.get("md_path")
        for p in papers
    )
    if has_pdf:
        mineru_client = MinerUClient(config.mineru)
        logger.info("MinerU client initialized for PDF parsing")

    # ── 初始化 Geocoder ──
    geocoder = Geocoder(config) if config.geocoding.enabled else None

    # ── 运行 BatchOrchestrator ──
    orchestrator = BatchOrchestrator(
        config=config,
        llm=llm,
        geocoder=geocoder,
        mineru_client=mineru_client,
        max_concurrent=config.concurrency.extract_workers,
        stop_after=stop_after,
    )

    stats = orchestrator.process_batch(papers=papers)

    # ── 汇总 ──
    logger.info("Pipeline complete!")
    logger.info(f"  Completed: {stats['completed']}")
    logger.info(f"  Failed: {stats['failed']}")
    logger.info(f"  Skipped: {stats['skipped']}")


if __name__ == "__main__":
    main()
