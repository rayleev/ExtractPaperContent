"""
结构化数据提取器 — 两阶段提取架构。

Phase 1 (论文级): 摘要 + 标题大纲 + 方法 → 识别试验章节 + 提取 paper 字段
Phase 2 (试验级): 每个试验章节的完整内容 → 提取 study + variety 数据
"""

import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.core.loader import get_paper_key
from src.core.models import ExtractionResult
from src.core.chunker import (
    build_document_tree,
    get_section_outline,
    collect_content,
    collect_tables,
    find_nodes_by_type,
    find_experiment_sections,
    save_tree_debug,
)

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """加载 prompt 模板。"""
    path = PROMPT_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_papers(
    papers: List[dict],
    parsed: dict,
    classifications: List[dict],
    config: AppConfig,
    llm: LLMClient,
    parsed_dir: Path | None = None,
) -> List[dict]:
    """
    对通过分类筛选的论文进行两阶段 LLM 结构化提取。
    支持缓存和并发。
    """
    cache_file = config.cache_path / "extraction_results.json"
    cached = _load_cache(cache_file)
    paper_prompt = _load_prompt("extract_paper.txt")
    study_prompt = _load_prompt("extract_study.txt")
    cache_lock = threading.Lock()
    workers = config.concurrency.extract_workers

    paper_lookup = {get_paper_key(p): p for p in papers}
    extractable = {cls["paper_id"]: cls for cls in classifications}

    # 检测过时缓存
    current_pids = set(extractable.keys())
    stale_keys = [k for k in cached if k not in current_pids]
    if stale_keys:
        logger.info(f"Discarding {len(stale_keys)} stale cached extractions (paper_id changed)")
        for k in stale_keys:
            del cached[k]

    # 过滤出需要提取的论文
    to_extract = []
    for pid, cls in extractable.items():
        if pid in cached:
            logger.info(f"SKIP (cached): {cls['title'][:50]}")
            continue
        paper = paper_lookup.get(pid)
        md_text = parsed.get(pid)
        if not paper or not md_text:
            logger.warning(f"No paper/text for {pid}")
            continue
        to_extract.append((pid, cls, paper, md_text))

    results = list(cached.values())
    if not to_extract:
        logger.info(f"Extraction complete: 0 new, {len(results)} total")
        return results

    logger.info(
        f"Extracting {len(to_extract)} papers (workers={workers}, "
        f"from {len(classifications)} classified)"
    )

    def _extract_one(pid: str, cls: dict, paper: dict, md_text: str) -> dict:
        """两阶段提取单篇论文（线程内执行）。"""

        # ── 构建文档树 ──
        tree = build_document_tree(md_text)
        logger.info(f"  [{pid[:25]}] Tree: {tree.total_chars()} chars, "
                     f"{len(tree.children)} top-level sections")

        # 保存树结构调试信息
        if parsed_dir:
            safe_name = pid.replace("/", "_").replace(":", "_").replace("\\", "_")
            save_tree_debug(tree, parsed_dir / f"{safe_name}_tree.txt")

        # ── Phase 1: 论文级提取 ──
        phase1_result = _phase1_extract(
            pid, paper, tree, paper_prompt, llm, config
        )

        paper_info = {}
        experiment_sections = []
        if phase1_result:
            paper_info = phase1_result.get("paper", {})
            experiment_sections = phase1_result.get("experiment_sections", [])
            logger.info(
                f"  [{pid[:25]}] Phase 1: paper_title={paper_info.get('paper_title', '?')[:40]}, "
                f"{len(experiment_sections)} experiment sections"
            )
        else:
            logger.warning(f"  [{pid[:25]}] Phase 1 failed, using fallback")

        # ── Phase 2: 试验级提取 ──
        actual_exp_sections = find_experiment_sections(tree)
        logger.info(
            f"  [{pid[:25]}] Phase 2: {len(actual_exp_sections)} experiment sections in tree"
        )

        studies = []
        for i, exp_node in enumerate(actual_exp_sections):
            # 从 Phase 1 获取该试验的上下文信息
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

            study_data = _phase2_extract(
                pid, paper, exp_node, study_context,
                study_prompt, llm, config
            )
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

        # ── 组合结果 ──
        combined = {
            "paper": paper_info,
            "studies": studies,
        }

        # Pydantic 验证 + 后处理
        extraction = combined
        if combined:
            try:
                result = ExtractionResult.model_validate(combined)
                result.compute_standard_yields()
                extraction = result.model_dump()
                logger.info(f"  [{pid[:25]}] Pydantic validation passed")
            except Exception as e:
                logger.warning(f"  [{pid[:25]}] Pydantic failed (using raw): {e}")

        # 多站点检测
        if extraction and "studies" in extraction:
            for study in extraction["studies"]:
                site_name = study.get("experimental_site_name", "") or ""
                if "、" in site_name or ("和" in site_name and len(site_name) > 10):
                    logger.warning(f"  [{pid[:25]}] Multi-site: '{site_name}'")
                    study["notes"] = ((study.get("notes") or "") +
                                     " [多站点警告]").strip()

        # 置信度验证
        confidence_result = None
        if extraction:
            from src.core.validator import validate_extraction
            confidence_result = validate_extraction(paper, extraction, config, llm)

        return {
            "paper_id": pid,
            "doi": paper["doi"],
            "title": paper["title"],
            "language": paper["language"],
            "category": cls.get("category"),
            "extraction": extraction,
            "confidence": confidence_result,
            "extracted_at": datetime.now().isoformat(),
        }

    new_count = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_extract_one, pid, cls, paper, md): (pid, cls)
            for pid, cls, paper, md in to_extract
        }
        for future in as_completed(futures):
            pid, cls = futures[future]
            try:
                record = future.result()
                with cache_lock:
                    results.append(record)
                    new_count += 1
                    _save_cache(cache_file, results)
                logger.info(f"  Done: {cls['title'][:50]}")
            except Exception as e:
                logger.error(f"  Extract failed [{pid[:25]}]: {e}")

    # ── 后处理: variety_code 一致性回填 ──
    # 收集全局 name → code 映射（取第一个非空值）
    code_map: dict = {}
    for record in results:
        ext = record.get("extraction")
        if not ext:
            continue
        for study in ext.get("studies", []):
            for variety in study.get("varieties", []):
                name = variety.get("variety_name", "")
                code = variety.get("variety_code", "")
                if name and code and name not in code_map:
                    code_map[name] = code

    # 回填空的 variety_code
    backfill_count = 0
    if code_map:
        for record in results:
            ext = record.get("extraction")
            if not ext:
                continue
            for study in ext.get("studies", []):
                for variety in study.get("varieties", []):
                    name = variety.get("variety_name", "")
                    if name and not variety.get("variety_code") and name in code_map:
                        variety["variety_code"] = code_map[name]
                        backfill_count += 1
        if backfill_count:
            logger.info(f"Variety code backfill: {backfill_count} records updated from {len(code_map)} known codes")

    # ── 后处理: 剔除非大田试验（盆栽、温室、单株计产等）──
    _filter_non_field_experiments(results)

    # ── 后处理: 剔除无产量数据的 study 和 yield_raw_unit 为 % 的记录 ──
    _filter_no_yield_studies(results)

    # ── 后处理: 同一论文内 site 信息一致性回填 ──
    _normalize_site_info(results)

    _save_cache(cache_file, results)
    logger.info(f"Extraction complete: {new_count} new, {len(results)} total")
    return results


