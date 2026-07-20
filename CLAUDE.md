# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Paper Extractor** — 农业科研文献结构化数据提取系统。从 PDF 论文中自动提取品种产量试验数据，基于 LangGraph StateGraph 有向图架构，每篇论文独立走完整条流程。

核心原则：**不让 LLM "猜" 数据**。经纬度/海拔等可计算字段均由程序根据地名反查（geo_source 字段标记来源），LLM 只负责抄录论文中明确写出的数值。

## Commands

```bash
# 安装依赖
pip install -r requirements.txt

# 完整流程
python run.py

# 分步执行（自动补全前置步骤）
python run.py --step classify          # 仅分类
python run.py --step parse             # 分类 + 解析
python run.py --step extract           # 完整流程

# 处理特定论文
python run.py --paper "早稻"           # 按 DOI/标题关键词匹配

# 地理编码模块自测（无需 config.yaml 时传 tk）
python -m src.core.geocoder [tk]

# 清除断点（从头重跑）
del cache\langgraph_checkpoint.db
del cache\geocoding_cache.json
```

**无 lint / 无测试框架**。项目未配置 ruff/mypy/pytest。修改后请手动跑 `python -m src.core.geocoder` 或 `python run.py --step classify` 验证。

## Architecture

### 有向图流程 (src/graph/graph.py)

```
START → classify → filter → parse → extract_phase1 → extract_phase2
  → postprocess → geocode → validate → [flagged?] → targeted_validate → END
```

- **条件路由**：filter/parse/phase1/postprocess 失败时提前退出
- **分步执行**：通过 `PaperState.stop_after` 字段控制，在指定节点后 END
- **节点计时**：每个节点包装 `_timed()`，>1s 打印 INFO
- **断点续跑**：`SqliteSaver` checkpoint，thread_id = paper_id

### 状态定义 (src/graph/state.py)

`PaperState(TypedDict)` — 单篇论文在 pipeline 中的完整状态，各节点依次读写。关键字段：
- 输入：`paper_id`, `paper_meta`, `stop_after`
- 提取结果：`phase1_result`, `phase2_results`, `extraction`（最终合并结果）
- 追踪：`status`, `errors`, `flagged_records`

### 批量编排 (src/graph/batch.py)

`BatchOrchestrator` 管理并发处理：
- **滑动窗口并发**：ThreadPoolExecutor 始终保持 N 个任务在跑（`extract_workers`）
- **注册表防重**：SQLite `paper_status` 表记录每篇论文完成的最高步骤
- **输出持久化**：完整提取结果写入 `output/paper_data.db`

### 两阶段提取 (src/graph/nodes/extract_phase1.py, extract_phase2.py)

- **Phase 1**（论文级）：输入摘要+大纲+方法 → 识别试验章节，输出 `phase1_result`
- **Phase 2**（试验级，**主要瓶颈**）：逐章节 LLM 调用 → 每个试验 1 次大 LLM 调用（max_tokens=8192）

### 地理编码 (src/geocoder.py)

**经纬度（4 级优先）**：查找表 → 天地图 → 百度(可选) → 省会兜底
**海拔（3 级优先）**：geocode 结果 → Open-Meteo Elevation API（指数退避重试 3 次） → 省会海拔近似值

关键设计：
- 查找表覆盖中国农科院/省级农科院/主要农业大学（关键词最长匹配）
- 天地图：`status="0"` 表示成功，`location.lon/lat/level`
- Open-Meteo：`{"elevation": [数值]}`，无需 Key
- 缓存持久化到 `cache/geocoding_cache.json`，键 `region||site`

### 数据模型 (src/core/models.py)

Pydantic v2 三级层次：`ExtractionResult → PaperInfo + List[StudyInfo] → VarietyYield`

字段标记约定：
- `[REQUIRED]`：LLM 必须填充
- `[OPTIONAL]`：有则填（如论文明文写出的经纬度）
- `[PROGRAM]`：程序自动计算（如 yield_standard_value）
- `[DO NOT FILL]`：系统后处理填充（如 geo_source, notes）

### 单位换算 (src/core/models.py)

LLM 提取原始产量值和单位，程序自动换算为 kg/ha：
- kg/ha, kg/hm² → ×1
- t/ha, t/hm² → ×1000
- kg/亩, kg/mu → ×15
- 斤/亩 → ×7.5
- g/株, kg/plot → 不可换算 → None

## Configuration (config.yaml)

**无外部 SDK**：MinerU / LLM / 地理编码全部通过 HTTP + `requests`/`httpx` 调用。

关键配置项：
- `extraction.crops`：目标作物列表，**无需改 prompt**，classify 节点动态注入
- `extraction.extractable_categories`：可提取的论文类别
- `geocoding.tianditu_tk`：天地图 API Key（推荐）
- `geocoding.baidu_api_key`：百度 Key（可选，空则跳过）
- `concurrency.extract_workers`：并发论文数（滑动窗口）

## Output

- **主数据库**：`output/paper_data.db`（SQLite，跨运行共享）
- **交接用宽表**：`varieties_flat`（paper+study+variety 全字段）
- **数据字典**：DB 内 `_schema_doc` 表，103 个字段中文注释
- **运行日志**：`output/runs/{timestamp}/logs/extractor.log`（含节点计时）

## Development Notes

### 添加新节点
1. 在 `src/graph/nodes/` 新建 `mynode.py`，函数签名 `(state: PaperState, config, ...) -> dict`
2. 在 `src/graph/state.py` 的 `PaperState` 添加输出字段
3. 在 `src/graph/graph.py` 注册节点 + 条件路由函数
4. 在 `src/graph/nodes/__init__.py` 导出

### 修改 Prompt
`src/prompts/` 下的 `.txt` 文件，修改后清除 checkpoint 重跑：
```bash
del cache\langgraph_checkpoint.db
```

### 地理编码缓存
- 命中缓存直接返回 `GeoResult`，不请求 API
- 缓存键：`region||site`（注意 `||` 分隔符）
- 强制刷新：删除 `cache/geocoding_cache.json`

### 防重机制
- **稳定 paper_id**：`P_{MD5(标题归一化)[:10]}`，同一标题跨运行 ID 不变
- 强制重跑：从 `paper_status` 表删除对应记录

### 外部服务依赖
| 服务 | 用途 | 必须 |
|------|------|------|
| MinerU | PDF → Markdown | 是 |
| LLM API (OpenAI 兼容) | 分类 + 提取 | 是 |
| 天地图 API | 地理编码（推荐） | 是 |
| Open-Meteo | 海拔（无需 Key） | 是（兜底用） |
| 百度地图 API | 地理编码备选 | 否 |
