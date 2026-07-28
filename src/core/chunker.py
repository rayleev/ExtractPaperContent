"""
文档树构建器 — 将 MinerU 解析的 Markdown 构建为层级树结构。

支持两阶段提取:
  Phase 1 (论文级): 摘要 + 标题大纲 + 方法 → 识别试验章节
  Phase 2 (试验级): 每个试验章节的完整内容 → 提取 study + variety 数据

核心改进（相比旧 chunker）:
  - 不再丢弃任何内容（旧版丢弃 60-80%）
  - 标题层级基于编号模式识别（2.1→level 2, 2.2.1→level 3）
  - 子章节继承父章节的分类（results 的子节点也是 results）
  - 按文档顺序收集内容，保持上下文连贯
"""

from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.core.constants import _SECTION_KEYWORDS, _PRUNE_KEYWORDS, _REVIEW_KEYWORDS, _INTRO_TITLES

logger = logging.getLogger("paper_extractor")


@dataclass
class SectionNode:
    """文档树节点 — 表示一个标题章节及其子节点。"""
    level: int                              # 标题层级 (1, 2, 3, ...)
    title: str                              # 标题文本
    content: str = ""                       # 本节点正文（标题后到下一个同级/上级标题之间的文本）
    children: List['SectionNode'] = field(default_factory=list)
    section_type: str = "other"             # abstract/methods/results/discussion/references/other
    should_prune: bool = False              # 是否应剪枝（参考文献、致谢等）

    @property
    def tables(self) -> List[str]:
        """提取本节点正文中的 Markdown 表格。"""
        return _extract_md_tables(self.content)

    def total_chars(self) -> int:
        """本节点及所有子孙节点的总字符数。"""
        total = len(self.content)
        for child in self.children:
            total += child.total_chars()
        return total


# ═══════════════════════════════════════════════════════════════
#  公共 API
# ═══════════════════════════════════════════════════════════════

def build_document_tree(markdown_text: str) -> SectionNode:
    """
    将 Markdown 文本构建为文档树。

    流程: 按标题切分 → 构建树 → 分类 → 继承 → 剪枝

    返回根节点 (level=0)，其 children 为顶层章节。
    """
    if not markdown_text:
        return SectionNode(level=0, title="root")

    flat_nodes = _split_into_flat_nodes(markdown_text)
    if not flat_nodes:
        return SectionNode(level=0, title="root", content=markdown_text)

    root = _build_tree_from_flat(flat_nodes)
    _flatten_introductions(root)
    _classify_all(root)
    _apply_inheritance(root)
    _prune_tree(root)

    # 日志统计
    type_chars = _count_type_chars(root)
    for stype, chars in sorted(type_chars.items()):
        if chars > 0:
            logger.info(f"  Tree [{stype}]: {chars} chars")

    return root


def get_section_outline(node: SectionNode, max_level: int = 4) -> str:
    """生成标题大纲（仅标题，不含正文），用于 Phase 1 让 LLM 了解文档结构。"""
    lines: List[str] = []

    def _walk(n: SectionNode, depth: int):
        if n.level > 0 and n.level <= max_level:
            indent = "  " * max(0, depth - 1)
            lines.append(f"{indent}[L{n.level}] {n.title}")
        for child in n.children:
            _walk(child, depth + 1)

    for child in node.children:
        _walk(child, 1)
    return "\n".join(lines)


def collect_content(node: SectionNode, max_chars: int = 0) -> str:
    """
    按文档顺序收集节点及其所有子孙的完整内容。

    每个章节输出格式: ### 标题\n正文（含表格）
    """
    parts: List[str] = []

    def _walk(n: SectionNode):
        if n.level > 0 and (n.content.strip() or n.title.strip()):
            prefix = "#" * min(n.level, 4)
            if n.content.strip():
                parts.append(f"{prefix} {n.title}\n\n{n.content.strip()}")
            elif n.title.strip():
                parts.append(f"{prefix} {n.title}")
        for child in n.children:
            _walk(child)

    _walk(node)
    result = "\n\n".join(parts)

    if max_chars and len(result) > max_chars:
        result = result[:max_chars] + "\n\n[...TRUNCATED...]"
    return result


