import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { ShieldCheck, Loader2, AlertTriangle, CheckCircle2, XCircle, Download, FileText, Upload, FolderOpen, Copy, FileInput, RefreshCw } from 'lucide-react';
import { checkApi, checkGraphApi, projectApi, type Project, type BizGraphRunDetail } from '../services/api';
import { useAppStore } from '../stores/appStore';
import StepHeader from '../components/common/StepHeader';
import RunStatusStrip from '../components/graph/RunStatusStrip';

type CheckType = 'fullCheck' | 'compliance' | 'disqualification' | 'qualification' | 'pricing' | 'fitScore' | 'selfcheck' | 'deposit' | 'signature' | 'validity' | 'consistency' | 'duplicate' | 'mandatoryReq' | 'docIntegrity' | 'aiTextCheck' | 'riskScore' | 'crossCheck' | 'sampleReport' | 'jointBid' | 'ebidSubmit' | 'pricingLogic';
type CheckMode = 'project' | 'upload';

interface CheckOption {
  key: CheckType;
  label: string;
  description: string;
  color: string;
}

// Worker I 任务1：缺料判定与后端 services/check/missing_materials.py 同口径
const MISSING_RE = /待补充|知识库无据|未提供|缺少|缺失|缺如|未响应|未见|无法核实|需补充/;

interface MissingFinding {
  check_id: string;
  check_name: string;
  detail: string;
  material_required?: boolean;
}

interface CheckSummarySnapshot {
  total: number;
  passed: number;
  failed: number;
  warning: number;
  missing: number;
}

