# Paper Extractor — 科研文献结构化数据提取工具

基于 LangGraph 的农业科研文献结构化数据提取系统。从 PDF 论文中自动提取品种产量试验数据，支持论文分类、PDF 解析、两阶段 LLM 提取、地理编码、单位换算、规则验证和覆盖率统计。

## 架构概览

采用 LangGraph StateGraph 有向图架构，每篇论文独立走完整条流程，通过 checkpoint 实现断点续跑。所有输出统一写入 SQLite 数据库，支持 1500 万级论文的大规模存储和查询。

```
论文 PDF + 元数据 CSV
       │
       ▼
┌──────────────┐
│  classify     │  LLM 分类（仅用元数据，支持多作物配置）
└──────┬───────┘
       ▼
┌──────────────┐
│  filter       │  筛选：China + 可提取类别
└──────┬───────┘  ──→ [不可提取] → END
       ▼
┌──────────────┐
│  parse        │  MinerU OCR / 复用已有 MD + 文档树构建
└──────┬───────┘  ──→ [解析失败] → END
       ▼
┌──────────────┐
│  extract_p1   │  Phase 1: 论文级（摘要+大纲+方法 → paper字段+试验章节识别）
└──────┬───────┘  ──→ [Phase1失败] → END
       ▼
┌──────────────┐
│  extract_p2   │  Phase 2: 试验级（逐章节 → study+variety数据）⚠️ 主要耗时
└──────┬───────┘
       ▼
┌──────────────┐
│  postprocess  │  Pydantic验证 + 产量换算 + 盆栽过滤 + 品种code回填 + site回填
└──────┬───────┘  ──→ [后处理失败] → END
       ▼
┌──────────────┐
│  geocode      │  地理编码（查找表 → 天地图 → 百度 → 省会兜底）
└──────┬───────┘
       ▼
┌──────────────┐
│  validate     │  规则验证（产量换算一致性、范围检查、增产率校验等）
└──────┬───────┘
       ▼  ──→ [无异常] → END
┌──────────────────┐
│  targeted_validate│  针对性 LLM 验证（仅验证规则标记为异常的记录）
└──────┬───────────┘
       ▼
      END → SQLite DB + CSV 导出 + 验证报告 + 覆盖率统计
```

**核心特性：**

- 断点续跑：SQLite checkpoint，崩溃后从最后成功节点恢复
- 条件路由：不可提取/解析失败的论文直接跳过
- 节点计时：每个节点自动记录执行耗时（日志可见）
- 滑动窗口并发：ThreadPoolExecutor 始终保持 N 个任务在跑，完成即补
- 分步执行：`--step classify/parse/extract`，自动补全前置步骤
- 防重机制：稳定 paper_id（MD5 指纹）+ paper_status 注册表
- 多作物支持：config.yaml 配置目标作物列表，prompt 动态联动
- 统一 SQLite 输出：所有数据写入单个 DB 文件，含完整数据字典

## 目录结构

