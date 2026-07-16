# Paper Extractor — 科研文献结构化数据提取工具

从农业科研文献（PDF）中自动提取品种产量试验的结构化数据，支持论文分类、PDF 解析、字段提取、地理编码、单位换算、置信度评估和覆盖率统计。

## Pipeline 流程

```
论文 PDF + 元数据 CSV
       │
       ▼
┌──────────────┐
│  1. classify  │  LLM 分类（仅用元数据，无需 PDF）
└──────┬───────┘
       ▼
┌──────────────┐
│  2. filter    │  筛选：research_country=China + 可提取类别
└──────┬───────┘
       ▼
┌──────────────┐
│  3. parse     │  MinerU OCR 解析 PDF → Markdown（仅解析通过筛选的论文）
└──────┬───────┘
       ▼
┌──────────────┐
│  4. extract   │  LLM 结构化提取 + Pydantic 验证 + 单位换算
└──────┬───────┘
       ▼
┌──────────────┐
│  5. geocode   │  根据地名计算经纬度和海拔
└──────┬───────┘
       ▼
┌──────────────┐
│  6. output    │  生成 CSV / JSON 输出
└──────┬───────┘
       ▼
┌──────────────┐
│  7. statistics│  字段覆盖率统计分析
└──────────────┘
```

## 目录结构

```
extract4paperQC/
├── run.py                          # CLI 入口
├── config.yaml                     # 配置文件
├── paper_extractor.py              # 原始单文件脚本（保留参考）
├── src/
│   ├── config.py                   #   配置加载 (AppConfig)
│   ├── clients/
│   │   ├── mineru.py               #   MinerU PDF 解析客户端
│   │   └── llm.py                  #   LLM API 客户端 (OpenAI 兼容)
│   ├── core/
│   │   ├── loader.py               #   论文发现与元数据匹配
│   │   ├── classifier.py           #   LLM 分类 + 筛选
│   │   ├── chunker.py              #   文档分块（按章节切分）
│   │   ├── extractor.py            #   LLM 结构化提取
│   │   ├── geocoder.py             #   地理编码（地名 → 经纬度）
│   │   ├── validator.py            #   置信度验证
│   │   ├── models.py               #   Pydantic 数据模型 (Paper→Study→Variety)
│   │   └── pipeline.py             #   流程编排与缓存管理
│   ├── prompts/
│   │   ├── classify.txt            #   分类 prompt 模板
│   │   ├── extract.txt             #   提取 prompt 模板
│   │   └── validate.txt            #   验证 prompt 模板
│   └── output/
│       ├── writer.py               #   CSV / JSON 输出
│       └── statistics.py           #   覆盖率统计
├── docs/                           # 参考文档与数据
│   ├── extract_template_liu.xlsx   #   提取模板（字段定义）
│   ├── 分类标准.txt                  #   分类标准（5 类定义）
│   └── 水稻产量_top_10/              #   论文 PDF + 元数据 CSV
│       ├── pdf_zh/                 #     中文论文
│       └── pdf_en/                 #     英文论文
├── cache/                          # 运行缓存（支持断点续跑）
└── output/
    ├── parsed/                     # MinerU 解析后的 Markdown + 分块文本
    ├── results/
    │   ├── classification/         # 分类结果 CSV + JSON
    │   ├── extraction/             # 提取结果 CSV + JSON
    │   ├── confidence/             # 置信度汇总 CSV
    │   └── statistics/             # 覆盖率统计报告
    └── logs/                       # 运行日志
```

## 安装

### 依赖

- Python 3.10+
- requests
- pyyaml
- pydantic >= 2.0
- httpx（地理编码 Nominatim 查询用，可选）

```bash
pip install requests pyyaml pydantic httpx
```

### 外部服务

| 服务 | 用途 | 配置位置 | 是否必须 |
|------|------|----------|----------|
| MinerU | PDF → Markdown 解析 | `config.yaml` → `mineru` | 是 |
| LLM API (OpenAI 兼容) | 分类 + 提取 + 验证 | `config.yaml` → `llm` | 是 |
| Nominatim (OpenStreetMap) | 地理编码 | 自动调用，无需配置 | 否（有内置查找表兜底） |

## 使用方法

### 基本用法

```bash
# 完整流程（分类 → 筛选 → 解析 → 提取 → 地理编码 → 输出 → 统计）
python run.py --step all

# 仅分类（基于元数据，不解析 PDF）
python run.py --step classify

# 分类 + 筛选 + 解析 PDF
python run.py --step parse

# 分类 + 筛选 + 解析 + 提取
python run.py --step extract
```

### 指定论文

```bash
# 按 DOI 或标题子串匹配
python run.py --step all --paper "10.14168"
python run.py --step extract --paper "缓控释氮肥"
```

### 指定配置文件

```bash
python run.py --step all --config /path/to/config.yaml
```

### 断点续跑

Pipeline 的每个阶段结果都缓存在 `cache/` 目录下：

| 缓存文件 | 对应阶段 |
|----------|----------|
| `classification_results.json` | 分类 |
| `parsed_pdfs.json` | PDF 解析 |
| `extraction_results.json` | 结构化提取 |

已缓存的论文在重新运行时会自动跳过。清除缓存即可强制重新处理：

```bash
# 清除所有缓存
rm cache/*.json

# 仅清除提取缓存（重新提取但保留解析结果）
rm cache/extraction_results.json
```

## 配置说明

