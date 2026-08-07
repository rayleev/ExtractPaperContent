"""
Phase 1 提取节点 — 基于 parse 输出的 study_list 遍历提取 study 级元数据。

输入：parse 输出（doc_context + extraction_hints + document_tree + variety_groups）
输出：paper 元数据 + studies 列表（试验级信息：年份/地点/面积等）

策略：
  - 遍历 parse 的 study_list，对每个 study 提取元数据
  - 使用 hints + document_tree 定位原文
  - 不依赖 abstract_text / methods_text
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


def extract_phase1_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """
    Phase 1 提取：基于 parse 的 study_list 遍历提取 study 级元数据。
    """
    pid = state["paper_id"]
    paper_meta = state["paper_meta"]
    prompt_template = _load_prompt("extract_paper.txt")

    # parse 输出
    doc_context = state.get("doc_context", {})
    extraction_hints = state.get("extraction_hints", [])
    document_tree = doc_context.get("document_tree", [])
    variety_groups = doc_context.get("variety_groups", [])
    study_list = doc_context.get("study_list", [])

    # 获取作物列表
    crops = doc_context.get("crops", [])
    classification = state.get("classification", [])
    if not crops and classification:
        crops = classification.get("crops", [])

    crops_text = ", ".join(crops) if crops else doc_context.get('crop', '')

    # 格式化 table_refs（现在是 dict 列表）
    table_refs = doc_context.get("table_refs", [])
    if table_refs:
        table_refs_text = "\n".join(
            f"  - {t.get('table_id', '')} ({t.get('section', '')}, {t.get('data_type', '')})"
            for t in table_refs
        )
    else:
        table_refs_text = "无"

    # 补充材料信息
    has_supplementary = doc_context.get("has_supplementary", False)
    data_file_link = doc_context.get("data_file_link")

    supplementary_info = ""
    if has_supplementary:
        supplementary_info = f"\n**补充材料**: 是（表格引用:\n{table_refs_text}）"
    elif table_refs:
        supplementary_info = f"\n**表格引用**:\n{table_refs_text}"

    if data_file_link:
        supplementary_info += f"\n**数据文件链接**: {data_file_link}"

    # 构建 hints 内容
    hints_text = "\n".join(
        f"- [{h.get('action', '?')}] {h.get('field', '')}: {h.get('value', '')}"
        for h in extraction_hints
    ) or "(无提取提示)"

    # 遍历 study_list 提取元数据
    phase1_studies = []

    logger.info(
        f"  [{pid[:25]}] Phase 1: {len(study_list)} study/studies from parse, "
        f"{len(extraction_hints)} extraction hints"
    )

    for i, study_info in enumerate(study_list):
        study_title = study_info.get("study_title", "")
        factors = study_info.get("factors", [])

        # 构建因子描述
        factors_desc = ", ".join(
            f"{f.get('name', '')} ({len(f.get('levels', []))} 水平)"
            for f in factors
        )

        logger.info(f"  [{pid[:25]}] Study {i+1}/{len(study_list)}: '{study_title[:50]}' ({factors_desc})")

        # 使用 hints 构建相关内容
        relevant_content = build_relevant_content_from_hints(extraction_hints)

        # 构建品种信息
        varieties_text = ""
        if variety_groups:
            varieties_text = "\n".join(
                f"  - {v.get('name', '')} (CK={v.get('is_check', False)})"
                for group in variety_groups
                for v in group.get("varieties", [])
            )

        prompt = prompt_template.format(
            paper_id=pid,
            doi=paper_meta.get("doi", ""),
            title=paper_meta.get("title", ""),
            year=paper_meta.get("year", ""),
            journal=paper_meta.get("journal", ""),
            crops_text=crops_text,
            study_title=study_title,
            factors_desc=factors_desc,
            varieties_text=varieties_text,
            relevant_content=relevant_content,
            extraction_hints=hints_text,
            supplementary_info=supplementary_info,
        )

        max_tokens = max(config.llm.max_tokens, 4096)
        study_data = llm.call_json(prompt, max_tokens=max_tokens, node_name="extract_phase1")

        if study_data:
            phase1_studies.append(study_data)
            logger.info(
                f"  [{pid[:25]}] Study {i+1}/{len(study_list)}: '{study_title[:40]}' → "
                f"year={study_data.get('trial_year', '?')}, location={study_data.get('site_administrative_region', '?')}"
            )
        else:
            logger.warning(
                f"  [{pid[:25]}] Study {i+1}/{len(study_list)}: '{study_title[:40]}' → FAILED (LLM超时/无返回)"
            )
            # 记录错误
            if "extraction_errors" not in state:
                state["extraction_errors"] = []
            state["extraction_errors"].append({
                "study_index": i,
                "study_title": study_title[:60],
                "error": "LLM提取超时或无返回",
            })
            # 仍然添加一个空 study 保持索引对齐
            phase1_studies.append({
                "study_title": study_title,
                "trial_year": "",
                "site_administrative_region": "",
                "_failed": True,
            })

    logger.info(f"  [{pid[:25]}] Phase 1 done: {len(phase1_studies)} study/studies processed")

    return {
        "phase1_result": {
            "paper": {
                "crop_species": crops_text,
                "data_file_link": data_file_link,
                "data_file_description": doc_context.get("data_file_description"),
            },
            "studies": phase1_studies,
        },
        "extraction_errors": state.get("extraction_errors", []),
        "status": "phase1_done",
        "node_status": {"extract_phase1": "phase1_done"},
    }
