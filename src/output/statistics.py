"""
统计分析模块 — 评估论文信息抽取的字段覆盖率。

关注「字段是否被填充」（Coverage），不关注「字段是否正确」（Confidence）。
为 Prompt 优化、Chunk 策略优化、OCR 优化和模型迭代提供量化指标。

输出:
  - paper_coverage.csv   每篇论文的字段覆盖率
  - field_coverage.csv   每个字段的全局抽取命中率
  - summary.json         批次总体统计
  - report.md            可读统计报告

用法:
  from src.output.statistics import generate_statistics
  generate_statistics(extractions, statistics_dir)
"""

from __future__ import annotations
import csv
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
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


# ── 输出写入 ─────────────────────────────────────────────

def _json_serializable(obj):
    """将任意对象转为 JSON 可序列化格式。"""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _write_summary_json(summary: dict, path: Path):
    """写入 summary.json。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=_json_serializable)
    logger.info(f"Statistics summary: {path}")


def _write_paper_coverage_csv(rows: List[dict], path: Path):
    """写入 paper_coverage.csv。"""
    if not rows:
        return
    fields = [
        "paper_id", "total_fields", "filled_fields", "coverage",
        "paper_level", "study_level", "variety_level",
        "num_studies", "num_varieties",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Paper coverage: {path} ({len(rows)} rows)")


def _write_field_coverage_csv(rows: List[dict], path: Path):
    """写入 field_coverage.csv。"""
    if not rows:
        return
    fields = ["field_name", "level", "hit_count", "miss_count", "total_count", "coverage"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info(f"Field coverage: {path} ({len(rows)} rows)")


def _pct(value: float) -> str:
    """将小数格式化为百分比字符串。"""
    return f"{value * 100:.1f}%"


def _write_report_md(
    summary: dict,
    paper_coverage: List[dict],
    field_coverage: List[dict],
    path: Path,
):
    """生成可读的 Markdown 统计报告。"""
    lines = []

    # ── 标题 ──
    lines.append("## 抽取质量统计报告")
    lines.append("")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # ── 总体概览 ──
    lines.append("### 总体概览")
    lines.append("")
    lines.append(f"| 指标 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 论文总数 | {summary['paper_count']} |")
    lines.append(f"| 试验总数 | {summary.get('total_studies', 'N/A')} |")
    lines.append(f"| 品种记录总数 | {summary.get('total_varieties', 'N/A')} |")
    lines.append(f"| 平均覆盖率 | {_pct(summary['average_coverage'])} |")
    lines.append(f"| Paper 层覆盖率 | {_pct(summary['paper_level'])} |")
    lines.append(f"| Study 层覆盖率 | {_pct(summary['study_level'])} |")
    lines.append(f"| Variety 层覆盖率 | {_pct(summary['variety_level'])} |")
    lines.append(f"| 最佳论文 | {summary['best_paper']} |")
    lines.append(f"| 最差论文 | {summary['worst_paper']} |")
    lines.append("")

    # ── 论文覆盖率明细 ──
    lines.append("### 论文覆盖率明细")
    lines.append("")
    lines.append("| paper_id | 总字段 | 已填 | 覆盖率 | Paper层 | Study层 | Variety层 |")
    lines.append("|----------|--------|------|--------|---------|---------|-----------|")
    for p in sorted(paper_coverage, key=lambda x: x["coverage"]):
        lines.append(
            f"| {p['paper_id'][:40]} | {p['total_fields']} "
            f"| {p['filled_fields']} | {_pct(p['coverage'])} "
            f"| {_pct(p['paper_level'])} | {_pct(p['study_level'])} "
            f"| {_pct(p['variety_level'])} |"
        )
    lines.append("")

    # ── 字段覆盖率 ──
    lines.append("### 字段覆盖率")
    lines.append("")
    lines.append("| 字段 | 层级 | 命中 | 缺失 | 总数 | 覆盖率 |")
    lines.append("|------|------|------|------|------|--------|")
    for f in field_coverage:
        bar = _coverage_bar(f["coverage"])
        lines.append(
            f"| {f['field_name']} | {f['level']} "
            f"| {f['hit_count']} | {f['miss_count']} "
            f"| {f['total_count']} | {_pct(f['coverage'])} {bar} |"
        )
    lines.append("")

    # ── 分层统计 ──
    for level_name in ["paper", "study", "variety"]:
        level_fields = [f for f in field_coverage if f["level"] == level_name]
        if not level_fields:
            continue
        lines.append(f"### {level_name.capitalize()} 层字段分析")
        lines.append("")

        best5 = sorted(level_fields, key=lambda x: x["coverage"], reverse=True)[:5]
        worst5 = sorted(level_fields, key=lambda x: x["coverage"])[:5]

        lines.append("**覆盖率最高:**")
        lines.append("")
        for f in best5:
            lines.append(f"- {f['field_name']}: {_pct(f['coverage'])}")

        lines.append("")
        lines.append("**覆盖率最低:**")
        lines.append("")
        for f in worst5:
            if f["coverage"] < 1.0:
                lines.append(f"- {f['field_name']}: {_pct(f['coverage'])}")

        lines.append("")

    # ── 优化建议 ──
    lines.append("### 优化建议")
    lines.append("")
    low_fields = [f for f in field_coverage if f["coverage"] < 0.5 and f["total_count"] > 0]
    if low_fields:
        lines.append("以下字段覆盖率低于 50%，建议重点优化 Prompt 或 Chunk 策略：")
        lines.append("")
        for f in low_fields:
            lines.append(f"- **{f['field_name']}** ({f['level']}): {_pct(f['coverage'])}")
    else:
        lines.append("所有字段覆盖率均 >= 50%，整体抽取质量良好。")

    lines.append("")

    # ── 输出文件说明 ──
    lines.append("### 输出文件说明")
    lines.append("")
    lines.append("| 文件 | 说明 |")
    lines.append("|------|------|")
    lines.append("| paper_coverage.csv | 每篇论文的字段覆盖率 |")
    lines.append("| field_coverage.csv | 每个字段的全局抽取命中率 |")
    lines.append("| summary.json | 批次总体统计（机器可读） |")
    lines.append("| report.md | 本统计报告（人可读） |")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"Statistics report: {path}")


def _coverage_bar(coverage: float) -> str:
    """生成简单的文本进度条。"""
    filled = int(coverage * 10)
    empty = 10 - filled
    return f"[{'█' * filled}{'░' * empty}]"


# ── 主入口 ───────────────────────────────────────────────

def generate_statistics(
    extractions: List[dict],
    statistics_dir: Path,
):
    """
    生成全部统计文件。

    Args:
        extractions: 提取结果列表（每个元素包含 paper_id + extraction dict）
        statistics_dir: 统计输出目录
    """
    if not extractions:
        logger.warning("No extraction data to compute statistics")
        return

    logger.info(f"Computing statistics for {len(extractions)} papers...")

    all_fields = _get_all_fields()
    total_field_count = sum(len(v) for v in all_fields.values())
    logger.info(
        f"Schema fields: paper={len(all_fields['paper'])}, "
        f"study={len(all_fields['study'])}, "
        f"variety={len(all_fields['variety'])} "
        f"(total={total_field_count})"
    )

    paper_cov = compute_paper_coverage(extractions, all_fields)
    field_cov = compute_field_coverage(extractions, all_fields)
    summary = compute_summary(paper_cov, field_cov, all_fields)

    _write_paper_coverage_csv(
        paper_cov,
        statistics_dir / "paper_coverage.csv",
    )
    _write_field_coverage_csv(
        field_cov,
        statistics_dir / "field_coverage.csv",
    )
    _write_summary_json(
        summary,
        statistics_dir / "summary.json",
    )
    _write_report_md(
        summary, paper_cov, field_cov,
        statistics_dir / "report.md",
    )

    logger.info(
        f"Statistics complete: avg coverage={_pct(summary['average_coverage'])}, "
        f"paper_level={_pct(summary['paper_level'])}, "
        f"study_level={_pct(summary['study_level'])}, "
        f"variety_level={_pct(summary['variety_level'])}"
    )