def _filter_non_field_experiments(results: list):
    """
    剔除非大田试验的 study（盆栽、温室、单株计产等）。

    综合判断信号：
      1. experimental_design_description / growth_facility_description 中包含盆栽/温室关键词
      2. yield_raw_unit 为 g/株（单株计产，通常非大田）
      3. measurement_method 包含"盆栽"/"单株"等关键词
    """
    # 盆栽/非大田关键词
    pot_keywords = [
        "盆栽", "pot experiment", "greenhouse", "温室",
        "人工气候室", "growth chamber", "培养箱",
        "水培", "hydroponic", "营养液",
        "模拟试验", "室内试验", "箱栽", "桶栽",
    ]

    removed_total = 0
    for record in results:
        ext = record.get("extraction")
        if not ext or "studies" not in ext:
            continue

        kept_studies = []
        for study in ext["studies"]:
            design = (study.get("experimental_design_description") or "").lower()
            facility = (study.get("growth_facility_description") or "").lower()
            combined_text = f"{design} {facility}"

            # 检查试验描述中的盆栽关键词
            is_pot = any(kw in combined_text for kw in pot_keywords)

            # 检查产量单位: g/株 通常表示单株计产
            is_single_plant = False
            varieties = study.get("varieties", [])
            if varieties:
                units = [v.get("yield_raw_unit", "") or "" for v in varieties]
                g_per_plant = sum(1 for u in units if "g/株" in u or "g·株" in u)
                if g_per_plant > len(units) * 0.5:  # 超过半数品种用 g/株
                    is_single_plant = True

            # 检查测定方法
            methods = [v.get("measurement_method", "") or "" for v in varieties]
            method_text = " ".join(methods).lower()
            is_pot_method = any(kw in method_text for kw in ["盆栽", "单株", "pot"])

            if is_pot or (is_single_plant and is_pot_method):
                title = study.get("study_title", "")[:40]
                logger.info(f"  Filtered non-field study: '{title}' (pot={is_pot}, single_plant={is_single_plant})")
                removed_total += 1
            else:
                kept_studies.append(study)

        ext["studies"] = kept_studies

    if removed_total:
        logger.info(f"Non-field experiment filter: removed {removed_total} studies")


