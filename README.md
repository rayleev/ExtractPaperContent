# Paper Extractor — 科研文献结构化数据提取系统

基于 LangGraph 的农业科研文献结构化数据提取系统。从 PDF 论文中自动提取品种产量试验数据，支持论文分类、PDF 解析、全文理解、两阶段 LLM 提取、证据验证、地理编码、单位换算、规则验证。

## 架构概览

采用 LangGraph StateGraph 有向图架构，每篇论文独立走完整条流程。所有输出统一写入 PostgreSQL 数据库，支持大规模存储和查询。

```
论文 PDF / Semantic Scholar API
       │
       ▼
┌──────────────┐
│  search       │  搜索/导入论文（Semantic Scholar API）
└──────┬───────┘
       ▼
┌──────────────┐
│  classify     │  LLM 分类（仅用元数据，支持多作物配置）
└──────┬───────┘
       ▼
┌──────────────┐
│  filter       │  筛选：China + 可提取类别 + 目标作物
└──────┬───────┘  ──→ [不可提取] → END
       ▼
┌──────────────┐
│  download     │  下载 PDF（Semantic Scholar）
└──────┬───────┘  ──→ [无 PDF] → END
       ▼
┌──────────────┐
│  parse        │  全文理解（LLM 理解全文，识别品种共用关系）
└──────┬───────┘  ──→ [解析失败] → END
       ▼
┌──────────────┐
│  extract_p1   │  Phase 1: 论文级+试验级提取
└──────┬───────┘  ──→ [Phase1失败] → END
       ▼
┌──────────────┐
│  extract_p2   │  Phase 2: 品种级提取（逐试验，利用共用品种信息）
└──────┬───────┘
       ▼
┌──────────────┐
│  lookup       │  补充查找（处理 needs_lookup 的项，条件触发）
└──────┬───────┘
       ▼
┌──────────────┐
│  postprocess  │  Pydantic验证 + 产量换算 + 过滤 + 回填
└──────┬───────┘  ──→ [非中国] → END
       ▼
┌──────────────┐
│  geocode      │  地理编码（查找表 → 天地图 → 百度 → 省会兜底）
└──────┬───────┘
       ▼
┌──────────────┐
│  evidence     │  证据验证（批量验证重要字段的原文证据）
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
      END → PostgreSQL DB + CSV 导出
```

**核心特性：**

- **全文理解**：parse 节点使用 LLM 理解全文，识别品种共用关系（CK 对照）
- **多作物支持**：config.yaml 配置目标作物，支持按作物拆分 study
- **共用品种识别**：自动识别跨试验共用的对照品种，统一名称
- **证据验证**：evidence 节点批量验证重要字段的原文证据
- **单位换算可配置**：config.yaml 配置单位换算表，支持扩展
- **规则验证编码**：validation_issues 使用编码（如 YIELD_001）便于查询
- **HTTP API**：FastAPI 提供 REST API 和监控面板
- **断点续跑**：LangGraph checkpoint，崩溃后从断点恢复
- **滑动窗口并发**：ThreadPoolExecutor 始终保持 N 个任务在跑

## 目录结构

