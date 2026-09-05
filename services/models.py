from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ProjectStatus(str, enum.Enum):
    CREATED = "created"
    INTERPRETING = "interpreting"
    ANALYZING = "analyzing"
    OUTLINING = "outlining"
    GENERATING = "generating"
    CHECKING = "checking"
    FORMATTING = "formatting"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class DocumentType(str, enum.Enum):
    TENDER = "tender"
    BID = "bid"
    TEMPLATE = "template"
    REFERENCE = "reference"


class CheckType(str, enum.Enum):
    COMPLIANCE = "compliance"
    DISQUALIFICATION = "disqualification"
    DUPLICATE = "duplicate"
    CONSISTENCY = "consistency"
    FORMAT = "format"
    QUALIFICATION = "qualification"
    DEPOSIT = "deposit"
    SIGNATURE = "signature"
    PRICING = "pricing"
    MANDATORY = "mandatory"
    VALIDITY = "validity"
    SELFCHECK = "selfcheck"
    FIT_SCORE = "fit_score"
    AI_TEXT = "ai_text"
    CROSS_CHECK = "cross_check"
    SAMPLE_REPORT = "sample_report"
    JOINT_BID = "joint_bid"
    EBID_SUBMIT = "ebid_submit"
    PRICING_LOGIC = "pricing_logic"
    DOC_INTEGRITY = "doc_integrity"
    RISK_SCORE = "risk_score"


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    PROJECT_MANAGER = "project_manager"
    WRITER = "writer"
    REVIEWER = "reviewer"


def _uuid_default():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    role = Column(String(20), default=UserRole.WRITER.value)
    avatar = Column(String(500), nullable=True)
    password_hash = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    projects = relationship("Project", back_populates="user")


class AuthSession(Base):
    """持久化认证会话。

    表内存 sha256(token) 而非明文 token，防拖库冒用。
    user_id 不设外键（允许开发账号 "dev-admin-0000" 与用户删除后的会话继续存在），
    email/name/role 为登录时快照，主要用于开发账号与用户已被删除的兜底场景；
    verify_token 会对真实用户做联表查询，避免返回过期快照。"""

    __tablename__ = "auth_sessions"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    email = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    role = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=datetime.now, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    status = Column(String(50), default=ProjectStatus.CREATED.value, index=True)
    tender_doc_id = Column(String(36), ForeignKey("documents.id"), nullable=True)
    config = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="projects")
    documents = relationship("Document", back_populates="project", foreign_keys="Document.project_id")
    analysis = relationship("Analysis", back_populates="project", uselist=False)
    outline = relationship("Outline", back_populates="project", uselist=False)
    chapters = relationship("Chapter", back_populates="project")
    check_reports = relationship("CheckReport", back_populates="project")


class Document(Base):
    __tablename__ = "documents"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    project_id = Column(String(36), ForeignKey("projects.id"), nullable=True, index=True)
    type = Column(String(20), default=DocumentType.TENDER.value)
    file_path = Column(String(500), nullable=False)
    original_name = Column(String(255), nullable=True)
    file_size = Column(Integer, nullable=True)
    parsed_content = Column(Text, nullable=True)
    doc_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="documents", foreign_keys=[project_id])


class Analysis(Base):
    __tablename__ = "analyses"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    project_id = Column(String(36), ForeignKey("projects.id"), nullable=False, unique=True, index=True)
    dimensions = Column(JSON, default=dict)
    scoring_matrix = Column(JSON, default=dict)
    risk_flags = Column(JSON, default=dict)
    sections = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    project = relationship("Project", back_populates="analysis")


class Outline(Base):
    __tablename__ = "outlines"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    project_id = Column(String(36), ForeignKey("projects.id"), nullable=False, unique=True, index=True)
    mode = Column(String(20), default="aligned")
    tree = Column(JSON, default=dict)
    score_mapping = Column(JSON, default=dict)
    reviewed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    project = relationship("Project", back_populates="outline")


class Chapter(Base):
    __tablename__ = "chapters"

    # BUG-13：复合主键 (project_id, id)。大纲节点号（"1"/"1.1"）只在项目内唯一，
    # 全局单列主键会导致跨项目 INSERT 冲突与按 id 串写。
    id = Column(String(36), primary_key=True, default=_uuid_default)
    project_id = Column(
        String(36), ForeignKey("projects.id"), nullable=False, index=True, primary_key=True
    )
    outline_id = Column(String(36), ForeignKey("outlines.id"), nullable=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=True)
    mode = Column(String(10), default="A")
    status = Column(String(20), default="pending")
    word_count = Column(Integer, default=0)
    # P-G G-2：引用对照表落库（{n: {chunk_id, source, excerpt}}，正文页【n】点查来源）
    citation_ledger = Column(JSON, nullable=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    project = relationship("Project", back_populates="chapters")


class CheckReport(Base):
    __tablename__ = "check_reports"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    project_id = Column(String(36), ForeignKey("projects.id"), nullable=False, index=True)
    type = Column(String(30), nullable=False, index=True)
    results = Column(JSON, default=dict)
    risk_level = Column(String(20), default="low")
    summary = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.now)

    project = relationship("Project", back_populates="check_reports")


