# 投标智航 / TenderPilot

投标智航（TenderPilot）是面向政府采购投标场景的本地化智能工作台。系统把招标文件解析、招标要求提取、投标大纲与正文生成、知识库检索、合规检查、人工决策、排版和导出放在同一套应用中。

> 本仓库只包含运行所需源码、配置模板和技能定义。企业资料、上传文件、向量库、数据库、运行日志和密钥均不会随仓库发布。

## 能力范围

- 招标文件：PDF、DOCX、TXT 解析，提取项目概况、资格条件、评分标准和废标条款
- 投标生成：大纲、章节正文、引用锚点、统一事实口径和可选 AI 配图
- RAG 知识库：法规库、企业事实库和项目资料的检索、入库与证据绑定
- 投标检查：资格、合规、保证金、有效期、报价、文件完整性、一致性、重复内容和电子提交前检查
- 人工决策：在检查结果基础上进行批准、驳回或改判，并保留决策记录
- 文档输出：模板化排版，导出 DOCX/PDF，支持目录、附件索引和已生成配图装配
- 资格预审、资讯监控、RBAC、MCP/A2A/AG-UI 和运行监控等辅助能力

## 技术架构

```text
React + TypeScript + Electron/Vite
                |
             REST API
                |
FastAPI + Agent/Skill + LangGraph 编排
       |                 |                |
 PostgreSQL          Redis/Celery       RAG
                                          |
                             ChromaDB + BM25/RRF
```

主要目录：

| 目录 | 用途 |
| --- | --- |
| `core/` | Agent、Skill、RAG、文档解析、存储和密钥解析核心引擎 |
| `services/` | FastAPI 路由、业务服务、异步任务和协议服务 |
| `services/knowledge_corpus/` | 法规/标书采集、解析和知识库入库源码 |
| `skills/` | 运行时加载的提示词与技能定义 |
| `packages/desktop/` | React/Electron 前端 |
| `db/` | PostgreSQL 初始化脚本和迁移 |
| `templates/` | 默认投标文档模板 |
| `scripts/` | 本地启动、停止和对象存储迁移工具 |

## 环境要求

- Windows 10/11、macOS 或 Linux
- Python 3.12+
- Node.js 18+
- PostgreSQL 16+
- Redis 7+；Windows 可使用 Memurai Developer Edition
- 如需真实 LLM、Embedding、OCR 或配图，需要在本机配置对应服务的 Key

## 快速开始

### Windows 推荐方式

在仓库根目录执行，启动脚本必须以仓库根目录为当前目录：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env

# 编辑 .env，填写数据库、Redis 和需要使用的服务地址
powershell -ExecutionPolicy Bypass -File .\scripts\dev_start.ps1
```

脚本会启动或检测 PostgreSQL、Redis/Memurai、FastAPI、MCP 和前端开发服务。停止全部由它创建的进程：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev_stop.ps1
```

### 手动启动

先安装 Python 依赖和前端依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Set-Location packages\desktop
npm install
Set-Location ..\..
```

启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn services.main:app --host 0.0.0.0 --port 8000
```

另开终端启动前端开发服务：

```powershell
Set-Location packages\desktop
npm run dev
```

需要 Electron 桌面壳时使用：

```powershell
npm run electron:dev
```

浏览器访问 `http://localhost:5173`，API 文档访问 `http://localhost:8000/docs`。

### Docker 基础设施

Docker Compose 提供 PostgreSQL、Redis、MinIO、API 和 Celery Worker：

```bash
docker compose -f docker/docker-compose.yml up -d
```

Compose 文件不会替代前端开发服务器；前端仍需在 `packages/desktop` 执行 `npm run dev`，或构建 Electron 应用。

## 配置与密钥

复制 `.env.example` 为 `.env` 后按需修改。常用变量：

| 变量 | 作用 |
| --- | --- |
| `BMP_DATABASE_URL` | PostgreSQL 连接串 |
| `BMP_REDIS_URL` | Redis/Celery 连接串 |
| `BMP_CHROMA_DIR` | 本地向量库目录 |
| `BMP_LLM_DEFAULT_MODEL` | 默认 LLM 模型 |
| `BMP_LLM_API_BASE` | LLM 兼容接口地址 |
| `BMP_EMBEDDING_MODE` | `api` 或 `local` |
| `BMP_OCR_MODE` | `off`、`mock`、`cloud` 或 `selfhosted` |
| `BMP_MCP_ENABLED` / `BMP_A2A_ENABLED` / `BMP_AGUI_ENABLED` | 协议服务开关 |

`BMP_*_API_KEY` 只应存在于本机 `.env` 或系统凭据存储中。平台设置页支持将 LLM、Embedding、OCR 和配图 Key 写入 Windows Keyring；界面只显示掩码值。不要把真实 Key 写进源码、提交记录、截图或 issue。

## 使用主流程

1. 创建项目并上传招标文件。
2. 在项目内完成解析和招标解读，确认资格要求、评分标准和关键时间。
3. 生成或调整大纲，再按章节生成正文；项目资料和企业事实通过知识中心入库后由 RAG 检索引用。
4. 运行全量检查，阅读每条失败/警告对应的证据、章节和处理建议。
5. 补充资料或修订正文后重新检查；仍有硬性风险时不要直接批准。
6. 检查通过后进入排版和导出。AI 配图是独立的可选操作，可按章节生成并在最终整篇导出时统一装配。
7. 签字、盖章、CA 签名和采购平台提交属于导出后的人工执行事项。

## 运行数据与隐私

以下目录默认只在本机生成，并已加入忽略规则：

```text
.env                  # 本地配置和密钥
projects/             # 项目、章节和导出文件
uploads/              # 上传原文件
chroma_db/            # 向量库
data/                 # trace、缓存和运行数据
```

生产环境请使用独立数据库、对象存储和密钥管理服务，并在备份、日志和访问控制中遵循企业隐私要求。

## 开发检查

开发依赖安装后，可运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\ruff.exe check core services start.py
Set-Location packages\desktop
npm run build
```

测试目录和开发验收资料不属于发布运行包，不会被提交到发布仓库。

## 许可证

- **整个项目**（包括平台源码、桌面端、数据库脚本、启动工具、模板和 `skills/` 技能定义）统一按 GNU Affero General Public License 3.0（AGPL-3.0）发布，完整条款见 [LICENSE](LICENSE)。
- 第三方依赖仍受各自许可证约束，不能因为本项目的许可证而改变其条款。
