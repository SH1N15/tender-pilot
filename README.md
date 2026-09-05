# 投标智航 / TenderPilot

> **项目定位**：独立设计并搭建的智能招投标 Agent 平台，覆盖招标文件解读、投标内容生成、
> 22 项合规检查、排版导出，以及带人工确认与匿名数据飞轮的资格预审 Agent。
>
> **文档入口**
> - [平台总览](docs/intelligent-bidding-agent-overview.md)
> - [架构总览与能力矩阵](docs/intelligent-bidding-agent-architecture.md)
> - [全平台演示路线](docs/intelligent-bidding-agent-demo.md)
> - [资格预审专项设计](docs/qualification-agent-architecture.md) / [资格预审演示](docs/qualification-agent-demo.md)
> - [简历包装](docs/resume-intelligent-bidding-agent.md)
> - [**项目交接文档**](docs/handover/README.md)

<div align="center">

![Version](https://img.shields.io/badge/version-0.2.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.12+-green.svg)
![License](https://img.shields.io/badge/license-MIT-orange.svg)

**AI 智能招投标助手 · 标书生成 · 投标检查 · 文档排版 · 资格预审**

[功能特性](#功能特性) • [技术架构](#技术架构) • [快速开始](#快速开始) • [配置说明](#配置说明) • [核心概念](#核心概念) • [文档入口](#文档入口)

</div>

## 项目简介

本平台是独立设计并搭建的智能招投标 Agent 平台。围绕「招标文件解读 → 投标内容生成 →
合规检查 → 排版导出」主流程，叠加多 Agent 编排、Skill 引擎、RAG 知识库与资格预审 Agent。

- **投标主流程**: 上传招标文件 → 解析（PDF/DOCX/TXT）→ 招标解读（LLM）→ 大纲 → 章节生成 → 22 项检查 → 排版 → 导出
- **22 项专业检查**: 合规 / 资质 / 保证金 / 签章 / 有效期 / 一致性 / 查重 / 报价等
- **LLM 网关**: OpenAI 兼容多 provider 配置（DeepSeek / 硅基流动 / OpenAI / 通义 / Ollama），代码使用 openai SDK 直连
- **RAG 知识库**: ChromaDB 向量库 + BM25 混合检索
- **资格预审 Agent**: 五类资格确定性匹配 + 人工确认（HITL）+ 匿名数据飞轮 + 24 个合成 case 离线评测
- **多 Agent / Skill / MCP**: 自研 Agent 框架 + LangGraph 编排器 + 42 个注册 Skill + 官方 mcp SDK（FastMCP v1）服务
- **商机监控**: 定时爬取招标公告 + 今日热点聚合

## 功能特性

### 1. 招标解读 (Interpret)
- 文档解析：支持 PDF / DOCX / TXT 等多格式
- 关键信息提取：项目信息、资格要求、评分标准、废标条款等维度
- 风险预警与评分矩阵

### 2. 投标生成 (Generate)
- 大纲生成：基于解读结果生成投标大纲
- 章节生成：按章节撰写正文，支持流式输出与扩写
- 知识检索：RAG 引擎检索企业知识库做参考
- AI 配图：接入图像生成接口生成示意图

### 3. 投标检查 (Check)
- 22 项专业检查：合规、资质、保证金、签章、有效期、一致性、重复率、报价合理性等
- 支持项目模式与上传文件模式

### 4. 文档输出 (Format)
- 智能排版与模板配置
- docx / PDF 导出
- 格式检查 / 差异对比

### 5. 资格预审 Agent (Qualification)
- 招标要求适配：把解读结果保守转为五类资格要求
- 企业材料候选抽取 + 人工证据绑定（HITL）
- 确定性匹配：met / unmet / insufficient 三态，无证据不判满足
- 匿名数据飞轮：Trace / 指标 / 匿名导出 / 合成基准评测

### 6. 辅助功能
- 资讯监控：定时抓取招标公告、关键词过滤、热度聚合
- 知识库管理：向量检索与文档管理
- 权限管理 (RBAC)：角色与操作级权限

### 7. MinerU OCR（已接入）
- 已接入 MinerU **官方云 API v4**（`api.mineru.net/v4`）与 **mock transport**（无真实 Key 时可确定性验证链路）；
- 自建 self-hosted endpoint 按配置支持（`BMP_OCR_MODE=selfhosted` + 自定义 endpoint）；
- 配置/连接测试/扫描/提交/轮询/结果写回 `parsed_content`，错误分类 auth/rate_limit/format/timeout/upstream；
- 说明：当前交付无真实 MinerU Key，**尚未做真实云端调用验证**；有 Key 后在「平台设置 → OCR识别」填入即可。

## 技术架构

```
┌─────────────────────────────────────────────────────┐
│                  Desktop Client                      │
│         React + Electron + TypeScript                │
└──────────────────┬──────────────────────────────────┘
                   │ REST API
┌──────────────────▼──────────────────────────────────┐
│                 FastAPI Server                       │
│              (Python 3.12+)                          │
├─────────────────────────────────────────────────────┤
│  Router Layer                                        │
│  ├── Projects  ├── Interpret  ├── Generate          │
│  ├── Check     ├── Format     ├── Skills            │
│  ├── News      ├── Knowledge  ├── RBAC              │
│  ├── AI-Image  ├── Qualification ├── Auth / LLM     │
│  └── Agent-Runtime / MCP                            │
├─────────────────────────────────────────────────────┤
│  Core Engines                                        │
│  ├── Agent Framework (自研 ReAct/工具/消息/内存/熔断) │
│  ├── LangGraph 编排器 (DSL + GateKeeper)            │
│  ├── Skill Engine (42 个注册 Skill)                 │
│  ├── RAG Engine (ChromaDB + BM25/RRF)               │
│  ├── LLM Gateway (openai SDK 多 provider)           │
│  └── Doc Engine (pdf/docx/txt 解析 + 章节检测)       │
├─────────────────────────────────────────────────────┤
│  Infrastructure                                      │
│  ├── PostgreSQL (AsyncPG)                           │
│  ├── Redis (Celery Broker)                          │
│  ├── Celery Worker (异步任务)                        │
│  └── MinIO (配置预留，代码未使用)                    │
└─────────────────────────────────────────────────────┘
```

### 核心技术栈（按代码事实）

#### 后端
| 技术 | 用途 | 版本 |
|------|------|------|
| FastAPI | Web 框架 | >=0.115 |
| Python | 编程语言 | >=3.12 |
| SQLAlchemy | ORM 框架（asyncio） | >=2.0 |
| PostgreSQL | 关系数据库 | 16 |
| AsyncPG | 异步数据库驱动 | >=0.30 |
| Redis | 缓存 / Celery Broker | 7 |
| Celery | 异步任务队列 | >=5.3 |
| MinIO | 对象存储（配置预留，代码未使用） | Latest |

#### AI & Agent
| 技术 | 用途 | 版本 |
|------|------|------|
| openai SDK | LLM 兼容直连（多 provider、降级重试、function calling） | >=1.0 |
| LangGraph | 编排器（StateGraph + DSL + GateKeeper） | >=0.2 |
| LangChain-Core | 依赖声明，代码未实际使用 | >=0.3 |
| ChromaDB | 向量数据库 | >=0.5 |
| Sentence-Transformers | 本地 embedding（可选，默认 api 模式） | >=3.0 |
| ONNX Runtime | 段落分类（可选，缺失时降级） | >=1.17 |
| mcp（官方 Python SDK，FastMCP v1 API） | MCP 工具 / 资源服务 | >=1.9,<2（已装 1.29.1） |
| a2a-sdk | A2A 协议（Agent Card / JSON-RPC） | 1.1.2（A2A 1.0） |
| ag-ui-protocol | AG-UI 协议（SSE 事件流） | 0.1.21（AG-UI 0.1.0） |
| opentelemetry-sdk | Tracing（可选 OTLP 导出） | 1.44.0 |

#### 前端
| 技术 | 用途 | 版本 |
|------|------|------|
| React | UI 框架 | ^19.0.0 |
| Electron | 桌面应用容器 | ^41.0.0 |
| TypeScript | 类型系统 | ^5.5.0 |
| Vite | 构建工具 | ^7.0.0 |
| Zustand | 状态管理 | ^5.0.0 |
| React Router | 路由管理 | ^7.0.0 |
| TanStack Query | 数据请求 | ^5.0.0 |
| Radix UI | 无头组件库 | ^1.1.0 |
| TailwindCSS | 样式框架 | ^4.0.0 |

#### 文档处理
| 技术 | 用途 | 版本 |
|------|------|------|
| python-docx | Word 文档处理 | >=1.1 |
| pdfplumber | PDF 文本提取 | >=0.11 |
| PyMuPDF | PDF 高级处理 | >=1.24 |
| Mammoth | DOCX 转 HTML | >=1.8 |
| WeasyPrint | HTML 转 PDF | >=61 |
| BeautifulSoup4 | HTML 解析 | >=4.12 |

## 快速开始

### 环境要求

- Python: 3.12+
- Node.js: 18+
- PostgreSQL: 16+
- Redis: 7+
- Docker & Docker Compose（推荐）

### 方式一：Docker 部署（推荐）

```bash
# 1. 克隆项目
git clone <repository-url>
cd <project-directory>

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填写 LLM API Key 等配置

# 3. 启动服务
docker compose -f docker/docker-compose.yml up -d
```

启动后自动运行：PostgreSQL (5432) / Redis (6379) / MinIO (9000) / FastAPI (8000) / Celery Worker

- API 文档: http://localhost:8000/docs
- MinIO 控制台: http://localhost:9001

### 方式二：本地开发

> Windows 推荐一键启动：
> ```powershell
> powershell -ExecutionPolicy Bypass -File scripts\dev_start.ps1
> ```
> 脚本会自动检测本机 Memurai/Redis、初始化 DB、启动 FastAPI/MCP/Vite，可用 `scripts\dev_stop.ps1` 停止。

以下为手动方式：

```bash
# 后端
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e .

# 前端
cd packages/desktop
npm install

# 启动基础设施
docker compose -f docker/docker-compose.yml up -d postgres redis minio

# 配置环境变量
cp .env.example .env
# 编辑 .env，至少配置 BMP_LLM_API_KEY / BMP_DATABASE_URL / BMP_REDIS_URL

# 初始化数据库
# 当前不使用 Alembic 迁移：FastAPI 启动时会通过 Base.metadata.create_all 自动建表。
# Alembic 迁移纳入后续工作（见 docs/handover/12-roadmap.md）。

# 启动后端
uvicorn services.main:app --reload --host 0.0.0.0 --port 8000

# 启动 Celery Worker
celery -A services.celery_app worker --loglevel=info

# 启动前端桌面应用
cd packages/desktop
npm run electron:dev
```

> 轻量环境跑资格预审演示：
> ```bash
> .\.venv\Scripts\python.exe scripts\qualification_agent_demo.py
> ```

### Windows 本地 Redis：Memurai（可选）

在 Windows 上可直接使用 Memurai Developer Edition 作为 Redis（官方 Windows 原生 Redis 兼容实现，默认端口 6379），免 Docker：

```powershell
winget install --id Memurai.MemuraiDeveloper
# 安装后服务名 Memurai，默认监听 127.0.0.1:6379，自动启动
```

> 注意：Memurai Developer Edition 仅用于开发/测试，授权要求每 10 天重启一次 Memurai 服务。
> 本项目 `scripts/dev_start.ps1` 会优先检测并启动本机 Memurai Windows Service；`/api/diagnostics` 的 Redis 检查会识别为 OK。

## 配置说明

### 核心环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `BMP_DEBUG` | 调试模式 | `true` |
| `BMP_PORT` | 服务端口 | `8000` |
| `BMP_DATABASE_URL` | PostgreSQL 连接串 | `postgresql+asyncpg://...` |
| `BMP_REDIS_URL` | Redis 连接串 | `redis://localhost:6379/0` |
| `BMP_CHROMA_DIR` | ChromaDB 存储路径 | `./chroma_db` |
| `BMP_LLM_DEFAULT_MODEL` | 默认 LLM 模型 | `deepseek/deepseek-chat` |
| `BMP_LLM_API_KEY` | LLM API Key | - |
| `BMP_LLM_API_BASE` | LLM API 地址 | `https://api.deepseek.com` |
| `BMP_LLM_FALLBACK_MODES` | 降级模型列表 | `ollama/qwen2.5` |
| `BMP_EMBEDDING_MODE` | 嵌入模式 (api/local) | `api` |
| `BMP_EMBEDDING_MODEL` | 嵌入模型 | `text-embedding-v3` |

### LLM 提供商支持

通过 LLM 网关（openai SDK 兼容直连）配置多 provider，可在平台设置中切换：

- DeepSeek: `deepseek/deepseek-chat`
- 硅基流动: `siliconflow/*`
- OpenAI: `gpt-4`, `gpt-3.5-turbo`
- 阿里云通义千问: `qwen-max`, `qwen-plus`
- Ollama（本地）: `ollama/qwen2.5`, `ollama/llama3`

### OCR 状态

MinerU OCR 已接入官方云 API + mock（自建 endpoint 按配置支持）；当前无真实 Key 时只能 mock，
尚未做真实云端调用验证。接入方式见「平台设置 → OCR识别」与上文功能 7。

## 核心概念

### Skill（技能）

Skill 是核心扩展机制，每个 Skill 代表一个独立的业务能力单元，注册后由 Skill 路由调用：

```python
from core.skill_engine.base import Skill, SkillContext, SkillResult

class MyCustomSkill(Skill):
    name = "my_custom_skill"
    description = "我的自定义技能"
    category = "generate"
    version = "1.0.0"

    async def execute(self, ctx: SkillContext) -> SkillResult:
        response = await ctx.llm.chat(messages=[...])
        return SkillResult(success=True, data={"result": response})
```

### Agent 流水线

编排器用 LangGraph 把 DSL 编译成流水线，配合闸门控制器保证阶段顺序：

```python
pipeline_dsl = {
    "entry": "parse_tender",
    "nodes": [
        {"id": "parse_tender", "skill": "tender_parser", "require_gate": True},
        {"id": "extract_requirements", "skill": "requirement_extractor"},
        {"id": "generate_outline", "skill": "outline_generator"},
    ],
    "edges": [
        {"from": "parse_tender", "to": "extract_requirements"},
        {"from": "extract_requirements", "to": "generate_outline"},
    ]
}
```

### Gate Keeper（闸门控制器）

前一阶段未完成则不能进入下一阶段：

```python
gate_keeper.mark_passed(project_id, "interpret")
if not gate_keeper.is_passed(project_id, "interpret"):
    raise GateNotPassedException("请先完成招标解读")
```

## 文档入口

- [平台总览](docs/intelligent-bidding-agent-overview.md)
- [架构总览与能力矩阵](docs/intelligent-bidding-agent-architecture.md)
- [全平台演示路线](docs/intelligent-bidding-agent-demo.md)
- [资格预审专项设计](docs/qualification-agent-architecture.md)
- [资格预审演示](docs/qualification-agent-demo.md)
- [简历包装](docs/resume-intelligent-bidding-agent.md)

## 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件


---

## vNext（0.2.0）：协议接入、OCR、受控学习与可观测性

> 详细文档：[vNext 完成度与端点](docs/vnext-overview.md) · [快速开始](docs/vnext-quickstart.md) · [规则治理](docs/rule-governance.md)

- **MinerU OCR 真接入**：官方云 API v4 / 自建 endpoint / mock；配置/测试/扫描/轮询/结果写回 `parsed_content`；错误分类（auth/rate_limit/format/timeout/upstream）；SettingsPage 配置 + Interpret 页一键 OCR
- **MCP 从存在到可用**：官方 `mcp` Python SDK（FastMCP v1，1.29.1），8 tools / 2 resources，stdio + streamable-http（同进程 `/mcp` 与独立 9001 端口），能力清单与健康测试
- **A2A 真协议**：官方 `a2a-sdk` 1.1.2（A2A 1.0），Agent Card `/.well-known/agent-card.json` + JSON-RPC，supervisor + 6 业务 Agent 映射为 skills，project/session/context 关联
- **AG-UI 后端 + Agent 工作台**：官方 `ag-ui-protocol` 0.1.21，标准 SSE 事件流（run 生命周期 / 文本增量 / tool call 生命周期 / step / 错误），HITL Interrupt + Resume（资格预审人工确认）；无 API Key 可进页面
- **数据飞轮 → 受控规则学习**：确定性统计候选 → 人工 approve/reject → 版本化 RulePack → 发布（白名单 + 灾难性正则防御 + 基准/回归门禁）→ 可回滚；默认永不自动发布
- **轻量 Tracing / 监控**：OpenTelemetry SDK（可选 OTLP）+ 内存/JSONL exporter；FastAPI/Agent/LLM/Tool/Skill/OCR/A2A/AG-UI span；运行监控页（成功率 / P50/P95 / Token / 错误分类 / 最近运行）；绝不记录 API Key 与完整投标正文
- **一键开发启动**：`scripts\dev_start.ps1` / `dev_stop.ps1`（隐藏窗口、pid 管理、Docker 不可用降级）；`python -m services.env_check` 环境自检

**新增页面**：Agent 工作台（/workbench）、运行监控（/monitor）、规则审核（/rules）；SettingsPage 新增 OCR / MCP-A2A-AG-UI / 环境诊断标签页。

**测试**：`pytest -q` 当前 **218 passed**（含 MCP smoke、A2A Agent Card/任务错误路径、AG-UI 事件序列与 HITL、MinerU mock、规则治理候选/审批/发布阻止/回滚、tracing 隐私）。
