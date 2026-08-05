"""
Phase 1 提取节点 — 论文级 + 试验级提取。

输入：parse 输出（doc_context + extraction_hints）+ 摘要 + 标题大纲 + 方法部分
输出：paper 元数据 + studies 列表（试验级信息）

策略：
  - parse 成功时，复用 doc_context（作物、study 数等），extraction_hints 辅助定位
  - parse 失败时，回退到原逻辑，自己理解全部
"""

from __future__ import annotations
import logging
from pathlib import Path

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.graph.state import PaperState

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
    Phase 1 提取：论文级 + 试验级信息。

    提取论文元数据（crop_species 等）和试验级信息（study 列表）。

    策略：
      - parse 成功时，复用 doc_context，利用 extraction_hints 辅助定位
      - parse 失败时，回退到原逻辑，自己理解全部
    """
    pid = state["paper_id"]
    paper_meta = state["paper_meta"]
    prompt_template = _load_prompt("extract_paper.txt")

    outline = state.get("tree_outline", "")
    abstract = state.get("abstract_text", "")[:5000]
    methods = state.get("methods_text", "")[:15000]

    # parse 输出（辅助上下文）
    doc_context = state.get("doc_context", {})
    extraction_hints = state.get("extraction_hints", [])
    parse_success = bool(doc_context)

    # 获取作物列表
    crops = doc_context.get("crops", [])
    classification = state.get("classification", [])
    if not crops and classification:
        crops = classification.get("crops", [])

    # 构建辅助上下文文本
    if parse_success:
        hints_text = "\n".join(
            f"- [{h.get('action', '?')}] {h.get('field', '')}: {h.get('value', '')}"
            for h in extraction_hints
        ) or "(无提取提示)"

        crops_text = ", ".join(crops) if crops else doc_context.get('crop', '')

        auxiliary_context = f"""
---

## 辅助上下文（parse 节点输出）

以下信息已由 parse 节点识别，请**复用**并**验证**：

**作物**: {crops_text}
**Study 数量**: {doc_context.get('study_count', '')}
**补充材料**: {'是' if doc_context.get('has_supplementary') else '否'}
**表格引用**: {', '.join(doc_context.get('table_refs', [])) or '无'}
**数据文件链接**: {doc_context.get('data_file_link', '无')}

## 提取提示（extraction_hints）

以下提示可帮助你**定位**相关内容：

{hints_text}

**使用方式**：
- `ok` → 信息完整，可直接复用
- `needs_lookup` → 需要去材料方法/补充表/表格查找完整信息
- `verify` → 信息不足或模糊，需核实

---

## 多作物处理

如果论文研究多种作物（如水稻和玉米），请按作物拆分 study：
- 每个 study 只包含一种作物的试验
- study_title 应包含作物名称（如"水稻品种比较试验"、"玉米品种比较试验"）
- 从 extraction_hints 中筛选该作物相关的提示

"""
    else:
        auxiliary_context = """
---

## 辅助上下文

parse 节点未成功，请自行理解论文内容。

"""

    prompt = prompt_template.format(
        paper_id=pid,
        doi=paper_meta.get("doi", ""),
        title=paper_meta.get("title", ""),
        year=paper_meta.get("year", ""),
        journal=paper_meta.get("journal", ""),
        outline=outline,
        abstract=abstract,
        methods=methods,
        auxiliary_context=auxiliary_context,
    )

    logger.info(f"  [{pid[:25]}] Phase1: prompt {len(prompt)} chars, "
                f"parse={'success' if parse_success else 'failed'}, calling LLM (max_tokens={config.llm.max_tokens})...")
    result = llm.call_json(prompt, max_tokens=config.llm.max_tokens, node_name="extract_phase1")
    if not result:
        logger.warning(f"  [{pid[:25]}] Phase1 FAILED: LLM returned no result (timeout/error)")
        # 记录提取错误，供 postprocess 区分"超时"和"无数据"
        if "extraction_errors" not in state:
            state["extraction_errors"] = []
        state["extraction_errors"].append({
            "study_index": -1,
            "study_title": "Phase1全文提取",
            "error": "LLM提取超时或无返回",
        })
        return {
            "phase1_result": {"paper": {}, "studies": []},
            "extraction_errors": state.get("extraction_errors", []),
            "status": "phase1_failed",
        }

    studies = result.get("studies", [])
    logger.info(f"  [{pid[:25]}] Phase1 done: {len(studies)} study/studies identified")

    return {
        "phase1_result": result,
        "status": "phase1_done",
    }
