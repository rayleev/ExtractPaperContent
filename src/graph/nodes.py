"""
LangGraph 节点函数 — 包装现有 pipeline 逻辑为 StateGraph 节点。

每个节点接收 PaperState，返回部分更新（dict）。
核心原则：复用 src/core/ 现有代码，节点只做薄包装。
"""

from __future__ import annotations
import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.clients.mineru import MinerUClient
from src.core.models import ExtractionResult
from src.core.chunker import (
    build_document_tree,
    get_section_outline,
    collect_content,
    find_nodes_by_type,
    find_experiment_sections,
)
from src.core.classifier import load_classify_prompt
from src.core.geocoder import Geocoder, _supplement_altitude_from_province, PROVINCE_CENTROIDS
from src.core.models import _convert_yield
from src.graph.rules import validate_extraction
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
#  节点函数
# ═══════════════════════════════════════════════════════════════

def classify_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """分类节点：LLM 判断论文类别。"""
    paper_meta = state["paper_meta"]
    prompt_template = load_classify_prompt()

    prompt = prompt_template.format(
        title=paper_meta.get("title", ""),
        abstract=paper_meta.get("abstract", ""),
        keywords=paper_meta.get("keywords", ""),
        year=paper_meta.get("year", ""),
    )

    result = llm.call_json(prompt, max_tokens=1000)
    classification = result or {"category": "unknown", "language": "zh"}
    classification["paper_id"] = state["paper_id"]
    classification["title"] = paper_meta.get("title", "")
    classification["doi"] = paper_meta.get("doi", "")

    return {
        "classification": classification,
        "status": "classified",
    }


def filter_node(state: PaperState, config: AppConfig) -> dict:
    """过滤节点：判断论文是否可提取。"""
    cls = state.get("classification", {})
    category = cls.get("category", "")
    country = cls.get("research_country", "")

    extractable_categories = config.extraction.extractable_categories
    is_extractable = (
        category in extractable_categories
        and country in ("China", "CN", "")
    )

    return {
        "is_extractable": is_extractable,
        "status": "filtered" if is_extractable else "skipped",
    }


def parse_node(
    state: PaperState,
    config: AppConfig,
    mineru_client: Optional[MinerUClient],
) -> dict:
    """解析节点：获取论文 Markdown 全文。"""
    paper_meta = state["paper_meta"]
    pid = state["paper_id"]

    md_text = None

    # 优先级 1: docs/ 下的 MD 文件
    md_path = paper_meta.get("md_path")
    if md_path and Path(md_path).exists():
        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
        logger.info(f"  [{pid[:25]}] Read MD: {Path(md_path).name}")

    # 优先级 2: 已有的 parsed MD 文件（按标题匹配）
    if not md_text:
        title = paper_meta.get("title", "")
        if title:
            import re
            title_norm = re.sub(r'\s+', '', title)
            parsed_dir = config.parsed_path
            if parsed_dir.exists():
                for md_file in parsed_dir.glob("*.md"):
                    if "_chunks" in md_file.stem:
                        continue
                    try:
                        with open(md_file, "r", encoding="utf-8") as f:
                            head = f.read(2000)
                        head_norm = re.sub(r'\s+', '', head)
                        if len(title_norm) >= 5 and title_norm in head_norm:
                            with open(md_file, "r", encoding="utf-8") as f:
                                md_text = f.read()
                            logger.info(f"  [{pid[:25]}] Reused MD: {md_file.name}")
                            break
                    except Exception:
                        pass

    # 优先级 3: MinerU 解析 PDF
    if not md_text and mineru_client:
        pdf_path = paper_meta.get("pdf_path")
        if pdf_path and Path(pdf_path).exists():
            logger.info(f"  [{pid[:25]}] Parsing PDF via MinerU...")
            md_text = mineru_client.parse_pdf(Path(pdf_path))
            if md_text:
                # 保存到 parsed 目录供后续复用
                safe_name = pid.replace("/", "_").replace(":", "_").replace("\\", "_")
                save_path = config.parsed_path / f"{safe_name}.md"
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(md_text)

    if not md_text:
        return {
            "errors": state.get("errors", []) + [
                {"node": "parse", "error": "No text available", "timestamp": datetime.now().isoformat()}
            ],
            "status": "failed",
        }

    # 构建文档树和收集各部分文本
    tree = build_document_tree(md_text)
    outline = get_section_outline(tree, max_level=3)

    abstract_nodes = find_nodes_by_type(tree, "abstract")
    abstract_text = "\n\n".join(
        n.content.strip() for n in abstract_nodes if n.content.strip()
    ) or "(摘要未识别)"

    methods_nodes = find_nodes_by_type(tree, "methods")
    methods_text = "\n\n".join(
        n.content.strip() for n in methods_nodes if n.content.strip()
    ) or "(方法部分未识别)"

    return {
        "parsed_text": md_text,
        "tree_outline": outline,
        "abstract_text": abstract_text[:5000],
        "methods_text": methods_text[:15000],
        "status": "parsed",
    }