```
extract4paperQC/
├── run.py                          # CLI 入口
├── config.yaml                     # 配置文件
├── requirements.txt                # Python 依赖
├── src/
│   ├── config.py                   #   配置加载 (AppConfig)
│   ├── clients/
│   │   ├── mineru.py               #   MinerU PDF 解析客户端
│   │   └── llm.py                  #   LLM API 客户端（含限流）
│   ├── core/                       #   共享基础模块
│   │   ├── loader.py               #   论文发现与元数据匹配
│   │   ├── chunker.py              #   文档层级树构建器
│   │   ├── geocoder.py             #   地理编码（5 级策略）
│   │   └── models.py               #   Pydantic 数据模型 + 产量换算
│   ├── graph/                      #   LangGraph Pipeline
│   │   ├── state.py                #   PaperState 状态定义
│   │   ├── graph.py                #   StateGraph + 条件路由 + 节点计时 + 分步控制
│   │   ├── batch.py                #   BatchOrchestrator（并发+断点续跑+注册表）
│   │   ├── output.py               #   SQLite 输出（建表/写入/导出/数据字典）
│   │   ├── rules.py                #   规则验证引擎（纯代码）
│   │   ├── postprocess_utils.py    #   后处理工具（过滤/回填）
│   │   └── nodes/                  #   节点函数（每个节点一个文件）
│   │       ├── classify.py         #     分类节点（支持多作物配置）
│   │       ├── filter.py           #     过滤节点
│   │       ├── parse.py            #     解析节点
│   │       ├── extract_phase1.py   #     Phase 1 提取
│   │       ├── extract_phase2.py   #     Phase 2 提取（主要耗时）
│   │       ├── postprocess.py      #     后处理节点
│   │       ├── geocode.py          #     地理编码节点
│   │       └── validate.py         #     规则验证 + 针对性 LLM 验证
│   ├── prompts/
│   │   ├── classify.txt            #   分类 prompt（支持 {crop_list}）
│   │   ├── extract_paper.txt       #   Phase 1 提取 prompt
│   │   └── extract_study.txt       #   Phase 2 提取 prompt
│   └── output/
│       └── statistics.py           #   覆盖率统计
├── docs/                           # 论文数据
│   ├── meta.csv                    #   元数据 CSV
│   └── *.pdf                       #   论文 PDF
├── cache/                          # 运行缓存
│   ├── geocoding_cache.json        #   地理编码缓存
│   └── langgraph_checkpoint.db     #   LangGraph checkpoint（SQLite）
└── output/
    ├── paper_data.db               # 主数据库（固定位置，跨运行共享）
    ├── parsed/                     # MinerU 解析后的 Markdown
    └── runs/{timestamp}/           # 每次运行的输出
        ├── logs/
        │   └── extractor.log       #   运行日志（含节点计时）
        └── results/
            ├── classification/
            │   └── classification.csv
            ├── extraction/
            │   ├── papers.csv
            │   ├── studies.csv
            │   ├── varieties.csv
            │   └── varieties_flat.csv  # 交接用宽表
            ├── validation/
            │   └── validation_issues.csv
            └── statistics/
                ├── report.md
                ├── summary.json
                ├── paper_coverage.csv
                └── field_coverage.csv
```

## 安装

```bash
pip install -r requirements.txt
```

核心依赖：Python 3.10+、pydantic >= 2.0、requests、pyyaml、langgraph >= 1.0

### 外部服务

| 服务 | 用途 | 配置 | 必须 |
|------|------|------|------|
| MinerU | PDF → Markdown 解析 | `config.yaml` → `mineru` | 是 |
| LLM API (OpenAI 兼容) | 分类 + 提取 | `config.yaml` → `llm` | 是 |
| 百度地图 API | 地理编码（经纬度） | `config.yaml` → `geocoding.baidu_api_key` | 否 |
| 天地图 API | 地理编码（经纬度，推荐） | `config.yaml` → `geocoding.tianditu_tk` | 是 |

## 使用方法

```bash
# 完整流程（默认）
python run.py

# 分步执行（自动补全前置步骤）
python run.py --step classify          # 仅分类（快速检查分类结果）
python run.py --step parse             # 分类 + 解析 PDF
python run.py --step extract           # 完整流程

# 处理特定论文（按 DOI 或标题关键词匹配）
python run.py --paper "早稻"

# 指定配置文件
python run.py --config /path/to/config.yaml
```

### 断点续跑

SQLite checkpoint 自动管理，崩溃后重跑同一命令即可从断点继续：

```bash
# 清除 checkpoint（从头开始）
del cache\langgraph_checkpoint.db

# 清除地理编码缓存
del cache\geocoding_cache.json
```

### 防重机制