def _filter_no_yield_studies(results: list):
    """
    剔除无产量数据的 study 和无效产量记录。

    处理：
      1. 删除 yield_raw_unit 为 "%" 的品种记录（增产/减产比例，非实际产量）
      2. 删除 yield_raw_value 和 yield_raw_unit 都为空的品种记录
      3. 如果一个 study 过滤后没有剩余品种，删除该 study
    """
    removed_studies = 0
    removed_varieties = 0

    for record in results:
        ext = record.get("extraction")
        if not ext or "studies" not in ext:
            continue

        kept_studies = []
        for study in ext["studies"]:
            varieties = study.get("varieties", [])
            kept_varieties = []

            for v in varieties:
                unit = (v.get("yield_raw_unit") or "").strip()
                value = v.get("yield_raw_value")

                # 剔除: yield_raw_unit 为 %（增产/减产比例，非实际产量）
                if unit == "%":
                    removed_varieties += 1
                    continue

                # 剔除: yield_raw_value 和 yield_raw_unit 都为空
                if value is None and not unit:
                    removed_varieties += 1
                    continue

                kept_varieties.append(v)

            if kept_varieties:
                study["varieties"] = kept_varieties
                kept_studies.append(study)
            else:
                # study 过滤后无品种数据 → 删除
                title = study.get("study_title", "")[:40]
                logger.info(f"  Filtered no-yield study: '{title}'")
                removed_studies += 1

        ext["studies"] = kept_studies

    if removed_studies or removed_varieties:
        logger.info(
            f"Yield filter: removed {removed_studies} studies, "
            f"{removed_varieties} variety records"
        )


def _normalize_site_info(results: list):
    """
    同一论文内 site 信息一致性回填。

    对于每篇论文：
      1. 收集所有 study 的 site_administrative_region 和 experimental_site_name
      2. 如果只有一个非空值（最常见情况），用它回填所有空值
      3. 如果有多个不同非空值（多地点试验），不回填，保持原样
    """
    backfill_count = 0

    for record in results:
        ext = record.get("extraction")
        if not ext or "studies" not in ext:
            continue

        studies = ext["studies"]
        if len(studies) <= 1:
            continue

        # 收集非空的 site 信息
        regions = set()
        sites = set()
        for study in studies:
            r = (study.get("site_administrative_region") or "").strip()
            s = (study.get("experimental_site_name") or "").strip()
            if r:
                regions.add(r)
            if s:
                sites.add(s)

        # 回填 region：只有一个非空值时回填
        if len(regions) == 1:
            fill_region = next(iter(regions))
            for study in studies:
                if not (study.get("site_administrative_region") or "").strip():
                    study["site_administrative_region"] = fill_region
                    backfill_count += 1

        # 回填 site_name：只有一个非空值时回填
        if len(sites) == 1:
            fill_site = next(iter(sites))
            for study in studies:
                if not (study.get("experimental_site_name") or "").strip():
                    study["experimental_site_name"] = fill_site
                    backfill_count += 1

    if backfill_count:
        logger.info(f"Site info backfill: {backfill_count} fields filled")


