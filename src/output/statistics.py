"""
统计分析模块 — 评估论文信息抽取的字段覆盖率。

关注「字段是否被填充」（Coverage），不关注「字段是否正确」（Confidence）。
为 Prompt 优化、Chunk 策略优化、OCR 优化和模型迭代提供量化指标。

输出（全部写入 PostgreSQL，由 src/graph/output.py 的 insert_statistics 落库）:
  - pe_aud_paper_coverage   每篇论文的字段覆盖率
  - pe_aud_field_coverage   每个字段的全局抽取命中率
  - pe_aud_stats_summary    批次总体统计

用法:
  from src.output.statistics import compute_paper_coverage, compute_field_coverage, compute_summary
  paper_cov = compute_paper_coverage(extractions, all_fields)
"""

from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Any, Optional

from src.core.models import ExtractionResult, PaperInfo, StudyInfo, VarietyYield

logger = logging.getLogger("paper_extractor")


# ── 字段定义（从 Pydantic Schema 自动获取）────────────────

def _get_model_fields(model_cls) -> List[str]:
    """获取 Pydantic 模型的所有字段名（排除 varieties/studies 等嵌套字段）。"""
    return [
        name for name, info in model_cls.model_fields.items()
        if name not in ("varieties", "studies", "paper")
    ]


def _get_all_fields() -> Dict[str, List[str]]:
    """获取三级层次的所有字段名。"""
    return {
        "paper": _get_model_fields(PaperInfo),
        "study": _get_model_fields(StudyInfo),
        "variety": _get_model_fields(VarietyYield),
    }


# ── 判定规则 ─────────────────────────────────────────────

def _is_filled(value) -> bool:
    """
    判断字段值是否为「成功抽取」（Hit）。

    命中条件（满足任一即为 Hit）：
      - 值非 None
      - 值非空字符串 ""
      - 值非空列表 []
      - 值非空字典 {}
    """
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    if isinstance(value, list) and len(value) == 0:
        return False
    if isinstance(value, dict) and len(value) == 0:
        return False
    return True


# ── 核心统计 ─────────────────────────────────────────────

def compute_paper_coverage(
    extractions: List[dict],
    all_fields: Dict[str, List[str]],
) -> List[dict]:
    """
    计算每篇论文的字段覆盖率。

    返回每篇论文一行，包含 total_fields / filled_fields / coverage
    以及 paper_level / study_level / variety_level 分层覆盖率。
    """
    rows = []
    paper_fields = all_fields["paper"]
    study_fields = all_fields["study"]
    variety_fields = all_fields["variety"]
    total_fields = len(paper_fields) + len(study_fields) + len(variety_fields)

    for ext in extractions:
        paper_id = ext.get("paper_id", "unknown")
        data = ext.get("extraction")
        if not data:
            rows.append({
                "paper_id": paper_id,
                "total_fields": total_fields,
                "filled_fields": 0,
                "coverage": 0.0,
                "paper_level": 0.0,
                "study_level": 0.0,
                "variety_level": 0.0,
                "num_studies": 0,
                "num_varieties": 0,
            })
            continue

        # Paper 层
        paper_data = data.get("paper", {})
        paper_filled = sum(1 for f in paper_fields if _is_filled(paper_data.get(f)))
        paper_level = paper_filled / len(paper_fields) if paper_fields else 0.0

        # Study 层（所有 study 取平均）
        studies = data.get("studies", [])
        num_studies = len(studies)
        if num_studies > 0:
            study_rates = []
            for s in studies:
                sf = sum(1 for f in study_fields if _is_filled(s.get(f)))
                study_rates.append(sf / len(study_fields) if study_fields else 0.0)
            study_level = sum(study_rates) / len(study_rates)
        else:
            study_level = 0.0

        # Variety 层（所有 variety 取平均）
        all_varieties = []
        for s in studies:
            all_varieties.extend(s.get("varieties", []))
        num_varieties = len(all_varieties)
        if num_varieties > 0:
            variety_rates = []
            for v in all_varieties:
                vf = sum(1 for f in variety_fields if _is_filled(v.get(f)))
                variety_rates.append(vf / len(variety_fields) if variety_fields else 0.0)
            variety_level = sum(variety_rates) / len(variety_rates)
        else:
            variety_level = 0.0

        # 综合覆盖率（加权平均）
        total_possible = (
            len(paper_fields)
            + len(study_fields) * max(num_studies, 1)
            + len(variety_fields) * max(num_varieties, 1)
        )
        total_filled = (
            paper_filled
            + sum(
                sum(1 for f in study_fields if _is_filled(s.get(f)))
                for s in studies
            )
            + sum(
                sum(1 for f in variety_fields if _is_filled(v.get(f)))
                for s in studies for v in s.get("varieties", [])
            )
        )
        coverage = total_filled / total_possible if total_possible > 0 else 0.0

        rows.append({
            "paper_id": paper_id,
            "total_fields": total_fields,
            "filled_fields": total_filled,
            "coverage": round(coverage, 4),
            "paper_level": round(paper_level, 4),
            "study_level": round(study_level, 4),
            "variety_level": round(variety_level, 4),
            "num_studies": num_studies,
            "num_varieties": num_varieties,
        })

    return rows


