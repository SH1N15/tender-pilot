from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from core.secret_resolver import KeyringSettingsSource

# 与 services/env_store.py 同一锚点：.env 固定在仓库根目录，不受进程 CWD 影响。
# BUG-4 修复：此前 env_file=".env" 相对 CWD，而 env_store 写入锚定仓库根；
# 两者在 CWD != 仓库根时指向不同文件，导致 OCR 等配置重启后"丢失"。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_FILE = str(_REPO_ROOT / ".env")


class Settings(BaseSettings):
    app_name: str = "投标智航 / TenderPilot"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000

    db_type: str = "postgresql"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/bidmaster"
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root"
    mysql_database: str = "bidmaster"

    redis_url: str = "redis://localhost:6379/0"
    chroma_dir: str = "./chroma_db"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "bidmaster"
    storage_backend: str = "local"  # P1-4: local | minio（BMP_STORAGE_BACKEND 切换）
    projects_root: str = "./projects"
    qualification_trace_dir: str = ""

    llm_default_model: str = "deepseek/deepseek-chat"
    llm_api_key: str = ""
    llm_api_base: str = "https://api.deepseek.com"
    # P8-1（G-0）：默认禁用降级。此前默认 "ollama/qwen2.5" 指向本机不存在的服务，
    # 主模型失败后网关降级重试 100% 变成二次失败（404 Model not exist），
    # 并把 NotFoundError 计入熔断（phase8 排查结论）。需降级时显式配置 BMP_LLM_FALLBACK_MODES。
    llm_fallback_modes: str = ""
    llm_max_retries: int = 3
    llm_timeout: int = 180              # P8-4 追修（2026-09-01）：60s 对大 prompt 太紧，traces 实测批量 60s 整点超时
    # 思考模式开关（2026-09-02）：qwen3.7-flash 混合思考模型默认开思考，官方实测耗时约为
    # 非思考 3 倍、关闭可省 60%~75%；None=不传参（交 API 默认），False=显式关闭
    llm_enable_thinking: bool | None = False
    llm_max_concurrency: int = 16       # BUG-6 修复（2026-09-01）：配额充足，16 并发实测通过；旧默认 2 曾致解读互相拖死

    # ── G-6 T1: 检查驱动修复回路 ───────────────────────────
    repair_max_tasks: int | None = None  # 单轮修复任务量上限；None=全量（任务书默认），设正整数可限流

    # ── G-7 收官（2026-09-03）：图运行递归上限 ───────────────
    # 背景：TBC 重的章节其 grounding 重试环单章即可打满 LangGraph 默认 10000 步上限，
    # qualification 门 resume 直接 GraphRecursionError 500（traceback 见 .dev/pg6/wf-dec-err.json）。
    # BMP_GRAPH_RECURSION_LIMIT 可按需上调；默认 30000 为实测安全余量。
    graph_recursion_limit: int = 30000

    # ── P0-5: 外部调用成本守卫 ─────────────────────────────
    guard_enabled: bool = True
    llm_daily_max_calls: int = 2000
    llm_daily_max_tokens: int = 2000000
    ocr_daily_max_calls: int = 500
    image_daily_max_calls: int = 500
    guard_failure_threshold: int = 5    # 连续失败 N 次熔断
    guard_cooldown_seconds: float = 60.0  # 熔断冷却期（秒），过后半开

    # ── P-C: BGE-Reranker 重排 ────────────────────────────
    reranker_enabled: bool = False      # 默认关（显式开启），无 Key/调用失败自动降级跳过重排
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_api_key: str = ""
    reranker_api_base: str = "https://api.siliconflow.cn/v1"
    reranker_timeout: int = 15          # 单次重排请求超时（秒）
    reranker_candidate_k: int = 20      # 进入重排的候选池大小
    # G-0-3：按域重排策略，对齐 eval 侧 --rerank-domain-policy。
    # "all"=全部 collection 重排；"tender_only"=仅招标域（kb_legal_*/kb_*）重排，
    # 企业域（kb_ent_*）跳过（企业小语料重排零增益/成本+96%，
    # 见 eval/reports/pf-c-rerank-*-20260901.md，默认 reranker_enabled=False 关闭）。
    reranker_domain_policy: str = "all"  # all | tender_only

    embedding_mode: str = "api"
    embedding_model: str = "text-embedding-v3"
    embedding_api_key: str = ""
    embedding_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # ── vNext: MinerU OCR ─────────────────────────────────
    # mode: off | mock | cloud | selfhosted
    ocr_mode: str = "off"
    ocr_endpoint: str = "https://api.mineru.net/v4"
    ocr_api_key: str = ""
    ocr_timeout: int = 60
    ocr_poll_interval: float = 3.0
    ocr_max_polls: int = 120

    # ── vNext: MCP / A2A / AG-UI ──────────────────────────
    mcp_enabled: bool = True
    mcp_http_port: int = 9001
    a2a_enabled: bool = True
    agui_enabled: bool = True
    # 对外公布的基础 URL（Agent Card / SSE 地址），空则用 localhost:port
    public_base_url: str = ""

    # ── G-7 收尾：AI 配图增量（备用状态） ─────────────────────
    # BMP_ILLUSTRATION_ENABLED：总开关，默认 false=现状（建议语只留元数据，正文无脚手架）。
    # 开关开 + key 非空（或 provider=fallback 无需 key）时，章节 gate 后按建议装配图片引用。
    illustration_enabled: bool = False
    # 图片供应商：任意字符串。内置别名 volcengine / google / fallback；
    # 其他值 = 自定义 OpenAI 兼容 images/generations 端点（需配 image_base_url）。
    image_provider: str = "fallback"
    image_base_url: str = ""            # 自定义端点 base_url（如 https://ark.cn-beijing.volces.com/api/v3）
    image_model: str = ""               # 自定义端点模型名（如 doubao-seedream）
    image_api_key: str = ""             # 图片 API key（自定义端点用；内置供应商仍走各自 env key）

    # ── vNext: Tracing / 监控 ─────────────────────────────
    trace_enabled: bool = True
    trace_dir: str = "./data/traces"
    otlp_endpoint: str = ""

    model_config = SettingsConfigDict(env_file=_DEFAULT_ENV_FILE, env_prefix="BMP_", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """secret 读取优先级：init > 进程环境变量 > keyring > .env 文件 > 默认值。"""
        return (
            init_settings,
            env_settings,
            KeyringSettingsSource(),
            dotenv_settings,
            file_secret_settings,
        )

    def get_database_url(self) -> str:
        if self.db_type == "mysql":
            return (
                f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
                f"?charset=utf8mb4"
            )
        return self.database_url


def _load_settings() -> Settings:
    """构造 Settings；BIDMASTER_ENV_FILE 可在运行时覆盖 .env 路径（与 env_store 一致，测试隔离用）。"""
    override = os.environ.get("BIDMASTER_ENV_FILE")
    if override:
        return Settings(_env_file=override)
    return Settings()


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = _load_settings()
    return _settings


def reload_settings() -> Settings:
    """重新从 .env/环境变量加载配置（写入 .env 后调用）。"""
    global _settings
    _settings = _load_settings()
    return _settings


def graph_runtime_config(thread_id: str) -> dict:
    """G-7 收官（2026-09-03）：统一的 LangGraph 运行配置。

    此前各图 _config 只带 thread_id，LangGraph 默认 recursion_limit=10007；
    TBC 重的章节其 grounding 重试环单章即可打满上限（GraphRecursionError 500）。
    统一由 BMP_GRAPH_RECURSION_LIMIT（默认 30000）控制，所有图的 _config 均应走本函数。
    """
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": get_settings().graph_recursion_limit,
    }
