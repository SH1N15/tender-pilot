import { useEffect, useState } from 'react';
import {
  Play,
  ClipboardCheck,
  Loader2,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  RefreshCw,
  FileText,
  RotateCcw,
  User,
  Download,
  FolderOpen,
} from 'lucide-react';
import {
  qualificationApi,
  qualificationGraphApi,
  type AdapterResult,
  type BizGraphRunDetail,
  type Credential,
  type CredentialAdapterResult,
  type CredentialCandidate,
  type EvalReport,
  type FlywheelMetrics,
  type MatchReport,
  type QualificationWorkflow,
  type Requirement,
  type ReviewDecision,
  type WorkflowDecision,
} from '../services/api';
import { useAppStore } from '../stores/appStore';
import RunStatusStrip from '../components/graph/RunStatusStrip';

const DEMO_REQUIREMENTS = JSON.stringify(
  [
    {
      requirement_id: 'r1',
      requirement_type: 'certificate',
      description: '质量管理体系认证且在有效期内',
      certificate_name: 'ISO9001',
      valid_until: '2026-12-31',
    },
    {
      requirement_id: 'r2',
      requirement_type: 'capital',
      description: '注册资本不低于1000万元',
      min_amount: '1000万元',
    },
    {
      requirement_id: 'r3',
      requirement_type: 'project_experience',
      description: '近三年类似项目业绩≥2项，单项合同额≥500万元',
      min_count: 2,
      min_amount: '500万元',
      date_from: '2022-01-01',
      date_to: '2025-12-31',
    },
  ],
  null,
  2,
);

const DEMO_CREDENTIALS = JSON.stringify(
  [
    {
      credential_id: 'c1',
      credential_type: 'certificate',
      certificate_name: 'ISO9001',
      expiry_date: '2027-06-30',
      evidence_refs: ['营业执照副本.pdf#p1', 'ISO9001证书扫描件.pdf#p3'],
    },
    {
      credential_id: 'c2',
      credential_type: 'capital',
      name: '营业执照',
      amount_text: '2000万元',
      currency: 'CNY',
      evidence_refs: ['营业执照副本.pdf#p1'],
    },
    {
      credential_id: 'c3',
      credential_type: 'project_experience',
      project_name: 'XX数据中心建设项目',
      contract_amount: '680万元',
      completion_date: '2023-09-30',
      evidence_refs: ['业绩合同扫描件.pdf#p2'],
    },
    {
      credential_id: 'c4',
      credential_type: 'project_experience',
      project_name: 'YY产业园弱电工程',
      contract_amount: '520万元',
      completion_date: '2024-05-15',
      evidence_refs: [], // 无证据引用 -> 触发人工确认演示
    },
  ],
  null,
  2,
);

const DEMO_DIMENSIONS = JSON.stringify(
  {
    qualification: {
      qualification_level: '具有建筑工程施工总承包三级及以上资质',
      registered_capital: '注册资本不低于1000万元',
      performance_requirement: '近三年内完成过至少2项单项合同金额500万元以上的类似项目',
      personnel_requirement: '项目经理1名、专职安全员不少于2人',
      other_requirements: '投标人须在广东省内注册',
    },
    timeline: {
      bid_deadline: '2026-12-31',
    },
  },
  null,
  2,
);

interface ReviewDraft {
  decision: ReviewDecision | '';
  reviewer: string;
  note: string;
}

const STATUS_META: Record<string, { label: string; color: string; bg: string }> = {
  met: { label: '满足', color: '#059669', bg: '#ecfdf5' },
  unmet: { label: '不满足', color: '#dc2626', bg: '#fef2f2' },
  insufficient: { label: '信息不足', color: '#d97706', bg: '#fffbeb' },
  completed: { label: '已完成', color: '#059669', bg: '#ecfdf5' },
  waiting_human: { label: '等待人工', color: '#d97706', bg: '#fffbeb' },
  resumed: { label: '待继续审批', color: '#1a56db', bg: '#eff6ff' },
};

const DECISION_LABELS: Record<ReviewDecision, string> = {
  confirm: '确认（背书判定）',
  reject: '否决',
  mark_insufficient: '标记信息不足',
};

function getErrMsg(e: unknown): string {
  if (e && typeof e === 'object' && 'response' in e) {
    const resp = (e as { response?: { data?: { detail?: string } } }).response;
    if (resp?.data?.detail) return resp.data.detail;
  }
  return e instanceof Error ? e.message : '请求失败';
}