- **稳定 paper_id**：`P_{MD5(标题归一化)[:10]}`，同一标题跨运行 ID 不变
- **paper_status 注册表**：SQLite 表记录每篇论文完成的最高步骤
- 跳过逻辑：`--step classify` 跳过已分类的，`--step parse` 跳过已解析的，以此类推
- 强制重跑：从 `paper_status` 表删除对应记录即可

## 配置说明

`config.yaml` 中的关键配置：

```yaml
paths:
  base_dir: "."
  papers_dir: "docs"           # 论文 PDF + 元数据 CSV
  cache_dir: "cache"
  parsed_dir: "output/parsed"
  runs_dir: "output/runs"

mineru:
  base_url: "http://172.17.1.122"
  api_key: "your-key"
  parse_method: "ocr"          # auto / txt / ocr

llm:
  base_url: "http://182.92.166.143:3200/v1"
  api_key: "your-key"
  model: "DSv4-flash"
  max_tokens: 8192
  temperature: 0.1
  max_retries: 3

extraction:
  max_text_chars: 120000       # 单章节最大字符数
  extractable_categories:      # 可提取的论文类别
    - "varietal_yield"
    - "management_yield"
  crops:                       # 目标作物列表（影响分类和筛选）
    - "水稻/Rice"
    - "玉米/Maize"
    - "小麦/Wheat"

geocoding:
  enabled: true
  use_tianditu: true
  tianditu_tk: "your-key"       # 天地图 API Key（推荐）
  # baidu_api_key: "your-key"   # 百度地图 API Key（可选）

concurrency:
  classify_workers: 5
  parse_workers: 8
  extract_workers: 3           # 并发论文数（滑动窗口）
```

### 添加新作物

在 `config.yaml` 的 `extraction.crops` 列表中添加作物即可，分类 prompt 会自动引用：

```yaml
extraction:
  crops:
    - "水稻/Rice"
    - "玉米/Maize"
    - "小麦/Wheat"
    - "大豆/Soybean"
    - "高粱/Sorghum"
```

无需修改 prompt 模板或代码，classify 节点会动态将作物列表注入 prompt。

## 数据库结构

所有数据统一存储在 `output/paper_data.db`（SQLite），DBeaver/Navicat 可直接打开。

### 表结构

| 表 | 用途 | 交接 |
|---|---|---|
| `papers` | 论文级元数据（一篇一行） | 内部 |
| `studies` | 试验级信息（一篇多行） | 内部 |
| `varieties` | 品种产量数据（主数据表） | 内部 |
| `varieties_flat` | 宽表（paper+study+variety 全字段） | **交接用** |
| `classification` | 论文分类结果 | 内部 |
| `validation_issues` | 验证问题明细（扁平化） | 内部 |
| `paper_status` | 论文处理状态（兼注册表） | 内部 |
| `_schema_doc` | 字段数据字典（103 个字段中文注释） | 参考 |

### 数据字典

DB 内含 `_schema_doc` 表，记录所有字段的中文说明、类型、是否必填、数据来源。DBeaver 中查询：

```sql
-- 查看某张表的字段说明
SELECT column_name, column_type, description, is_required, source
FROM _schema_doc WHERE table_name = 'varieties' ORDER BY rowid;

-- 查看所有表的字段统计
SELECT table_name, COUNT(*) as field_count
FROM _schema_doc GROUP BY table_name ORDER BY table_name;
```

### 交接数据

给下游单位提供 `varieties_flat.csv` 宽表即可，每行包含 paper+study+variety 全部字段：

```sql
-- DBeaver 中直接导出
SELECT * FROM varieties_flat;
```

或在代码中调用：

```python
from src.graph.output import export_delivery_csv
export_delivery_csv(conn, Path("delivery.csv"))
```

## 数据模型

三级层次结构，Pydantic v2 定义（共 42 字段）：

```
ExtractionResult
├── paper: PaperInfo          (8 字段: DOI, 标题, 年份, 期刊, 作物等)
└── studies: List[StudyInfo]
    ├── study 级字段           (20 字段: 试验名称, 年份, 地点, 经纬度, 试验设计等)
    └── varieties: List[VarietyYield]
        └── variety 级字段     (14 字段: 品种名, 产量, 对照, 增产率等)
```

