"""
Phase 1 提取节点 — 论文级元数据 + 试验章节识别。

输入：摘要 + 标题大纲 + 方法部分（精简上下文，不传全文）
输出：paper 字段 + experiment_sections 列表（为 Phase 2 做准备）
"""

from __future__ import annotations
import logging
from pathlib import Path

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_phase1_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """
    Phase 1 提取：从摘要、大纲和方法部分提取论文级信息。

    识别所有试验章节（一个年份×一个站点 = 一个试验），
    为 Phase 2 的逐章节提取提供上下文。
    """
    pid = state["paper_id"]
    paper_meta = state["paper_meta"]
    prompt_template = _load_prompt("extract_paper.txt")

    outline = state.get("tree_outline", "")
    abstract = state.get("abstract_text", "")[:5000]
    methods = state.get("methods_text", "")[:15000]

    prompt = prompt_template.format(
        paper_id=pid,
        doi=paper_meta.get("doi", ""),
        title=paper_meta.get("title", ""),
        year=paper_meta.get("year", ""),
        journal=paper_meta.get("journal", ""),
        outline=outline,
        abstract=abstract,
        methods=methods,
    )

    logger.info(f"  [{pid[:25]}] Phase1: prompt {len(prompt)} chars, calling LLM...")
    result = llm.call_json(prompt, max_tokens=config.llm.max_tokens)
    if not result:
        logger.warning(f"  [{pid[:25]}] Phase1 FAILED: LLM returned no result")
        return {
            "phase1_result": {"paper": {}, "experiment_sections": []},
            "status": "phase1_failed",
        }

    sections = result.get("experiment_sections", [])
    logger.info(f"  [{pid[:25]}] Phase1 done: {len(sections)} experiment section(s) identified")

    return {
        "phase1_result": result,
        "status": "phase1_done",
    }