class SkillConfig(Base):
    __tablename__ = "skill_configs"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    name = Column(String(100), unique=True, nullable=False)
    category = Column(String(50), nullable=False)
    version = Column(String(20), default="1.0.0")
    config = Column(JSON, default=dict)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class AgentConfig(Base):
    __tablename__ = "agent_configs"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    name = Column(String(100), unique=True, nullable=False)
    workflow_dsl = Column(JSON, default=dict)
    skills = Column(JSON, default=list)
    config = Column(JSON, default=dict)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    channel = Column(String(50), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String(20), default="pending")
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    name = Column(String(200), nullable=False)
    doc_count = Column(Integer, default=0)
    embedding_model = Column(String(100), default="text-embedding-v3")
    collection_name = Column(String(200), nullable=True)
    # P-B 双库治理：legal=法规合规库 / enterprise=企业私有库
    kb_type = Column(String(20), nullable=False, default="enterprise")
    # 审核状态：draft（草稿）| reviewed（已复核）| published（已发布）
    review_status = Column(String(20), nullable=False, default="draft")
    # 语料有效期（法规修订/资质到期后需复核），nullable=长期有效
    valid_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class MonitoringTask(Base):
    __tablename__ = "monitoring_tasks"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    keywords = Column(Text, nullable=False)
    exclude_keywords = Column(Text, default="")
    must_contain_keywords = Column(Text, default="")
    sites = Column(JSON, default=list)
    interval_minutes = Column(Integer, default=60)
    enabled = Column(Boolean, default=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class CrawlResult(Base):
    __tablename__ = "crawl_results"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    task_id = Column(String(36), ForeignKey("monitoring_tasks.id"), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    url = Column(String(1000), nullable=False)
    source = Column(String(500), default="")
    pub_date = Column(String(50), nullable=True)
    content = Column(Text, nullable=True)
    keyword_score = Column(Float, default=0.0)
    relevance_score = Column(Float, default=0.0)
    category = Column(String(50), default="general")
    is_hot = Column(Boolean, default=False)
    hot_score = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.now)


class RBACRole(Base):
    __tablename__ = "rbac_roles"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    name = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, default="")
    is_system = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)


class RBACPermission(Base):
    __tablename__ = "rbac_permissions"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    code = Column(String(200), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    category = Column(String(100), nullable=False)
    description = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)


class RBACUserRole(Base):
    __tablename__ = "rbac_user_roles"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    role_id = Column(String(36), ForeignKey("rbac_roles.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)


class RBACRolePermission(Base):
    __tablename__ = "rbac_role_permissions"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    role_id = Column(String(36), ForeignKey("rbac_roles.id"), nullable=False, index=True)
    permission_id = Column(String(36), ForeignKey("rbac_permissions.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now)


class TenderEntity(Base):
    """P-A：招投标实体（规则+LLM 双路抽取产物，带页码证据）。"""

    __tablename__ = "tender_entities"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    project_id = Column(String(36), ForeignKey("projects.id"), nullable=True, index=True)
    document_id = Column(String(36), ForeignKey("documents.id"), nullable=True, index=True)
    entity_type = Column(String(50), nullable=False, index=True)
    value = Column(String(500), nullable=False)
    norm = Column(String(500), nullable=False, default="")
    source = Column(String(20), default="rule")  # rule|llm
    confidence = Column(Float, default=0.0)
    page = Column(Integer, default=0)
    evidence = Column(Text, default="")
    conflict = Column(Boolean, default=False)
    review_status = Column(String(20), default="auto")  # auto|待审
    created_at = Column(DateTime, default=datetime.now)


class StructuredArtifact(Base):
    """P-A：结构化产物登记（JSON 落盘路径 + 摘要，供查询）。"""

    __tablename__ = "structured_artifacts"

    id = Column(String(36), primary_key=True, default=_uuid_default)
    project_id = Column(String(36), ForeignKey("projects.id"), nullable=True, index=True)
    document_id = Column(String(36), ForeignKey("documents.id"), nullable=True, index=True)
    artifact_type = Column(String(30), nullable=False)  # layout|tables|chunks|entities
    path = Column(String(500), nullable=False)
    summary = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.now)
