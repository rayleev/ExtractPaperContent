"""
Phase 2 提取节点 — 逐试验章节提取 study + variety 数据。

对每个试验章节独立调用 LLM，传入章节完整内容和 Phase 1 识别的上下文。
多次 LLM 调用串行执行（受 LLM 限流约束），是整条 pipeline 的主要耗时点。
"""

from __future__ import annotations
import logging
from pathlib import Path

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.core.chunker import (
    build_document_tree,
    collect_content,
    find_experiment_sections,
)
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_phase2_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """
    Phase 2 提取：逐试验章节提取品种产量数据。

    每个 experiment section 独立调用 LLM，传入章节全文 + Phase 1 上下文。
    输出 studies 列表，每个 study 包含 varieties 数组。
    """
    pid = state["paper_id"]
    paper_meta = state["paper_meta"]
    phase1 = state.get("phase1_result", {})
    experiment_sections = phase1.get("experiment_sections", [])
    md_text = state.get("parsed_text", "")

    prompt_template = _load_prompt("extract_study.txt")

    # 重新构建文档树以获取实际的实验章节节点
    tree = build_document_tree(md_text)
    actual_exp_sections = find_experiment_sections(tree)

    logger.info(
        f"  [{pid[:25]}] Phase 2: {len(actual_exp_sections)} experiment sections, "
        f"{len(experiment_sections)} from Phase 1"
    )

    studies = []
    for i, exp_node in enumerate(actual_exp_sections):
        # 从 Phase 1 获取该试验的上下文（标题、年份、地点）
        study_context = ""
        if i < len(experiment_sections):
            es = experiment_sections[i]
            study_context = (
                f"章节标题: {es.get('section_title', exp_node.title)}\n"
                f"试验名称: {es.get('study_title', '')}\n"
                f"试验年份: {es.get('trial_year', '')}\n"
                f"试验地点: {es.get('site_description', '')}"
            )
        else:
            study_context = f"章节标题: {exp_node.title}"

        # 收集章节内容（子节点递归收集）
        section_content = collect_content(exp_node)
        max_content = config.extraction.max_text_chars
        if len(section_content) > max_content:
            section_content = section_content[:max_content] + "\n\n[...TRUNCATED...]"

        prompt = prompt_template.format(
            paper_id=pid,
            doi=paper_meta.get("doi", ""),
            title=paper_meta.get("title", ""),
            year=paper_meta.get("year", ""),
            study_context=study_context,
            section_content=section_content,
        )

        # LLM 调用 — 这是主要耗时步骤
        max_tokens = max(config.llm.max_tokens, 8192)
        study_data = llm.call_json(prompt, max_tokens=max_tokens)

        if study_data:
            studies.append(study_data)
            n_varieties = len(study_data.get("varieties", []))
            logger.info(
                f"  [{pid[:25]}] Study {i+1}: '{exp_node.title[:40]}' → "
                f"{n_varieties} varieties"
            )
        else:
            logger.warning(
                f"  [{pid[:25]}] Study {i+1}: '{exp_node.title[:40]}' → FAILED"
            )

    return {
        "phase2_results": studies,
        "status": "phase2_done",
    }