所有运行时配置集中在 `config.yaml` 中：

```yaml
paths:
  base_dir: "D:\\workspace\\local_project\\extract4paperQC"
  papers_dir: "docs/水稻产量_top_10"     # 论文数据目录
  cache_dir: "cache"                     # 运行缓存
  parsed_dir: "output/parsed"            # 解析后的 Markdown
  log_dir: "output/logs"                 # 日志文件
  classification_dir: "output/results/classification"
  extraction_dir: "output/results/extraction"
  confidence_dir: "output/results/confidence"
  statistics_dir: "output/results/statistics"

mineru:
  base_url: "http://172.17.1.122"
  api_key: "your-api-key"
  lang_list: ["ch", "en"]
  parse_method: "ocr"        # auto / txt / ocr
  poll_interval: 5
  poll_timeout: 600

llm:
  base_url: "http://182.92.166.143:3200/v1"
  api_key: "your-api-key"
  model: "DSv4-flash"
  max_tokens: 8192
  temperature: 0.1

extraction:
  max_text_chars: 80000      # 发送给 LLM 的最大文本长度
  extractable_categories:    # 允许提取的论文分类
    - "varietal_yield"
    - "management_yield"
  confidence_threshold: 0.5

geocoding:
  enabled: true              # 是否启用地理编码后处理
  use_nominatim: true        # 是否使用 Nominatim 在线查询
  nominatim_delay: 1.1       # Nominatim 请求间隔（秒）
```

## 输出文件

每次运行生成带时间戳的输出文件：

### 分类结果 (`output/results/classification/`)

| 文件 | 说明 |
|------|------|
| `classification_{ts}.csv` | 每篇论文一行，含分类、置信度、关键信号 |
| `classification_{ts}.json` | 完整分类结果（JSON） |

### 提取结果 (`output/results/extraction/`)

| 文件 | 说明 |
|------|------|
| `paper_{ts}.csv` | 每篇论文一行（6 字段） |
| `study_{ts}.csv` | 每个试验一行（19 字段） |
| `variety_{ts}.csv` | 每个品种一行（17 字段） |
| `full_flat_{ts}.csv` | 完整扁平化（所有 40+ 字段） |
| `extraction_{ts}.json` | 完整层级结构 JSON |

### 置信度 (`output/results/confidence/`)

| 文件 | 说明 |
|------|------|
| `confidence_summary_{ts}.csv` | 每篇论文的置信度和逻辑检查结果 |

### 统计报告 (`output/results/statistics/`)

| 文件 | 说明 |
|------|------|
| `paper_coverage_{ts}.csv` | 每篇论文的字段覆盖率 |
| `field_coverage_{ts}.csv` | 每个字段的全局命中率 |
| `summary_{ts}.json` | 批次总体统计 |
| `report_{ts}.md` | 可读统计报告（含进度条和可视化） |

## 数据模型

三级层次结构，使用 Pydantic v2 定义：

```
ExtractionResult
├── paper: PaperInfo          (8 字段)
└── studies: List[StudyInfo]
    ├── study 级字段           (18 字段)
    └── varieties: List[VarietyYield]
        └── variety 级字段     (14 字段)
```

### 字段标记

- **[REQUIRED]** — LLM 必须填充
- **[PROGRAM]** — 程序自动计算（LLM 不要填），如 `yield_standard_value`
- **[DO NOT FILL]** — 程序后处理填充（LLM 不要填），如 `latitude`, `longitude`, `altitude`

### 单位换算

LLM 只提取原始产量值和单位，程序自动换算为标准 kg/ha：

| 原始单位 | 换算规则 |
|----------|----------|
| kg/亩 | × 15 |
| t/ha | × 1000 |
| kg/ha | 不变 |
| g/plot, kg/plot | 需要小区面积，暂无法自动换算 |

## 论文分类标准

论文分为五类（详细定义见 `docs/分类标准.txt`）：

| 类别 | 说明 | 是否提取 |
|------|------|----------|
| `varietal_yield` | 品种型产量 — 核心关注基因型(G)表现 | 是 |
| `management_yield` | 管理/环境型产量 — 核心关注栽培措施(M)或环境(E) | 是 |
| `remote_sensing_yield` | 遥感/区域宏观产量 — 宏观空间尺度 | 否 |
| `mechanistic_yield` | 机制型产量 — 分子/生理机制 | 否 |
| `irrelevant` | 无关 — 非产量研究 | 否 |

## Prompt 调优

分类、提取、验证的 prompt 模板存放在 `src/prompts/` 目录下，可独立编辑而不需要修改代码。修改后重新运行即可生效（需清除对应阶段的缓存）。

## 论文数据格式

`docs/水稻产量_top_10/` 目录结构：

```
水稻产量_top_10/
├── pdf_en/                     # 英文论文
│   ├── {doi_folder}/
│   │   └── {paper}.pdf
│   ├── 水稻产量_en_{ts}.csv         # 元数据 CSV
│   └── 水稻产量_en_{ts}_report.csv  # 下载报告
└── pdf_zh/                     # 中文论文
    ├── {doi_folder}/
    │   └── {paper}.pdf
    ├── 水稻产量_zh_{ts}.csv
    └── 水稻产量_zh_{ts}_port.csv
```

元数据 CSV 必须包含 `doi`, `title`, `abstract`, `keywords`, `publication_year`, `journal`, `pdf_file_path` 等字段。

## License

Internal use only.