# ═══════════════════════════════════════════════════════════════
#  Phase 1: 论文级提取
# ═══════════════════════════════════════════════════════════════

def _phase1_extract(
    pid: str,
    paper: dict,
    tree,
    prompt_template: str,
    llm: LLMClient,
    config: AppConfig,
) -> Optional[dict]:
    """
    Phase 1: 从摘要、标题大纲和方法部分提取论文级信息和试验章节列表。
    """
    # 收集内容
    outline = get_section_outline(tree, max_level=3)

    abstract_nodes = find_nodes_by_type(tree, "abstract")
    abstract_text = "\n\n".join(
        n.content.strip() for n in abstract_nodes if n.content.strip()
    )
    if not abstract_text:
        abstract_text = "(摘要未识别)"

    methods_nodes = find_nodes_by_type(tree, "methods")
    methods_text = "\n\n".join(
        n.content.strip() for n in methods_nodes if n.content.strip()
    )
    if not methods_text:
        methods_text = "(方法部分未识别)"

    # 限制各部分长度
    max_abstract = 5000
    max_methods = 15000
    if len(abstract_text) > max_abstract:
        abstract_text = abstract_text[:max_abstract] + "\n\n[...TRUNCATED...]"
    if len(methods_text) > max_methods:
        methods_text = methods_text[:max_methods] + "\n\n[...TRUNCATED...]"

    logger.info(
        f"  [{pid[:25]}] Phase 1 context: outline={len(outline)}, "
        f"abstract={len(abstract_text)}, methods={len(methods_text)}"
    )

    # 构建 prompt
    prompt = prompt_template.format(
        paper_id=pid,
        doi=paper.get("doi", ""),
        title=paper.get("title", ""),
        year=paper.get("year", ""),
        journal=paper.get("journal", ""),
        outline=outline,
        abstract=abstract_text,
        methods=methods_text,
    )

    # LLM 调用
    raw_dict = llm.call_json(prompt, max_tokens=config.llm.max_tokens)
    return raw_dict


# ═══════════════════════════════════════════════════════════════
#  Phase 2: 试验级提取
# ═══════════════════════════════════════════════════════════════

def _phase2_extract(
    pid: str,
    paper: dict,
    exp_node,
    study_context: str,
    prompt_template: str,
    llm: LLMClient,
    config: AppConfig,
) -> Optional[dict]:
    """
    Phase 2: 从某个试验章节的完整内容中提取 study + variety 数据。
    """
    # 收集该章节的完整内容
    section_content = collect_content(exp_node)

    # 限制长度
    max_content = config.extraction.max_text_chars
    if len(section_content) > max_content:
        logger.warning(
            f"  [{pid[:25]}] Section content ({len(section_content)} chars) "
            f"exceeds limit, truncating to {max_content}"
        )
        section_content = section_content[:max_content] + "\n\n[...TRUNCATED...]"

    logger.info(
        f"  [{pid[:25]}] Phase 2: section='{exp_node.title[:40]}' "
        f"({len(section_content)} chars)"
    )

    # 构建 prompt
    prompt = prompt_template.format(
        paper_id=pid,
        doi=paper.get("doi", ""),
        title=paper.get("title", ""),
        year=paper.get("year", ""),
        study_context=study_context,
        section_content=section_content,
    )

    # LLM 调用（Phase 2 可能需要更多 token 用于输出多个 variety）
    max_tokens = max(config.llm.max_tokens, 8192)
    raw_dict = llm.call_json(prompt, max_tokens=max_tokens)
    return raw_dict


# ═══════════════════════════════════════════════════════════════
#  缓存
# ═══════════════════════════════════════════════════════════════

def _load_cache(cache_file: Path) -> dict:
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            cached_list = json.load(f)
            cached = {r["paper_id"]: r for r in cached_list}
        logger.info(f"Loaded {len(cached)} cached extractions")
        return cached
    return {}


def _save_cache(cache_file: Path, results: list):
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
