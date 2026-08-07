"""
PaperState — LangGraph 状态定义。

每篇论文在 pipeline 中的完整状态，各节点依次读写。
"""

from __future__ import annotations
from typing import Optional, List, TypedDict, Annotated
from langgraph.graph import add_messages


class PaperState(TypedDict, total=False):
    """单篇论文的处理状态。"""

    # ── 输入（BatchOrchestrator 设置）──
    paper_id: str                       # P_{md5_fingerprint}
    paper_meta: dict                    # {doi, title, year, journal, pdf_path, md_path, ...}
    stop_after: str                     # 分步执行：在此节点后停止（默认 "" = 跑完整流程）

    # ── search 节点（BatchOrchestrator 调用，批量搜索阶段）──
    search_results: list                # 搜索结果（search 节点输出）
    search_total: int                   # 搜索到的论文总数

    # ── classify 节点 ──
    classification: dict                # {category, language, reasoning, ...}

    # ── filter 节点 ──
    is_extractable: bool                # 是否通过筛选

    # ── download 节点 ──
    pdf_missing: bool                   # PDF 下载失败标记

    # ── parse 节点 ──
    parsed_text: str                    # MinerU 或 MD 全文
    tree_outline: str                   # 标题大纲文本
    abstract_text: str                  # 摘要文本
    methods_text: str                   # 方法部分文本
    doc_context: dict                   # Document Context（作物、study 数、chunk 列表等）
    extraction_hints: list              # 提取提示（字段位置、查找需求）
    needs_lookup: bool                  # 是否需要 lookup phase（去补充材料/表格查找）
    parse_quality: dict                 # parse 质量门控信息（has_crop, has_study_list, has_hints, overall）

    # ── extract_phase1 节点 ──
    phase1_result: dict                 # {paper: {...}, studies: [...]}

    # ── extract_phase2 节点 ──
    phase2_results: list                # [{study_index, varieties}, ...]
    extraction_errors: list              # [{study_index, study_title, error}, ...] LLM提取超时/无返回记录

    # ── evidence/validate 节点 ──
    validation_errors: list             # [{node, study_index, variety_index, variety_name, error}, ...] LLM验证超时记录

    # ── lookup 节点（动态，needs_lookup=True 时触发）──
    lookup_results: list                # lookup 节点输出（补充后的信息）

    # ── postprocess 节点 ──
    extraction: dict                    # {paper: {...}, studies: [...]}  最终合并结果

    # ── geocode 节点 ──
    geocoded: bool                      # 是否完成地理编码

    # ── evidence 节点（证据验证）──
    evidence_nodes: list                # 验证后的证据列表

    # ── validate 节点 ──
    validation_report: dict             # {issues: [], warnings: [], stats: {...}}
    flagged_records: list               # 需要 LLM 针对性验证的记录

    # ── output 节点 ──
    output_done: bool                   # 是否已写入 CSV

    # ── 错误追踪 ──
    errors: list                        # [{node, error, timestamp}, ...]
    status: str                         # pending/classifying/filtering/parsing/
                                        # extracting/processing/validating/
                                        # outputting/completed/failed/skipped
