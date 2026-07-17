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

    # ── classify 节点 ──
    classification: dict                # {category, language, reasoning, ...}

    # ── filter 节点 ──
    is_extractable: bool                # 是否通过筛选

    # ── parse 节点 ──
    parsed_text: str                    # MinerU 或 MD 全文
    tree_outline: str                   # 标题大纲文本
    abstract_text: str                  # 摘要文本
    methods_text: str                   # 方法部分文本

    # ── extract_phase1 节点 ──
    phase1_result: dict                 # {paper: {...}, experiment_sections: [...]}

    # ── extract_phase2 节点 ──
    phase2_results: list                # [{study_title, varieties, ...}, ...]

    # ── postprocess 节点 ──
    extraction: dict                    # {paper: {...}, studies: [...]}  最终合并结果

    # ── geocode 节点 ──
    geocoded: bool                      # 是否完成地理编码

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
