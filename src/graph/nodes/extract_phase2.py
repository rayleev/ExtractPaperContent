"""
Phase 2 提取节点 — 品种级提取。

对每个试验独立调用 LLM，传入试验内容、Phase 1 识别的试验上下文、
parse 节点的提取提示（extraction_hints）。

策略：
  - 利用 extraction_hints 辅助品种名定位
  - needs_lookup 的品种需要去材料方法/补充表查找全称
  - 输出 varieties 列表（品种级字段）
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
    Phase 2 提取：品种级提取。

    对每个试验，提取品种产量数据（varieties 列表）。

    输入：
      - 试验内容（从文档树收集）
      - Phase 1 上下文（试验标题、年份、地点等）
      - parse 提取提示（品种名、位置、查找需求）

    输出：
      - phase2_results: 每个 study 的 varieties 列表
    """
    pid = state["paper_id"]
    paper_meta = state["paper_meta"]
    phase1 = state.get("phase1_result", {})
    studies = phase1.get("studies", [])
    md_text = state.get("parsed_text", "")

    # parse 输出（提取提示）
    doc_context = state.get("doc_context", {})
    extraction_hints = state.get("extraction_hints", [])

    prompt_template = _load_prompt("extract_study.txt")

    # 按论文类别注入提取指令：
    #   management_yield（管理/栽培措施型）→ 只提取对照组（CK/基准处理）数据
    #   其他类别（如 varietal_yield）       → 不注入额外指令，提取所有品种
    category = state.get("classification", {}).get("category", "")
    if category == "management_yield":
        category_instruction = _load_prompt("extract_study_management.txt")
        logger.info(f"  [{pid[:25]}] Phase 2: management_yield → extract control group only")
    else:
        category_instruction = ""

    # 构建文档树（复用 parse 节点的树结构，仅作为内容来源）
    tree = build_document_tree(md_text)
    actual_exp_sections = find_experiment_sections(tree)

    logger.info(
        f"  [{pid[:25]}] Phase 2: {len(actual_exp_sections)} experiment sections, "
        f"{len(studies)} from Phase 1, "
        f"{len(extraction_hints)} extraction hints"
    )

    # ── Phase 1 优先逻辑 ──
    # 当 Phase 1 成功识别 studies 时，以 Phase 1 的 studies 数量为准
    # actual_exp_sections 仅作为内容来源，不再决定 study 数量
    use_phase1_studies = len(studies) > 0
    if use_phase1_studies:
        logger.info(f"  [{pid[:25]}] Phase 2: using Phase 1 studies ({len(studies)}) as primary")
    else:
        logger.warning(
            f"  [{pid[:25]}] Phase 2: no Phase 1 studies, "
            f"falling back to document tree ({len(actual_exp_sections)} sections)"
        )

    # 构建提取提示文本（含上下文片段）
    if extraction_hints:
        hints_lines = []
        for h in extraction_hints:
            action = h.get("action", "?")
            field = h.get("field", "")
            value = h.get("value", "")
            context = h.get("context", "")
            reason = h.get("reason", "")
            hints_lines.append(f"- [{action}] {field}: {value}")
            if context:
                hints_lines.append(f"  上下文: {context}")
            if reason:
                hints_lines.append(f"  原因: {reason}")
        hints_text = "\n".join(hints_lines)
    else:
        hints_text = "(无提取提示)"

    # 补充材料信息
    has_supplementary = doc_context.get("has_supplementary", False)
    table_refs = doc_context.get("table_refs", [])
    variety_groups = doc_context.get("variety_groups", [])
    supplementary_info = ""
    if has_supplementary:
        supplementary_info = f"\n**补充材料**: 是（表格引用: {', '.join(table_refs) or '无'}）"
    elif table_refs:
        supplementary_info = f"\n**表格引用**: {', '.join(table_refs)}"

    # 共用品种信息
    shared_varieties_info = ""
    if variety_groups:
        shared_lines = []
        for group in variety_groups:
            group_name = group.get("group_name", "")
            shared = group.get("shared_across_studies", False)
            varieties = group.get("varieties", [])
            for v in varieties:
                vname = v.get("name", "")
                is_check = v.get("is_check", False)
                source_tables = v.get("source_tables", [])
                check_str = "（CK）" if is_check else ""
                tables_str = f" 出现在: {', '.join(source_tables)}" if source_tables else ""
                shared_str = " [共用]" if shared else ""
                shared_lines.append(f"- {vname}{check_str}{shared_str}{tables_str}")
        if shared_lines:
            shared_varieties_info = "\n\n## 共用品种（多个试验共用）\n" + "\n".join(shared_lines)

    phase2_results = []

    def _build_relevant_content_from_hints(hints: list, max_chars: int = 8000) -> str:
        """
        从 extraction_hints 构建相关内容文本。

        每个 hint 包含 context（字段出现的原文片段）和 source_tables。
        去重后拼接，作为 LLM 输入（替代整篇论文）。
        """
        if not hints:
            return ""

        seen_contexts = set()
        parts = []
        total_chars = 0

        for hint in hints:
            context = hint.get("context", "").strip()
            if not context or context in seen_contexts:
                continue
            seen_contexts.add(context)

            # 添加 source_tables 信息（如果有）
            source_tables = hint.get("source_tables", [])
            table_info = f" [来源: {', '.join(source_tables)}]" if source_tables else ""

            part = f"{context}{table_info}"
            if total_chars + len(part) > max_chars:
                break
            parts.append(part)
            total_chars += len(part)

        return "\n\n---\n\n".join(parts)

    def _find_best_section_for_study(study_index: int, study_title: str) -> str:
        """
        为指定 study 找到最匹配的实验章节内容。
        优先按 study_index 对应，否则按标题匹配。
        """
        # 优先按索引对应
        if study_index < len(actual_exp_sections):
            content = collect_content(actual_exp_sections[study_index])
            return content
        # 回退：按标题匹配
        for sec in actual_exp_sections:
            if study_title and study_title in sec.title:
                return collect_content(sec)
        # 最后回退：返回第一个 section 或全文
        if actual_exp_sections:
            return collect_content(actual_exp_sections[0])
        return md_text[:config.extraction.max_text_chars]

    if use_phase1_studies:
        # ── 以 Phase 1 studies 为准 ──
        logger.info(f"  [{pid[:25]}] Phase 2: processing {len(studies)} study/studies from Phase 1")
        for i, s in enumerate(studies):
            study_title = s.get("study_title", "")
            logger.info(f"  [{pid[:25]}] Study {i+1}/{len(studies)}: '{study_title[:50]}' → calling LLM...")
            study_context = (
                f"试验名称: {study_title}\n"
                f"试验年份: {s.get('trial_year', '')}\n"
                f"试验地点: {s.get('site_administrative_region', '')}\n"
                f"试验站: {s.get('experimental_site_name', '')}"
            )

            # 优先使用 hints 构建相关内容（比整篇论文小很多）
            relevant_content = _build_relevant_content_from_hints(extraction_hints)
            if len(relevant_content) >= 2000:
                # hints 提供足够覆盖，使用精简内容
                section_content = relevant_content
                logger.info(
                    f"  [{pid[:25]}] Study {i+1}: using hints-based content "
                    f"({len(relevant_content)} chars)"
                )
            else:
                # hints 覆盖不足，回退到完整章节内容
                section_content = _find_best_section_for_study(i, s.get("study_title", ""))
                max_content = config.extraction.max_text_chars
                if len(section_content) > max_content:
                    section_content = section_content[:max_content] + "\n\n[...TRUNCATED...]"
                logger.info(
                    f"  [{pid[:25]}] Study {i+1}: hints insufficient ({len(relevant_content)} chars), "
                    f"using section content ({len(section_content)} chars)"
                )

            prompt = prompt_template.format(
                paper_id=pid,
                doi=paper_meta.get("doi", ""),
                title=paper_meta.get("title", ""),
                year=paper_meta.get("year", ""),
                study_context=study_context + supplementary_info + shared_varieties_info,
                section_content=section_content,
                category_instruction=category_instruction,
                extraction_hints=hints_text,
            )

            # LLM 调用
            max_tokens = max(config.llm.max_tokens, 8192)
            logger.info(f"  [{pid[:25]}] Study {i+1}/{len(studies)}: LLM calling (max_tokens={max_tokens})...")
            study_data = llm.call_json(prompt, max_tokens=max_tokens)

            if study_data:
                varieties = study_data.get("varieties", [])
                phase2_results.append({
                    "study_index": i,
                    "varieties": varieties,
                })
                logger.info(
                    f"  [{pid[:25]}] Study {i+1}/{len(studies)}: '{study_title[:40]}' → "
                    f"{len(varieties)} varieties"
                )
            else:
                logger.warning(
                    f"  [{pid[:25]}] Study {i+1}/{len(studies)}: '{study_title[:40]}' → FAILED"
                )
                phase2_results.append({
                    "study_index": i,
                    "varieties": [],
                })
        logger.info(f"  [{pid[:25]}] Phase 2: done, {len(phase2_results)} study/studies processed")
    else:
        # ── Fallback: 使用文档树 ──
        logger.info(f"  [{pid[:25]}] Phase 2: processing {len(actual_exp_sections)} study/studies from document tree")
        for i, exp_node in enumerate(actual_exp_sections):
            study_title = exp_node.title or ""
            logger.info(f"  [{pid[:25]}] Study {i+1}/{len(actual_exp_sections)}: '{study_title[:50]}' → calling LLM...")
            study_context = f"试验名称: {study_title}"

            # 优先使用 hints 构建相关内容
            relevant_content = _build_relevant_content_from_hints(extraction_hints)
            if len(relevant_content) >= 2000:
                section_content = relevant_content
            else:
                # hints 覆盖不足，回退到文档树章节内容
                section_content = collect_content(exp_node)
                max_content = config.extraction.max_text_chars
                if len(section_content) > max_content:
                    section_content = section_content[:max_content] + "\n\n[...TRUNCATED...]"

            prompt = prompt_template.format(
                paper_id=pid,
                doi=paper_meta.get("doi", ""),
                title=paper_meta.get("title", ""),
                year=paper_meta.get("year", ""),
                study_context=study_context + supplementary_info + shared_varieties_info,
                section_content=section_content,
                category_instruction=category_instruction,
                extraction_hints=hints_text,
            )

            max_tokens = max(config.llm.max_tokens, 8192)
            logger.info(f"  [{pid[:25]}] Study {i+1}/{len(actual_exp_sections)}: LLM calling (max_tokens={max_tokens})...")
            study_data = llm.call_json(prompt, max_tokens=max_tokens)

            if study_data:
                varieties = study_data.get("varieties", [])
                phase2_results.append({
                    "study_index": i,
                    "varieties": varieties,
                })
                logger.info(
                    f"  [{pid[:25]}] Study {i+1}/{len(actual_exp_sections)}: '{study_title[:40]}' → "
                    f"{len(varieties)} varieties"
                )
            else:
                logger.warning(
                    f"  [{pid[:25]}] Study {i+1}/{len(actual_exp_sections)}: '{study_title[:40]}' → FAILED"
                )
                phase2_results.append({
                    "study_index": i,
                    "varieties": [],
                })
        logger.info(f"  [{pid[:25]}] Phase 2: done, {len(phase2_results)} study/studies processed")

    return {
        "phase2_results": phase2_results,
        "status": "phase2_done",
    }