export default function QualificationPage() {
  const [requirementsText, setRequirementsText] = useState(DEMO_REQUIREMENTS);
  const [credentialsText, setCredentialsText] = useState(DEMO_CREDENTIALS);
  const [jsonError, setJsonError] = useState('');
  const [apiError, setApiError] = useState('');
  const [loading, setLoading] = useState(false);
  const [mode, setMode] = useState<'idle' | 'match' | 'workflow'>('idle');
  const [report, setReport] = useState<MatchReport | null>(null);
  const [workflow, setWorkflow] = useState<QualificationWorkflow | null>(null);
  const [reviews, setReviews] = useState<Record<string, ReviewDraft>>({});
  const [analysisText, setAnalysisText] = useState(DEMO_DIMENSIONS);
  const [adapter, setAdapter] = useState<AdapterResult | null>(null);
  const currentProjectId = useAppStore((s) => s.currentProjectId);
  const [metrics, setMetrics] = useState<FlywheelMetrics | null>(null);
  const [metricsLoading, setMetricsLoading] = useState(false);
  const [metricsError, setMetricsError] = useState('');
  const [evalReport, setEvalReport] = useState<EvalReport | null>(null);
  const [evalLoading, setEvalLoading] = useState(false);
  const [evalError, setEvalError] = useState('');
  const [credText, setCredText] = useState('');
  const [credResult, setCredResult] = useState<CredentialAdapterResult | null>(null);
  const [credLoading, setCredLoading] = useState(false);
  const [credError, setCredError] = useState('');
  const [confirmRefs, setConfirmRefs] = useState<Record<string, string>>({});
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [confirmedIds, setConfirmedIds] = useState<string[]>([]);

  useEffect(() => {
    loadMetrics();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const parseInputs = (): { requirements: Requirement[]; credentials: Credential[] } | null => {
    try {
      const requirements = JSON.parse(requirementsText) as Requirement[];
      const credentials = JSON.parse(credentialsText) as Credential[];
      if (!Array.isArray(requirements) || !Array.isArray(credentials)) {
        setJsonError('requirements 与 credentials 都必须是 JSON 数组');
        return null;
      }
      setJsonError('');
      return { requirements, credentials };
    } catch (e) {
      setJsonError(`JSON 解析失败：${e instanceof Error ? e.message : String(e)}`);
      return null;
    }
  };

  const resetDemo = () => {
    setRequirementsText(DEMO_REQUIREMENTS);
    setCredentialsText(DEMO_CREDENTIALS);
    setAnalysisText(DEMO_DIMENSIONS);
    setAdapter(null);
    setJsonError('');
    setApiError('');
    setMode('idle');
    setReport(null);
    setWorkflow(null);
    setReviews({});
  };

  const handleMatch = async () => {
    const parsed = parseInputs();
    if (!parsed) return;
    setLoading(true);
    setApiError('');
    setMode('match');
    setWorkflow(null);
    setReviews({});
    try {
      const res = await qualificationApi.match(parsed.requirements, parsed.credentials);
      setReport(res.data);
    } catch (e) {
      setApiError(getErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const initReviews = (wf: QualificationWorkflow) => {
    const drafts: Record<string, ReviewDraft> = {};
    wf.review_items.forEach((item) => {
      drafts[item.requirement_id] = {
        decision: (item.decision as ReviewDecision) || '',
        reviewer: '',
        note: '',
      };
    });
    setReviews(drafts);
  };

  const handleRunWorkflow = async () => {
    const parsed = parseInputs();
    if (!parsed) return;
    setLoading(true);
    setApiError('');
    setMode('workflow');
    setReport(null);
    try {
      // G-5 T1：启动走资格预审图运行（提取→比对→HITL 审批门），响应形状与旧 workflow/run 一致
      const started = await qualificationGraphApi.start({
        requirements: parsed.requirements,
        credentials: parsed.credentials,
      });
      const runId = started.data?.run_id;
      if (!runId) throw new Error('创建资格预审图运行失败');
      await pollQualificationRun(runId);
    } catch (e) {
      setApiError(getErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  // ── G-5 T1：资格预审图运行状态条 + 轮询 ──
  const [graphRunDetail, setGraphRunDetail] = useState<BizGraphRunDetail | null>(null);
  const [graphPolling, setGraphPolling] = useState(false);

  const pollQualificationRun = async (runId: string) => {
    setGraphPolling(true);
    try {
      const maxPolls = 100; // 本图无 LLM，通常瞬时；3s×100 兜底
      for (let i = 0; i < maxPolls; i++) {
        try {
          const res = await qualificationGraphApi.get(runId);
          setGraphRunDetail(res.data);
          const wfStatus = res.data.snapshot?.workflow_status as string | undefined;
          if (wfStatus && wfStatus !== 'running') {
            setWorkflow({
              workflow_id: runId,
              status: wfStatus as QualificationWorkflow['status'],
              report: (res.data.snapshot?.report ?? { overall_status: 'met', summary: { total: 0, met: 0, unmet: 0, insufficient: 0 }, results: [], warnings: [] }) as unknown as QualificationWorkflow['report'],
              review_items: (res.data.snapshot?.review_items ?? []) as unknown as QualificationWorkflow['review_items'],
              decisions: (res.data.snapshot?.decisions ?? []) as unknown as QualificationWorkflow['decisions'],
              warnings: (res.data.snapshot?.warnings ?? []) as string[],
            });
            initReviewsFromGraph(res.data);
            return;
          }
        } catch {
          // 轮询失败继续
        }
        await new Promise((r) => setTimeout(r, 3000));
      }
      setApiError('资格预审图运行查询超时');
    } finally {
      setGraphPolling(false);
    }
  };

  const initReviewsFromGraph = (detail: BizGraphRunDetail) => {
    const items = (detail.snapshot?.review_items ?? []) as Array<{ requirement_id: string; status: string; reason: string; evidence_refs: string[]; decision?: string | null }>;
    const drafts: Record<string, { decision: ReviewDecision | ''; reviewer: string; note: string }> = {};
    for (const item of items) {
      drafts[item.requirement_id] = {
        decision: (item.decision as ReviewDecision) || '',
        reviewer: '',
        note: '',
      };
    }
    setReviews(drafts);
  };

  // Analysis/project inputs both start the qualification graph directly.
  const refreshRunStrip = async (workflowId: string) => {
    if (!workflowId.startsWith('qrun_')) return;
    try {
      const res = await qualificationGraphApi.get(workflowId);
      setGraphRunDetail(res.data);
    } catch {
      /* 历史非图 workflow 无图 run，忽略 */
    }
  };

  const parseAnalysis = (): Record<string, unknown> | null => {
    try {
      const dimensions = JSON.parse(analysisText) as Record<string, unknown>;
      if (typeof dimensions !== 'object' || dimensions === null || Array.isArray(dimensions)) {
        setJsonError('分析结果必须是 JSON 对象（dimensions dict）');
        return null;
      }
      setJsonError('');
      return dimensions;
    } catch (e) {
      setJsonError(`分析结果 JSON 解析失败：${e instanceof Error ? e.message : String(e)}`);
      return null;
    }
  };

  const handleImportAnalysis = async () => {
    const dimensions = parseAnalysis();
    if (!dimensions) return;
    setLoading(true);
    setApiError('');
    setAdapter(null);
    try {
      const res = await qualificationApi.fromAnalysis(dimensions);
      setAdapter(res.data);
      setRequirementsText(JSON.stringify(res.data.requirements, null, 2));
    } catch (e) {
      setApiError(getErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const handleRunFromAnalysis = async () => {
    const dimensions = parseAnalysis();
    if (!dimensions) return;
    let credentials: Credential[];
    try {
      const parsed = JSON.parse(credentialsText) as Credential[];
      if (!Array.isArray(parsed)) {
        setJsonError('credentials 必须是 JSON 数组');
        return;
      }
      credentials = parsed;
    } catch (e) {
      setJsonError(`credentials JSON 解析失败：${e instanceof Error ? e.message : String(e)}`);
      return;
    }
    setLoading(true);
    setApiError('');
    setMode('workflow');
    setReport(null);
    try {
      const adapted = await qualificationApi.fromAnalysis(dimensions);
      setAdapter(adapted.data);
      setRequirementsText(JSON.stringify(adapted.data.requirements, null, 2));
      const started = await qualificationGraphApi.start({ requirements: adapted.data.requirements, credentials });
      await pollQualificationRun(started.data.run_id);
    } catch (e) {
      setApiError(getErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const handleImportFromProject = async () => {
    if (!currentProjectId) {
      setApiError('未选择项目：请先在页面顶部选择当前项目');
      return;
    }
    setLoading(true);
    setApiError('');
    setAdapter(null);
    try {
      const res = await qualificationApi.fromProject(currentProjectId);
      setAdapter(res.data);
      setRequirementsText(JSON.stringify(res.data.requirements, null, 2));
    } catch (e) {
      setApiError(getErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const handleRunFromProject = async () => {
    if (!currentProjectId) {
      setApiError('未选择项目：请先在页面顶部选择当前项目');
      return;
    }
    let credentials: Credential[];
    try {
      const parsed = JSON.parse(credentialsText) as Credential[];
      if (!Array.isArray(parsed)) {
        setJsonError('credentials 必须是 JSON 数组');
        return;
      }
      credentials = parsed;
    } catch (e) {
      setJsonError(`credentials JSON 解析失败：${e instanceof Error ? e.message : String(e)}`);
      return;
    }
    setLoading(true);
    setApiError('');
    setMode('workflow');
    setReport(null);
    try {
      const adapted = await qualificationApi.fromProject(currentProjectId);
      setAdapter(adapted.data);
      setRequirementsText(JSON.stringify(adapted.data.requirements, null, 2));
      const started = await qualificationGraphApi.start({ requirements: adapted.data.requirements, credentials, project_id: currentProjectId });
      await pollQualificationRun(started.data.run_id);
    } catch (e) {
      setApiError(getErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const loadMetrics = async () => {
    setMetricsLoading(true);
    setMetricsError('');
    try {
      const res = await qualificationApi.flywheelMetrics();
      setMetrics(res.data);
    } catch (e) {
      setMetricsError(getErrMsg(e));
    } finally {
      setMetricsLoading(false);
    }
  };

  const handleExportFlywheel = async () => {
    setMetricsLoading(true);
    setMetricsError('');
    try {
      const res = await qualificationApi.flywheelExport();
      const blob = new Blob([res.data as unknown as BlobPart], { type: 'application/x-ndjson' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'qualification_flywheel.jsonl';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setMetricsError(getErrMsg(e));
    } finally {
      setMetricsLoading(false);
    }
  };

  const handleRunEval = async () => {
    setEvalLoading(true);
    setEvalError('');
    try {
      const res = await qualificationApi.evalRun('synthetic_baseline');
      setEvalReport(res.data);
    } catch (e) {
      setEvalError(getErrMsg(e));
    } finally {
      setEvalLoading(false);
    }
  };

  const candidateFields = (c: CredentialCandidate): string => {
    const parts: string[] = [];
    if (c.certificate_name) parts.push(c.certificate_name);
    if (c.certificate_number) parts.push(`编号:${c.certificate_number}`);
    if (c.amount_text) parts.push(`金额:${c.amount_text}`);
    if (c.expiry_date) parts.push(`有效期:${c.expiry_date}`);
    if (c.project_name) parts.push(c.project_name);
    if (c.contract_amount_text) parts.push(`合同:${c.contract_amount_text}`);
    if (c.completion_date) parts.push(`完成:${c.completion_date}`);
    if (c.personnel_title) parts.push(c.personnel_title);
    if (c.region) parts.push(c.region);
    return parts.join('；') || '—';
  };

  const handleCredFromText = async () => {
    if (!credText.trim()) {
      setCredError('请先输入企业材料文本');
      return;
    }
    setCredLoading(true);
    setCredError('');
    try {
      const res = await qualificationApi.credentialsFromText(credText);
      setCredResult(res.data);
    } catch (e) {
      setCredError(getErrMsg(e));
    } finally {
      setCredLoading(false);
    }
  };

  const handleCredFromProject = async () => {
    if (!currentProjectId) {
      setCredError('未选择项目：请先在页面顶部选择当前项目');
      return;
    }
    setCredLoading(true);
    setCredError('');
    try {
      const res = await qualificationApi.credentialsFromProject(currentProjectId);
      setCredResult(res.data);
    } catch (e) {
      setCredError(getErrMsg(e));
    } finally {
      setCredLoading(false);
    }
  };

  const appendCredential = (cred: Credential) => {
    try {
      const current = JSON.parse(credentialsText) as Credential[];
      if (!Array.isArray(current)) throw new Error('credentials 不是数组');
      const next = [...current.filter((c) => c.credential_id !== cred.credential_id), cred];
      setCredentialsText(JSON.stringify(next, null, 2));
    } catch (e) {
      setCredError(`credentials JSON 无效，无法追加：${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const handleConfirmCandidate = async (candidate: CredentialCandidate) => {
    const ref = (confirmRefs[candidate.candidate_id] || '').trim();
    if (!ref) {
      setCredError('请先填写 evidence_ref（如 document:doc123#p3 或 manual:标签）');
      return;
    }
    setConfirmingId(candidate.candidate_id);
    setCredError('');
    try {
      const res = await qualificationApi.confirmCredential(candidate, ref);
      appendCredential(res.data);
      setConfirmedIds((prev) => [...prev, candidate.candidate_id]);
    } catch (e) {
      setCredError(getErrMsg(e));
    } finally {
      setConfirmingId(null);
    }
  };

  const handleApprove = async () => {
    if (!workflow) return;
    const decisions: WorkflowDecision[] = Object.entries(reviews)
      .filter(([, v]) => v.decision)
      .map(([requirement_id, v]) => ({
        requirement_id,
        decision: v.decision as ReviewDecision,
        reviewer: v.reviewer,
        note: v.note,
      }));
    if (decisions.length === 0) {
      setApiError('请先为至少一个评审项选择决策（confirm / reject / mark_insufficient）');
      return;
    }
    setLoading(true);
    setApiError('');
    try {
      // G-5 T1：审批走资格预审图人工审批门（/qualification/graph/runs/{id}/decision，语义同旧 approve）
      const res = await qualificationGraphApi.decide(workflow.workflow_id, decisions);
      // 审批后刷新最终快照（图内 approve 语义复用旧状态机，报告按决策重建）
      const detailRes = await qualificationGraphApi.get(workflow.workflow_id);
      const detail = detailRes.data;
      setGraphRunDetail(detail);
      const wfStatus = (detail.snapshot?.workflow_status as string) || 'completed';
      setWorkflow({
        workflow_id: workflow.workflow_id,
        status: wfStatus as QualificationWorkflow['status'],
        report: (detail.snapshot?.report ?? workflow.report) as QualificationWorkflow['report'],
        review_items: (detail.snapshot?.review_items ?? workflow.review_items) as QualificationWorkflow['review_items'],
        decisions: (detail.snapshot?.decisions ?? workflow.decisions) as QualificationWorkflow['decisions'],
        warnings: (detail.snapshot?.warnings ?? workflow.warnings) as string[],
      });
      initReviewsFromGraph(detail);
      void res;
    } catch (e) {
      setApiError(getErrMsg(e));
    } finally {
      setLoading(false);
    }
  };

  const setReview = (requirementId: string, patch: Partial<ReviewDraft>) => {
    setReviews((prev) => ({
      ...prev,
      [requirementId]: { ...prev[requirementId], ...patch },
    }));
  };

  const activeReport = workflow ? workflow.report : report;

  return (
    <div className="page-fade-in" style={{ maxWidth: '1200px', margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '6px' }}>
        <ClipboardCheck size={26} color="#0f766e" />
        <h1 style={{ fontSize: '20px', fontWeight: 700, color: '#0f172a' }}>资格预审</h1>
      </div>
      <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginBottom: '18px' }}>
        招标要求—企业能力资格预审：确定性规则匹配 + 可暂停/人工确认（HITL）流程，纯本地规则，不调用 LLM。
      </p>

      {jsonError && (
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', background: '#fef2f2', border: '1px solid #fecaca', color: '#b91c1c', padding: '10px 14px', borderRadius: '8px', fontSize: '13px', marginBottom: '14px' }}>
          <AlertTriangle size={16} style={{ marginTop: '1px', flexShrink: 0 }} />
          <span>{jsonError}</span>
        </div>
      )}
      {apiError && (
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '8px', background: '#fef2f2', border: '1px solid #fecaca', color: '#b91c1c', padding: '10px 14px', borderRadius: '8px', fontSize: '13px', marginBottom: '14px' }}>
          <XCircle size={16} style={{ marginTop: '1px', flexShrink: 0 }} />
          <span>{apiError}</span>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '14px', marginBottom: '14px' }}>
        <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '14px' }}>
          <label style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)', display: 'block', marginBottom: '8px' }}>
            招标要求（requirements JSON）
          </label>
          <textarea
            value={requirementsText}
            onChange={(e) => setRequirementsText(e.target.value)}
            spellCheck={false}
            style={{
              width: '100%', height: '260px', padding: '10px', fontSize: '12px', fontFamily: 'monospace',
              background: '#f8fafc', border: '1px solid var(--color-border)', borderRadius: '8px', color: '#334155', resize: 'vertical', lineHeight: 1.5,
            }}
          />
        </div>
        <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '14px' }}>
          <label style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)', display: 'block', marginBottom: '8px' }}>
            企业能力证明材料（credentials JSON）
          </label>
          <textarea
            value={credentialsText}
            onChange={(e) => setCredentialsText(e.target.value)}
            spellCheck={false}
            style={{
              width: '100%', height: '260px', padding: '10px', fontSize: '12px', fontFamily: 'monospace',
              background: '#f8fafc', border: '1px solid var(--color-border)', borderRadius: '8px', color: '#334155', resize: 'vertical', lineHeight: 1.5,
            }}
          />
        </div>
      </div>

      <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '14px', marginBottom: '14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', marginBottom: '8px' }}>
          <label style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)', display: 'block' }}>
            从当前项目导入（项目级真实数据）
          </label>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={handleImportFromProject}
              disabled={loading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: 'var(--color-primary)', color: '#fff', border: 'none', fontSize: '12px', fontWeight: 600,
              }}
            >
              <FolderOpen size={14} />
              从当前项目分析导入
            </button>
            <button
              onClick={handleRunFromProject}
              disabled={loading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: '#0f766e', color: '#fff', border: 'none', fontSize: '12px', fontWeight: 600,
              }}
            >
              {loading && mode === 'workflow' ? <Loader2 size={14} className="animate-spin" /> : <ClipboardCheck size={14} />}
              从当前项目启动审批
            </button>
          </div>
        </div>
        <div style={{ fontSize: '12px', color: currentProjectId ? '#0f766e' : '#b45309' }}>
          {currentProjectId
            ? `当前项目：${currentProjectId}（读取该项目已完成的招标解读 Analysis.dimensions）`
            : '未选择项目：请先在页面顶部选择当前项目，再使用此功能。'}
        </div>
      </div>

      <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '14px', marginBottom: '14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', marginBottom: '10px' }}>
          <label style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)', display: 'block' }}>
            数据飞轮指标（运行 Trace + HITL 反馈回流）
          </label>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={loadMetrics}
              disabled={metricsLoading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: 'var(--color-surface)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)', fontSize: '12px',
              }}
            >
              <RefreshCw size={14} />
              刷新指标
            </button>
            <button
              onClick={handleExportFlywheel}
              disabled={metricsLoading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: 'var(--color-surface)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)', fontSize: '12px',
              }}
            >
              <Download size={14} />
              导出匿名评测数据
            </button>
            <button
              onClick={handleRunEval}
              disabled={evalLoading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: '#0f766e', color: '#fff', border: 'none', fontSize: '12px', fontWeight: 600,
              }}
            >
              {evalLoading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              运行基准评测
            </button>
          </div>
        </div>
        {metricsError && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: '#b91c1c', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: '6px', padding: '6px 10px', marginBottom: '8px' }}>
            <XCircle size={13} />
            {metricsError}
          </div>
        )}
        {metricsLoading && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--color-text-secondary)', marginBottom: '8px' }}>
            <Loader2 size={14} className="animate-spin" />
            加载中…
          </div>
        )}
        {!metricsLoading && !metricsError && metrics && (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '8px' }}>
            {[
              { label: '运行数', value: String(metrics.run_count) },
              { label: '自动完成率', value: `${(metrics.auto_complete_rate * 100).toFixed(1)}%` },
              { label: '人工介入率', value: `${(metrics.human_intervention_rate * 100).toFixed(1)}%` },
              { label: '信息不足率', value: `${(metrics.insufficient_rate * 100).toFixed(1)}%` },
              { label: '人工改判率', value: `${(metrics.human_override_rate * 100).toFixed(1)}%` },
            ].map((c) => (
              <div key={c.label} style={{ background: '#f8fafc', border: '1px solid var(--color-border)', borderRadius: '8px', padding: '10px', textAlign: 'center' }}>
                <div style={{ fontSize: '18px', fontWeight: 700, color: '#0f172a' }}>{c.value}</div>
                <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>{c.label}</div>
              </div>
            ))}
          </div>
        )}
        {!metricsLoading && !metricsError && !metrics && (
          <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>
            暂无数据：点击「刷新指标」查看运行 Trace 与人工审批指标（数据仅收集聚合字段，不包含企业材料原文）。
          </div>
        )}

        {evalError && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: '#b91c1c', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: '6px', padding: '6px 10px', marginTop: '10px' }}>
            <XCircle size={13} />
            {evalError}
          </div>
        )}
        {evalReport && (
          <div style={{ marginTop: '12px', border: '1px solid var(--color-border)', borderRadius: '8px', padding: '10px 12px', background: '#f8fafc' }}>
            <div style={{ fontSize: '12px', fontWeight: 600, color: '#0f172a', marginBottom: '8px' }}>
              离线基准评测：{evalReport.dataset_name} v{evalReport.dataset_version}（{evalReport.case_count} cases）
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '8px' }}>
              {[
                { label: '案例数', value: String(evalReport.case_count) },
                { label: '要求级准确率', value: `${(evalReport.requirement_accuracy * 100).toFixed(1)}%` },
                { label: '整体准确率', value: `${(evalReport.overall_accuracy * 100).toFixed(1)}%` },
                { label: '证据不变量违规', value: String(evalReport.evidence_invariant_violations.length) },
                { label: '失败案例', value: String(evalReport.failed_cases.length) },
              ].map((c) => (
                <div key={c.label} style={{ background: '#fff', border: '1px solid var(--color-border)', borderRadius: '6px', padding: '8px', textAlign: 'center' }}>
                  <div style={{ fontSize: '16px', fontWeight: 700, color: '#0f172a' }}>{c.value}</div>
                  <div style={{ fontSize: '10px', color: 'var(--color-text-secondary)' }}>{c.label}</div>
                </div>
              ))}
            </div>
            {(evalReport.failed_cases.length > 0 || Object.keys(evalReport.by_requirement_type).length > 0) && (
              <details style={{ marginTop: '8px', fontSize: '12px' }}>
                <summary style={{ cursor: 'pointer', color: 'var(--color-text-secondary)' }}>
                  展开：按类型准确率 / 失败案例 ID
                </summary>
                <div style={{ marginTop: '6px', display: 'grid', gap: '4px' }}>
                  {Object.entries(evalReport.by_requirement_type).map(([rt, v]) => (
                    <div key={rt} style={{ fontSize: '11px', color: '#475569' }}>
                      {rt}：{v.correct}/{v.total}（{(v.accuracy * 100).toFixed(1)}%）
                    </div>
                  ))}
                  {evalReport.failed_cases.length > 0 && (
                    <div style={{ marginTop: '4px', fontSize: '11px', color: '#b45309' }}>
                      失败案例：{evalReport.failed_cases.map((f) => `${f.case_id}(${f.requirement_id}: ${f.expected}→${f.actual})`).join('，')}
                    </div>
                  )}
                </div>
              </details>
            )}
          </div>
        )}
      </div>

      <details style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '14px', marginBottom: '14px' }}>
        <summary style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)', cursor: 'pointer' }}>
          企业材料候选抽取（候选 + 人工确认，非自动认证）
        </summary>
        <div style={{ marginTop: '12px' }}>
          <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
            <button
              onClick={handleCredFromText}
              disabled={credLoading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: 'var(--color-primary)', color: '#fff', border: 'none', fontSize: '12px', fontWeight: 600,
              }}
            >
              {credLoading ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              从文本抽取
            </button>
            <button
              onClick={handleCredFromProject}
              disabled={credLoading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: '#0f766e', color: '#fff', border: 'none', fontSize: '12px', fontWeight: 600,
              }}
            >
              <FolderOpen size={14} />
              从当前项目材料抽取
            </button>
          </div>
          <div style={{ fontSize: '12px', color: currentProjectId ? '#0f766e' : '#b45309', marginBottom: '8px' }}>
            {currentProjectId
              ? `当前项目：${currentProjectId}（读取该项目 bid/reference 文档的解析正文）`
              : '未选择项目：需先选择当前项目才能从项目材料抽取。'}
          </div>
          <textarea
            value={credText}
            onChange={(e) => setCredText(e.target.value)}
            spellCheck={false}
            placeholder="粘贴企业材料文本（证书/营业执照/业绩/人员/注册地址等）…"
            style={{
              width: '100%', height: '90px', padding: '10px', fontSize: '12px', fontFamily: 'monospace',
              background: '#f8fafc', border: '1px solid var(--color-border)', borderRadius: '8px', color: '#334155', resize: 'vertical', lineHeight: 1.5,
            }}
          />
          {credError && (
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '6px', fontSize: '12px', color: '#b91c1c', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: '6px', padding: '6px 10px', marginTop: '8px' }}>
              <XCircle size={13} style={{ flexShrink: 0, marginTop: '1px' }} />
              <span>{credError}</span>
            </div>
          )}
          {credLoading && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: '8px' }}>
              <Loader2 size={14} className="animate-spin" />
              抽取中…
            </div>
          )}
          {credResult && (
            <div style={{ marginTop: '10px', display: 'grid', gap: '8px' }}>
              {credResult.warnings.map((w, i) => (
                <div key={`cw-${i}`} style={{ display: 'flex', alignItems: 'flex-start', gap: '6px', fontSize: '12px', color: '#b45309', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: '6px', padding: '6px 10px' }}>
                  <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: '1px' }} />
                  <span>{w}</span>
                </div>
              ))}
              {credResult.unresolved_items.length > 0 && (
                <div style={{ fontSize: '11px', color: '#78350f', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: '6px', padding: '6px 8px' }}>
                  未解析 {credResult.unresolved_items.length} 项：{credResult.unresolved_items.map((u) => u.reason).join('；')}
                </div>
              )}
              {credResult.candidates.length === 0 ? (
                <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>未抽取到候选（可查看上方 unresolved / warnings）。</div>
              ) : (
                credResult.candidates.map((c) => (
                  <div key={c.candidate_id} style={{ border: '1px solid var(--color-border)', borderRadius: '8px', padding: '10px', background: '#fff' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap', marginBottom: '4px' }}>
                      <span style={{ fontSize: '11px', fontWeight: 700, padding: '1px 8px', borderRadius: '999px', color: '#1a56db', background: '#eff6ff', border: '1px solid #bfdbfe' }}>{c.credential_type}</span>
                      <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>置信度：{c.confidence_level}（规则级，需人工确认）</span>
                      {confirmedIds.includes(c.candidate_id) && (
                        <span style={{ fontSize: '11px', fontWeight: 700, color: '#059669', background: '#ecfdf5', border: '1px solid #a7f3d0', borderRadius: '999px', padding: '1px 8px' }}>已确认并追加</span>
                      )}
                    </div>
                    <div style={{ fontSize: '12px', color: '#334155' }}>{candidateFields(c)}</div>
                    {c.source_excerpt && (
                      <div style={{ fontSize: '11px', fontFamily: 'monospace', color: '#64748b', marginTop: '4px' }}>原文：{c.source_excerpt}</div>
                    )}
                    {c.warnings.map((w, i) => (
                      <div key={`cwarn-${i}`} style={{ fontSize: '11px', color: '#b45309', marginTop: '2px' }}>⚠ {w}</div>
                    ))}
                    <div style={{ display: 'flex', gap: '6px', marginTop: '8px' }}>
                      <input
                        value={confirmRefs[c.candidate_id] || ''}
                        onChange={(e) => setConfirmRefs((prev) => ({ ...prev, [c.candidate_id]: e.target.value }))}
                        placeholder="evidence_ref，如 document:doc123#p3 或 manual:标签"
                        style={{
                          flex: 1, padding: '7px 8px', fontSize: '12px', borderRadius: '6px',
                          border: '1px solid var(--color-border)', background: '#fff', fontFamily: 'monospace',
                        }}
                      />
                      <button
                        onClick={() => handleConfirmCandidate(c)}
                        disabled={credLoading || confirmingId === c.candidate_id}
                        style={{
                          display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 12px', borderRadius: '6px', cursor: 'pointer',
                          background: '#059669', color: '#fff', border: 'none', fontSize: '12px', fontWeight: 600, whiteSpace: 'nowrap',
                        }}
                      >
                        {confirmingId === c.candidate_id ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle2 size={13} />}
                        确认并追加
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </details>

      <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '14px', marginBottom: '14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px', marginBottom: '8px' }}>
          <label style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)', display: 'block' }}>
            招标解读分析结果（dimensions JSON）— 一键导入为资格要求
          </label>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={handleImportAnalysis}
              disabled={loading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: 'var(--color-primary)', color: '#fff', border: 'none', fontSize: '12px', fontWeight: 600,
              }}
            >
              <Download size={14} />
              导入分析结果
            </button>
            <button
              onClick={handleRunFromAnalysis}
              disabled={loading}
              style={{
                display: 'flex', alignItems: 'center', gap: '5px', padding: '7px 14px', borderRadius: '8px', cursor: 'pointer',
                background: '#0f766e', color: '#fff', border: 'none', fontSize: '12px', fontWeight: 600,
              }}
            >
              {loading && mode === 'workflow' ? <Loader2 size={14} className="animate-spin" /> : <ClipboardCheck size={14} />}
              从分析启动审批
            </button>
          </div>
        </div>
        <textarea
          value={analysisText}
          onChange={(e) => setAnalysisText(e.target.value)}
          spellCheck={false}
          style={{
            width: '100%', height: '140px', padding: '10px', fontSize: '12px', fontFamily: 'monospace',
            background: '#f8fafc', border: '1px solid var(--color-border)', borderRadius: '8px', color: '#334155', resize: 'vertical', lineHeight: 1.5,
          }}
        />
        {adapter && (
          <div style={{ marginTop: '10px', border: '1px solid var(--color-border)', borderRadius: '8px', padding: '10px 12px', background: '#f8fafc' }}>
            <div style={{ fontSize: '12px', fontWeight: 600, color: '#0f172a', marginBottom: '6px' }}>
              适配结果：已生成 {adapter.requirements.length} 条资格要求（已自动填充到上方 requirements 编辑区）
            </div>
            {adapter.warnings.map((w, i) => (
              <div key={`aw-${i}`} style={{ display: 'flex', alignItems: 'flex-start', gap: '6px', fontSize: '12px', color: '#b45309', marginBottom: '3px' }}>
                <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: '1px' }} />
                <span>{w}</span>
              </div>
            ))}
            {adapter.unresolved_items.length > 0 && (
              <div style={{ marginTop: '6px' }}>
                <div style={{ fontSize: '12px', fontWeight: 600, color: '#92400e', marginBottom: '4px' }}>
                  未解析条目（{adapter.unresolved_items.length}）— 模糊文本未被强行判定，请人工处理
                </div>
                {adapter.unresolved_items.map((item, i) => (
                  <div key={`ui-${i}`} style={{ fontSize: '11px', color: '#78350f', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: '6px', padding: '6px 8px', marginBottom: '4px' }}>
                    <div><b>{item.source_field}</b>：{item.reason}</div>
                    <div style={{ fontFamily: 'monospace', marginTop: '2px', color: '#92400e' }}>{item.source_text}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div style={{ display: 'flex', gap: '10px', marginBottom: '18px', alignItems: 'center' }}>
        <button
          onClick={handleMatch}
          disabled={loading}
          style={{
            display: 'flex', alignItems: 'center', gap: '6px', padding: '9px 18px', borderRadius: '8px', cursor: 'pointer',
            background: 'var(--color-primary)', color: '#fff', border: 'none', fontSize: '13px', fontWeight: 600,
          }}
        >
          {loading && mode === 'match' ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
          立即匹配
        </button>
        <button
          onClick={handleRunWorkflow}
          disabled={loading}
          style={{
            display: 'flex', alignItems: 'center', gap: '6px', padding: '9px 18px', borderRadius: '8px', cursor: 'pointer',
            background: '#0f766e', color: '#fff', border: 'none', fontSize: '13px', fontWeight: 600,
          }}
        >
          {loading && mode === 'workflow' ? <Loader2 size={15} className="animate-spin" /> : <ClipboardCheck size={15} />}
          启动资格图
        </button>
        <button
          onClick={resetDemo}
          disabled={loading}
          style={{
            display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 14px', borderRadius: '8px', cursor: 'pointer',
            background: 'var(--color-surface)', color: 'var(--color-text-secondary)', border: '1px solid var(--color-border)', fontSize: '12px',
          }}
        >
          <RotateCcw size={13} />
          重置示例
        </button>
      </div>

      {loading && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--color-text-secondary)', fontSize: '13px', marginBottom: '14px' }}>
          <Loader2 size={16} className="animate-spin" />
          处理中…
        </div>
      )}

      {mode === 'idle' && !loading && (
        <div style={{ textAlign: 'center', padding: '40px 20px', background: 'var(--color-surface)', border: '1px dashed var(--color-border)', borderRadius: '12px', color: 'var(--color-text-secondary)', fontSize: '13px' }}>
          <FileText size={28} style={{ margin: '0 auto 8px', color: '#94a3b8' }} />
          填写左侧 requirements 与 credentials JSON，然后点击「立即匹配」或「启动资格图」。
        </div>
      )}

      {activeReport && mode !== 'idle' && (
        <div style={{ display: 'grid', gap: '14px' }}>
          <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '16px' }}>
            {mode === 'workflow' && graphRunDetail && (
              <RunStatusStrip detail={graphRunDetail} loading={graphPolling} error={null} title="资格预审图运行状态（提取→比对→HITL 审批门）" />
            )}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '10px', marginBottom: '14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--color-text)' }}>
                  {mode === 'workflow' ? '图运行结果' : '匹配结论'}
                </span>
                <span style={{ fontSize: '12px', fontWeight: 700, padding: '3px 10px', borderRadius: '999px', color: STATUS_META[activeReport.overall_status]?.color || '#475569', background: STATUS_META[activeReport.overall_status]?.bg || '#f1f5f9' }}>
                  整体：{STATUS_META[activeReport.overall_status]?.label || activeReport.overall_status}
                </span>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '10px', marginBottom: '16px' }}>
              {[
                { label: '总要求', value: activeReport.summary.total, color: '#475569', bg: '#f8fafc' },
                { label: '满足', value: activeReport.summary.met, color: '#059669', bg: '#ecfdf5' },
                { label: '不满足', value: activeReport.summary.unmet, color: '#dc2626', bg: '#fef2f2' },
                { label: '信息不足', value: activeReport.summary.insufficient, color: '#d97706', bg: '#fffbeb' },
              ].map((c) => (
                <div key={c.label} style={{ padding: '12px', background: c.bg, borderRadius: '8px', textAlign: 'center' }}>
                  <div style={{ fontSize: '24px', fontWeight: 700, color: c.color }}>{c.value}</div>
                  <div style={{ fontSize: '11px', color: c.color }}>{c.label}</div>
                </div>
              ))}
            </div>

            {activeReport.warnings.length > 0 && (
              <div style={{ marginBottom: '12px' }}>
                {activeReport.warnings.map((w, i) => (
                  <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: '6px', fontSize: '12px', color: '#b45309', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: '6px', padding: '6px 10px', marginBottom: '4px' }}>
                    <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: '1px' }} />
                    <span>{w}</span>
                  </div>
                ))}
              </div>
            )}

            {activeReport.results.length === 0 ? (
              <div style={{ textAlign: 'center', padding: '24px', color: 'var(--color-text-secondary)', fontSize: '13px' }}>无匹配结果</div>
            ) : (
              <div style={{ display: 'grid', gap: '10px' }}>
                {activeReport.results.map((r) => {
                  const meta = STATUS_META[r.status] || { label: r.status, color: '#475569', bg: '#f1f5f9' };
                  return (
                    <div key={r.requirement_id} style={{ border: '1px solid var(--color-border)', borderRadius: '10px', padding: '12px 14px', background: '#fff' }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', flexWrap: 'wrap' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          {r.status === 'met' ? <CheckCircle2 size={16} color="#059669" /> : r.status === 'unmet' ? <XCircle size={16} color="#dc2626" /> : <AlertTriangle size={16} color="#d97706" />}
                          <span style={{ fontSize: '13px', fontWeight: 600, color: '#0f172a' }}>
                            {r.requirement_id}
                            <span style={{ marginLeft: '8px', fontWeight: 400, color: 'var(--color-text-secondary)', fontSize: '11px' }}>{r.requirement_type}</span>
                          </span>
                        </div>
                        <span style={{ fontSize: '11px', fontWeight: 700, padding: '2px 8px', borderRadius: '999px', color: meta.color, background: meta.bg }}>{meta.label}</span>
                      </div>
                      <div style={{ fontSize: '12px', color: '#475569', marginTop: '6px', lineHeight: 1.6 }}>{r.reason}</div>
                      {r.evidence_refs.length > 0 && (
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '8px' }}>
                          {r.evidence_refs.map((ref, i) => (
                            <span key={i} style={{ fontSize: '11px', fontFamily: 'monospace', background: '#eff6ff', color: '#1a56db', border: '1px solid #bfdbfe', borderRadius: '4px', padding: '2px 6px' }}>{ref}</span>
                          ))}
                        </div>
                      )}
                      {r.warnings.length > 0 && (
                        <div style={{ marginTop: '6px', fontSize: '11px', color: '#b45309' }}>
                          {r.warnings.map((w, i) => (
                            <div key={i}>⚠ {w}</div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {mode === 'workflow' && workflow && workflow.review_items.length > 0 && workflow.status !== 'completed' && (
            <div style={{ background: 'var(--color-surface)', border: '1px solid #fcd34d', borderRadius: '12px', padding: '16px' }}>
              <div style={{ fontSize: '14px', fontWeight: 600, color: '#92400e', marginBottom: '4px' }}>人工确认（HITL）</div>
              <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginBottom: '12px' }}>
                规则引擎判定信息不足或存在警告，请逐条确认 / 否决 / 标记信息不足。审批只追加决策记录，不会改写原始证明材料。
              </div>
              <div style={{ display: 'grid', gap: '10px' }}>
                {workflow.review_items.map((item) => {
                  const draft = reviews[item.requirement_id] || { decision: '', reviewer: '', note: '' };
                  return (
                    <div key={item.requirement_id} style={{ border: '1px solid var(--color-border)', borderRadius: '10px', padding: '12px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
                        <AlertTriangle size={15} color="#d97706" />
                        <span style={{ fontSize: '13px', fontWeight: 600 }}>{item.requirement_id}</span>
                        <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>规则判定：{STATUS_META[item.status]?.label || item.status}</span>
                      </div>
                      <div style={{ fontSize: '12px', color: '#475569', marginBottom: '10px' }}>{item.reason}</div>
                      {item.evidence_refs.length > 0 && (
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '10px' }}>
                          {item.evidence_refs.map((ref, i) => (
                            <span key={i} style={{ fontSize: '11px', fontFamily: 'monospace', background: '#eff6ff', color: '#1a56db', border: '1px solid #bfdbfe', borderRadius: '4px', padding: '2px 6px' }}>{ref}</span>
                          ))}
                        </div>
                      )}
                      <div style={{ display: 'grid', gridTemplateColumns: '150px 1fr 1fr', gap: '8px' }}>
                        <select
                          value={draft.decision}
                          onChange={(e) => setReview(item.requirement_id, { decision: e.target.value as ReviewDecision })}
                          style={{ padding: '7px 8px', fontSize: '12px', borderRadius: '6px', border: '1px solid var(--color-border)', background: '#fff', color: 'var(--color-text)' }}
                        >
                          <option value="">选择决策…</option>
                          {(Object.keys(DECISION_LABELS) as ReviewDecision[]).map((d) => (
                            <option key={d} value={d}>{DECISION_LABELS[d]}</option>
                          ))}
                        </select>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                          <User size={13} color="#94a3b8" />
                          <input
                            value={draft.reviewer}
                            onChange={(e) => setReview(item.requirement_id, { reviewer: e.target.value })}
                            placeholder="审批人（可选）"
                            style={{ width: '100%', padding: '7px 8px', fontSize: '12px', borderRadius: '6px', border: '1px solid var(--color-border)', background: '#fff' }}
                          />
                        </div>
                        <input
                          value={draft.note}
                          onChange={(e) => setReview(item.requirement_id, { note: e.target.value })}
                          placeholder="审批备注（可选）"
                          style={{ width: '100%', padding: '7px 8px', fontSize: '12px', borderRadius: '6px', border: '1px solid var(--color-border)', background: '#fff' }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
              <button
                onClick={handleApprove}
                disabled={loading}
                style={{
                  display: 'flex', alignItems: 'center', gap: '6px', marginTop: '12px', padding: '9px 18px', borderRadius: '8px', cursor: 'pointer',
                  background: '#0f766e', color: '#fff', border: 'none', fontSize: '13px', fontWeight: 600,
                }}
              >
                {loading ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
                提交审批
              </button>
            </div>
          )}

          {mode === 'workflow' && workflow && workflow.decisions.length > 0 && (
            <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '16px' }}>
              <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--color-text)', marginBottom: '10px' }}>审批审计记录（decisions）</div>
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
                  <thead>
                    <tr style={{ color: 'var(--color-text-secondary)', textAlign: 'left' }}>
                      <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--color-border)' }}>要求ID</th>
                      <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--color-border)' }}>决策</th>
                      <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--color-border)' }}>审批人</th>
                      <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--color-border)' }}>备注</th>
                      <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--color-border)' }}>时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {workflow.decisions.map((d, i) => (
                      <tr key={i} style={{ color: 'var(--color-text)' }}>
                        <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9' }}>{d.requirement_id}</td>
                        <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9' }}>{DECISION_LABELS[d.decision] || d.decision}</td>
                        <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9' }}>{d.reviewer || '-'}</td>
                        <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9' }}>{d.note || '-'}</td>
                        <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9', fontFamily: 'monospace', fontSize: '11px' }}>{d.decided_at}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