def collect_tables(node: SectionNode) -> str:
    """收集节点及子孙中所有 Markdown 表格。"""
    tables: List[str] = []

    def _walk(n: SectionNode):
        tables.extend(n.tables)
        for child in n.children:
            _walk(child)

    _walk(node)
    return "\n\n".join(tables)


def find_nodes_by_type(node: SectionNode, section_type: str) -> List[SectionNode]:
    """查找所有指定类型的节点。"""
    result: List[SectionNode] = []

    def _walk(n: SectionNode):
        if n.section_type == section_type:
            result.append(n)
        for child in n.children:
            _walk(child)

    _walk(node)
    return result


def find_experiment_sections(root: SectionNode) -> List[SectionNode]:
    """
    查找包含试验数据的章节（同时拥有 methods 和 results 子节点的章节）。

    对于学位论文: 通常是第二章、第三章等实验章节。
    对于期刊论文: 可能是整篇论文本身（如果 methods 和 results 都在顶层）。
    """
    candidates: List[SectionNode] = []

    def _walk(n: SectionNode):
        if n.level > 0:
            types_in_descendants = {d.section_type for d in _all_descendants(n)}
            has_methods = "methods" in types_in_descendants or n.section_type == "methods"
            has_results = "results" in types_in_descendants or n.section_type == "results"
            if has_methods and has_results:
                candidates.append(n)
                return  # 不再深入，避免重复匹配子章节
        for child in n.children:
            _walk(child)

    _walk(root)

    # 如果没有找到明确的实验章节，退化为返回 results 节点的父节点
    if not candidates:
        results_nodes = find_nodes_by_type(root, "results")
        if results_nodes:
            parents = []
            seen = set()
            for rn in results_nodes:
                parent = _find_parent(root, rn)
                if parent and id(parent) not in seen:
                    parents.append(parent)
                    seen.add(id(parent))
            candidates = parents

    return candidates


def save_tree_debug(tree: SectionNode, filepath):
    """保存树结构调试信息到文件。"""
    with open(filepath, "w", encoding="utf-8") as f:
        def _walk(n: SectionNode, depth: int):
            indent = "  " * depth
            chars = len(n.content)
            total = n.total_chars()
            prune_mark = " [PRUNED]" if n.should_prune else ""
            type_mark = f" ({n.section_type})" if n.section_type != "other" else ""
            table_mark = f" [+{len(n.tables)} tables]" if n.tables else ""
            f.write(
                f"{indent}[L{n.level}] {n.title[:60]}"
                f"  -- {chars} chars, total {total}{type_mark}{table_mark}{prune_mark}\n"
            )
            for child in n.children:
                _walk(child, depth + 1)

        _walk(tree, 0)


# ═══════════════════════════════════════════════════════════════
#  向后兼容（旧 API）
# ═══════════════════════════════════════════════════════════════

def chunk_paper(markdown_text: str) -> Dict[str, str]:
    """[兼容] 旧 API — 将论文按章节切分。内部使用新的树构建器。"""
    tree = build_document_tree(markdown_text)
    return {
        "abstract": _collect_type_content(tree, "abstract"),
        "methods": _collect_type_content(tree, "methods"),
        "results": _collect_type_content(tree, "results"),
        "tables": collect_tables(tree),
        "full_text": markdown_text,
    }


def build_extraction_context(chunks: Dict[str, str], max_chars: int = 60000) -> str:
    """[兼容] 旧 API — 将分块内容组装为 LLM 提取上下文。"""
    parts = []
    if chunks.get("methods"):
        parts.append(f"## 试验设计与方法\n\n{chunks['methods']}")
    if chunks.get("results"):
        parts.append(f"## 试验结果\n\n{chunks['results']}")
    if chunks.get("tables"):
        parts.append(f"## 数据表格\n\n{chunks['tables']}")
    if chunks.get("abstract"):
        parts.append(f"## 摘要\n\n{chunks['abstract']}")

    core_text = "\n\n".join(parts)
    if len(core_text) < 500:
        logger.warning("  Chunked content too short, falling back to full text")
        return chunks.get("full_text", "")

    if len(core_text) > max_chars:
        logger.warning(f"  Chunked content ({len(core_text)} chars) exceeds limit, truncating")
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


# ═══════════════════════════════════════════════════════════════
#  内部实现
# ═══════════════════════════════════════════════════════════════