### 字段标记

| 标记 | 含义 |
|------|------|
| `[REQUIRED]` | LLM 必须填充 |
| `[OPTIONAL]` | LLM 有则填（如论文中明确写出的经纬度） |
| `[PROGRAM]` | 程序自动计算（如 yield_standard_value） |
| `[DO NOT FILL]` | 程序后处理填充（如 geo_source, notes） |

### 单位换算

LLM 提取原始产量值和单位，程序自动换算为 kg/ha：

| 原始单位 | 换算 | 示例 |
|----------|------|------|
| kg/ha, kg/hm² | × 1 | 8728.5 → 8728.5 |
| t/ha, t/hm² | × 1000 | 8.5 → 8500.0 |
| kg/亩, kg/mu | × 15 | 612.5 → 9187.5 |
| kg/667m² | × 15 | 482.0 → 7230.0 |
| 斤/亩 | × 7.5 | 800 → 6000.0 |
| g/株, kg/plot | 不可换算 → None | 需要额外信息 |

### 地理编码策略（4 级优先）

```
论文中明确写出的经纬度 → 内置机构查找表 → 天地图 API → 百度地图 API → 省会兜底
     (geo_source=paper)   (geo_source=lookup)  (geo_source=tianditu)  (geo_source=baidu)  (province_fallback)
```

**海拔补充**：geocode 结果 → Open-Meteo Elevation API（SRTM，精度约 90m，无需 Key） → 省会海拔近似值

## 性能说明

单篇论文的典型耗时分布（MD 已缓存，不含 MinerU 解析）：

| 节点 | 耗时 | 说明 |
|------|------|------|
| classify | ~5s | 1 次 LLM 调用 |
| filter | <1ms | 纯代码 |
| parse | <1s | 读 MD + 构建文档树 |
| extract_phase1 | 30-90s | 1 次 LLM 调用（max_tokens=8192） |
| **extract_phase2** | **N × 30-90s** | **每个试验章节 1 次 LLM 调用** |
| postprocess | <1s | Pydantic + 后处理 |
| geocode | <5s | 查找表 + API |
| validate | <1s | 规则检查 |

**瓶颈是 extract_phase2**：N 个试验章节 = N 次大 LLM 调用。3 个试验的论文约需 3-5 分钟。

日志中每个节点的执行耗时会自动记录（超过 1 秒的显示 INFO 级别）。

可通过 DBeaver 查询 `paper_status` 表分析批量运行性能：

```sql
-- 各状态论文数
SELECT status, COUNT(*) FROM paper_status GROUP BY status;

-- 耗时最长的论文
SELECT paper_id, title, duration_sec, status FROM paper_status
ORDER BY duration_sec DESC LIMIT 20;
```

## 论文分类标准

| 类别 | 说明 | 是否提取 |
|------|------|----------|
| `varietal_yield` | 品种型产量 — 核心关注基因型表现 | 是 |
| `management_yield` | 管理/环境型产量 — 核心关注栽培措施 | 是 |
| `remote_sensing_yield` | 遥感/区域宏观产量 | 否 |
| `mechanistic_yield` | 机制型产量 — 分子/生理机制 | 否 |
| `irrelevant` | 无关 — 非目标作物产量研究 | 否 |

## Prompt 调优

Prompt 模板在 `src/prompts/` 下，修改后清除 checkpoint 重新运行即可：

| 文件 | 用途 | 修改后操作 |
|------|------|-----------|
| `classify.txt` | 论文分类 | `del cache\langgraph_checkpoint.db` |
| `extract_paper.txt` | Phase 1 提取 | `del cache\langgraph_checkpoint.db` |
| `extract_study.txt` | Phase 2 提取 | `del cache\langgraph_checkpoint.db` |

## License

Internal use only.
