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
from src.graph.state import PaperState
from src.graph.nodes._common import build_relevant_content_from_hints

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

    # 复用 parse 节点的 document_tree，不再自己重建
    document_tree = doc_context.get("document_tree", [])

    logger.info(
        f"  [{pid[:25]}] Phase 2: document_tree from parse ({len(document_tree)} top-level sections), "
        f"{len(studies)} from Phase 1, "
        f"{len(extraction_hints)} extraction hints"
    )

    # ── Phase 1 优先逻辑 ──
    # 当 Phase 1 成功识别 studies 时，以 Phase 1 的 studies 数量为准
    # document_tree 仅作为内容来源，不再决定 study 数量
    use_phase1_studies = len(studies) > 0
    if use_phase1_studies:
        logger.info(f"  [{pid[:25]}] Phase 2: using Phase 1 studies ({len(studies)}) as primary")
    else:
        logger.warning(
            f"  [{pid[:25]}] Phase 2: no Phase 1 studies, "
            f"falling back to document tree ({len(document_tree)} top-level sections)"
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

    # 从 extraction_hints 提取权威品种列表
    variety_hints = [h for h in extraction_hints if h.get("field") == "variety_name"]
    if variety_hints:
        authoritative_varieties = list(dict.fromkeys(h.get("value") for h in variety_hints))
        variety_constraint = (
            "## 品种列表（parse 节点识别，必须使用）\n"
            "这篇论文包含以下品种，请用这个列表填写 variety_name：\n"
            + "\n".join(f"- {v}" for v in authoritative_varieties)
            + "\n**禁止**填写不在列表中的品种名。同一 study 内所有处理的 variety_name 应保持一致。"
        )
    else:
        variety_constraint = ""

    # 补充材料信息
    has_supplementary = doc_context.get("has_supplementary", False)
    table_refs = doc_context.get("table_refs", [])
    variety_groups = doc_context.get("variety_groups", [])

    # 格式化 table_refs（现在是 dict 列表）
    if table_refs:
        table_refs_str = "\n".join(
            f"  - {t.get('table_id', '')} ({t.get('section', '')}, {t.get('data_type', '')})"
            for t in table_refs
        )
    else:
        table_refs_str = "无"

    supplementary_info = ""
    if has_supplementary:
        supplementary_info = f"\n**补充材料**: 是（表格引用:\n{table_refs_str}）"
    elif table_refs:
        supplementary_info = f"\n**表格引用**:\n{table_refs_str}"

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

    if use_phase1_studies:
        # ── 以 Phase 1 studies 为准 ──
        logger.info(f"  [{pid[:25]}] Phase 2: processing {len(studies)} study/studies from Phase 1")
        for i, s in enumerate(studies):
            # 跳过 Phase 1 失败的 study（避免空上下文 LLM 调用）
            if s.get("_failed"):
                logger.warning(
                    f"  [{pid[:25]}] Study {i+1}/{len(studies)}: '{s.get('study_title', '')[:40]}' → SKIPPED (Phase 1 failed)"
                )
                phase2_results.append({
                    "study_index": i,
                    "varieties": [],
                })
                continue

            study_title = s.get("study_title", "")
            logger.info(f"  [{pid[:25]}] Study {i+1}/{len(studies)}: '{study_title[:50]}' → calling LLM...")
            study_context = (
                f"试验名称: {study_title}\n"
                f"试验年份: {s.get('trial_year', '')}\n"
                f"试验地点: {s.get('site_administrative_region', '')}\n"
                f"试验站: {s.get('experimental_site_name', '')}"
            )

            # 使用 hints 构建相关内容（比整篇论文小很多）
            relevant_content = build_relevant_content_from_hints(extraction_hints)
            section_content = relevant_content
            logger.info(
                f"  [{pid[:25]}] Study {i+1}: using hints-based content "
                f"({len(relevant_content)} chars)"
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
                variety_constraint=variety_constraint,
            )

            # LLM 调用
            max_tokens = max(config.llm.max_tokens, 8192)
            logger.info(f"  [{pid[:25]}] Study {i+1}/{len(studies)}: LLM calling (max_tokens={max_tokens})...")
            study_data = llm.call_json(prompt, max_tokens=max_tokens, node_name="extract_phase2")

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
                    f"  [{pid[:25]}] Study {i+1}/{len(studies)}: '{study_title[:40]}' → FAILED (LLM超时/无返回)"
                )
                phase2_results.append({
                    "study_index": i,
                    "varieties": [],
                })
                # 记录提取错误，供 postprocess 区分"超时"和"无数据"
                if "extraction_errors" not in state:
                    state["extraction_errors"] = []
                state["extraction_errors"].append({
                    "study_index": i,
                    "study_title": study_title[:60],
                    "error": "LLM提取超时或无返回",
                })
        logger.info(f"  [{pid[:25]}] Phase 2: done, {len(phase2_results)} study/studies processed")
    else:
        # ── Fallback: 使用 parse 的 document_tree ──
        logger.info(f"  [{pid[:25]}] Phase 2: processing {len(document_tree)} study/studies from document tree")
        for i, section in enumerate(document_tree):
            study_title = section.get("title", "")
            logger.info(f"  [{pid[:25]}] Study {i+1}/{len(document_tree)}: '{study_title[:50]}' → calling LLM...")
            study_context = f"试验名称: {study_title}"

            # 使用 hints 构建相关内容
            relevant_content = build_relevant_content_from_hints(extraction_hints)
            section_content = relevant_content
            logger.info(
                f"  [{pid[:25]}] Study {i+1}: using hints-based content "
                f"({len(relevant_content)} chars)"
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
                variety_constraint=variety_constraint,
            )

            # LLM 调用
            max_tokens = max(config.llm.max_tokens, 8192)
            logger.info(f"  [{pid[:25]}] Study {i+1}/{len(document_tree)}: LLM calling (max_tokens={max_tokens})...")
            study_data = llm.call_json(prompt, max_tokens=max_tokens, node_name="extract_phase2")

            if study_data:
                varieties = study_data.get("varieties", [])
                phase2_results.append({
                    "study_index": i,
                    "varieties": varieties,
                })
                logger.info(
                    f"  [{pid[:25]}] Study {i+1}/{len(document_tree)}: '{study_title[:40]}' → "
                    f"{len(varieties)} varieties"
                )
            else:
                logger.warning(
                    f"  [{pid[:25]}] Study {i+1}/{len(document_tree)}: '{study_title[:40]}' → FAILED (LLM超时/无返回)"
                )
                phase2_results.append({
                    "study_index": i,
                    "varieties": [],
                })
                # 记录提取错误，供 postprocess 区分"超时"和"无数据"
                if "extraction_errors" not in state:
                    state["extraction_errors"] = []
                state["extraction_errors"].append({
                    "study_index": i,
                    "study_title": study_title[:60],
                    "error": "LLM提取超时或无返回",
                })
        logger.info(f"  [{pid[:25]}] Phase 2: done, {len(phase2_results)} study/studies processed")

    return {
        "phase2_results": phase2_results,
        "extraction_errors": state.get("extraction_errors", []),
        "status": "phase2_done",
        "node_status": {"extract_phase2": "phase2_done"},
    }