# ── 标题层级检测 ──────────────────────────────────────────────

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
_CHAPTER_RE = re.compile(r'^第[一二三四五六七八九十百千\d]+[章节编篇]')
# Only match numbered sections with at least one dot (e.g., 2.1, 2.1.1)
# A bare number like "293" in "嘉育293" should NOT match
_NUMBERED_RE = re.compile(r'^(\d+(?:\.\d+)+)\s')
# Single number followed by text (e.g., "1 Introduction", "2 Methods")
_SINGLE_NUM_RE = re.compile(r'^(\d+)\s+(.+)')

# ── 标题层级检测 ──────────────────────────────────────────────

def _detect_level(heading_text: str, hash_count: int) -> int:
    """
    检测标题层级。优先级:
      1. 中文章节标记 (第一章 → level 1)
      2. 带点编号 (2.1 → level 2, 2.1.1 → level 3)
      3. 单位数编号 (1-30 → level 1，排除品种名中的大数字如"嘉育293")
      4. # 数量回退
    """
    text = heading_text.strip()

    # 1. 中文章节标记
    if _CHAPTER_RE.match(text):
        return 1

    # 2. 带点编号 (2.1, 3.2.1, etc.)
    m = _NUMBERED_RE.match(text)
    if m:
        return m.group(1).count('.') + 1

    # 3. 单位数编号 (1-30) — 仅当数字较小且后面有文字时视为章节号
    m = _SINGLE_NUM_RE.match(text)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 30:
            return 1

    # 4. # 数量回退
    return hash_count


# ── 扁平节点提取 ──────────────────────────────────────────────

@dataclass
class _FlatNode:
    """中间结构：按标题切分后的扁平节点。"""
    level: int
    title: str
    content: str


def _split_into_flat_nodes(text: str) -> List[_FlatNode]:
    """按 Markdown 标题切分文本为扁平节点列表。"""
    nodes: List[_FlatNode] = []
    last_end = 0
    last_level = 0
    last_title = ""

    for match in _HEADING_RE.finditer(text):
        # 保存上一个标题之后的内容
        if match.start() > last_end:
            content = text[last_end:match.start()].strip()
            if content or last_title:
                nodes.append(_FlatNode(level=last_level, title=last_title, content=content))

        hash_count = len(match.group(1))
        heading_text = match.group(2).strip()
        last_level = _detect_level(heading_text, hash_count)
        last_title = heading_text
        last_end = match.end()

    # 最后一块
    if last_end < len(text):
        content = text[last_end:].strip()
        if content or last_title:
            nodes.append(_FlatNode(level=last_level, title=last_title, content=content))

    return nodes


# ── 树构建 ─────────────────────────────────────────────────────

def _build_tree_from_flat(flat_nodes: List[_FlatNode]) -> SectionNode:
    """将扁平节点列表构建为层级树。"""
    root = SectionNode(level=0, title="root")
    stack: List[SectionNode] = [root]

    for fn in flat_nodes:
        new_node = SectionNode(level=fn.level, title=fn.title, content=fn.content)

        # 找到正确的父节点：栈中最后一个 level < 当前 level 的节点
        while len(stack) > 1 and stack[-1].level >= fn.level:
            stack.pop()

        parent = stack[-1]
        parent.children.append(new_node)
        stack.append(new_node)

    return root


# ── 引言展平 ──────────────────────────────────────────────────

def _flatten_introductions(node: SectionNode):
    """
    展平"引言"节点：将其子节点重新归属到正确的章节标题下。

    中文学位论文中，"引言"常出现在章节标题和编号子节之间，
    导致该章节的所有子节（如 2.1, 2.2）错误地成为"引言"的子节点。

    处理策略：
      1. 如果"引言"前面有同级的章节标题，将子节点移到该章节下
      2. 否则直接提升到"引言"的父级
    """
    new_children: List[SectionNode] = []
    for i, child in enumerate(node.children):
        if child.title.strip() in _INTRO_TITLES and child.children:
            # 如果引言有自己的正文内容，保留为一个叶子节点
            if child.content.strip():
                intro_leaf = SectionNode(level=child.level, title=child.title, content=child.content)
                new_children.append(intro_leaf)

            # 尝试找到前面最近的同级章节标题（非引言、非空内容）
            preceding_chapter = None
            for prev in reversed(new_children):
                if (prev.level <= child.level
                        and prev.title.strip() not in _INTRO_TITLES
                        and prev.content.strip() == ""
                        and prev.title.strip()):
                    preceding_chapter = prev
                    break

            if preceding_chapter:
                # 将引言的子节点移到该章节标题下
                preceding_chapter.children.extend(child.children)
            else:
                # 直接提升
                new_children.extend(child.children)
        else:
            _flatten_introductions(child)
            new_children.append(child)
    node.children = new_children


