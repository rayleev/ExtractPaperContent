"""
解析节点 — 获取论文 Markdown 全文、构建文档树、理解论文内容。

优先级：
  1. download 时下载的 MD 文件（md_path）
  2. MinerU OCR 解析 PDF

LLM 理解策略（根据论文长度自动选择）：
  - 短论文（< 上下文窗口 50%）：一次性给全文
  - 长论文 + 有章节标题：分段理解 + 合并
  - 长论文 + 无章节标题：滑动窗口 + 合并
"""

from __future__ import annotations
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from src.config import AppConfig
from src.clients.mineru import MinerUClient
from src.clients.llm import LLMClient
from src.core.chunker import (
    build_document_tree,
    get_section_outline,
    find_nodes_by_type,
)
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _understand_document_full_text(
    md_text: str,
    tree_outline: str,
    llm: LLMClient,
    config: AppConfig,
    max_tokens_override: int = 0,
) -> Optional[dict]:
    """一次性给全文，LLM 理解（适用于短论文）。"""
    prompt_template = (PROMPT_DIR / "parse_understanding.txt").read_text(encoding="utf-8")

    prompt = prompt_template.format(
        md_text=md_text,
        tree_outline=tree_outline,
        abstract_text="",
        methods_text="",
        strategy="full_text",
        chunk_info="",
    )
    max_tokens = max_tokens_override if max_tokens_override > 0 else config.llm.max_tokens
    return llm.call_json(prompt, max_tokens=max_tokens, node_name="parse")


def _merge_partial_results(partial_results: list[dict]) -> dict:
    """程序合并多个分段理解结果（无 LLM 调用）。"""
    if not partial_results:
        return {}

    # 取第一个作为基础
    merged = partial_results[0].copy()

    # 合并 extraction_hints（程序去重）
    all_hints = []
    seen_hints = set()
    for result in partial_results:
        for hint in result.get("extraction_hints", []):
            # 基于 field + value + variety + treatment 去重
            key = (
                hint.get("field", ""),
                str(hint.get("value", ""))[:200],
                hint.get("variety", ""),
                hint.get("treatment", ""),
            )
            if key not in seen_hints:
                seen_hints.add(key)
                all_hints.append(hint)
    merged["extraction_hints"] = all_hints

    # 合并 crops（去重）
    all_crops = set()
    for result in partial_results:
        for crop in result.get("doc_context", {}).get("crops", []):
            all_crops.add(crop)
    if "doc_context" not in merged:
        merged["doc_context"] = {}
    merged["doc_context"]["crops"] = sorted(all_crops)

    # 合并 study_list（取最完整的版本）
    best_study_list = []
    best_study_count = 0
    for result in partial_results:
        study_list = result.get("doc_context", {}).get("study_list", [])
        if len(study_list) > best_study_count:
            best_study_count = len(study_list)
            best_study_list = study_list
    merged["doc_context"]["study_list"] = best_study_list

    # 合并 document_tree（取最完整的版本）
    best_tree = []
    best_tree_depth = 0
    for result in partial_results:
        tree = result.get("doc_context", {}).get("document_tree", [])
        depth = _tree_depth(tree)
        if depth > best_tree_depth:
            best_tree_depth = depth
            best_tree = tree
    merged["doc_context"]["document_tree"] = best_tree

    # 合并 table_refs（基于 table_id 去重）
    all_table_refs = {}
    for result in partial_results:
        for ref in result.get("doc_context", {}).get("table_refs", []):
            table_id = ref.get("table_id", "")
            if table_id and table_id not in all_table_refs:
                all_table_refs[table_id] = ref
    merged["doc_context"]["table_refs"] = list(all_table_refs.values())

    # 合并 variety_groups（按 group_name 去重）
    all_variety_groups = {}
    for result in partial_results:
        for group in result.get("doc_context", {}).get("variety_groups", []):
            group_name = group.get("group_name", "")
            if group_name and group_name not in all_variety_groups:
                all_variety_groups[group_name] = group
    merged["doc_context"]["variety_groups"] = list(all_variety_groups.values())

    # 保留 data_file_link 和 data_file_description（任一分段有就保留）
    for result in partial_results:
        if result.get("doc_context", {}).get("data_file_link"):
            merged["doc_context"]["data_file_link"] = result["doc_context"]["data_file_link"]
        if result.get("doc_context", {}).get("data_file_description"):
            merged["doc_context"]["data_file_description"] = result["doc_context"]["data_file_description"]

    return merged