def extract_phase1_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """提取阶段 1：论文级元数据 + 试验章节识别。"""
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

    result = llm.call_json(prompt, max_tokens=config.llm.max_tokens)
    if not result:
        return {
            "phase1_result": {"paper": {}, "experiment_sections": []},
            "status": "phase1_failed",
        }

    return {
        "phase1_result": result,
        "status": "phase1_done",
    }


def extract_phase2_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """提取阶段 2：逐试验章节提取 study + variety 数据。"""
    pid = state["paper_id"]
    paper_meta = state["paper_meta"]
    phase1 = state.get("phase1_result", {})
    experiment_sections = phase1.get("experiment_sections", [])
    md_text = state.get("parsed_text", "")

    prompt_template = _load_prompt("extract_study.txt")

    # 重新构建树以获取实际的实验章节
    tree = build_document_tree(md_text)
    actual_exp_sections = find_experiment_sections(tree)

    studies = []
    for i, exp_node in enumerate(actual_exp_sections):
        # 从 Phase 1 获取上下文
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

        # 收集章节内容
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


def postprocess_node(state: PaperState, config: AppConfig) -> dict:
    """后处理节点：合并结果 + 所有代码级后处理。"""
    pid = state["paper_id"]
    phase1 = state.get("phase1_result", {})
    phase2 = state.get("phase2_results", [])

    paper_info = phase1.get("paper", {})
    combined = {
        "paper": paper_info,
        "studies": phase2,
    }

    # Pydantic 验证 + 产量换算
    try:
        result = ExtractionResult.model_validate(combined)
        result.compute_standard_yields()
        extraction = result.model_dump()
    except Exception as e:
        logger.warning(f"  [{pid[:25]}] Pydantic failed: {e}")
        extraction = combined

    # ── 后处理步骤（复用 extractor.py 中的逻辑）──
    if "studies" in extraction:
        # 1. 多站点检测
        for study in extraction["studies"]:
            site_name = study.get("experimental_site_name", "") or ""
            if "、" in site_name or ("和" in site_name and len(site_name) > 10):
                study["notes"] = ((study.get("notes") or "") + " [多站点警告]").strip()

        # 2. variety_code 一致性回填
        code_map: dict = {}
        for study in extraction["studies"]:
            for v in study.get("varieties", []):
                name = v.get("variety_name", "")
                code = v.get("variety_code", "")
                if name and code and name not in code_map:
                    code_map[name] = code
        if code_map:
            for study in extraction["studies"]:
                for v in study.get("varieties", []):
                    name = v.get("variety_name", "")
                    if name and not v.get("variety_code") and name in code_map:
                        v["variety_code"] = code_map[name]

        # 3. 盆栽/非大田试验过滤
        from src.core.extractor import _filter_non_field_experiments
        _filter_non_field_experiments([{"extraction": extraction}])

        # 4. 无产量 study 和 % 单位过滤
        from src.core.extractor import _filter_no_yield_studies
        _filter_no_yield_studies([{"extraction": extraction}])

        # 5. site 信息一致性回填
        from src.core.extractor import _normalize_site_info
        _normalize_site_info([{"extraction": extraction}])

    return {
        "extraction": extraction,
        "status": "postprocessed",
    }