```
extract4paperQC/
├── run.py                          # CLI 入口
├── config.yaml                     # 配置文件
├── requirements.txt                # Python 依赖
├── Dockerfile                      # Docker 构建
├── docker-compose.yml              # Docker Compose 配置
├── src/
│   ├── config.py                   # 配置加载 (AppConfig)
│   ├── clients/
│   │   ├── mineru.py               # MinerU PDF 解析客户端
│   │   ├── llm.py                  # LLM API 客户端
│   │   └── semantic_scholar.py     # Semantic Scholar API 客户端
│   ├── core/
│   │   ├── loader.py               # 论文发现与元数据匹配
│   │   ├── chunker.py              # 文档层级树构建器
│   │   ├── geocoder.py             # 地理编码（4 级策略）
│   │   ├── constants.py            # 常量定义
│   │   └── models.py               # Pydantic 数据模型 + 产量换算
│   ├── graph/
│   │   ├── state.py                # PaperState 状态定义
│   │   ├── graph.py                # StateGraph + 条件路由
│   │   ├── batch.py                # BatchOrchestrator（并发+注册表）
│   │   ├── output.py               # PostgreSQL 输出（建表/写入/导出）
│   │   ├── rules.py                # 规则验证引擎（纯代码）
│   │   ├── country_utils.py        # 国家判断工具
│   │   ├── postprocess_utils.py    # 后处理工具（过滤/回填）
│   │   └── nodes/                  # 节点函数
│   │       ├── search.py           # 搜索/导入论文
│   │       ├── classify.py         # 分类节点
│   │       ├── filter.py           # 过滤节点
│   │       ├── download.py         # 下载节点
│   │       ├── parse.py            # 全文理解节点
│   │       ├── extract_phase1.py   # Phase 1 提取
│   │       ├── extract_phase2.py   # Phase 2 提取
│   │       ├── lookup.py           # 补充查找节点
│   │       ├── postprocess.py      # 后处理节点
│   │       ├── geocode.py          # 地理编码节点
│   │       ├── evidence.py         # 证据验证节点
│   │       └── validate.py         # 规则验证 + 针对性 LLM 验证
│   ├── prompts/                    # LLM Prompt 模板
│   │   ├── classify.txt            # 分类 prompt
│   │   ├── parse_understanding.txt # 全文理解 prompt
│   │   ├── parse_merge.txt         # 分段合并 prompt
│   │   ├── extract_paper.txt       # Phase 1 提取 prompt
│   │   ├── extract_study.txt       # Phase 2 提取 prompt
│   │   ├── extract_study_management.txt  # 管理型论文提取 prompt
│   │   ├── lookup.txt              # 补充查找 prompt
│   │   └── evidence.txt            # 证据验证 prompt
│   ├── api/                        # HTTP API
│   │   ├── main.py                 # FastAPI 应用
│   │   ├── routes.py               # API 路由
│   │   ├── schemas.py              # API 数据模型
│   │   └── static/dashboard.html   # 监控面板
│   └── output/
│       └── statistics.py           # 覆盖率统计
├── docs/                           # 论文数据（PDF）
├── migrations/                     # 数据库迁移
│   └── add_parse_context.py        # 添加 parse_context 字段
└── tests/                          # 测试工具（本地使用）
    ├── import_by_id.py             # 通过 SS paper_id 导入
    ├── init_db.py                  # 数据库初始化
    └── process_from_db.py          # 从数据库处理 pending 论文
```

## 安装

```bash
pip install -r requirements.txt
```

### 外部服务

| 服务 | 用途 | 配置 | 必须 |
|------|------|------|------|
| MinerU | PDF → Markdown 解析 | `config.yaml` → `mineru` | 是 |
| LLM API (OpenAI 兼容) | 分类 + 提取 | `config.yaml` → `llm` | 是 |
| Semantic Scholar API | 论文搜索 + PDF 下载 | `config.yaml` → `semantic_scholar` | 否 |
| 天地图 API | 地理编码（推荐） | `config.yaml` → `geocoding.tianditu_tk` | 是 |
| 百度地图 API | 地理编码（备选） | `config.yaml` → `geocoding.baidu_api_key` | 否 |
| PostgreSQL | 数据存储 | `config.yaml` → `database` | 是 |

## 使用方法

```bash
# 完整流程（默认）
python run.py

# 分步执行（自动补全前置步骤）
python run.py --step classify          # 仅分类
python run.py --step parse             # 分类 + 解析
python run.py --step extract           # 完整流程

# 处理特定论文（按 DOI 或标题关键词匹配）
python run.py --paper "水稻"

# 指定配置文件
python run.py --config /path/to/config.yaml

# 启动 HTTP API 服务
python run.py --serve --port 8000
```

### HTTP API

启动后可访问：

| 地址 | 说明 |
|------|------|
| http://localhost:8000/ | 健康检查 |
| http://localhost:8000/dashboard | 监控仪表盘 |
| http://localhost:8000/docs | Swagger API 文档 |

### 数据库初始化

```bash
python tests/init_db.py
```

### 通过 SS paper_id 导入论文

```bash
python tests/import_by_id.py <ss_paper_id>
```

## 配置说明

`config.yaml` 中的关键配置：

