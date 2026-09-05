"""招标要求—企业能力资格预审：结构化数据模型。

本模块只定义 Pydantic 数据模型，不包含任何匹配逻辑（逻辑见 matcher.py）。
字段刻意宽松（日期/金额同时接受原生类型与字符串），
以便把"解析/校验"交给确定性规则引擎处理，而不是直接抛异常。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 支持的资格要求类型
RequirementType = Literal["certificate", "capital", "project_experience", "personnel", "region"]
REQUIREMENT_TYPES: tuple[str, ...] = ("certificate", "capital", "project_experience", "personnel", "region")

# 匹配状态
MatchStatus = Literal["met", "unmet", "insufficient"]


class Requirement(BaseModel):
    """招标文件中的一条资格要求。"""

    model_config = ConfigDict(extra="ignore")

    requirement_id: str
    requirement_type: RequirementType
    description: str = ""

    # certificate：证书名称；valid_until 表示证书须有效覆盖到该日期
    certificate_name: str | None = None
    valid_until: date | str | None = None

    # capital / project_experience：金额要求（数值按元计，字符串可带 万/亿/元 单位）
    min_amount: float | str | None = None
    currency: str = "CNY"

    # project_experience / personnel：数量要求
    min_count: int | None = None

    # project_experience：业绩时间窗口（须同时提供 date_from 与 date_to）
    date_from: date | str | None = None
    date_to: date | str | None = None

    # personnel：岗位名称
    personnel_title: str | None = None

    # region：注册地要求
    region: str | None = None

    # 来源证据（analysis_adapter 填充，不参与匹配，向后兼容）
    source_refs: list[str] = Field(default_factory=list)
    source_text: str | None = None
    source_path: str | None = None


class Credential(BaseModel):
    """企业提供的一份证明材料。"""

    model_config = ConfigDict(extra="ignore")

    credential_id: str
    credential_type: str
    name: str | None = None

    # certificate
    certificate_name: str | None = None
    issue_date: date | str | None = None
    expiry_date: date | str | None = None

    # capital
    amount: float | str | None = None
    amount_text: str | None = None
    currency: str = "CNY"

    # project_experience
    project_name: str | None = None
    contract_amount: float | str | None = None
    contract_amount_text: str | None = None
    start_date: date | str | None = None
    completion_date: date | str | None = None

    # personnel
    personnel_title: str | None = None
    certificate_number: str | None = None

    # region
    region: str | None = None

    # 证据引用：指向原始材料（文件路径 / 页码 / 条目 ID 等），不允许空引用判定 met
    evidence_refs: list[str] = Field(default_factory=list)
    source: str | None = None


class MatchResult(BaseModel):
    """单条资格要求的匹配结果。"""

    requirement_id: str
    requirement_type: str
    status: MatchStatus
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    matched_credential_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MatchSummary(BaseModel):
    total: int = 0
    met: int = 0
    unmet: int = 0
    insufficient: int = 0


class MatchReport(BaseModel):
    """整体资格预审报告。"""

    overall_status: MatchStatus
    summary: MatchSummary
    results: list[MatchResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MatchRequest(BaseModel):
    """POST /api/qualification/match 请求体。"""

    requirements: list[Requirement] = Field(default_factory=list)
    credentials: list[Credential] = Field(default_factory=list)


__all__ = [
    "REQUIREMENT_TYPES",
    "Requirement",
    "RequirementType",
    "Credential",
    "MatchResult",
    "MatchSummary",
    "MatchReport",
    "MatchRequest",
]
