"""
BatchOrchestrator — 管理上万篇论文的批量处理。

功能：
  - 10 篇论文并发处理（可配置）
  - SQLite checkpoint 断点续跑
  - 逐篇追加输出到 CSV
  - 进度追踪和统计
"""

from __future__ import annotations
import csv
import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.clients.mineru import MinerUClient
from src.core.geocoder import Geocoder
from src.core.models import ExtractionResult
from src.graph.state import PaperState
from src.graph.graph import build_paper_graph

logger = logging.getLogger("paper_extractor")


class BatchOrchestrator:
    """批量论文处理编排器。"""

    def __init__(
        self,
        config: AppConfig,
        llm: LLMClient,
        geocoder: Geocoder,
        mineru_client: Optional[MinerUClient] = None,
        max_concurrent: int = 10,
    ):
        self.config = config
        self.llm = llm
        self.geocoder = geocoder
        self.mineru_client = mineru_client
        self.max_concurrent = max_concurrent

        # Checkpoint 路径
        self.checkpoint_path = str(config.cache_path / "langgraph_checkpoint.db")

        # 输出路径
        self.output_dir = config.extraction_path
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # CSV 写入锁
        self._csv_lock = threading.Lock()
        self._csv_headers_written = False

        # 统计
        self.stats = {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "started_at": None,
        }

    def process_batch(
        self,
        papers: List[dict],
        classifications: Optional[List[dict]] = None,
    ) -> dict:
        """
        批量处理论文列表。

        Args:
            papers: 论文列表，每个 dict 包含 paper_id, doi, title, year, journal,
                    pdf_path, md_path 等字段
            classifications: 已有的分类结果（可选，跳过 classify 步骤）

        Returns:
            统计摘要 dict
        """
        self.stats["total"] = len(papers)
        self.stats["started_at"] = datetime.now().isoformat()

        logger.info(
            f"BatchOrchestrator starting: {len(papers)} papers, "
            f"max_concurrent={self.max_concurrent}"
        )

        # 构建分类查找表
        cls_lookup = {}
        if classifications:
            for cls in classifications:
                cls_lookup[cls.get("paper_id", "")] = cls

        # 并发处理
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            futures = {}
            for paper in papers:
                pid = paper.get("paper_id", "")
                future = executor.submit(
                    self._process_one_paper,
                    paper,
                    cls_lookup.get(pid),
                )
                futures[future] = paper

            for future in as_completed(futures):
                paper = futures[future]
                pid = paper.get("paper_id", "")
                try:
                    result = future.result()
                    status = result.get("status", "unknown")

                    if status in ("validated_complete", "validated", "geocoded"):
                        self.stats["completed"] += 1
                        # 追加写入 CSV
                        self._append_to_csv(result, pid)
                    elif status == "skipped":
                        self.stats["skipped"] += 1
                    else:
                        self.stats["failed"] += 1

                    self._log_progress()

                except Exception as e:
                    logger.error(f"Paper {pid[:25]} failed: {e}")
                    self.stats["failed"] += 1
                    self._log_progress()

        self.stats["finished_at"] = datetime.now().isoformat()
        logger.info(
            f"Batch complete: {self.stats['completed']} ok, "
            f"{self.stats['failed']} failed, {self.stats['skipped']} skipped "
            f"(total {self.stats['total']})"
        )
        return self.stats

    def _process_one_paper(
        self,
        paper: dict,
        classification: Optional[dict],
    ) -> dict:
        """处理单篇论文（在线程内执行）。"""
        pid = paper.get("paper_id", "")

        # 构建初始状态
        initial_state: PaperState = {
            "paper_id": pid,
            "paper_meta": paper,
            "status": "pending",
            "errors": [],
        }

        # 如果有已存在的分类结果，注入并跳过 classify/filter
        if classification:
            initial_state["classification"] = classification
            category = classification.get("category", "")
            extractable_cats = self.config.extraction.extractable_categories
            initial_state["is_extractable"] = category in extractable_cats
            if not initial_state["is_extractable"]:
                initial_state["status"] = "skipped"
                return initial_state
            initial_state["status"] = "filtered"

        # 构建 graph（每篇论文独立的 graph 实例，共享 checkpoint）
        graph = build_paper_graph(
            config=self.config,
            llm=self.llm,
            mineru_client=self.mineru_client,
            geocoder=self.geocoder,
            checkpoint_path=self.checkpoint_path,
        )

        # 运行 graph
        thread_config = {
            "configurable": {
                "thread_id": pid,  # 每篇论文独立的 thread
            }
        }

        try:
            # 如果已有分类，从 parse 节点开始
            if classification:
                for event in graph.stream(
                    initial_state,
                    config=thread_config,
                    stream_mode="updates",
                ):
                    # 更新状态
                    for node_name, node_output in event.items():
                        if isinstance(node_output, dict):
                            initial_state.update(node_output)
            else:
                for event in graph.stream(
                    initial_state,
                    config=thread_config,
                    stream_mode="updates",
                ):
                    for node_name, node_output in event.items():
                        if isinstance(node_output, dict):
                            initial_state.update(node_output)

        except Exception as e:
            logger.error(f"  [{pid[:25]}] Graph execution failed: {e}")
            initial_state["status"] = "failed"
            initial_state["errors"] = initial_state.get("errors", []) + [
                {"node": "graph", "error": str(e), "timestamp": datetime.now().isoformat()}
            ]

        return initial_state

    def _append_to_csv(self, result: dict, paper_id: str):
        """将单篇论文的提取结果追加写入 CSV 文件。"""
        extraction = result.get("extraction", {})
        if not extraction:
            return

        try:
            model = ExtractionResult.model_validate(extraction)
            rows = model.to_flat_csv_rows(paper_id=paper_id)
        except Exception:
            # fallback: 直接从 dict 生成行
            rows = extraction.get("studies", [])

        if not rows:
            return

        csv_path = self.output_dir / "full_flat.csv"

        with self._csv_lock:
            file_exists = csv_path.exists() and csv_path.stat().st_size > 0

            with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
                if not file_exists or not self._csv_headers_written:
                    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                    writer.writeheader()
                    self._csv_headers_written = True

                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                for row in rows:
                    writer.writerow(row)

    def _log_progress(self):
        """记录处理进度。"""
        total = self.stats["total"]
        done = self.stats["completed"] + self.stats["failed"] + self.stats["skipped"]
        pct = done * 100 // max(total, 1)
        logger.info(
            f"  Progress: {done}/{total} ({pct}%) — "
            f"{self.stats['completed']} ok, {self.stats['failed']} fail, "
            f"{self.stats['skipped']} skip"
        )