# ── 分类 ──────────────────────────────────────────────────────

def _classify_all(node: SectionNode):
    """对所有节点进行分类和剪枝标记。"""
    _classify_recursive(node, is_root=True)


def _classify_recursive(node: SectionNode, is_root: bool = False):
    """递归分类：先检查剪枝 → 再检查章节类型。"""
    if not is_root:
        title_lower = node.title.lower()

        # 剪枝检查
        for pattern in _PRUNE_KEYWORDS:
            if re.search(pattern, title_lower, re.IGNORECASE):
                node.should_prune = True
                break

        # 文献综述检查
        if not node.should_prune:
            for pattern in _REVIEW_KEYWORDS:
                if re.search(pattern, title_lower, re.IGNORECASE):
                    node.should_prune = True
                    break

        # 章节类型分类
        if not node.should_prune:
            for section_type, patterns in _SECTION_KEYWORDS.items():
                for pattern in patterns:
                    if re.search(pattern, title_lower, re.IGNORECASE):
                        node.section_type = section_type
                        break
                if node.section_type != "other":
                    break

    for child in node.children:
        _classify_recursive(child)


def _apply_inheritance(node: SectionNode):
    """子节点继承父节点的 section_type（仅当子节点为 other 时）。"""
    for child in node.children:
        if child.section_type == "other" and node.section_type != "other":
            child.section_type = node.section_type
        _apply_inheritance(child)


def _prune_tree(node: SectionNode):
    """移除标记为剪枝的节点。如果被剪枝节点有未剪枝的子节点，保留子节点。"""
    new_children: List[SectionNode] = []
    for child in node.children:
        if child.should_prune:
            # 保留未剪枝的孙子节点（提升到当前层级）
            for grandchild in child.children:
                if not grandchild.should_prune:
                    new_children.append(grandchild)
        else:
            _prune_tree(child)
            new_children.append(child)
    node.children = new_children


# ── 辅助函数 ──────────────────────────────────────────────────

def _all_descendants(node: SectionNode) -> List[SectionNode]:
    """获取所有子孙节点。"""
    result: List[SectionNode] = []
    for child in node.children:
        result.append(child)
        result.extend(_all_descendants(child))
    return result


def _find_parent(root: SectionNode, target: SectionNode) -> Optional[SectionNode]:
    """查找目标节点的父节点。"""
    for child in root.children:
        if child is target:
            return root
        found = _find_parent(child, target)
        if found:
            return found
    return None


def _count_type_chars(node: SectionNode) -> Dict[str, int]:
    """统计各类型的字符数。"""
    counts: Dict[str, int] = {}

    def _walk(n: SectionNode):
        if n.level > 0:
            t = n.section_type
            counts[t] = counts.get(t, 0) + len(n.content)
        for child in n.children:
            _walk(child)

    _walk(node)
    return counts


def _collect_type_content(tree: SectionNode, section_type: str) -> str:
    """收集所有指定类型节点的内容（按文档顺序）。"""
    nodes = find_nodes_by_type(tree, section_type)
    parts: List[str] = []
    for n in nodes:
        header = f"### {n.title}\n\n" if n.title else ""
        if n.content.strip():
            parts.append(header + n.content.strip())
    return "\n\n".join(parts)


def _extract_md_tables(text: str) -> List[str]:
    """从文本中提取所有 Markdown 表格。"""
    tables: List[str] = []
    lines = text.split("\n")
    in_table = False
    current_table: List[str] = []

    for line in lines:
        stripped = line.strip()
        is_table_line = (
            stripped.startswith("|")
            or (stripped.count("|") >= 2 and "---" in stripped)
        )
        if is_table_line:
            if not in_table:
                in_table = True
                current_table = []
            current_table.append(line)
        else:
            if in_table and current_table:
                tables.append("\n".join(current_table))
                current_table = []
            in_table = False

    if current_table:
        tables.append("\n".join(current_table))

    return tables
