"""MinerU OCR Provider Adapter（vNext）。

以 MinerU 官方云 API v4 为准（https://mineru.net/api/v4）：
- 文件上传：POST /api/v4/file-urls/batch 获取上传链接，再 PUT 上传本地文件
- 任务查询：GET  /api/v4/extract/task/{batch_id}  （done 后返回 zip，提取 Markdown）
同时支持自定义 self-hosted endpoint（若部署了 MinerU 官方 HTTP 服务）。

错误分类：auth / rate_limit / format / timeout / upstream / parse。
隐私：API Key 只在内存与本地 .env 中保存；日志与响应一律掩码；不记录文件正文。
Mock：无真实 key 时可用 MockMinerUClient 做确定性测试。
"""

from __future__ import annotations

import io
import logging
import os
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MINERU_CLOUD_DEFAULT_ENDPOINT = "https://mineru.net/api/v4"
MINERU_API_REF = "MinerU 官方云 API v4 (2026-08)"

# 结果状态
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
FINAL_STATES = {STATE_DONE, STATE_FAILED}


class MinerUError(Exception):
    """OCR 错误基类。error_class: auth/rate_limit/format/timeout/upstream/parse"""

    def __init__(self, error_class: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.status_code = status_code


class MinerUAuthError(MinerUError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__("auth", message, status_code)


class MinerURateLimitError(MinerUError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__("rate_limit", message, status_code)


class MinerUFormatError(MinerUError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__("format", message, status_code)


class MinerUTimeoutError(MinerUError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__("timeout", message, status_code)


class MinerUUpstreamError(MinerUError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__("upstream", message, status_code)


class MinerUParseError(MinerUError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__("parse", message, status_code)


@dataclass
class OCRConfig:
    mode: str = "off"  # off | cloud | selfhosted
    endpoint: str = MINERU_CLOUD_DEFAULT_ENDPOINT
    api_key: str = ""
    timeout: int = 60
    poll_interval: float = 3.0
    max_polls: int = 120

    def masked_api_key(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 4:
            return "••••"
        return "••••" + self.api_key[-4:]

    def to_public(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "endpoint": self.endpoint,
            "api_key_masked": self.masked_api_key(),
            "api_key_set": bool(self.api_key),
            "timeout": self.timeout,
            "poll_interval": self.poll_interval,
            "max_polls": self.max_polls,
            "api_ref": MINERU_API_REF,
        }


@dataclass
class OCRTask:
    task_id: str
    project_id: str
    document_id: str
    file_path: str
    state: str = STATE_PENDING
    error_class: str | None = None
    error_message: str | None = None
    markdown: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    polls: int = 0


class OCRTaskStore:
    """进程内 OCR 任务存储（后续可迁移 DB）。"""

    _instance: "OCRTaskStore | None" = None

    def __init__(self) -> None:
        self._tasks: dict[str, OCRTask] = {}

    @classmethod
    def instance(cls) -> "OCRTaskStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def add(self, task: OCRTask) -> None:
        self._tasks[task.task_id] = task

    def get(self, task_id: str) -> OCRTask | None:
        return self._tasks.get(task_id)

    def list_by_project(self, project_id: str) -> list[OCRTask]:
        return [t for t in self._tasks.values() if t.project_id == project_id]

    def pending(self) -> list[OCRTask]:
        return [t for t in self._tasks.values() if t.state not in FINAL_STATES]


class MinerUClient:
    """真实 MinerU 客户端（cloud / selfhosted）。"""

    def __init__(self, config: OCRConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=config.timeout)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    @property
    def _json_headers(self) -> dict[str, str]:
        return {**self._headers, "Content-Type": "application/json"}

    @staticmethod
    def _api_base(endpoint: str) -> str:
        """把用户填写的 endpoint 规范化为 API 基础地址（含 /api/v4）。"""
        e = endpoint.rstrip("/")
        for suffix in ("/extract/task", "/file/extract"):
            if e.endswith(suffix):
                return e[: -len(suffix)]
        if e.endswith("/v4"):
            return e
        return e

    def _classify(self, exc: Exception, resp: httpx.Response | None = None) -> MinerUError:
        if isinstance(exc, httpx.TimeoutException):
            return MinerUTimeoutError(f"MinerU 请求超时（{self.config.timeout}s）")
        if resp is not None:
            status = resp.status_code
            if status in (401, 403):
                return MinerUAuthError("MinerU API Key 无效或权限不足", status)
            if status == 429:
                return MinerURateLimitError("MinerU 限流，请稍后重试", status)
            if 400 <= status < 500:
                return MinerUFormatError(f"MinerU 请求被拒绝: HTTP {status}", status)
            if status >= 500:
                return MinerUUpstreamError(f"MinerU 上游错误: HTTP {status}", status)
        return MinerUUpstreamError(f"MinerU 连接失败: {exc}")

    async def test_connection(self) -> dict[str, Any]:
        """连接测试：官方 API 无健康检查端点，这里只校验配置已就绪。

        真实连通性通过 /api/ocr/run（真实上传→轮询→取回 Markdown）验证。
        """
        base = self._api_base(self.config.endpoint)
        return {
            "ok": True,
            "message": f"MinerU 配置已就绪（{self.config.mode} @ {base}）；真实连通性请使用 OCR 上传解析验证",
        }

    async def submit_file(self, file_path: str, is_ocr: bool = True) -> str:
        """官方 API 批量上传流程：申请上传链接 -> PUT 文件 -> 返回 batch_id。"""
        # P0-5 成本守卫：对外请求前必须过守卫（熔断/日配额拒绝时不发起真实请求）
        from core.cost_guard import get_cost_guard

        guard = get_cost_guard()
        await guard.precheck("ocr")
        base = self._api_base(self.config.endpoint)
        filename = os.path.basename(str(file_path))
        data_id = f"data_{uuid.uuid4().hex[:12]}"
        apply_payload = {
            "files": [
                {
                    "name": filename,
                    "is_ocr": bool(is_ocr),
                    "data_id": data_id,
                }
            ],
            "model_version": "pipeline",
        }
        apply_url = f"{base}/file-urls/batch"
        try:
            resp = await self._client.post(apply_url, headers=self._json_headers, json=apply_payload)
            if resp.status_code >= 400:
                raise self._classify(None, resp)
            payload = resp.json()
            if payload.get("code") not in (0, None) and payload.get("code") != 0:
                raise MinerUUpstreamError(f"MinerU 申请上传链接失败: {payload.get('msg', payload)}")
            data = payload.get("data") or {}
            batch_id = data.get("batch_id")
            file_urls = data.get("file_urls") or []
            if not batch_id or not file_urls:
                raise MinerUParseError(f"MinerU 响应缺少 batch_id/file_urls: {str(payload)[:300]}")
            upload_url = file_urls[0].get("file_url") if isinstance(file_urls[0], dict) else file_urls[0]
            if not upload_url:
                raise MinerUParseError("MinerU 上传链接为空")
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            # 官方文档：上传文件时不要设置 Content-Type，避免 OSS 签名校验失败。
            put_resp = await self._client.put(upload_url, content=file_bytes)
            if put_resp.status_code >= 400:
                raise self._classify(None, put_resp)
            await guard.record_result("ocr", ok=True)
            return str(batch_id)
        except MinerUError:
            await guard.record_result("ocr", ok=False)
            raise
        except Exception as e:
            await guard.record_result("ocr", ok=False)
            raise self._classify(e) from e

    async def query_task(self, task_id: str) -> dict[str, Any]:
        """查询任务状态，返回 {state, markdown, error}。"""
        base = self._api_base(self.config.endpoint)
        try:
            resp = await self._client.get(f"{base}/extract/task/{task_id}", headers=self._json_headers)
            if resp.status_code >= 400:
                raise self._classify(None, resp)
            payload = resp.json()
            msg = str(payload.get("msg") or "")
            # 批量上传返回的是 batch_id，官方批量结果端点为 /extract-results/batch/{id}
            if payload.get("code") not in (0, None) and ("not found" in msg.lower() or "expire" in msg.lower()):
                batch_resp = await self._client.get(
                    f"{base}/extract-results/batch/{task_id}", headers=self._json_headers
                )
                if batch_resp.status_code >= 400:
                    raise self._classify(None, batch_resp)
                payload = batch_resp.json()
            if payload.get("code") not in (0, None) and payload.get("code") != 0:
                raise MinerUUpstreamError(f"MinerU 查询失败: {payload.get('msg', payload)}")
            data = payload.get("data") or {}
            # 批量结果可能在 data.results / data.tasks / data.extract_result 下
            results = data.get("results") or data.get("tasks") or data.get("extract_result") or []
            if isinstance(results, list) and results:
                data = results[0] or {}
            state = str(data.get("state", "pending")).lower()
            if state in ("done", "success", "succeed", "task_done"):
                markdown = await self._fetch_markdown(data)
                return {"state": STATE_DONE, "markdown": markdown, "error": None}
            if state in ("failed", "error", "canceled", "cancelled", "task_failed"):
                return {
                    "state": STATE_FAILED,
                    "markdown": None,
                    "error": str(data.get("err_msg") or data.get("error_msg") or "MinerU 任务失败"),
                }
            if state in ("running", "processing", "task_running"):
                return {"state": STATE_RUNNING, "markdown": None, "error": None}
            return {"state": STATE_PENDING, "markdown": None, "error": None}
        except MinerUError:
            raise
        except Exception as e:
            raise self._classify(e) from e

    async def _fetch_markdown(self, data: dict[str, Any]) -> str:
        """从查询响应中提取 Markdown：优先内联字段，其次下载 full_zip_url 解压。"""
        inline = self._extract_markdown(data)
        if inline:
            return inline
        zip_url = data.get("full_zip_url")
        if not zip_url:
            return ""
        try:
            # CDN 下载使用独立 client 并关闭代理/信任环境，避免系统代理导致连接失败。
            async with httpx.AsyncClient(timeout=self.config.timeout, trust_env=False) as dl:
                resp = await dl.get(zip_url)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                names = zf.namelist()
                for name in names:
                    lower = name.lower()
                    if lower.endswith(".md") or "full.md" in lower:
                        return zf.read(name).decode("utf-8", errors="replace")
                for name in names:
                    return zf.read(name).decode("utf-8", errors="replace")
        except MinerUError:
            raise
        except Exception as e:
            raise self._classify(e) from e
        return ""

    @staticmethod
    def _extract_markdown(data: dict[str, Any]) -> str:
        """从 extract_result 中提取 markdown（兼容不同响应结构）。"""
        extract_result = data.get("extract_result") or []
        if isinstance(extract_result, list) and extract_result:
            first = extract_result[0] or {}
            extract_data = first.get("extract_data") or first
            md = extract_data.get("markdown")
            if isinstance(md, str) and md.strip():
                return md
        # 兜底：直接取 markdown 字段
        md = data.get("markdown")
        if isinstance(md, str) and md.strip():
            return md
        return ""


class MockMinerUClient:
    """确定性 Mock：无真实 key 时用于测试/开发，不访问网络。"""

    def __init__(self, config: OCRConfig):
        self.config = config
        self._submitted: dict[str, dict[str, Any]] = {}
        self.fail_mode: str | None = None  # None | auth | rate_limit | upstream | failed

    def set_fail_mode(self, mode: str | None) -> None:
        self.fail_mode = mode

    async def close(self) -> None:
        pass

    async def test_connection(self) -> dict[str, Any]:
        if self.fail_mode == "auth":
            raise MinerUAuthError("mock: 401 Unauthorized", 401)
        if self.fail_mode == "rate_limit":
            raise MinerURateLimitError("mock: 429 Too Many Requests", 429)
        if self.fail_mode == "upstream":
            raise MinerUUpstreamError("mock: 500 upstream", 500)
        return {"ok": True, "message": "Mock MinerU 服务可达（模拟）"}

    async def submit_file(self, file_path: str, is_ocr: bool = True) -> str:
        if self.fail_mode == "auth":
            raise MinerUAuthError("mock: 401 Unauthorized", 401)
        task_id = "mock-" + uuid.uuid4().hex[:12]
        self._submitted[task_id] = {
            "state": STATE_PENDING,
            "markdown": None,
            "error": None,
            "polls": 0,
        }
        return task_id

    async def query_task(self, task_id: str) -> dict[str, Any]:
        task = self._submitted.get(task_id)
        if task is None:
            raise MinerUUpstreamError(f"mock: 未知 task_id {task_id}")
        if self.fail_mode == "failed":
            return {"state": STATE_FAILED, "markdown": None, "error": "mock: 解析失败"}
        task["polls"] += 1
        if task["polls"] < 2:
            return {"state": STATE_RUNNING, "markdown": None, "error": None}
        md = (
            "# Mock MinerU OCR 结果\n\n"
            "## 项目概况\n本项目为测试用途，正文由 Mock transport 提供，用于确定性验证 OCR 链路。\n\n"
            "## 资格要求\n1. 具有独立法人资格\n2. 注册资金不低于 500 万元\n"
        )
        task["state"] = STATE_DONE
        task["markdown"] = md
        return {"state": STATE_DONE, "markdown": md, "error": None}


def build_ocr_config(
    mode: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    timeout: int | None = None,
    poll_interval: float | None = None,
    max_polls: int | None = None,
) -> OCRConfig:
    from core.settings import get_settings

    s = get_settings()
    return OCRConfig(
        mode=(mode if mode is not None else s.ocr_mode) or "off",
        endpoint=(endpoint if endpoint is not None else s.ocr_endpoint) or MINERU_CLOUD_DEFAULT_ENDPOINT,
        api_key=(api_key if api_key is not None else s.ocr_api_key) or "",
        timeout=timeout if timeout is not None else s.ocr_timeout,
        poll_interval=poll_interval if poll_interval is not None else s.ocr_poll_interval,
        max_polls=max_polls if max_polls is not None else s.ocr_max_polls,
    )


async def get_ocr_client(config: OCRConfig | None = None) -> MinerUClient | MockMinerUClient | None:
    """按配置返回客户端；mode=off 返回 None；无 key 且 cloud/selfhosted 时返回 None（由调用方提示配置）。"""
    cfg = config or build_ocr_config()
    if cfg.mode == "off":
        return None
    if not cfg.api_key and cfg.mode == "cloud":
        return None
    if cfg.mode == "selfhosted" and not cfg.endpoint:
        return None
    if cfg.mode == "mock" or (cfg.mode in ("cloud", "selfhosted") and cfg.api_key.lower().startswith("mock-")):
        return MockMinerUClient(cfg)
    return MinerUClient(cfg)


def mask_api_key(value: str) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 4 else "••••"