export default function CheckPage() {
  const [checkMode, setCheckMode] = useState<CheckMode>('upload');
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string>('');
  const [activeCheck, setActiveCheck] = useState<CheckType>('fullCheck');
  const [reports, setReports] = useState<Array<{ id: string; type: string; risk_level: string; created_at: string }>>([]);

  const [bidFile, setBidFile] = useState<File | null>(null);
  const [tenderFile, setTenderFile] = useState<File | null>(null);
  const bidFileRef = useRef<HTMLInputElement>(null);
  const tenderFileRef = useRef<HTMLInputElement>(null);

  const { currentProjectId } = useAppStore();

  useEffect(() => {
    loadProjects();
  }, []);

  useEffect(() => {
    if (currentProjectId && !selectedProjectId) {
      setSelectedProjectId(currentProjectId);
    }
  }, [currentProjectId]);

  const loadProjects = async () => {
    try {
      const res = await projectApi.list();
      setProjects(res.data.projects || []);
    } catch (e) {
      console.error('加载项目列表失败', e);
    }
  };

  const checkOptions: CheckOption[] = [
    { key: 'fullCheck', label: '全面检查', description: '运行所有检查项', color: '#1a56db' },
    { key: 'compliance', label: '合规性检查', description: '逐条检查硬性要求响应', color: '#059669' },
    { key: 'disqualification', label: '废标项检查', description: '检查废标条款响应', color: '#dc2626' },
    { key: 'mandatoryReq', label: '★▲参数对照', description: '强制性参数逐条响应', color: '#dc2626' },
    { key: 'qualification', label: '资质核查', description: '证书有效期/名称/三证合一', color: '#d97706' },
    { key: 'deposit', label: '保证金核查', description: '金额/形式/到账/保函有效期', color: '#d97706' },
    { key: 'signature', label: '签章核查', description: '法人签字/公章/骑缝章/CA', color: '#d97706' },
    { key: 'validity', label: '有效期核查', description: '投标有效期/保函/资质/CA', color: '#475569' },
    { key: 'pricing', label: '报价核查', description: '限价/算术/大小写/安全区间', color: '#475569' },
    { key: 'consistency', label: '一致性校验', description: '跨章节数据一致性', color: '#0f766e' },
    { key: 'duplicate', label: '标书查重', description: '内部重复/模板痕迹检测', color: '#0f766e' },
    { key: 'docIntegrity', label: '文件完整性', description: '正副本/密封/页码/附件', color: '#be185d' },
    { key: 'fitScore', label: '贴合度评分', description: '内容贴合/针对性/通用套话', color: '#be185d' },
    { key: 'selfcheck', label: '废标自查', description: '20项自查清单', color: '#be185d' },
    { key: 'crossCheck', label: '交叉比对', description: '评分标准↔投标内容逐项对照', color: '#1a56db' },
    { key: 'aiTextCheck', label: 'AI文本检查', description: '拼写/标点/实体识别', color: '#059669' },
    { key: 'riskScore', label: '风险评分', description: '6维度加权综合风险评分', color: '#dc2626' },
    { key: 'sampleReport', label: '样品/检测报告', description: 'CMA/CNAS/检测项核查', color: '#d97706' },
    { key: 'jointBid', label: '联合投标协议', description: '联合体协议完整性核查', color: '#475569' },
    { key: 'ebidSubmit', label: '电子投标提交', description: 'OFD/PDF/CA签章核查', color: '#0f766e' },
    { key: 'pricingLogic', label: '报价逻辑闭环', description: '人天×单价/成本分配验证', color: '#be185d' },
  ];

  // G-5 T1：项目模式检查改走检查图运行（POST /check/{p}/graph + 轮询快照，响应形状与旧垫片一致）
  const [graphRunDetail, setGraphRunDetail] = useState<BizGraphRunDetail | null>(null);
  const [graphPolling, setGraphPolling] = useState(false);

  const CHECK_TYPE_TO_GRAPH_ID: Record<string, string> = {
    compliance: 'compliance_check',
    disqualification: 'disqualification_check',
    qualification: 'qualification_check',
    pricing: 'pricing_check',
    fitScore: 'fit_score',
    deposit: 'deposit_check',
    signature: 'signature_check',
    validity: 'validity_check',
    consistency: 'consistency_check',
    duplicate: 'duplicate_check',
    mandatoryReq: 'mandatory_req_check',
    docIntegrity: 'doc_integrity_check',
    aiTextCheck: 'ai_text_check',
    riskScore: 'risk_score',
    crossCheck: 'cross_check',
    sampleReport: 'sample_report_check',
    jointBid: 'joint_bid_check',
    ebidSubmit: 'ebid_submit_check',
    pricingLogic: 'pricing_logic_check',
  };
  const GRAPH_ID_TO_CAMEL: Record<string, string> = Object.fromEntries(
    Object.entries(CHECK_TYPE_TO_GRAPH_ID).map(([k, v]) => [v, k]),
  );

  const navigate = useNavigate();

  // Worker I 任务1：缺料→补料→复检引导闭环
  const [missingFindings, setMissingFindings] = useState<MissingFinding[]>([]);
  const [recheckBefore, setRecheckBefore] = useState<CheckSummarySnapshot | null>(null);
  const [recheckAfter, setRecheckAfter] = useState<CheckSummarySnapshot | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const currentSummaryRef = useRef<CheckSummarySnapshot | null>(null);

  // 从检查图快照提取：结果映射（旧响应形状）+ 全量摘要 + 缺料 findings
  const mapSnapshot = (run: BizGraphRunDetail, checkIds: string[] | null) => {
    const snap = (run.snapshot || {}) as Record<string, any>;
    let items = (snap.results as Array<Record<string, unknown>> | Record<string, Record<string, unknown>> | undefined)
      ?? ((snap.report as Record<string, unknown> | undefined)?.results as Record<string, Record<string, unknown>> | undefined)
      ?? {};
    if (Array.isArray(items)) {
      items = Object.fromEntries(items.map((r, i) => [String((r as any).check_id ?? i), r]));
    }
    const toShape = (item: Record<string, unknown>) => ({
      success: item.status !== 'error' && item.status !== 'skipped',
      data: (item.data as Record<string, unknown>) ?? {},
      error: item.status === 'error' ? ((item.reason as string) || (item.error as string)) : null,
      warnings: (item.warnings as string[] | undefined) ?? [],
    });

    let mapped: Record<string, unknown>;
    if (checkIds) {
      const first = Object.values(items)[0] ?? {};
      mapped = toShape(first as Record<string, unknown>) as unknown as Record<string, unknown>;
    } else {
      const byCamel = Object.fromEntries(
        Object.entries(items).map(([id, item]) => [GRAPH_ID_TO_CAMEL[id] ?? id, toShape(item as Record<string, unknown>)]),
      );
      const data = Object.fromEntries(
        Object.entries(byCamel).map(([k, v]) => [k, { success: v.success, data: v.data, error: v.error }]),
      );
      mapped = {
        success: run.status === 'completed',
        data,
        has_critical: Object.values(items).some(
          (item) => (item as Record<string, unknown>).status === 'fail' || (item as Record<string, unknown>).status === 'error',
        ),
      };
    }

    // 摘要 + 缺料：优先后端权威值（report.summary / report.missing_material_findings），回退前端扫描
    const report = (snap.report as Record<string, any>) || null;
    let missing: MissingFinding[] = [];
    if (Array.isArray(report?.missing_material_findings)) {
      // 后端已区分“企业材料缺口”和“当前阶段无需提交、后续执行”的流程事项。
      missing = (report.missing_material_findings as MissingFinding[])
        .filter((finding) => finding.material_required !== false);
    } else {
      for (const [checkId, item] of Object.entries(items)) {
        const it = item as Record<string, any>;
        if (!it || (it.status !== 'fail' && it.status !== 'warning')) continue;
        const refs = Array.isArray(it?.data?.checks) ? it.data.checks : [];
        for (const ref of refs) {
          const detail = String(ref?.detail || ref?.reason || '');
          if (detail && !detail.startsWith('缺输入') && MISSING_RE.test(detail)) {
            missing.push({ check_id: checkId, check_name: String(it.check_name || checkId), detail: detail.slice(0, 200) });
          }
        }
      }
    }

    const statuses = Object.values(items).map((it) => String((it as any)?.status || ''));
    const summary: CheckSummarySnapshot = {
      total: statuses.length,
      passed: statuses.filter((s) => s === 'pass').length,
      failed: statuses.filter((s) => s === 'fail' || s === 'error').length,
      warning: statuses.filter((s) => s === 'warning').length,
      missing: missing.length,
    };

    return { mapped, summary, missing };
  };

  // 复用：启动检查图 → 轮询 → 映射 → setResults（返回摘要与缺料供复检对比）
  const executeProjectCheck = async (
    checkKey: CheckType,
    opts?: { maxSeconds?: number },
  ): Promise<{ summary: CheckSummarySnapshot; missing: MissingFinding[] }> => {
    const isFull = checkKey === 'fullCheck';
    const checkIds = isFull ? null : [CHECK_TYPE_TO_GRAPH_ID[checkKey]].filter(Boolean);
    const started = await checkGraphApi.start(selectedProjectId, { check_ids: checkIds, formats: [] });
    const runId = started.data?.run_id;
    if (!runId) throw new Error('创建检查图运行失败');

    const maxPolls = opts?.maxSeconds ? Math.ceil(opts.maxSeconds / 3) : (isFull ? 500 : 100);
    let run: BizGraphRunDetail | null = null;
    for (let i = 0; i < maxPolls; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      try {
        const res = await checkGraphApi.get(selectedProjectId, runId);
        run = res.data;
        setGraphRunDetail(run);
        if (run && (run.status === 'completed' || run.status === 'failed')) break;
      } catch {
        // 轮询失败继续
      }
    }
    if (!run || run.status === 'failed') {
      throw new Error(run?.error || '检查图执行失败');
    }

    const { mapped, summary, missing } = mapSnapshot(run, checkIds);
    setResults(mapped as unknown as Record<string, unknown>);
    setMissingFindings(missing);
    currentSummaryRef.current = summary;
    return { summary, missing };
  };

  const handleProjectCheck = async () => {
    if (!selectedProjectId) return;
    setLoading(true);
    setError('');
    setResults(null);
    setGraphRunDetail(null);
    setMissingFindings([]);
    setRecheckBefore(null);
    setRecheckAfter(null);
    setGraphPolling(true);
    try {
      await executeProjectCheck(activeCheck);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '检查失败');
    } finally {
      setLoading(false);
      setGraphPolling(false);
      // 检查完成后刷新报告列表，避免新报告不出现
      if (selectedProjectId) loadReports();
    }
  };

  // Worker I 任务1："资料已补充，重新检查修复"——一键重跑全量检查（管线自带 repair 闭环）→ 展示前后对比
  const handleRecheckWithRepair = async () => {
    if (!selectedProjectId) return;
    const before = currentSummaryRef.current;
    setRechecking(true);
    setError('');
    setGraphRunDetail(null);
    setGraphPolling(true);
    try {
      const { summary: after } = await executeProjectCheck('fullCheck');
      setRecheckBefore(before);
      setRecheckAfter(after);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '复检失败');
    } finally {
      setRechecking(false);
      setGraphPolling(false);
      if (selectedProjectId) loadReports();
    }
  };

  // Worker I 任务1："上传补充资料"→ 跳转知识中心并预选企业私有库
  const handleGoUploadMaterial = () => {
    navigate('/knowledge', { state: { preselectType: 'enterprise', from: 'check-recheck', projectId: selectedProjectId } });
  };

  // Worker I 任务2：导出《需补充材料清单》（docx 表格）
  const handleExportMissingMaterials = async (format: 'docx' | 'markdown' = 'docx') => {
    if (!selectedProjectId) return;
    try {
      const res = await checkApi.exportMissingMaterials(selectedProjectId, format);
      const disposition: string = (res.headers?.['content-disposition'] as string) || '';
      const m = /filename\*=UTF-8''([^;]+)/.exec(disposition);
      const filename = m ? decodeURIComponent(m[1]) : `需补充材料清单.${format === 'markdown' ? 'md' : 'docx'}`;
      const contentType = String(
        res.headers?.['content-type']
          || (format === 'markdown' ? 'text/markdown' : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
      );
      const url = URL.createObjectURL(new Blob([res.data], { type: contentType }));
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error('导出需补充材料清单失败', e);
      setError(e instanceof Error ? e.message : '导出需补充材料清单失败');
    }
  };

  // Worker I 任务3：配图 DOCX 导出走 /generate 页（"含 AI 配图"开关），本页不重复挂按钮。

  const handleUploadCheck = async () => {
    if (!bidFile) {
      setError('请先上传投标文件');
      return;
    }
    setLoading(true);
    setError('');
    setResults(null);
    try {
      const res = await checkApi.uploadCheck(bidFile, tenderFile, activeCheck);
      setResults(res.data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '检查失败');
    } finally {
      setLoading(false);
    }
  };

  const handleCheck = () => {
    if (checkMode === 'upload') {
      handleUploadCheck();
    } else {
      handleProjectCheck();
    }
  };

  const getRiskColor = (level: string) => {
    switch (level) {
      case 'high': return '#dc2626';
      case 'medium': return '#d97706';
      case 'low': return '#059669';
      default: return '#6b7280';
    }
  };

  const loadReports = async () => {
    if (!selectedProjectId) return;
    try {
      const res = await checkApi.listReports(selectedProjectId);
      setReports(res.data.reports || []);
    } catch (e) {
      console.error('加载报告列表失败', e);
    }
  };

  const handleExportReport = async (reportId: string, format: string = 'markdown') => {
    if (!selectedProjectId) return;
    try {
      const res = await checkApi.exportReport(selectedProjectId, reportId, format);
      // P5：按响应 content-type / 扩展名设置 MIME，不再恒为 text/markdown
      const contentType = (res.headers?.['content-type'] as string | undefined) || '';
      const mime =
        contentType && !contentType.includes('text/plain')
          ? contentType
          : format === 'html'
            ? 'text/html'
            : format === 'json'
              ? 'application/json'
              : 'text/markdown';
      const blob = new Blob([res.data as unknown as BlobPart], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `check_report_${reportId.slice(0, 8)}.${format === 'markdown' ? 'md' : format === 'html' ? 'html' : 'json'}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      console.error('导出报告失败', e);
    }
  };

  useEffect(() => {
    if (selectedProjectId && checkMode === 'project') loadReports();
  }, [selectedProjectId, checkMode]);

  const renderUploadZone = (
    label: string,
    file: File | null,
    setFileFn: (f: File | null) => void,
    ref: React.RefObject<HTMLInputElement | null>,
    accept: string,
    required: boolean,
  ) => (
    <div>
      <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>
        {label} {required && <span style={{ color: '#dc2626' }}>*</span>}
      </label>
      <div
        onClick={() => ref.current?.click()}
        style={{
          border: `2px dashed ${file ? '#059669' : 'var(--color-border)'}`,
          borderRadius: '10px',
          padding: '20px',
          textAlign: 'center',
          cursor: 'pointer',
          background: file ? '#ecfdf5' : '#f8fafc',
          transition: 'all 0.2s',
        }}
      >
        <Upload size={24} color={file ? '#059669' : '#94a3b8'} style={{ margin: '0 auto 8px' }} />
        <div style={{ fontSize: '13px', fontWeight: 500 }}>
          {file ? file.name : '点击上传文件'}
        </div>
        {file && (
          <div style={{ fontSize: '11px', color: '#059669', marginTop: '4px' }}>
            {(file.size / 1024).toFixed(1)} KB
          </div>
        )}
        {!file && (
          <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
            支持 .docx .pdf .txt .md 格式
          </div>
        )}
      </div>
      <input
        ref={ref}
        type="file"
        accept={accept}
        onChange={(e) => {
          if (e.target.files && e.target.files[0]) {
            setFileFn(e.target.files[0]);
          }
        }}
        style={{ display: 'none' }}
      />
      {file && (
        <button
          onClick={(e) => { e.stopPropagation(); setFileFn(null); }}
          style={{ marginTop: '4px', background: 'none', border: 'none', cursor: 'pointer', fontSize: '12px', color: '#dc2626' }}
        >
          移除文件
        </button>
      )}
    </div>
  );

  // 后端返回信封结构 {success, data, error, warnings}，渲染前解包取内层 data
  const unwrapEnvelope = (x: unknown): Record<string, unknown> => {
    if (x && typeof x === 'object' && typeof (x as Record<string, unknown>).success === 'boolean') {
      const inner = (x as Record<string, unknown>).data;
      if (inner && typeof inner === 'object') return inner as Record<string, unknown>;
    }
    return (x || {}) as Record<string, unknown>;
  };

  const renderFullCheckSummary = (raw: Record<string, unknown>) => {
    const data = unwrapEnvelope(raw);
    const checks = Object.entries(data);
    let passCount = 0;
    let failCount = 0;
    let errorCount = 0;

    const checkItems = checks.map(([key, val]) => {
      const v = val as Record<string, unknown>;
      const d = (v.data || {}) as Record<string, unknown>;
      const success = v.success as boolean;
      const riskLevel = d.risk_level as string || 'low';
      const hasCritical = d.has_critical_issues as boolean || false;
      const isHigh = riskLevel === 'high' || hasCritical;

      if (!success) errorCount++;
      else if (isHigh) failCount++;
      else passCount++;

      // full-check 后端返回的键为 snake_case（如 fit_score），映射回前端 checkOptions 的 key
      const label = checkOptions.find(o => o.key === key || o.key === key.replace('_score', 'Score'))?.label || key;

      return { key, label, success, riskLevel, hasCritical, isHigh, data: d };
    });

    return (
      <div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px', marginBottom: '16px' }}>
          <div style={{ padding: '12px', background: '#ecfdf5', borderRadius: '8px', textAlign: 'center' }}>
            <div style={{ fontSize: '24px', fontWeight: 700, color: '#059669' }}>{passCount}</div>
            <div style={{ fontSize: '11px', color: '#059669' }}>通过</div>
          </div>
          <div style={{ padding: '12px', background: '#fef2f2', borderRadius: '8px', textAlign: 'center' }}>
            <div style={{ fontSize: '24px', fontWeight: 700, color: '#dc2626' }}>{failCount}</div>
            <div style={{ fontSize: '11px', color: '#dc2626' }}>存在问题</div>
          </div>
          <div style={{ padding: '12px', background: '#fffbeb', borderRadius: '8px', textAlign: 'center' }}>
            <div style={{ fontSize: '24px', fontWeight: 700, color: '#d97706' }}>{errorCount}</div>
            <div style={{ fontSize: '11px', color: '#d97706' }}>执行异常</div>
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {checkItems.map(item => (
            <div
              key={item.key}
              style={{
                padding: '10px 14px',
                border: '1px solid var(--color-border)',
                borderRadius: '8px',
                borderLeft: item.isHigh ? '3px solid #dc2626' : item.success ? '3px solid #059669' : '3px solid #d97706',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                {item.success ? (
                  item.isHigh ? <XCircle size={16} color="#dc2626" /> : <CheckCircle2 size={16} color="#059669" />
                ) : (
                  <AlertTriangle size={16} color="#d97706" />
                )}
                <span style={{ fontSize: '13px', fontWeight: 500 }}>{item.label}</span>
              </div>
              <span style={{
                fontSize: '12px',
                padding: '2px 8px',
                borderRadius: '10px',
                background: item.isHigh ? '#fef2f2' : item.success ? '#ecfdf5' : '#fffbeb',
                color: item.isHigh ? '#dc2626' : item.success ? '#059669' : '#d97706',
              }}>
                {item.isHigh ? '高风险' : item.success ? '通过' : '异常'}
              </span>
            </div>
          ))}
        </div>
      </div>
    );
  };

  const renderSingleCheckResult = (raw: Record<string, unknown>) => {
    const data = unwrapEnvelope(raw);
    const hasCritical = data.has_critical_issues as boolean
      || data.disqualification_risk as boolean
      || false;
    const selfCheckNotPassed = data.all_passed === false || data.can_submit === false;
    // 嵌套 items 中任一子项 risk_level=high 或缺失响应（如废标项检查输出）时，视为高风险
    const nestedItems = Array.isArray(data.items) ? (data.items as Array<Record<string, unknown>>) : [];
    const nestedHigh = nestedItems.some(it => it?.risk_level === 'high' || it?.response_status === 'missing');
    const riskLevel = (data.risk_level as string)
      || (data.overall_risk as string)
      || (hasCritical || nestedHigh ? 'high' : selfCheckNotPassed ? 'medium' : 'low');

    return (
      <div>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '12px',
          borderRadius: '8px',
          marginBottom: '12px',
          background: `${getRiskColor(riskLevel)}15`,
        }}>
          {riskLevel === 'high' || hasCritical ? (
            <XCircle size={20} color="#dc2626" />
          ) : (
            <CheckCircle2 size={20} color="#059669" />
          )}
          <span style={{ fontWeight: 600, color: getRiskColor(riskLevel) }}>
            风险等级：{riskLevel === 'high' ? '高风险' : riskLevel === 'medium' ? '中风险' : '低风险'}
          </span>
        </div>
        <div style={{ position: 'relative' }}>
          <button
            onClick={async () => {
              try { await navigator.clipboard.writeText(JSON.stringify(data, null, 2)); } catch { /* fallback */ }
              const btn = document.getElementById('check-copy-btn');
              if (btn) { btn.textContent = '已复制'; setTimeout(() => { btn.textContent = '复制'; }, 2000); }
            }}
            id="check-copy-btn"
            style={{ position: 'absolute', top: '8px', right: '8px', zIndex: 1, padding: '4px 8px', background: 'white', border: '1px solid var(--color-border)', borderRadius: '4px', cursor: 'pointer', fontSize: '11px', display: 'flex', alignItems: 'center', gap: '4px', color: '#64748b' }}
          >
            <Copy size={12} /> 复制
          </button>
          <pre style={{ background: '#f8fafc', padding: '16px', borderRadius: '8px', fontSize: '12px', overflow: 'auto', maxHeight: '500px', paddingRight: '60px' }}>
            {JSON.stringify(data, null, 2)}
          </pre>
        </div>
      </div>
    );
  };

  return (
    <div className="page-fade-in">
      <StepHeader
        step={3}
        title="投标检查"
        subtitle="上传已有标书直接检查，或从项目中检查，21项全面审核"
        color="#d97706"
        nextPath="/format"
        nextLabel="下一步：文档输出"
      />

      <div style={{ display: 'flex', gap: '8px', marginBottom: '20px' }}>
        {([
          { key: 'upload' as CheckMode, label: '上传标书检查', icon: <Upload size={14} /> },
          { key: 'project' as CheckMode, label: '项目检查', icon: <FolderOpen size={14} /> },
        ]).map(tab => (
          <button
            key={tab.key}
            onClick={() => { setCheckMode(tab.key); setResults(null); setError(''); }}
            style={{
              padding: '8px 16px',
              background: checkMode === tab.key ? 'var(--color-primary)' : 'var(--color-surface)',
              color: checkMode === tab.key ? 'white' : 'var(--color-text)',
              border: `1px solid ${checkMode === tab.key ? 'var(--color-primary)' : 'var(--color-border)'}`,
              borderRadius: '8px',
              cursor: 'pointer',
              fontSize: '13px',
              fontWeight: 500,
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            {tab.icon} {tab.label}
          </button>
        ))}
      </div>

      {checkMode === 'upload' && (
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)', marginBottom: '20px' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>上传标书文件</h3>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px' }}>
            {renderUploadZone('投标文件（必填）', bidFile, setBidFile, bidFileRef, '.docx,.pdf,.txt,.md', true)}
            {renderUploadZone('招标文件（可选）', tenderFile, setTenderFile, tenderFileRef, '.docx,.pdf,.txt,.md', false)}
          </div>
          <div style={{ marginTop: '12px', padding: '10px', background: '#f0f9ff', borderRadius: '6px', fontSize: '12px', color: '#1e40af' }}>
            💡 上传招标文件后可进行合规性检查、废标项检查、★▲参数对照等需要对照招标文件的检查项。仅上传投标文件时，可进行标书查重、AI文本检查、报价核查等。
          </div>
        </div>
      )}

      {checkMode === 'project' && (
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)', marginBottom: '20px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <h3 style={{ fontSize: '14px', fontWeight: 600 }}>选择项目</h3>
            {/* Worker I 任务2：项目选中即可导出《需补充材料清单》（不依赖本次检查结果） */}
            <button
              onClick={() => handleExportMissingMaterials('docx')}
              disabled={!selectedProjectId}
              title="从最新检查报告+章节【待补充】标注确定性提取（无 LLM）"
              style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '6px 12px', background: selectedProjectId ? '#d97706' : '#e5e7eb', color: 'white', border: 'none', borderRadius: '8px', cursor: selectedProjectId ? 'pointer' : 'not-allowed', fontSize: '12px' }}
            >
              <FileInput size={12} /> 导出需补充材料清单
            </button>
          </div>
          <select
            value={selectedProjectId}
            onChange={(e) => setSelectedProjectId(e.target.value)}
            style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px' }}
          >
            <option value="">请选择项目</option>
            {projects.map(p => (
              <option key={p.id} value={p.id}>{p.name} ({p.status})</option>
            ))}
          </select>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '10px', marginBottom: '20px' }}>
        {checkOptions.map(opt => {
          const needsTender = ['compliance', 'disqualification', 'mandatoryReq', 'crossCheck', 'fitScore'].includes(opt.key);
          const disabled = checkMode === 'upload' && needsTender && !tenderFile;

          return (
            <div
              key={opt.key}
              onClick={() => { if (!disabled) setActiveCheck(opt.key); }}
              style={{
                padding: '12px',
                borderRadius: '8px',
                border: `2px solid ${activeCheck === opt.key ? opt.color : 'var(--color-border)'}`,
                background: activeCheck === opt.key ? `${opt.color}10` : disabled ? '#f9fafb' : 'var(--color-surface)',
                cursor: disabled ? 'not-allowed' : 'pointer',
                transition: 'all 0.15s',
                opacity: disabled ? 0.5 : 1,
              }}
            >
              <div style={{ fontSize: '13px', fontWeight: 600, color: activeCheck === opt.key ? opt.color : disabled ? '#9ca3af' : 'var(--color-text)' }}>
                {opt.label}
              </div>
              <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
                {opt.description}
              </div>
              {disabled && (
                <div style={{ fontSize: '10px', color: '#d97706', marginTop: '2px' }}>需上传招标文件</div>
              )}
            </div>
          );
        })}
      </div>

      <button
        onClick={handleCheck}
        disabled={loading || (checkMode === 'project' && !selectedProjectId) || (checkMode === 'upload' && !bidFile)}
        style={{
          width: '100%',
          padding: '12px',
          background: checkOptions.find(o => o.key === activeCheck)?.color || 'var(--color-primary)',
          color: 'white',
          border: 'none',
          borderRadius: '8px',
          cursor: loading || (checkMode === 'project' && !selectedProjectId) || (checkMode === 'upload' && !bidFile) ? 'not-allowed' : 'pointer',
          fontSize: '14px',
          fontWeight: 600,
          opacity: (checkMode === 'project' && !selectedProjectId) || (checkMode === 'upload' && !bidFile) ? 0.5 : 1,
          marginBottom: '20px',
        }}
      >
        {loading ? '检查中...' : `运行${checkOptions.find(o => o.key === activeCheck)?.label || '检查'}`}
      </button>

      {graphRunDetail && (
        <RunStatusStrip detail={graphRunDetail} loading={graphPolling} error={null} title="检查图运行状态（G-3 检查图）" />
      )}

      {results && (
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '16px', fontWeight: 600 }}>
              检查结果
              {(results as Record<string, unknown>).source === 'upload' && (
                <span style={{ fontSize: '12px', fontWeight: 400, color: 'var(--color-text-secondary)', marginLeft: '8px' }}>
                  来源：上传文件
                </span>
              )}
            </h3>
          </div>

          {activeCheck === 'fullCheck' && typeof results === 'object' && results !== null && 'data' in results
            ? renderFullCheckSummary((results as Record<string, unknown>).data as Record<string, unknown>)
            : renderSingleCheckResult(results as Record<string, unknown>)}
        </div>
      )}

      {checkMode === 'project' && !loading && !rechecking && results && missingFindings.length > 0 && (
        <div style={{ marginTop: '16px', background: 'var(--color-surface)', borderRadius: '12px', padding: '20px 24px', border: '1px solid #fbbf24', borderLeft: '4px solid #d97706' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
            <AlertTriangle size={16} color="#d97706" />
            <span style={{ fontSize: '14px', fontWeight: 600, color: '#d97706' }}>
              发现 {missingFindings.length} 处事实型缺料（正文重写无法根治，需线下补料）
            </span>
          </div>
          <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginBottom: '10px' }}>
            典型项：{missingFindings.slice(0, 3).map((f, i) => (
              <span key={i} style={{ display: 'inline-block', marginRight: '8px' }}>
                [{f.check_name}] {f.detail.slice(0, 40)}
              </span>
            ))}
            {missingFindings.length > 3 && <span>等</span>}
          </div>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            <button
              onClick={handleGoUploadMaterial}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 14px', background: '#1a56db', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              <Upload size={13} /> 上传补充资料（企业私有库）
            </button>
            <button
              onClick={handleRecheckWithRepair}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 14px', background: '#059669', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              <RefreshCw size={13} /> 资料已补充，重新检查修复
            </button>
            <button
              onClick={() => handleExportMissingMaterials('docx')}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 14px', background: '#d97706', color: 'white', border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              <FileInput size={13} /> 导出需补充材料清单
            </button>
            <button
              onClick={() => handleExportMissingMaterials('markdown')}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 14px', background: 'var(--color-surface)', color: 'var(--color-text)', border: '1px solid var(--color-border)', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              <FileText size={13} /> Markdown
            </button>
          </div>
        </div>
      )}

      {checkMode === 'project' && !loading && !rechecking && results && missingFindings.length === 0 && (currentSummaryRef.current?.total ?? 0) > 0 && (
        <div style={{ marginTop: '16px', padding: '12px 20px', background: '#ecfdf5', borderRadius: '10px', border: '1px solid #a7f3d0', fontSize: '13px', color: '#059669', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <CheckCircle2 size={15} /> 未发现事实型缺料项（无需线下补料）
          <span style={{ marginLeft: 'auto' }}>
            <button onClick={() => handleExportMissingMaterials('docx')} style={{ padding: '4px 10px', background: 'white', color: '#d97706', border: '1px solid #d97706', borderRadius: '6px', cursor: 'pointer', fontSize: '11px' }}>
              仍需导出材料清单
            </button>
          </span>
        </div>
      )}

      {rechecking && (
        <div style={{ marginTop: '16px', padding: '14px 20px', background: '#f0f9ff', borderRadius: '10px', fontSize: '13px', color: '#1e40af', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Loader2 size={15} className="spin" /> 复检修复中：全量检查重跑 + 自动 repair 闭环（约 1-25 分钟）...
        </div>
      )}

      {recheckBefore && recheckAfter && !rechecking && (
        <div style={{ marginTop: '16px', background: 'var(--color-surface)', borderRadius: '12px', padding: '20px 24px', border: '1px solid var(--color-border)' }}>
          <h3 style={{ fontSize: '15px', fontWeight: 600, marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}>
            <RefreshCw size={14} color="#059669" /> 复检数字对比（修复前 → 修复后）
          </h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '10px' }}>
            {([
              { label: '通过', before: recheckBefore.passed, after: recheckAfter.passed, color: '#059669' },
              { label: '存在问题', before: recheckBefore.failed, after: recheckAfter.failed, color: '#dc2626' },
              { label: '警告', before: recheckBefore.warning, after: recheckAfter.warning, color: '#d97706' },
              { label: '缺料 findings', before: recheckBefore.missing, after: recheckAfter.missing, color: '#be185d' },
            ]).map((row) => {
              const delta = row.after - row.before;
              const improved = row.label === '通过' ? delta > 0 : delta < 0;
              return (
                <div key={row.label} style={{ padding: '10px', background: '#f8fafc', borderRadius: '8px', textAlign: 'center' }}>
                  <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginBottom: '4px' }}>{row.label}</div>
                  <div style={{ fontSize: '16px', fontWeight: 700, color: row.color }}>
                    {row.before} → {row.after}
                  </div>
                  {delta !== 0 && (
                    <div style={{ fontSize: '11px', color: improved ? '#059669' : '#dc2626' }}>
                      {improved ? '↑ 改善' : '↓ 恶化'} {delta > 0 ? `+${delta}` : delta}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: '10px' }}>
            复检走全量检查图（POST /api/check/graph），repair 闭环由管线自动执行（检查发现→单章重写→硬门→复检）。
          </div>
        </div>
      )}

      {error && (
        <div style={{ marginTop: '16px', padding: '12px', background: '#fef2f2', borderRadius: '8px', color: '#dc2626', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <AlertTriangle size={16} /> {error}
        </div>
      )}

      {checkMode === 'project' && reports.length > 0 && (
        <div style={{ marginTop: '20px', background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '12px' }}>检查报告</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {reports.map(report => (
              <div key={report.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px', border: '1px solid var(--color-border)', borderRadius: '8px' }}>
                <div>
                  <span style={{ fontSize: '13px', fontWeight: 500 }}>{report.type}</span>
                  <span style={{ fontSize: '12px', color: getRiskColor(report.risk_level), marginLeft: '8px' }}>
                    {report.risk_level === 'high' ? '高风险' : report.risk_level === 'medium' ? '中风险' : '低风险'}
                  </span>
                  <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginLeft: '8px' }}>
                    {report.created_at ? new Date(report.created_at).toLocaleString() : ''}
                  </span>
                </div>
                <div style={{ display: 'flex', gap: '6px' }}>
                  <button onClick={() => handleExportReport(report.id, 'markdown')} style={{ padding: '4px 10px', background: '#059669', color: 'white', border: 'none', borderRadius: '4px', cursor: 'pointer', fontSize: '11px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                    <Download size={10} /> Markdown
                  </button>
                  <button onClick={() => handleExportReport(report.id, 'html')} style={{ padding: '4px 10px', background: '#2563eb', color: 'white', border: 'none', borderRadius: '4px', cursor: 'pointer', fontSize: '11px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                    <Download size={10} /> HTML
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