def geocode_node(state: PaperState, config: AppConfig, geocoder: Geocoder) -> dict:
    """地理编码节点：填充经纬度和海拔。"""
    extraction = state.get("extraction", {})
    studies = extraction.get("studies", [])

    geocoded_count = 0
    for study in studies:
        lat = study.get("latitude")
        lon = study.get("longitude")

        if lat is not None and lon is not None:
            study["geo_source"] = "paper"
            continue

        region = study.get("site_administrative_region", "") or ""
        site = study.get("experimental_site_name", "") or ""

        if not region and not site:
            study["geo_source"] = "unknown"
            continue

        result = geocoder.geocode(region, site)
        if result:
            study["latitude"] = result.latitude
            study["longitude"] = result.longitude
            study["geo_source"] = result.source
            if result.altitude is not None and study.get("altitude") is None:
                study["altitude"] = result.altitude
            if study.get("altitude") is None:
                _supplement_altitude_from_province(study, region, site)
            geocoded_count += 1
        else:
            study["geo_source"] = "unknown"

    return {
        "extraction": extraction,
        "geocoded": True,
        "status": "geocoded",
    }


def validate_node(state: PaperState, config: AppConfig) -> dict:
    """规则验证节点：纯代码检查，不消耗 token。"""
    extraction = state.get("extraction", {})
    paper_meta = state.get("paper_meta", {})

    report = validate_extraction(extraction, paper_meta)

    stats = report["stats"]
    logger.info(
        f"  [{state['paper_id'][:25]}] Validation: "
        f"{stats['issues_count']} issues, {stats['warnings_count']} warnings, "
        f"{stats['flagged_records']} flagged"
    )

    for issue in report["issues"]:
        logger.warning(f"  [{state['paper_id'][:25]}] ISSUE: {issue}")

    return {
        "validation_report": report,
        "flagged_records": report.get("flagged_variety_indices", []),
        "status": "validated",
    }


def targeted_llm_validate_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """针对性 LLM 验证节点：只对规则标记为可疑的记录做 LLM 核对。"""
    flagged = state.get("flagged_records", [])
    if not flagged:
        return {"status": "validated_complete"}

    extraction = state.get("extraction", {})
    studies = extraction.get("studies", [])
    pid = state["paper_id"]

    verified_count = 0
    for si, vi in flagged:
        if si >= len(studies):
            continue
        study = studies[si]
        varieties = study.get("varieties", [])
        if vi >= len(varieties):
            continue

        v = varieties[vi]
        vname = v.get("variety_name", "?")

        # 构建简洁的验证 prompt
        prompt = (
            f"请核对以下从论文表格中提取的品种产量数据是否正确：\n\n"
            f"品种名称: {vname}\n"
            f"产量: {v.get('yield_raw_value')} {v.get('yield_raw_unit')}\n"
            f"对照品种: {v.get('is_check_variety')}\n"
            f"增产率: {v.get('pct_over_check')}%\n"
            f"数据来源: {v.get('source_location')}\n\n"
            f"如果数据看起来合理，输出 {{\"verified\": true}}\n"
            f"如果有明显错误，输出 {{\"verified\": false, \"reason\": \"错误描述\"}}"
        )

        result = llm.call_json(prompt, max_tokens=200)
        if result and result.get("verified"):
            verified_count += 1
        elif result and not result.get("verified"):
            reason = result.get("reason", "")
            logger.info(f"  [{pid[:25]}] LLM flagged {vname}: {reason}")

    logger.info(f"  [{pid[:25]}] Targeted validation: {verified_count}/{len(flagged)} verified")

    return {"status": "validated_complete"}