```yaml
# 路径配置
paths:
  base_dir: "."
  papers_dir: "docs"
  cache_dir: "cache"
  parsed_dir: "output/parsed"
  runs_dir: "output/runs"

# MinerU PDF 解析服务
mineru:
  base_url: "http://172.17.1.122"
  api_key: "your-key"
  parse_method: "ocr"          # auto / txt / ocr

# LLM 服务
llm:
  base_url: "http://your-llm-host/v1"
  api_key: "your-key"
  model: "DSv4-flash"
  max_tokens: 8192
  temperature: 0.1

# Semantic Scholar API（与 MinerU 共用同一服务）
semantic_scholar:
  base_url: "http://172.17.1.122"
  api_key: "your-key"

# 提取参数
extraction:
  max_text_chars: 120000       # 单章节最大字符数
  extractable_categories:      # 可提取的论文类别
    - "varietal_yield"
    - "management_yield"
  crops:                       # 目标作物列表
    - "水稻/Rice"
    - "玉米/Maize"
    - "小麦/Wheat"

# 单位换算配置（可通过配置文件新增）
unit_conversion:
  mass_to_kg:
    g: 0.001
    kg: 1.0
    t: 1000.0
    斤: 0.5
    公斤: 1.0
  area_to_ha:
    m2: 0.0001
    ha: 1.0
    hm2: 1.0
    亩: 0.0666667

# 证据验证配置
evidence_validation:
  enabled: true
  fields:
    - field: crop_species
      required: true
    - field: variety_name
      required: true
    - field: yield_raw_value
      required: true

# parse 节点配置
parse:
  chunked_enabled: true
  sliding_window_enabled: true
  full_text_threshold: 0.5

# 地理编码
geocoding:
  enabled: true
  use_tianditu: true
  tianditu_tk: "your-key"

# 并发控制
concurrency:
  classify_workers: 5
  parse_workers: 8
  extract_workers: 3

# PostgreSQL 数据库
database:
  host: "localhost"
  port: 5432
  dbname: "paper_extractor"
  user: "postgres"
  password: "Admin123!"
```

## 数据库结构

所有数据统一存储在 PostgreSQL 数据库。

### 表结构

| 表 | 用途 |
|---|---|
| `papers` | 论文级元数据（含 parse_context JSONB） |
| `studies` | 试验级信息 |
| `varieties` | 品种产量数据（主数据表） |
| `varieties_flat` | 宽表（paper+study+variety 全字段，交接用） |
| `classification` | 论文分类结果 |
| `validation_issues` | 验证问题明细（含编码） |
| `evidence` | 证据验证明细 |
| `paper_status` | 论文处理状态（兼注册表） |
| `_schema_doc` | 字段数据字典 |

### 验证问题编码

| 编码 | 说明 |
|------|------|
| `CK_001` | 缺少对照品种 |
| `YEAR_001` | trial_year > publication_year |
| `GEO_001` | 经纬度超出中国范围 |
| `MULTI_SITE_001` | 多站点警告 |
| `YIELD_001` | 产量换算不一致 |
| `YIELD_002` | 产量异常（超出范围） |
| `YIELD_003` | 增产率偏差 |
| `YIELD_004` | yield_raw_unit 为 % |
| `SOURCE_001` | source_location 为空 |
| `CONSISTENCY_001` | 跨 study 产量波动 |

## 论文分类标准

| 类别 | 说明 | 是否提取 |
|------|------|----------|
| `varietal_yield` | 品种型产量 — 核心关注基因型表现 | 是 |
| `management_yield` | 管理/环境型产量 — 核心关注栽培措施 | 是 |
| `remote_sensing_yield` | 遥感/区域宏观产量 | 否 |
| `mechanistic_yield` | 机制型产量 — 分子/生理机制 | 否 |
| `irrelevant` | 无关 | 否 |

## 单位换算

LLM 提取原始产量值和单位，程序自动换算为 kg/ha。换算表在 `config.yaml` 中配置，支持扩展。

### 支持的单位格式

| 格式 | 示例 | 说明 |
|------|------|------|
| Unicode 上标 | `kg·ha⁻¹`, `kg·hm⁻²` | 标准格式 |
| 普通减号 | `t·hm-2`, `kg·ha-1` | 兼容格式 |
| 中间点 | `kg·hm-2`, `t·ha-1` | 兼容格式 |
| 特定面积 | `kg/667m2`, `kg/20m2` | 特定面积 |
| 中文单位 | `g·株-1`, `公斤/亩` | 中文单位 |

### 地理编码策略（4 级优先）

```
论文中明确写出的经纬度 → 内置机构查找表 → 天地图 API → 百度地图 API → 省会兜底
     (geo_source=paper)   (geo_source=lookup)  (geo_source=tianditu)  (geo_source=baidu)  (province_fallback)
```

## Prompt 调优

Prompt 模板在 `src/prompts/` 下，修改后清除 checkpoint 重新运行：

| 文件 | 用途 |
|------|------|
| `classify.txt` | 论文分类 |
| `parse_understanding.txt` | 全文理解 |
| `parse_merge.txt` | 分段合并 |
| `extract_paper.txt` | Phase 1 提取 |
| `extract_study.txt` | Phase 2 提取 |
| `extract_study_management.txt` | 管理型论文提取 |
| `lookup.txt` | 补充查找 |
| `evidence.txt` | 证据验证 |

```bash
# 清除 checkpoint（从头开始）
del cache\langgraph_checkpoint.db
```

## License

Internal use only.
