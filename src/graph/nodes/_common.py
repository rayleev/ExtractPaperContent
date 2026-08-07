"""
extract_phase1 / extract_phase2 共享的工具函数。

目前仅提供 build_relevant_content_from_hints：从 extraction_hints
构建传给 LLM 的相关内容文本。
"""

from __future__ import annotations


def build_relevant_content_from_hints(hints: list, max_chars: int = 8000) -> str:
    """
    从 extraction_hints 构建相关内容文本。

    每个 hint 包含 context（字段出现的原文片段）和
    source_table / source_tables（来源表格）。去重后拼接，
    作为 LLM 输入（替代整篇论文），并附来源表格信息便于定位。
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

        # 兼容新格式 source_table（字符串）与旧格式 source_tables（列表）
        source_table = hint.get("source_table", "")
        source_tables = hint.get("source_tables", [])
        if source_table:
            table_info = f" [来源: {source_table}]"
        elif source_tables:
            table_info = f" [来源: {', '.join(source_tables)}]"
        else:
            table_info = ""

        part = f"{context}{table_info}"
        if total_chars + len(part) > max_chars:
            break
        parts.append(part)
        total_chars += len(part)

    return "\n\n---\n\n".join(parts)
