# Paper Extractor — 科研文献结构化数据提取工具

从农业科研文献（PDF）中自动提取品种产量试验的结构化数据。支持论文分类、PDF 解析、两阶段结构化提取、地理编码、单位换算、规则验证和覆盖率统计。

## 架构概览

项目提供两套 Pipeline，共享相同的数据模型和后处理逻辑：

| Pipeline | 命令 | 适用场景 | 特性 |
|----------|------|----------|------|
| **Legacy**（默认） | `python run.py --step extract` | 小规模（<50 篇）、快速验证 | ThreadPoolExecutor 并发 |
| **LangGraph** | `python run.py --graph --step all` | 大规模（千-万篇）、生产环境 | 断点续跑、条件路由、规则验证、逐篇追加输出 |

### LangGraph Pipeline 流程

```
论文 PDF + 元数据 CSV
       │
       ▼
┌──────────────┐
│  classify     │  LLM 分类（仅用元数据）
└──────┬───────┘
       ▼
┌──────────────┐
│  filter       │  筛选：China + 可提取类别
└──────┬───────┘  ──→ [不可提取] → END
       ▼
┌──────────────┐
│  parse        │  MinerU OCR / 复用已有 MD
└──────┬───────┘  ──→ [解析失败] → END
       ▼
┌──────────────┐
│  extract_p1   │  Phase 1: 论文级（摘要+大纲+方法 → paper字段+试验章节识别）
└──────┬───────┘  ──→ [Phase1失败] → END
       ▼
┌──────────────┐
│  extract_p2   │  Phase 2: 试验级（逐章节 → study+variety数据）
└──────┬───────┘
       ▼
┌──────────────┐
│  postprocess  │  Pydantic验证 + 产量换算 + 盆栽过滤 + 品种code回填 + site回填
└──────┬───────┘  ──→ [后处理失败] → END
       ▼
┌──────────────┐
│  geocode      │  地理编码（查找表 → 百度 → Nominatim → 省会兜底）
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
      END → 追加写入 CSV
```

### Legacy Pipeline 流程

```
classify → filter → parse → extract（单次全量提取）→ geocode → output → statistics
```

## 目录结构

```
extract4paperQC/
├── run.py                          # CLI 入口（--graph 切换 LangGraph）
├── config.yaml                     # 配置文件
├── requirements.txt                # Python 依赖
├── src/
│   ├── config.py                   #   配置加载 (AppConfig)
│   ├── clients/
│   │   ├── mineru.py               #   MinerU PDF 解析客户端
│   │   └── llm.py                  #   LLM API 客户端（含限流）
│   ├── core/                       #   Legacy Pipeline 核心模块
│   │   ├── loader.py               #   论文发现与元数据匹配
│   │   ├── classifier.py           #   LLM 分类 + 筛选
│   │   ├── chunker.py              #   文档层级树构建器
│   │   ├── extractor.py            #   两阶段提取 + 后处理
│   │   ├── geocoder.py             #   地理编码（5 级策略）
│   │   ├── validator.py            #   置信度验证（legacy）
│   │   ├── models.py               #   Pydantic 数据模型
│   │   └── pipeline.py             #   流程编排与缓存管理
│   ├── graph/                      #   LangGraph Pipeline
│   │   ├── state.py                #   PaperState 状态定义
│   │   ├── nodes.py                #   9 个节点函数
│   │   ├── rules.py                #   规则验证引擎（纯代码）
│   │   ├── graph.py                #   StateGraph + 条件路由 + checkpoint
│   │   └── batch.py                #   BatchOrchestrator（并发+断点续跑）
│   ├── prompts/
│   │   ├── classify.txt            #   分类 prompt
│   │   ├── extract_paper.txt       #   Phase 1 提取 prompt
│   │   ├── extract_study.txt       #   Phase 2 提取 prompt
│   │   ├── extract.txt             #   Legacy 提取 prompt
│   │   └── validate.txt            #   Legacy 验证 prompt
│   └── output/
│       ├── writer.py               #   CSV / JSON 输出
│       └── statistics.py           #   覆盖率统计
├── docs/                           # 论文数据
│   ├── meta.csv                    #   元数据 CSV
│   └── *.pdf                       #   论文 PDF
├── cache/                          # 运行缓存
│   ├── parsed_pdfs.json            #   MinerU 解析缓存
│   ├── classification_results.json #   分类缓存
│   ├── extraction_results.json     #   提取缓存（Legacy）
│   ├── geocoding_cache.json        #   地理编码缓存
│   └── langgraph_checkpoint.db     #   LangGraph checkpoint（SQLite）
└── output/
    ├── parsed/                     # MinerU 解析后的 Markdown
    └── runs/{timestamp}/           # 每次运行的输出
        ├── logs/                   #   运行日志
        └── results/
            ├── classification/     #   分类结果
            ├── extraction/         #   提取结果 CSV + JSON
            ├── confidence/         #   置信度汇总
            └── statistics/         #   覆盖率统计
```