def _tree_depth(tree: list) -> int:
    """计算文档树的最大深度。"""
    if not tree:
        return 0
    max_depth = 0
    for node in tree:
        depth = node.get("level", 0)
        children = node.get("children", [])
        if children:
            depth = max(depth, _tree_depth(children))
        max_depth = max(max_depth, depth)
    return max_depth


def _understand_document_chunked(
    md_text: str,
    tree_outline: str,
    abstract_text: str,
    methods_text: str,
    llm: LLMClient,
    config: AppConfig,
    max_tokens_override: int = 0,
) -> Optional[dict]:
    """分段理解 + 程序合并（适用于长论文 + 有章节标题）。"""
    prompt_template = (PROMPT_DIR / "parse_understanding.txt").read_text(encoding="utf-8")
    max_tokens = max_tokens_override if max_tokens_override > 0 else config.llm.max_tokens

    # 按文档树顶层章节切分
    chunks = _split_by_top_level_sections(md_text)

    partial_results = []
    for i, chunk in enumerate(chunks):
        prompt = prompt_template.format(
            md_text=chunk,
            tree_outline=tree_outline,
            abstract_text="",
            methods_text="",
            strategy="chunked",
            chunk_info=f"第 {i+1}/{len(chunks)} 段",
        )
        partial = llm.call_json(prompt, max_tokens=max_tokens, node_name="parse")
        if partial:
            partial_results.append(partial)

    if not partial_results:
        return None

    # 程序合并（无 LLM 调用）
    return _merge_partial_results(partial_results)


def _understand_document_sliding_window(
    md_text: str,
    tree_outline: str,
    abstract_text: str,
    methods_text: str,
    llm: LLMClient,
    config: AppConfig,
    max_tokens_override: int = 0,
) -> Optional[dict]:
    """滑动窗口 + 程序合并（适用于长论文 + 无章节标题）。"""
    prompt_template = (PROMPT_DIR / "parse_understanding.txt").read_text(encoding="utf-8")
    max_tokens = max_tokens_override if max_tokens_override > 0 else config.llm.max_tokens

    chunks = _sliding_window(md_text, config.sliding_window_size, config.sliding_window_step)

    partial_results = []
    for i, (chunk, start_pos) in enumerate(chunks):
        prompt = prompt_template.format(
            md_text=chunk,
            tree_outline=tree_outline,
            abstract_text="",
            methods_text="",
            strategy="sliding_window",
            chunk_info=f"窗口 {i+1}（起始位置 {start_pos}）",
        )
        partial = llm.call_json(prompt, max_tokens=max_tokens, node_name="parse")
        if partial:
            partial_results.append(partial)

    if not partial_results:
        return None

    # 程序合并（无 LLM 调用）
    return _merge_partial_results(partial_results)


def _split_into_chunks(text: str, max_length: int = 50000) -> list[str]:
    """按长度切分文本（中文约 33000 tokens/段，适配 1M 上下文窗口的 50% threshold）。"""
    if len(text) <= max_length:
        return [text]

    chunks = []
    for i in range(0, len(text), max_length):
        chunks.append(text[i:i + max_length])
    return chunks


def _sliding_window(text: str, window_size: int, step: int) -> list[tuple[str, int]]:
    """滑动窗口切分文本，返回 (chunk, start_pos) 列表。"""
    if len(text) <= window_size:
        return [(text, 0)]

    chunks = []
    for i in range(0, len(text), step):
        chunks.append((text[i:i + window_size], i))
        if i + window_size >= len(text):
            break
    return chunks


def _split_by_top_level_sections(md_text: str) -> list[str]:
    """按文档树顶层章节切分文本。

    每个顶层 section（level=1 的子节点）及其所有子孙内容为一个 chunk。
    如果文档树构建失败或只有一个节点，回退到按 50k chars 切分。
    """
    from src.core.chunker import build_document_tree, collect_content

    try:
        tree = build_document_tree(md_text)
        children = tree.children

        # 过滤掉 level=0 的 root 节点
        top_level = [c for c in children if c.level == 1]

        if len(top_level) <= 1:
            # 只有一个顶层章节，回退到按 50k chars 切分
            return _split_into_chunks(md_text, max_length=50000)

        # 每个顶层 section 收集完整内容
        chunks = []
        for sec in top_level:
            content = collect_content(sec)
            if content.strip():
                chunks.append(content)

        return chunks if chunks else _split_into_chunks(md_text, max_length=50000)

    except Exception as e:
        logger.warning(f"Failed to build document tree for section splitting: {e}")
        return _split_into_chunks(md_text, max_length=50000)