def compute_field_coverage(
    extractions: List[dict],
    all_fields: Dict[str, List[str]],
) -> List[dict]:
    """
    计算每个字段在所有论文中的抽取命中率。

    返回每个字段一行，包含 hit_count / miss_count / coverage。
    """
    paper_fields = all_fields["paper"]
    study_fields = all_fields["study"]
    variety_fields = all_fields["variety"]

    # 统计结构: {field_name: {"level": str, "hit": int, "total": int}}
    field_stats: Dict[str, dict] = {}

    for level_name, fields in [("paper", paper_fields), ("study", study_fields), ("variety", variety_fields)]:
        for f in fields:
            field_stats[f] = {"level": level_name, "hit": 0, "total": 0}

    for ext in extractions:
        data = ext.get("extraction")
        if not data:
            continue

        # Paper 层
        paper_data = data.get("paper", {})
        for f in paper_fields:
            field_stats[f]["total"] += 1
            if _is_filled(paper_data.get(f)):
                field_stats[f]["hit"] += 1

        # Study 层
        studies = data.get("studies", [])
        for s in studies:
            for f in study_fields:
                field_stats[f]["total"] += 1
                if _is_filled(s.get(f)):
                    field_stats[f]["hit"] += 1

            # Variety 层
            for v in s.get("varieties", []):
                for f in variety_fields:
                    field_stats[f]["total"] += 1
                    if _is_filled(v.get(f)):
                        field_stats[f]["hit"] += 1

    rows = []
    for field_name, stats in field_stats.items():
        hit = stats["hit"]
        total = stats["total"]
        rows.append({
            "field_name": field_name,
            "level": stats["level"],
            "hit_count": hit,
            "miss_count": total - hit,
            "total_count": total,
            "coverage": round(hit / total, 4) if total > 0 else 0.0,
        })

    # 按覆盖率升序排列（最低覆盖率的排在最前面，方便发现薄弱环节）
    rows.sort(key=lambda r: r["coverage"])
    return rows


def compute_summary(
    paper_coverage: List[dict],
    field_coverage: List[dict],
    all_fields: Dict[str, List[str]],
) -> dict:
    """计算批次总体统计信息。"""
    paper_count = len(paper_coverage)

    if paper_count == 0:
        return {
            "paper_count": 0,
            "average_coverage": 0.0,
            "paper_level": 0.0,
            "study_level": 0.0,
            "variety_level": 0.0,
            "best_paper": "",
            "worst_paper": "",
            "top_missing_fields": [],
            "generated_at": datetime.now().isoformat(),
        }

    avg_coverage = sum(p["coverage"] for p in paper_coverage) / paper_count
    avg_paper = sum(p["paper_level"] for p in paper_coverage) / paper_count
    avg_study = sum(p["study_level"] for p in paper_coverage) / paper_count
    avg_variety = sum(p["variety_level"] for p in paper_coverage) / paper_count

    best = max(paper_coverage, key=lambda p: p["coverage"])
    worst = min(paper_coverage, key=lambda p: p["coverage"])

    # 按缺失次数排序，取 Top 10
    missing_sorted = sorted(
        field_coverage, key=lambda f: f["miss_count"], reverse=True
    )
    top_missing = [
        {"field": f["field_name"], "level": f["level"], "miss_count": f["miss_count"], "coverage": f["coverage"]}
        for f in missing_sorted[:10]
        if f["miss_count"] > 0
    ]

    return {
        "paper_count": paper_count,
        "average_coverage": round(avg_coverage, 4),
        "paper_level": round(avg_paper, 4),
        "study_level": round(avg_study, 4),
        "variety_level": round(avg_variety, 4),
        "best_paper": best["paper_id"],
        "worst_paper": worst["paper_id"],
        "top_missing_fields": top_missing,
        "total_studies": sum(p["num_studies"] for p in paper_coverage),
        "total_varieties": sum(p["num_varieties"] for p in paper_coverage),
        "generated_at": datetime.now().isoformat(),
    }