## 安装

### 依赖

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
| Nominatim (OpenStreetMap) | 地理编码备选 | 自动调用，免费 | 否 |

## 使用方法

### LangGraph Pipeline（推荐）

```bash
# 完整流程
python run.py --graph --step all

# 单篇论文测试
python run.py --graph --step all --paper "早稻"

# 指定配置文件
python run.py --graph --step all --config /path/to/config.yaml
```

**LangGraph 特性：**
- **断点续跑**：SQLite checkpoint，崩溃后自动从最后成功节点恢复
- **条件路由**：不可提取的论文直接跳过，解析失败不影响其他论文
- **规则验证**：纯代码检查（产量换算一致性、范围检查、增产率校验等），不消耗 token
- **针对性 LLM 验证**：仅对规则标记为异常的记录做 LLM 核对
- **逐篇追加 CSV**：每篇论文处理完立即写入，随时查看进度
- **并发控制**：可配置同时处理的论文数（默认 10 篇）

### Legacy Pipeline

```bash
# 完整流程
python run.py --step all

# 仅分类
python run.py --step classify

# 分类 + 解析
python run.py --step parse

# 分类 + 解析 + 提取
python run.py --step extract

# 指定论文
python run.py --step extract --paper "10.14168"
```

### 断点续跑

**Legacy Pipeline** — 缓存在 `cache/` 目录，已缓存的论文自动跳过：

```bash
# 清除提取缓存（重新提取，保留解析结果）
del cache\extraction_results.json

# 清除所有缓存
del cache\*.json
```

**LangGraph Pipeline** — SQLite checkpoint 自动管理，崩溃后重跑同一命令即可从断点继续。

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
  extractable_categories:
    - "varietal_yield"
    - "management_yield"

geocoding:
  enabled: true
  use_nominatim: false
  baidu_api_key: "your-key"    # 百度地图 API Key

concurrency:
  classify_workers: 5
  parse_workers: 5
  extract_workers: 3           # LangGraph 并发论文数
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

### 地理编码策略（5 级优先）

```
论文中明确写出的经纬度 → 内置机构查找表 → 百度地图 API → Nominatim → 省会兜底
     (geo_source=paper)   (geo_source=lookup)  (geo_source=baidu)  (nominatim)  (province_fallback)
```

## 后处理流水线

提取完成后自动执行以下后处理步骤：

1. **Pydantic 验证** — 数据模型校验 + 类型转换
2. **产量换算** — yield_raw → yield_standard (kg/ha)
3. **多站点检测** — site_name 含 "、" 的标记警告
4. **variety_code 回填** — 同一品种名在不同 study 间的审定编号一致性
5. **盆栽试验过滤** — 剔除盆栽、温室、单株计产等非大田试验
6. **无产量 study 过滤** — 剔除 yield_raw_unit 为 % 或无产量数据的 study
7. **site 信息回填** — 同一论文内只有一个地点时，回填到其他 study
8. **规则验证** — 产量换算一致性、范围检查、增产率校验、跨 study 波动检查
9. **地理编码** — 根据地名填充经纬度和海拔
10. **针对性 LLM 验证**（仅 LangGraph）— 对规则标记异常的记录做 LLM 核对

## 论文分类标准

| 类别 | 说明 | 是否提取 |
|------|------|----------|
| `varietal_yield` | 品种型产量 — 核心关注基因型表现 | 是 |
| `management_yield` | 管理/环境型产量 — 核心关注栽培措施 | 是 |
| `remote_sensing_yield` | 遥感/区域宏观产量 | 否 |
| `mechanistic_yield` | 机制型产量 — 分子/生理机制 | 否 |
| `irrelevant` | 无关 — 非产量研究 | 否 |

## Prompt 调优

Prompt 模板在 `src/prompts/` 下，修改后无需改代码：

| 文件 | 用途 | 修改后操作 |
|------|------|-----------|
| `classify.txt` | 论文分类 | 清除 `cache/classification_results.json` |
| `extract_paper.txt` | Phase 1 提取 | 清除 `cache/extraction_results.json` |
| `extract_study.txt` | Phase 2 提取 | 清除 `cache/extraction_results.json` |
| `extract.txt` | Legacy 提取 | 清除 `cache/extraction_results.json` |

## License

Internal use only.