def _understand_document(
    md_text: str,
    tree_outline: str,
    abstract_text: str,
    methods_text: str,
    llm: LLMClient,
    config: AppConfig,
    max_tokens_override: int = 0,
) -> Optional[dict]:
    """
    根据论文长度选择策略，调用 LLM 理解论文。

    Returns:
        {"doc_context": {...}, "entity_index": [...], "evidence_nodes": [...]}
        或 None（LLM 调用失败时）
    """
    # 估算 token 数（中文约 1.5 char/token）
    estimated_tokens = len(md_text) / 1.5
    threshold_tokens = config.context_window * config.full_text_threshold

    if estimated_tokens < threshold_tokens:
        # 短论文：一次性给全文
        logger.info(f"    Using full-text strategy (est. {estimated_tokens:.0f} tokens, context_window={config.context_window}, threshold={threshold_tokens:.0f})")
        return _understand_document_full_text(
            md_text, tree_outline, llm, config,
            max_tokens_override=max_tokens_override,
        )

    # 长论文：检查是否有章节标题
    has_structure = tree_outline and tree_outline != "(无章节标题)"

    if has_structure:
        # 有标题：分段理解
        if config.chunked_enabled:
            logger.info(f"    Using chunked strategy (est. {estimated_tokens:.0f} tokens)")
            return _understand_document_chunked(
                md_text, tree_outline, abstract_text, methods_text, llm, config,
                max_tokens_override=max_tokens_override,
            )
        else:
            logger.info(f"    Chunked strategy disabled, falling back to full-text")

    # 无标题：滑动窗口
    if config.sliding_window_enabled:
        logger.info(f"    Using sliding-window strategy (est. {estimated_tokens:.0f} tokens)")
        return _understand_document_sliding_window(
            md_text, tree_outline, abstract_text, methods_text, llm, config,
            max_tokens_override=max_tokens_override,
        )
    else:
        logger.info(f"    Sliding-window strategy disabled, falling back to full-text")

    # 兜底：一次性给全文（可能超上下文，但总比失败好）
    return _understand_document_full_text(
        md_text, tree_outline, llm, config,
        max_tokens_override=max_tokens_override,
    )


def _compute_parse_max_tokens(llm_max_tokens: int, md_text: str) -> int:
    """
    parse 节点的 max_tokens。

    parse 输出需要完整的 doc_context + extraction_hints，
    长论文（含多个 study、多个 variety）输出量较大。
    直接设固定高值 32768，避免动态计算不够。
    """
    return max(llm_max_tokens, 32768)


def _check_parse_quality(
    doc_context: dict,
    extraction_hints: list,
    pid: str,
) -> dict:
    """
    检查 parse 输出的质量。

    Returns:
        {
            "has_crop": bool,
            "has_study_list": bool,
            "has_hints": bool,
            "overall": "ok" | "weak" | "failed"
        }
    """
    # crops 可能是列表（新 prompt）或字符串（兼容旧格式）
    crops_val = doc_context.get("crops") or doc_context.get("crop")
    if isinstance(crops_val, list):
        has_crop = len(crops_val) > 0
        crops_val_display = str(crops_val)
    else:
        has_crop = bool(crops_val) and crops_val not in (None, "", "NONE", "null")
        crops_val_display = crops_val or "NONE"

    # 支持 study_list（新格式）和 study_count（旧格式兼容）
    study_list = doc_context.get("study_list", [])
    if isinstance(study_list, list):
        has_study_list = len(study_list) > 0
        study_count_display = len(study_list)
    else:
        has_study_list = doc_context.get("study_count", 0) > 0
        study_count_display = doc_context.get("study_count", 0)

    has_hints = len(extraction_hints) > 0

    if has_crop and has_study_list and has_hints:
        overall = "ok"
    elif has_crop or has_study_list:
        overall = "weak"
    else:
        overall = "failed"

    if overall != "ok":
        logger.warning(
            f"  [{pid[:25]}] Parse quality: {overall} "
            f"(crop={crops_val_display}, "
            f"studies={study_count_display}, "
            f"hints={len(extraction_hints)})"
        )

    return {
        "has_crop": has_crop,
        "has_study_list": has_study_list,
        "has_hints": has_hints,
        "overall": overall,
    }


