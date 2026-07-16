"""
文档分块器 — 基于 MinerU 解析的 Markdown 结构，按章节切分论文。

切分策略：
  1. 按 Markdown 标题（## / ###）切分章节
  2. 识别章节类型：摘要 / 材料与方法 / 结果 / 表格 / 讨论 / 其他
  3. 提取时只发送相关章节给 LLM，减少噪音、提高精度

用法：
  from src.core.chunker import chunk_paper
  chunks = chunk_paper(markdown_text)
  methods_text = chunks.get("methods", "")
  results_text = chunks.get("results", "")
  tables_text  = chunks.get("tables", "")
"""

from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger("paper_extractor")

# 章节类型关键词映射（中英文）
SECTION_PATTERNS = {
    "abstract": [
        r"摘\s*要", r"abstract", r"summary",
    ],
    "methods": [
        r"材料[与和]方法", r"试验[与和]方法", r"试验设计", r"材料与方法",
        r"研究方法", r"试验方案", r"田间试验", r"试验材料",
        r"materials?\s*(and|&)\s*methods?", r"experimental\s*design",
        r"study\s*area", r"field\s*experiment",
    ],
    "results": [
        r"结\s*果", r"结果[与和]分析", r"试验结果", r"产量[表分]析",
        r"results?", r"yield\s*analysis", r"experimental\s*results",
    ],
    "discussion": [
        r"讨\s*论", r"结论", r"结论[与和]讨论", r"小结",
        r"discussion", r"conclusion",
    ],
    "references": [
        r"参\s*考\s*文\s*献", r"references?", r"bibliography",
    ],
}


@dataclass
class Chunk:
    """一个章节块"""
    section_type: str          # abstract / methods / results / discussion / table / other
    title: str                 # 章节标题
    content: str               # 章节内容
    tables: List[str] = field(default_factory=list)  # 章节内的表格（markdown格式）


def chunk_paper(markdown_text: str) -> Dict[str, str]:
    """
    将论文 Markdown 按章节切分，返回各类型内容的拼接文本。

    返回 dict 的 key:
      - "abstract": 摘要
      - "methods": 材料与方法 / 试验设计
      - "results": 结果
      - "tables": 所有表格（独立拼接）
      - "full_text": 原文（兜底用）
    """
    if not markdown_text:
        return {"abstract": "", "methods": "", "results": "", "tables": "", "full_text": ""}

    chunks = _split_by_headers(markdown_text)
    categorized = _categorize_chunks(chunks)
    tables = _extract_tables(markdown_text)

    result = {
        "abstract": _join_by_type(categorized, "abstract"),
        "methods": _join_by_type(categorized, "methods"),
        "results": _join_by_type(categorized, "results"),
        "tables": "\n\n".join(tables) if tables else "",
        "full_text": markdown_text,
    }

    # 统计日志
    for k, v in result.items():
        if k != "full_text" and v:
            logger.info(f"  Chunk [{k}]: {len(v)} chars")

    return result


def build_extraction_context(chunks: Dict[str, str], max_chars: int = 60000) -> str:
    """
    将分块内容组装为 LLM 提取上下文。

    优先级：methods > results > tables > abstract
    按优先级裁剪，确保不超过 max_chars。
    """
    parts = []

    if chunks.get("methods"):
        parts.append(f"## 试验设计与方法\n\n{chunks['methods']}")

    if chunks.get("results"):
        parts.append(f"## 试验结果\n\n{chunks['results']}")

    if chunks.get("tables"):
        parts.append(f"## 数据表格\n\n{chunks['tables']}")

    if chunks.get("abstract"):
        parts.append(f"## 摘要\n\n{chunks['abstract']}")

    # 如果核心内容太短，补充全文兜底
    core_text = "\n\n".join(parts)
    if len(core_text) < 500:
        logger.warning("  Chunked content too short, falling back to full text")
        return chunks.get("full_text", "")

    # 按优先级裁剪
    if len(core_text) > max_chars:
        logger.warning(f"  Chunked content ({len(core_text)} chars) exceeds limit, truncating")
        # 按优先级逐个添加
        result_parts = []
        current_len = 0
        for part in parts:
            if current_len + len(part) > max_chars:
                remaining = max_chars - current_len
                if remaining > 200:
                    result_parts.append(part[:remaining] + "\n\n[...TRUNCATED...]")
                break
            result_parts.append(part)
            current_len += len(part)
        return "\n\n".join(result_parts)

    return core_text


def _split_by_headers(text: str) -> List[Chunk]:
    """按 Markdown 标题切分文本。"""
    # 匹配 ## 或 ### 标题行
    header_pattern = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)
    
    chunks = []
    last_end = 0
    last_title = ""
    
    for match in header_pattern.finditer(text):
        # 保存上一个块
        if match.start() > last_end:
            content = text[last_end:match.start()].strip()
            if content:
                chunks.append(Chunk(
                    section_type="other",
                    title=last_title,
                    content=content,
                ))
        
        last_title = match.group(2).strip()
        last_end = match.end()
    
    # 最后一块
    if last_end < len(text):
        content = text[last_end:].strip()
        if content:
            chunks.append(Chunk(
                section_type="other",
                title=last_title,
                content=content,
            ))

    return chunks


def _categorize_chunks(chunks: List[Chunk]) -> List[Chunk]:
    """根据标题关键词给每个 chunk 分类。"""
    for chunk in chunks:
        title_lower = chunk.title.lower()
        for section_type, patterns in SECTION_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, title_lower, re.IGNORECASE):
                    chunk.section_type = section_type
                    break
            if chunk.section_type != "other":
                break
    return chunks


def _extract_tables(text: str) -> List[str]:
    """从 Markdown 文本中提取所有表格。"""
    tables = []
    lines = text.split("\n")
    in_table = False
    current_table = []

    for line in lines:
        stripped = line.strip()
        # 检测表格行（以 | 开头或包含多个 |）
        if stripped.startswith("|") or (stripped.count("|") >= 2 and "---" in stripped):
            if not in_table:
                in_table = True
                current_table = []
            current_table.append(line)
        else:
            if in_table and current_table:
                tables.append("\n".join(current_table))
                current_table = []
            in_table = False

    # 尾部表格
    if current_table:
        tables.append("\n".join(current_table))

    return tables


def _join_by_type(chunks: List[Chunk], section_type: str) -> str:
    """拼接指定类型的 chunk 内容。"""
    parts = []
    for chunk in chunks:
        if chunk.section_type == section_type:
            header = f"### {chunk.title}\n\n" if chunk.title else ""
            parts.append(header + chunk.content)
    return "\n\n".join(parts)
