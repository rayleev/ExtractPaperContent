"""
Lookup 节点 — 处理 needs_lookup 的提取提示。

当 parse 节点识别出需要去其他位置查找的信息时（如代号需查全称、
补充材料需提取具体数值），此节点负责补充这些信息。

触发条件：parse 节点输出 needs_lookup = True
"""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def lookup_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """
    Lookup 节点：处理 needs_lookup 的提取提示。

    输入：
      - extraction_hints（parse 输出）
      - phase2_results（已提取的 study + variety 数据）
      - parsed_text（MD 全文）

    输出：
      - lookup_results: 补充后的信息列表
    """
    pid = state["paper_id"]
    extraction_hints = state.get("extraction_hints", [])
    phase2_results = state.get("phase2_results", [])
    md_text = state.get("parsed_text", "")

    # 筛选出 needs_lookup 的 hints
    lookup_hints = [h for h in extraction_hints if h.get("action") == "needs_lookup"]
    if not lookup_hints:
        logger.info(f"  [{pid[:25]}] Lookup: no needs_lookup hints, skipping")
        return {"lookup_results": [], "status": "lookup_skipped", "node_status": {"lookup": "lookup_skipped"}}

    if not needs_lookup:
        return {"lookup_results": [], "status": "lookup_skipped", "node_status": {"lookup": "lookup_skipped"}}

    logger.info(f"  [{pid[:25]}] Lookup: processing {len(lookup_hints)} hints")

    prompt_template = _load_prompt("lookup.txt")

    # 构建 lookup 上下文
    lookup_context = []
    for h in lookup_hints:
        lookup_context.append({
            "field": h.get("field", ""),
            "value": h.get("value", ""),
            "crop": h.get("crop", ""),  # ← 新增：按作物查找
            "reason": h.get("reason", ""),
            "location": h.get("location", {}),
        })

    prompt = prompt_template.format(
        paper_id=pid,
        lookup_context=lookup_context,
        phase2_results=phase2_results,
        md_text=md_text[:config.extraction.max_text_chars],
    )

    result = llm.call_json(prompt, max_tokens=config.llm.max_tokens, node_name="lookup")
    if not result:
        logger.warning(f"  [{pid[:25]}] Lookup FAILED: LLM returned no result (timeout/error)")
        # 记录提取错误，供 postprocess 区分"超时"和"无数据"
        if "extraction_errors" not in state:
            state["extraction_errors"] = []
        state["extraction_errors"].append({
            "study_index": -1,
            "study_title": "Lookup补充查找",
            "error": "LLM提取超时或无返回",
        })
        return {
            "lookup_results": [],
            "extraction_errors": state.get("extraction_errors", []),
            "status": "lookup_failed",
            "node_status": {"lookup": "lookup_failed"},
        }

    lookup_results = result.get("lookup_results", [])
    logger.info(f"  [{pid[:25]}] Lookup done: {len(lookup_results)} results")

    return {
        "lookup_results": lookup_results,
        "status": "lookup_done",
        "node_status": {"lookup": "lookup_done"},
    }