def parse_node(
    state: PaperState,
    config: AppConfig,
    mineru_client: Optional[MinerUClient],
    llm: Optional[LLMClient] = None,
) -> dict:
    """
    解析节点：获取论文全文、构建文档树、理解论文内容。

    返回：
      - parsed_text（MD 全文）
      - tree_outline（标题大纲）
      - abstract_text（摘要）
      - methods_text（方法部分）
      - doc_context（Document Context：作物、study 数、chunk 列表等）
      - extraction_hints（提取提示：字段位置、查找需求）
      - parse_quality（parse 质量门控信息）
    """
    paper_meta = state["paper_meta"]
    pid = state["paper_id"]
    md_text = None

    # ── 优先级 1: download 时下载的 MD ──
    md_path = paper_meta.get("md_path")
    if md_path and Path(md_path).exists():
        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
        logger.info(f"  [{pid[:25]}] Read MD: {Path(md_path).name}")

    # ── 优先级 1.5: 本地 parsed 目录缓存（避免重复调用 MinerU）──
    # 即使 download 节点没设 md_path（比如它下载了 PDF），
    # 只要 parsed 目录已有非空的 .md 文件，就直接复用
    if not md_text:
        local_cache = config.parsed_path / f"{pid}.md"
        if local_cache.exists() and local_cache.stat().st_size > 100:
            with open(local_cache, "r", encoding="utf-8") as f:
                md_text = f.read()
            logger.info(f"  [{pid[:25]}] Read local cache: {local_cache.name} ({len(md_text)} chars)")

    # ── 优先级 2: MinerU 解析 PDF ──
    if not md_text and mineru_client:
        pdf_path = paper_meta.get("pdf_path")
        if pdf_path and Path(pdf_path).exists():
            logger.info(f"  [{pid[:25]}] Parsing PDF via MinerU...")
            md_text = mineru_client.parse_pdf(Path(pdf_path))
            if md_text:
                # 保存到 parsed 目录供后续复用
                save_path = config.parsed_path / f"{pid}.md"
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(md_text)
        elif pdf_path:
            logger.warning(f"  [{pid[:25]}] PDF not found at: {pdf_path}")
        else:
            logger.warning(f"  [{pid[:25]}] No pdf_path in paper_meta, cannot parse")
    elif not md_text and not mineru_client:
        logger.warning(f"  [{pid[:25]}] MinerU client not available, skipping PDF parse")

    if not md_text:
        logger.warning(f"  [{pid[:25]}] Parse failed: no text available")
        return {
            "errors": state.get("errors", []) + [
                {"node": "parse", "error": "No text available", "timestamp": datetime.now().isoformat()}
            ],
            "status": "failed",
        }

    # ── 构建文档树 + 收集各部分文本 ──
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

    # ── LLM 理解论文内容 ──
    doc_context = {}
    extraction_hints = []
    needs_lookup = False
    parse_max_tokens = _compute_parse_max_tokens(config.llm.max_tokens, md_text)

    if llm:
        # 使用动态计算的 max_tokens（比默认值更大）
        understanding = _understand_document(
            md_text, outline, abstract_text, methods_text, llm, config.parse,
            max_tokens_override=parse_max_tokens,
        )
        if understanding:
            doc_context = understanding.get("doc_context", {})
            extraction_hints = understanding.get("extraction_hints", [])
            # 根据 extraction_hints 判断是否需要 lookup phase
            needs_lookup = any(h.get("action") == "needs_lookup" for h in extraction_hints)
            crops_display = doc_context.get("crops") or doc_context.get("crop") or "?"
            study_list = doc_context.get("study_list", [])
            study_count = len(study_list) if isinstance(study_list, list) else doc_context.get("study_count", 0)
            logger.info(f"  [{pid[:25]}] Document understood: {crops_display}, "
                       f"{study_count} studies, "
                       f"{len(extraction_hints)} hints, "
                       f"needs_lookup={needs_lookup}, "
                       f"max_tokens={parse_max_tokens}")
        else:
            logger.warning(f"  [{pid[:25]}] LLM understanding failed")
    else:
        logger.warning(f"  [{pid[:25]}] LLM client not available, skipping understanding")

    # ── 质量门控 ──
    parse_quality = _check_parse_quality(doc_context, extraction_hints, pid)

    # 质量不达标时标记为 failed，避免后续节点空跑
    if parse_quality.get("overall") == "failed":
        return {
            "parsed_text": md_text,
            "tree_outline": outline,
            "abstract_text": abstract_text[:5000],
            "methods_text": methods_text[:15000],
            "doc_context": {},
            "extraction_hints": [],
            "needs_lookup": False,
            "parse_quality": parse_quality,
            "status": "failed",
            "node_status": {"parse": "failed"},
            "errors": [{"node": "parse", "error": "LLM understanding failed or quality too weak"}],
        }

    return {
        "parsed_text": md_text,
        "tree_outline": outline,
        "abstract_text": abstract_text[:5000],
        "methods_text": methods_text[:15000],
        "doc_context": doc_context,
        "extraction_hints": extraction_hints,
        "needs_lookup": needs_lookup,
        "parse_quality": parse_quality,
        "status": "parsed",
        "node_status": {"parse": "parsed"},
    }
