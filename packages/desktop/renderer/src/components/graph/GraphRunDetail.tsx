// 运行详情：3s 轮询直至终态；节点拓扑 + 决策包四要素卡片 + 决策门交互（批准/改判，改判理由强校验）+ 成本面板。
// 契约：GET /api/graph/runs/{id}、POST /api/graph/runs/{id}/decision、GET /api/graph/runs/{id}/cost（core/agent_engine/README.md）。

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, CheckCircle2, Download, FileCheck2, Loader2, RefreshCw, ShieldQuestion, Upload, XCircle } from 'lucide-react';
import { checkApi, generateApi, graphApi, interpretApi, type GraphCostReport, type GraphRunDetail, type GraphRunSnapshot } from '../../services/api';
import NodeTopology from './NodeTopology';
import DecisionPackageCard from './DecisionPackageCard';
import CostPanel from './CostPanel';
import StageOutcomeSummary from './StageOutcomeSummary';
import { badgeStyle, cardStyle, GraphMonitorStyles, LEVEL_META, levelMeta, RUN_STATUS_META, sectionTitleStyle } from './graphShared';

const POLL_INTERVAL_MS = 3000;
const TERMINAL_STATUSES = new Set(['finalized', 'failed']);

const LEVEL_OPTIONS = ['BID', 'CAUTION', 'NO_BID'];

const CHECK_LABELS: Record<string, string> = {
  compliance_check: '合规性检查',
  disqualification_check: '废标项检查',
  qualification_check: '资质核查',
  pricing_check: '报价核查',
  fit_score: '贴合度评分',
  deposit_check: '保证金核查',
  signature_check: '签章核查',
  validity_check: '有效期核查',
  consistency_check: '一致性校验',
  duplicate_check: '标书查重',
  mandatory_req_check: '强制性参数对照',
  doc_integrity_check: '文件完整性',
  ai_text_check: 'AI 文本检查',
  risk_score: '风险评分',
  cross_check: '交叉比对',
  sample_report_check: '样品/检测报告',
  joint_bid_check: '联合投标协议',
  ebid_submit_check: '电子投标提交',
  pricing_logic_check: '报价逻辑闭环',
  selfcheck_list: '废标自查',
  whitelist_filter: '白名单过滤',
  policy_consistency_check: '政策划型一致性',
  check_report_export: '检查报告汇总',
};

const CHECK_STATUS_META: Record<string, { label: string; color: string; bg: string }> = {
  pass: { label: '通过', color: '#047857', bg: '#ecfdf5' },
  warning: { label: '警告', color: '#b45309', bg: '#fffbeb' },
  fail: { label: '失败', color: '#b91c1c', bg: '#fef2f2' },
  error: { label: '错误', color: '#b91c1c', bg: '#fef2f2' },
  skipped: { label: '跳过', color: '#64748b', bg: '#f1f5f9' },
};

const MATERIAL_HINTS: Record<string, string> = {
  qualification_check: '对应资质证书扫描件、证书编号/有效期页，或能证明资格条件的原始材料',
  mandatory_req_check: '逐条技术参数响应依据、产品说明书/检测报告/承诺函等原始佐证',
  disqualification_check: '废标条款逐条响应表、偏离表及对应的资格/技术证明附件',
  validity_check: '营业执照、资质证书、保函或其他能证明投标截止日仍有效的文件',
  compliance_check: '营业执照、税社保缴纳证明、财务报表、无重大违法记录声明等资格材料',
  cross_check: '评分项对应的案例合同、技术方案、人员证明或其他得分佐证',
  deposit_check: '保证金缴纳回执、银行保函或采购文件要求的其他保证金凭证',
  signature_check: '已签字盖章的原件扫描件，或可验证的电子签章/CA 文件',
  doc_integrity_check: '完整投标文件、目录、附件清单、页码和签章后的最终 PDF/DOCX',
  ebid_submit_check: '最终递交版 PDF/OFD、CA 签章文件及平台提交回执',
  fit_score: '针对本项目的技术方案、实施计划、服务承诺和逐条响应内容',
  pricing_check: '正式报价表、分项报价、报价说明及授权签字盖章页',
  pricing_logic_check: '人天/单价/数量计算依据、成本分配表和报价审批材料',
  sample_report_check: 'CMA/CNAS 检测报告、样品证明及对应型号/批次材料',
  joint_bid_check: '联合体协议、各方营业执照/资质及授权文件',
  consistency_check: '涉及金额、日期、人员、项目名称的原始合同或正式证明',
  ai_text_check: '无需上传实体材料；根据提示修改正文中的错别字、格式或表述',
  duplicate_check: '无需上传实体材料；提供可核对的原创内容来源或项目专属资料',
};

type MissingMaterialItem = {
  key: string;
  checkId: string;
  checkName: string;
  chapterId?: string;
  finding: string;
  suggestion: string;
  priority: '高' | '中' | '低';
  factRequired: boolean;
  action: '上传材料' | '补正文' | '人工确认/执行';
};

// 检查器有时会把“本阶段无需提交、后续阶段再办理”的说明放进缺口数组。
// 这是流程状态，不是企业材料缺失；按语义识别，避免绑定某一种保函或当前项目文本。
function isNonCurrentMaterialMatter(text: string): boolean {
  const value = text.replace(/[\s、，。；;：:（）()【】[\]“”'\"`]/g, '');
  const notRequired = /无需|无须|不需要|不必|不用|免收|免于|未要求|不要求|不适用|视为合规|无需补充|无需提交|无需提供|无需上传/.test(value);
  const laterStage = /中标后|成交后|合同签订后|签约后|履约阶段|后续履约|后续执行|后续办理|实施阶段|交付阶段|递交后|提交后/.test(value);
  const currentStage = /当前阶段|本阶段|投标阶段|资格审查阶段|递交阶段/.test(value);
  return (notRequired && (laterStage || currentStage)) || /(?:无需|无须|不适用|视为合规).{0,80}(?:材料|文件|证明|提交|上传)/.test(value);
}

function missingSuggestion(checkId: string, text: string): string {
  const value = `${checkId} ${text}`;
  if (isNonCurrentMaterialMatter(value)) return '当前阶段无需上传材料；请在招标文件规定的适用阶段执行';
  return MATERIAL_HINTS[checkId] || '上传能够直接证明该项要求的原始材料或扫描件';
}

function classifyMissingAction(checkId: string, text: string): MissingMaterialItem['action'] {
  const value = `${checkId} ${text}`;
  if (isNonCurrentMaterialMatter(value)) return '人工确认/执行';
  // CA/电子签章/平台回执属于最终递交动作，不是企业知识库材料。
  // 先判定这类词，避免“CA数字证书”被后面的“证书”规则误归为上传材料。
  if (/电子?CA|CA证书|电子签章|电子签字|平台提交|提交回执|加密上传|解密/.test(value)) return '人工确认/执行';
  // 资质、保证金、检测、业绩等是企业资料库可补齐的实体材料；即使描述中
  // 出现“扫描件/签字”，也不能把上传动作误归到线下人工执行。
  if (/qualification_check|validity_check|compliance_check|deposit_check|sample_report_check|joint_bid_check|cross_check|consistency_check/.test(checkId)
    && /证书|资质|营业执照|社保|税收|财务|合同|业绩|检测|报告|保证金|保函|人员|授权|证明|扫描件/.test(value)) return '上传材料';
  if (/pricing_check|pricing_logic_check/.test(checkId) && /报价|金额|价格|限价/.test(value)) return '人工确认/执行';
  if (/最高限价|限价|正本副本|份数|到账时间|缴纳形式|投标截止时间前到账|签字|盖章|签章|扫描件|CA|平台提交|回执/.test(value)) return '人工确认/执行';
  if (/投标有效期|投标函|技术响应|技术方案|目录|附件清单|逐条响应|偏离表|报价表/.test(value)) return '补正文';
  if (/证书|营业执照|社保|税收|合同|保函|人员证明|检测报告|联合体协议/.test(value)) return '上传材料';
  return '人工确认/执行';
}

export default function GraphRunDetail({ runId, onBack }: { runId: string; onBack: () => void }) {
  const navigate = useNavigate();
  const [detail, setDetail] = useState<GraphRunDetail | null>(null);
  const [cost, setCost] = useState<GraphCostReport | null>(null);
  const [costError, setCostError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [polling, setPolling] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  // 改判表单
  const [overrideMode, setOverrideMode] = useState(false);
  const [targetLevel, setTargetLevel] = useState('');
  const [reason, setReason] = useState('');
  const [reasonTouched, setReasonTouched] = useState(false);
  const [qualificationDecisions, setQualificationDecisions] = useState<Record<string, string>>({});
  const [scopeChapterIds, setScopeChapterIds] = useState<Set<string>>(new Set());
  const [exporting, setExporting] = useState(false);
  const [exportingReport, setExportingReport] = useState(false);
  const [uploadingMaterials, setUploadingMaterials] = useState(false);
  const [selectedCheckIds, setSelectedCheckIds] = useState<Set<string>>(new Set());

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const aliveRef = useRef(true);

  const fetchOnce = useCallback(async () => {
    try {
      const r = await graphApi.get(runId);
      if (!aliveRef.current) return;
      setDetail(r.data);
      setLoadError(null);
      return r.data;
    } catch (e) {
      if (!aliveRef.current) return;
      setLoadError(`加载运行详情失败：${(e as Error).message}`);
      return undefined;
    }
  }, [runId]);

  const fetchCost = useCallback(async () => {
    try {
      const r = await graphApi.cost(runId);
      if (!aliveRef.current) return;
      setCost(r.data.cost);
      setCostError(null);
    } catch (e) {
      if (!aliveRef.current) return;
      // 成本面板失败不阻塞详情（如节点尚未产生、404 等）
      setCostError(`成本数据暂不可用：${(e as Error).message}`);
    }
  }, [runId]);

  useEffect(() => {
    aliveRef.current = true;
    setPolling(true);
    const tick = async () => {
      const d = await fetchOnce();
      if (!aliveRef.current) return;
      const terminal = d && TERMINAL_STATUSES.has(d.status);
      if (terminal) {
        setPolling(false);
        fetchCost();
      } else {
        // 运行中也定期刷新成本面板（节点 LLM 调用即时产生成本）
        fetchCost();
        timerRef.current = setTimeout(tick, POLL_INTERVAL_MS);
      }
    };
    tick();
    return () => {
      aliveRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [fetchOnce, fetchCost]);

  const status = detail?.status;
  const snapshot: GraphRunSnapshot | null = detail?.snapshot || null;
  const pendingNamespace = snapshot?.pending_gate_namespace || detail?.pending_gate_namespace || (
    snapshot?.pending_gate === 'qualification_hitl_gate' ? 'qualification' :
      snapshot?.pending_gate === 'scope_hitl_gate' ? 'scope' :
      snapshot?.pending_gate === 'decision_hitl_gate' ? 'decision' : null
  );
  const isPendingQualification = status === 'pending_decision' && pendingNamespace === 'qualification';
  const isPendingScope = status === 'pending_decision' && pendingNamespace === 'scope';
  const isPendingDecision = status === 'pending_decision' && pendingNamespace === 'decision';
  const checkNodeStatus = String(snapshot?.node_status?.check || '').toLowerCase();
  const checkIsRunning = checkNodeStatus === 'running'
    || snapshot?.current_stage === 'check'
    || snapshot?.current_stage === 'recheck_requested';
  const pkg = checkIsRunning ? null : (snapshot?.decision_package || detail?.decision_package || null);
  const overrideReason = snapshot?.override_reason ?? detail?.override_reason ?? null;
  const humanDecision = snapshot?.human_decision || null;
  const finalLevel = snapshot?.final_level || detail?.snapshot?.final_level || null;
  const qualificationResult = snapshot?.stage_results?.qualification || {};
  const reviewItems = Array.isArray(qualificationResult.review_items)
    ? qualificationResult.review_items as Array<Record<string, unknown>>
    : [];
  const outlineResult = snapshot?.stage_results?.outline || {};
  const scopeChapters = Array.isArray(outlineResult.chapters_plan)
    ? outlineResult.chapters_plan as Array<Record<string, unknown>>
    : [];
  const checkResult = checkIsRunning ? {} : (snapshot?.stage_results?.check || {});
  const checkReport = (checkResult.report && typeof checkResult.report === 'object')
    ? checkResult.report as Record<string, unknown>
    : {};
  const existingCheckRows: Array<Record<string, unknown>> = Array.isArray(checkReport.check_results)
    ? checkReport.check_results as Array<Record<string, unknown>>
    : Object.entries((checkReport.results && typeof checkReport.results === 'object') ? checkReport.results as Record<string, unknown> : {})
      .map(([check_id, item]) => ({ check_id, ...(item && typeof item === 'object' ? item as Record<string, unknown> : {}) }));
  const existingCheckIds = new Set(existingCheckRows.map((item) => String(item.check_id || '')).filter(Boolean));
  const checkRows: Array<Record<string, unknown>> = [
    ...existingCheckRows,
    ...Object.keys(CHECK_LABELS)
      .filter((check_id) => !existingCheckIds.has(check_id))
      .map((check_id) => ({ check_id, check_name: CHECK_LABELS[check_id], status: 'not_run' })),
  ];
  const missingMaterialItems: MissingMaterialItem[] = (() => {
    const byKey = new Map<string, MissingMaterialItem>();
    const normalizeFinding = (value: string) => value
      .toLowerCase()
      .replace(/[\s、，。；;：:（）()【】[\]“”'"`]/g, '');
    const findingTokens = (value: string) => {
      const normalized = normalizeFinding(value);
      const tokens = new Set<string>();
      for (let i = 0; i < normalized.length - 1; i += 1) tokens.add(normalized.slice(i, i + 2));
      return tokens;
    };
    const duplicateFinding = (left: MissingMaterialItem, right: MissingMaterialItem) => {
      if (left.checkId !== right.checkId) return false;
      if (left.chapterId && right.chapterId && left.chapterId !== right.chapterId) return false;
      const a = normalizeFinding(left.finding);
      const b = normalizeFinding(right.finding);
      if (!a || !b) return false;
      if (a.includes(b) || b.includes(a)) return true;
      const at = findingTokens(a);
      const bt = findingTokens(b);
      const shared = Array.from(at).filter((token) => bt.has(token)).length;
      return shared >= 6 && shared / Math.max(1, Math.min(at.size, bt.size)) >= 0.55;
    };
    const materialKey = (checkId: string, chapterId: string | undefined, suggestion: string, finding: string) => {
      // 同一检查项可能有多个彼此独立的 finding；不能只用检查项+建议材料去重，
      // 否则不同缺陷会被错误合并成一个待处理事项。以 finding 作为主指纹，
      // 仅合并同一章节/同一语义 finding 从 raw report 与 feedback.tasks 的重复回传。
      const normalized = (finding || suggestion || MATERIAL_HINTS[checkId] || checkId)
        .toLowerCase()
        .replace(/[\s、，。；;：:（）()【】[\]“”'"`]/g, '')
        .slice(0, 180);
      return `${checkId}:${chapterId || ''}:${normalized}`;
    };
    const add = (item: MissingMaterialItem) => {
      if (!item.checkId || !item.finding) return;
      const existing = byKey.get(item.key)
        || Array.from(byKey.values()).find((candidate) => duplicateFinding(candidate, item));
      if (!existing) byKey.set(item.key, item);
      else {
        if (!existing.chapterId && item.chapterId) existing.chapterId = item.chapterId;
        if (item.finding && !existing.finding.includes(item.finding)) existing.finding = `${existing.finding}；${item.finding}`.slice(0, 500);
        if (item.suggestion && item.suggestion !== existing.suggestion) existing.suggestion = `${existing.suggestion}；${item.suggestion}`.slice(0, 500);
        existing.factRequired = existing.factRequired || item.factRequired;
        if (item.action === '上传材料') existing.action = item.action;
      }
    };
    const rawMissing = Array.isArray(checkReport.missing_material_findings)
      ? checkReport.missing_material_findings as Array<Record<string, unknown>>
      : [];
    rawMissing.forEach((item, index) => {
      const checkId = String(item.check_id || '');
      const checkName = String(item.check_name || CHECK_LABELS[checkId] || checkId);
      const chapterId = item.chapter_id ? String(item.chapter_id) : undefined;
      const finding = String(item.detail || item.reason || '检查发现当前材料不足');
      // 后端标记为当前阶段无需提交的事项不进入材料缺口清单；
      // 对旧报告缺少标记的场景，使用同一通用语义判定兜底。
      if (item.material_required === false || isNonCurrentMaterialMatter(finding)) return;
      const suggestion = missingSuggestion(checkId, finding);
      add({
        key: materialKey(checkId, chapterId, suggestion, finding),
        checkId,
        checkName,
        chapterId,
        finding,
        suggestion,
        priority: '高',
        factRequired: true,
        action: classifyMissingAction(checkId, String(item.detail || item.reason || '')),
      });
    });
    const feedback = checkReport.feedback && typeof checkReport.feedback === 'object'
      ? checkReport.feedback as Record<string, unknown>
      : {};
    const tasks = Array.isArray(feedback.tasks) ? feedback.tasks as Array<Record<string, unknown>> : [];
    tasks.forEach((task, index) => {
        const checkId = String(task.check_id || '');
      const factRequired = task.fact_required === true;
      // 反馈队列同时记录“正文修复”和“人工执行”事项。只有明确
      // 标记为 fact_required 的任务才属于仍需上传的企业证据；
      // 结构、格式、签章、CA、平台提交等任务展示在检查结果/人工
      // 执行区，不能再次混入“缺少材料”清单，避免把已修复或历史
      // 反馈误报成当前缺料。
      if (!factRequired) return;
      const chapterId = task.chapter_id ? String(task.chapter_id) : undefined;
      const finding = String(task.finding || '检查发现需要补充证明材料');
      const suggestion = String(task.suggestion || missingSuggestion(checkId, finding));
      add({
        key: materialKey(checkId, chapterId, suggestion, finding),
        checkId,
        checkName: String(task.check_name || CHECK_LABELS[checkId] || checkId),
        chapterId,
        finding,
        suggestion,
        priority: factRequired ? '高' : '中',
        factRequired,
        action: classifyMissingAction(checkId, `${String(task.finding || '')} ${String(task.suggestion || '')}`),
      });
    });
    reviewItems.forEach((item, index) => {
      const status = String(item.status || '');
      if (status !== 'insufficient' && status !== 'unmet') return;
      const checkId = 'qualification_check';
      add({
        key: `qualification:${String(item.requirement_id || index)}`,
        checkId,
        checkName: '资格核对',
        finding: String(item.reason || item.requirement_text || '资格要求缺少可核验证据'),
        suggestion: String(item.expected_evidence || MATERIAL_HINTS[checkId]),
        priority: '高',
        factRequired: true,
        action: classifyMissingAction(checkId, `${String(item.reason || '')} ${String(item.expected_evidence || '')}`),
      });
    });
    return Array.from(byKey.values());
  })();
  const missingByAction = missingMaterialItems.reduce<Record<MissingMaterialItem['action'], number>>((counts, item) => {
    counts[item.action] = (counts[item.action] || 0) + 1;
    return counts;
  }, { '上传材料': 0, '补正文': 0, '人工确认/执行': 0 });
  const uploadMissingCount = missingByAction['上传材料'];
  const hasUploadMissing = uploadMissingCount > 0;
  const checkStatusCounts = checkRows.reduce<Record<string, number>>((counts, item) => {
    const key = String(item.status || 'not_run').toLowerCase();
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {});

  useEffect(() => {
    if (!reviewItems.length) return;
    setQualificationDecisions((previous) => {
      const next = { ...previous };
      for (const item of reviewItems) {
        const id = String(item.requirement_id || '');
        if (id && item.decision && !next[id]) next[id] = String(item.decision);
      }
      return next;
    });
  }, [snapshot?.stage_results?.qualification]);

  useEffect(() => {
    if (!isPendingScope || !scopeChapters.length) return;
    setScopeChapterIds((previous) => {
      if (previous.size) return previous;
      return new Set(scopeChapters.map((item) => String(item.id || '')).filter(Boolean));
    });
  }, [isPendingScope, snapshot?.stage_results?.outline]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!checkRows.length) return;
    setSelectedCheckIds((previous) => previous.size
      ? previous
      : new Set(
        (checkRows.some((item) => ['fail', 'warning', 'error'].includes(String(item.status || '').toLowerCase()))
          ? checkRows.filter((item) => ['fail', 'warning', 'error'].includes(String(item.status || '').toLowerCase()))
          : checkRows.filter((item) => String(item.status || '').toLowerCase() !== 'not_run')
        ).map((item) => String(item.check_id || '')).filter(Boolean),
      ));
  }, [snapshot?.stage_results?.check]); // eslint-disable-line react-hooks/exhaustive-deps

  const reasonInvalid = overrideMode && !reason.trim();

  const submitDecision = async (action: 'approve' | 'override') => {
    if (submitting) return;
    // 铁律5：改判必须带理由 —— 前端强校验，理由为空禁提交
    if (action === 'override') {
      setReasonTouched(true);
      if (!targetLevel) { setActionMsg('改判必须选择目标级别'); return; }
      if (!reason.trim()) { setActionMsg('改判必须填写理由（铁律5）'); return; }
    }
    setSubmitting(true);
    setActionMsg(null);
    try {
      const body = action === 'approve'
        ? { action: 'approve' as const }
        : { action: 'override' as const, level: targetLevel, reason: reason.trim() };
      const r = await graphApi.decide(runId, body);
      if (!aliveRef.current) return;
      setDetail((prev) => (prev ? { ...prev, status: 'running', snapshot: r.data.snapshot } : prev));
      setOverrideMode(false);
      setActionMsg('最终确认已提交，正在完成收尾并准备导出');
      fetchCost();
    } catch (e) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } };
      const code = err.response?.status;
      const detailMsg = err.response?.data?.detail || (e as Error).message;
      setActionMsg(`提交失败${code ? `（HTTP ${code}）` : ''}：${detailMsg}`);
    } finally {
      if (aliveRef.current) setSubmitting(false);
    }
  };

  const submitQualification = async () => {
    if (submitting || !reviewItems.length) return;
    const decisions = reviewItems.map((item) => ({
      requirement_id: String(item.requirement_id || ''),
      decision: qualificationDecisions[String(item.requirement_id || '')] || '',
      reviewer: 'admin',
      note: '总图资格门人工确认',
    }));
    if (decisions.some((item) => !item.requirement_id || !item.decision)) {
      setActionMsg('请逐条选择“确认、否决”或“标记信息不足”后再提交');
      return;
    }
    setSubmitting(true);
    setActionMsg(null);
    try {
      const r = await graphApi.decide(runId, { action: 'approve', namespace: 'qualification', decisions });
      if (!aliveRef.current) return;
      setDetail((prev) => (prev ? { ...prev, status: 'running', snapshot: r.data.snapshot } : prev));
      setActionMsg('资格判断已记录，正在继续生成投标文件大纲');
    } catch (e) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } };
      setActionMsg(`提交失败${err.response?.status ? `（HTTP ${err.response.status}）` : ''}：${err.response?.data?.detail || (e as Error).message}`);
    } finally {
      if (aliveRef.current) setSubmitting(false);
    }
  };

  const uploadQualificationMaterials = async (files: FileList | null) => {
    if (!files?.length || !detail?.project_id || uploadingMaterials) return;
    setUploadingMaterials(true);
    setActionMsg(null);
    try {
      const uploaded = await interpretApi.upload(detail.project_id, Array.from(files), 'reference');
      const rows = (uploaded.data?.uploaded || []) as Array<{ document_id?: string }>;
      for (const row of rows) {
        if (row.document_id) await interpretApi.parseDocument(row.document_id);
      }
      const refreshed = await graphApi.decide(runId, { action: 'refresh', namespace: 'qualification' });
      if (!aliveRef.current) return;
      setQualificationDecisions({});
      setDetail((prev) => (prev ? { ...prev, status: 'running', snapshot: refreshed.data.snapshot } : prev));
      setActionMsg(`已上传并解析 ${rows.length} 份企业材料，系统正在重新核对资格要求`);
    } catch (e) {
      setActionMsg(`补充材料失败：${(e as Error).message}`);
    } finally {
      if (aliveRef.current) setUploadingMaterials(false);
    }
  };

  const uploadCheckMaterials = async (files: FileList | null) => {
    if (!files?.length || !detail?.project_id || uploadingMaterials) return;
    setUploadingMaterials(true);
    setActionMsg(null);
    try {
      const uploaded = await interpretApi.upload(detail.project_id, Array.from(files), 'reference');
      const rows = (uploaded.data?.uploaded || []) as Array<{ document_id?: string }>;
      for (const row of rows) {
        if (row.document_id) await interpretApi.parseDocument(row.document_id);
      }
      setActionMsg(`已上传并解析 ${rows.length} 份补充材料。请选择检查项后点击“重新检查并修复”`);
    } catch (e) {
      setActionMsg(`补充材料失败：${(e as Error).message}`);
    } finally {
      if (aliveRef.current) setUploadingMaterials(false);
    }
  };

  const submitRecheck = async () => {
    if (submitting || selectedCheckIds.size === 0) return;
    setSubmitting(true);
    setActionMsg(null);
    try {
      const r = await graphApi.decide(runId, {
        action: 'recheck',
        namespace: 'decision',
        check_ids: Array.from(selectedCheckIds),
      });
      if (!aliveRef.current) return;
      setDetail((prev) => (prev ? { ...prev, status: 'running', snapshot: r.data.snapshot } : prev));
      setActionMsg(`已提交 ${selectedCheckIds.size} 项复检，系统会自动修复可处理问题并重新生成决策依据`);
    } catch (e) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } };
      setActionMsg(`重新检查失败${err.response?.status ? `（HTTP ${err.response.status}）` : ''}：${err.response?.data?.detail || (e as Error).message}`);
    } finally {
      if (aliveRef.current) setSubmitting(false);
    }
  };

  const submitScope = async () => {
    if (submitting || scopeChapterIds.size === 0) return;
    setSubmitting(true);
    setActionMsg(null);
    try {
      const r = await graphApi.decide(runId, {
        action: 'approve',
        namespace: 'scope',
        chapter_ids: Array.from(scopeChapterIds),
      });
      if (!aliveRef.current) return;
      setDetail((prev) => (prev ? { ...prev, status: 'running', snapshot: r.data.snapshot } : prev));
      setActionMsg(`已确认 ${scopeChapterIds.size} 章，正文将按每批最多 15 章生成`);
    } catch (e) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } };
      setActionMsg(`提交失败${err.response?.status ? `（HTTP ${err.response.status}）` : ''}：${err.response?.data?.detail || (e as Error).message}`);
    } finally {
      if (aliveRef.current) setSubmitting(false);
    }
  };

  const exportDocx = async () => {
    if (!detail?.project_id || exporting) return;
    setExporting(true);
    setActionMsg(null);
    try {
      const response = await generateApi.exportDocxDirect(detail.project_id, false);
      const url = URL.createObjectURL(response.data);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `TenderPilot-${detail.project_id.slice(0, 8)}-投标正文.docx`;
      anchor.click();
      URL.revokeObjectURL(url);
      setActionMsg('正文 DOCX 已开始下载');
    } catch (e) {
      setActionMsg(`导出失败：${(e as Error).message}`);
    } finally {
      setExporting(false);
    }
  };

  const exportCheckReport = async () => {
    if (!detail?.project_id || exportingReport) return;
    setExportingReport(true);
    setActionMsg(null);
    try {
      const reports = await checkApi.listReports(detail.project_id);
      const latest = [...(reports.data.reports || [])].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
      const response = latest
        ? await checkApi.exportReport(detail.project_id, latest.id, 'markdown')
        : await graphApi.exportReport(runId, 'markdown');
      const url = URL.createObjectURL(response.data);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `TenderPilot-${detail.project_id.slice(0, 8)}-检查报告.md`;
      anchor.click();
      URL.revokeObjectURL(url);
      setActionMsg('检查报告已开始下载');
    } catch (e) {
      setActionMsg(`检查报告导出失败：${(e as Error).message}`);
    } finally {
      setExportingReport(false);
    }
  };

  const exportMissingMaterials = async () => {
    if (!detail?.project_id || exportingReport) return;
    setExportingReport(true);
    setActionMsg(null);
    try {
      const response = await checkApi.exportMissingMaterials(detail.project_id, 'docx');
      const url = URL.createObjectURL(response.data);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `TenderPilot-${detail.project_id.slice(0, 8)}-需补充材料清单.docx`;
      anchor.click();
      URL.revokeObjectURL(url);
      setActionMsg('需补充材料清单已开始下载');
    } catch (e) {
      setActionMsg(`缺料清单导出失败：${(e as Error).message}`);
    } finally {
      setExportingReport(false);
    }
  };

  const statusMeta = status ? RUN_STATUS_META[status] : null;
  const finalMeta = levelMeta(finalLevel);

  return (
    <div>
      <GraphMonitorStyles />
      {/* 头部 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 14, flexWrap: 'wrap' }}>
        <button
          onClick={onBack}
          style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', background: '#fff', cursor: 'pointer', fontSize: 12, color: '#475569' }}
        >
          <ArrowLeft size={13} /> 返回列表
        </button>
        <span style={{ fontSize: 14, fontWeight: 700, color: '#0f172a', fontFamily: 'monospace' }}>{runId}</span>
        {statusMeta && <span style={badgeStyle(statusMeta.color, statusMeta.bg)}>{statusMeta.label}</span>}
        {finalLevel && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#64748b' }}>
            最终定级 <span style={badgeStyle(finalMeta.color, finalMeta.bg)}>{finalMeta.label}</span>
          </span>
        )}
        {polling && !isPendingQualification && !isPendingScope && !isPendingDecision && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, color: '#64748b' }}>
            <Loader2 size={12} className="spin" /> 状态自动更新中…
          </span>
        )}
        {(isPendingQualification || isPendingScope || isPendingDecision) && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, color: '#92400e' }}>
            <ShieldQuestion size={12} /> 等待人工确认
          </span>
        )}
        <button
          onClick={() => { fetchOnce(); if (!polling) fetchCost(); }}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginLeft: 'auto', padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', background: '#fff', cursor: 'pointer', fontSize: 12, color: '#475569' }}
        >
          <RefreshCw size={12} /> 刷新
        </button>
      </div>

      {loadError && (
        <div style={{ background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 10, padding: '10px 12px', marginBottom: 12, fontSize: 12, color: '#b91c1c' }}>
          {loadError}
        </div>
      )}
      {detail?.error && (
        <div style={{ background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 10, padding: '10px 12px', marginBottom: 12, fontSize: 12, color: '#b91c1c' }}>
          运行错误：{detail.error}
        </div>
      )}

      <StageOutcomeSummary snapshot={snapshot} />

      {detail?.project_id && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10 }}>
          <button onClick={() => navigate('/knowledge', { state: { projectId: detail.project_id, from: 'check-recheck', preselectType: 'enterprise' } })} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 10px', border: '1px solid #bfdbfe', borderRadius: 6, background: '#f8fbff', color: '#1d4ed8', fontSize: 11, cursor: 'pointer' }}>
            查看当前项目证据 RAG
          </button>
        </div>
      )}

      {/* 节点拓扑 */}
      <div style={{ ...cardStyle, marginBottom: 14 }}>
        <div style={sectionTitleStyle}>全流程进度</div>
        <NodeTopology snapshot={snapshot} />
      </div>

      {/* 决策门交互（pending_decision 时展示） */}
      {isPendingQualification && (
        <div style={{ ...cardStyle, marginBottom: 14, borderColor: '#93c5fd', background: '#eff6ff' }}>
          <div style={{ ...sectionTitleStyle, display: 'flex', alignItems: 'center', gap: 6, color: '#1e3a8a' }}>
            <ShieldQuestion size={15} /> 需要你确认：企业材料能否证明这些资格要求
          </div>
          <p style={{ fontSize: 12, color: '#475569', margin: '0 0 10px' }}>
            请以招标原文和实际证明材料为准。系统没有找到足够证据，不等于企业一定不满足；缺少材料时请选择“暂缺材料/信息”。
          </p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 10, padding: '9px 10px', border: '1px solid #bfdbfe', background: '#fff', borderRadius: 7, flexWrap: 'wrap' }}>
            <div style={{ flex: 1, minWidth: 240, fontSize: 11, color: '#475569' }}>企业证件、合同、人员证明不在系统中时，可在这里补充；上传后会自动解析并重新核对，不需要离开全链路页面。</div>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 10px', border: '1px solid #93c5fd', background: '#eff6ff', color: '#1d4ed8', borderRadius: 6, fontSize: 11, fontWeight: 700, cursor: uploadingMaterials ? 'wait' : 'pointer' }}>
              {uploadingMaterials ? <Loader2 size={13} className="spin" /> : <Upload size={13} />}
              {uploadingMaterials ? '上传解析中…' : '补充企业证明材料'}
              <input type="file" multiple accept=".pdf,.docx,.doc,.wps,.txt,.md" hidden disabled={uploadingMaterials} onChange={(event) => { uploadQualificationMaterials(event.target.files); event.currentTarget.value = ''; }} />
            </label>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {reviewItems.map((item) => {
              const id = String(item.requirement_id || '');
              const selected = qualificationDecisions[id] || '';
              return (
                <div key={id} style={{ background: '#fff', border: '1px solid #dbeafe', borderRadius: 9, padding: '10px 12px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, alignItems: 'flex-start' }}>
                    <div>
                      <div style={{ fontSize: 13, fontWeight: 700, color: '#1e293b' }}>{String(item.title || item.category_label || '资格要求待确认')}</div>
                      {Boolean(item.requirement_text) && <div style={{ fontSize: 12, color: '#334155', marginTop: 6, lineHeight: 1.55 }}><strong>招标原文：</strong>{String(item.requirement_text)}</div>}
                      <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>来源：{String(item.source_label || '招标文件 · 资格要求')}</div>
                      <div style={{ fontSize: 12, color: '#475569', marginTop: 6 }}><strong>系统判断：</strong>{String(item.reason || '系统未提供说明')}</div>
                      <div style={{ fontSize: 12, color: '#475569', marginTop: 4 }}><strong>应提供：</strong>{String(item.expected_evidence || '能够直接证明该项资格的原始材料')}</div>
                      <div style={{ fontSize: 12, color: '#475569', marginTop: 4 }}><strong>当前匹配：</strong>{String(item.matched_evidence_summary || '未找到可直接匹配的证明材料')}</div>
                      {Boolean(item.recommendation) && <div style={{ fontSize: 11, color: '#1d4ed8', marginTop: 6 }}>{String(item.recommendation)}</div>}
                      {Array.isArray(item.evidence_refs) && item.evidence_refs.length > 0 && <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>证据定位：{item.evidence_refs.join('、')}</div>}
                    </div>
                    <span style={badgeStyle(
                      item.status === 'met' ? '#047857' : item.status === 'unmet' ? '#b91c1c' : '#92400e',
                      item.status === 'met' ? '#ecfdf5' : item.status === 'unmet' ? '#fef2f2' : '#fffbeb',
                    )}>{item.status === 'met' ? '系统判断：满足' : item.status === 'unmet' ? '系统判断：不满足' : '系统判断：证据不足'}</span>
                  </div>
                  <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap', marginTop: 9 }}>
                    {[
                      { value: 'confirm', label: '材料满足要求', color: '#047857', bg: '#ecfdf5' },
                      { value: 'reject', label: '确认不满足', color: '#b91c1c', bg: '#fef2f2' },
                      { value: 'mark_insufficient', label: '暂缺材料/信息', color: '#92400e', bg: '#fffbeb' },
                    ].map((option) => (
                      <button key={option.value} onClick={() => setQualificationDecisions((prev) => ({ ...prev, [id]: option.value }))} style={{ padding: '5px 9px', borderRadius: 6, border: selected === option.value ? `2px solid ${option.color}` : '1px solid #cbd5e1', background: selected === option.value ? option.bg : '#fff', color: option.color, fontSize: 11, fontWeight: 600, cursor: 'pointer' }}>{option.label}</button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
          <button onClick={submitQualification} disabled={submitting || !reviewItems.length} style={{ marginTop: 10, display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 15px', borderRadius: 7, background: '#1d4ed8', color: '#fff', border: 0, fontSize: 12, fontWeight: 600, cursor: submitting ? 'wait' : 'pointer', opacity: submitting ? 0.7 : 1 }}>
            {submitting ? <Loader2 size={13} className="spin" /> : <CheckCircle2 size={13} />} 提交资格判断
          </button>
          {actionMsg && <p style={{ fontSize: 12, color: '#1e3a8a', margin: '8px 0 0' }}>{actionMsg}</p>}
        </div>
      )}

      {isPendingScope && (
        <div style={{ ...cardStyle, marginBottom: 14, borderColor: '#93c5fd', background: '#eff6ff' }}>
          <div style={{ ...sectionTitleStyle, display: 'flex', alignItems: 'center', gap: 6, color: '#1e3a8a' }}>
            <FileCheck2 size={15} /> 需要你确认：本次生成哪些章节
          </div>
          <p style={{ fontSize: 12, color: '#475569', margin: '0 0 10px' }}>
            大纲已经生成。默认已选择全部章节，你可以取消暂时不需要的章节；系统会按每批最多 15 章生成并持续显示进度。
          </p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: '#1e3a8a' }}>已选 {scopeChapterIds.size}/{scopeChapters.length} 章</span>
            <button onClick={() => setScopeChapterIds(new Set(scopeChapters.map((item) => String(item.id || '')).filter(Boolean)))} style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #bfdbfe', background: '#fff', color: '#1d4ed8', fontSize: 11, cursor: 'pointer' }}>全选</button>
            <button onClick={() => setScopeChapterIds(new Set())} style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #bfdbfe', background: '#fff', color: '#475569', fontSize: 11, cursor: 'pointer' }}>清空</button>
          </div>
          <div style={{ maxHeight: 360, overflowY: 'auto', background: '#fff', border: '1px solid #dbeafe', borderRadius: 7 }}>
            {scopeChapters.map((chapter) => {
              const chapterId = String(chapter.id || '');
              const checked = scopeChapterIds.has(chapterId);
              return (
                <label key={chapterId} style={{ display: 'grid', gridTemplateColumns: '22px minmax(0, 1fr)', alignItems: 'center', gap: 6, padding: '7px 10px', borderBottom: '1px solid #eff6ff', cursor: 'pointer', fontSize: 12, color: '#334155' }}>
                  <input type="checkbox" checked={checked} onChange={() => setScopeChapterIds((previous) => {
                    const next = new Set(previous);
                    if (next.has(chapterId)) next.delete(chapterId); else next.add(chapterId);
                    return next;
                  })} />
                  <span style={{ paddingLeft: Math.max(0, Number(chapter.level || 1) - 1) * 14 }}>{String(chapter.title || `章节 ${chapterId}`)}</span>
                </label>
              );
            })}
          </div>
          <button onClick={submitScope} disabled={submitting || scopeChapterIds.size === 0} style={{ marginTop: 10, display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 15px', borderRadius: 7, background: scopeChapterIds.size ? '#1d4ed8' : '#94a3b8', color: '#fff', border: 0, fontSize: 12, fontWeight: 600, cursor: submitting || !scopeChapterIds.size ? 'not-allowed' : 'pointer' }}>
            {submitting ? <Loader2 size={13} className="spin" /> : <CheckCircle2 size={13} />} 确认范围并开始生成
          </button>
          {actionMsg && <p style={{ fontSize: 12, color: '#1e3a8a', margin: '8px 0 0' }}>{actionMsg}</p>}
        </div>
      )}

      {isPendingDecision && checkRows.length > 0 && (
        <>
        {missingMaterialItems.length > 0 && (
          <div style={{ ...cardStyle, marginBottom: 14, borderColor: '#fbbf24', background: '#fffbeb' }}>
            <div style={{ ...sectionTitleStyle, display: 'flex', alignItems: 'center', gap: 6, color: '#92400e' }}>
              <FileCheck2 size={15} /> 待处理事项（{missingMaterialItems.length} 条）
            </div>
            <div style={{ display: 'flex', gap: 7, flexWrap: 'wrap', marginBottom: 10 }}>
              <span style={badgeStyle('#1d4ed8', '#dbeafe')}>上传材料 {missingByAction['上传材料']}</span>
              <span style={badgeStyle('#6d28d9', '#ede9fe')}>补正文 {missingByAction['补正文']}</span>
              <span style={badgeStyle('#475569', '#f1f5f9')}>人工确认/执行 {missingByAction['人工确认/执行']}</span>
            </div>
            {hasUploadMissing && <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 10px', border: '1px solid #f59e0b', background: '#fff', color: '#92400e', borderRadius: 6, fontSize: 11, fontWeight: 700, cursor: uploadingMaterials ? 'wait' : 'pointer' }}>
                {uploadingMaterials ? <Loader2 size={13} className="spin" /> : <Upload size={13} />}
                {uploadingMaterials ? '上传解析中…' : '上传这些材料'}
                <input type="file" multiple accept=".pdf,.docx,.doc,.wps,.txt,.md" hidden disabled={uploadingMaterials} onChange={(event) => { uploadCheckMaterials(event.target.files); event.currentTarget.value = ''; }} />
              </label>
              <button onClick={exportMissingMaterials} disabled={exportingReport} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 10px', border: '1px solid #fcd34d', background: '#fff', color: '#92400e', borderRadius: 6, fontSize: 11, fontWeight: 700, cursor: exportingReport ? 'wait' : 'pointer' }}>
                <Download size={13} /> 导出材料清单
              </button>
            </div>}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 7, maxHeight: 360, overflowY: 'auto' }}>
              {missingMaterialItems.map((item) => (
                <div key={item.key} style={{ background: '#fff', border: '1px solid #fde68a', borderRadius: 7, padding: '9px 10px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 12, fontWeight: 700, color: '#78350f' }}>{item.suggestion}</span>
                    <span style={badgeStyle(item.action === '上传材料' ? '#1d4ed8' : item.action === '补正文' ? '#6d28d9' : '#475569', item.action === '上传材料' ? '#dbeafe' : item.action === '补正文' ? '#ede9fe' : '#f1f5f9')}>{item.action}</span>
                    <span style={badgeStyle(item.priority === '高' ? '#b91c1c' : '#b45309', item.priority === '高' ? '#fee2e2' : '#fef3c7')}>{item.priority}优先</span>
                    <span style={{ fontSize: 11, color: '#64748b' }}>来源：{item.checkName}{item.chapterId ? ` · 第 ${item.chapterId} 章` : ''}</span>
                  </div>
                  <div style={{ fontSize: 11, color: '#475569', marginTop: 5, lineHeight: 1.5 }}>{item.finding}</div>
                </div>
              ))}
            </div>
          </div>
        )}
        <div style={{ ...cardStyle, marginBottom: 14, borderColor: '#c4b5fd', background: '#faf5ff' }}>
          <div style={{ ...sectionTitleStyle, display: 'flex', alignItems: 'center', gap: 6, color: '#5b21b6' }}>
            <RefreshCw size={15} /> 重新检查 / 修复
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, padding: '9px 10px', border: '1px solid #ddd6fe', background: '#fff', borderRadius: 7, flexWrap: 'wrap' }}>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 10px', border: '1px solid #c4b5fd', background: '#f5f3ff', color: '#6d28d9', borderRadius: 6, fontSize: 11, fontWeight: 700, cursor: uploadingMaterials ? 'wait' : 'pointer' }}>
              {uploadingMaterials ? <Loader2 size={13} className="spin" /> : <Upload size={13} />}
              {uploadingMaterials ? '上传解析中…' : '补充企业材料'}
              <input type="file" multiple accept=".pdf,.docx,.doc,.wps,.txt,.md" hidden disabled={uploadingMaterials} onChange={(event) => { uploadCheckMaterials(event.target.files); event.currentTarget.value = ''; }} />
            </label>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: '#5b21b6' }}>选择要重跑的检查项：{selectedCheckIds.size}/{checkRows.length}</span>
            <button onClick={() => setSelectedCheckIds(new Set(checkRows.map((item) => String(item.check_id || '')).filter(Boolean)))} style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #ddd6fe', background: '#fff', color: '#6d28d9', fontSize: 11, cursor: 'pointer' }}>全选</button>
            <button onClick={() => setSelectedCheckIds(new Set())} style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #ddd6fe', background: '#fff', color: '#475569', fontSize: 11, cursor: 'pointer' }}>清空</button>
            <span style={{ marginLeft: 'auto', fontSize: 11, color: '#64748b' }}>
              当前：通过 {String(checkStatusCounts.pass || 0)} · 失败 {String(checkStatusCounts.fail || 0)} · 警告 {String(checkStatusCounts.warning || 0)} · 错误 {String(checkStatusCounts.error || 0)}
            </span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 6, maxHeight: 230, overflowY: 'auto', padding: 2 }}>
            {checkRows.map((item) => {
              const checkId = String(item.check_id || '');
              if (!checkId) return null;
              const statusKey = String(item.status || '').toLowerCase();
              const meta = CHECK_STATUS_META[statusKey] || { label: statusKey || '待检查', color: '#64748b', bg: '#f1f5f9' };
              const checked = selectedCheckIds.has(checkId);
              return (
                <label key={checkId} style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '7px 8px', border: `1px solid ${checked ? '#c4b5fd' : '#e2e8f0'}`, background: checked ? '#f5f3ff' : '#fff', borderRadius: 6, cursor: 'pointer', fontSize: 11, color: '#334155' }}>
                  <input type="checkbox" checked={checked} onChange={() => setSelectedCheckIds((previous) => {
                    const next = new Set(previous);
                    if (next.has(checkId)) next.delete(checkId); else next.add(checkId);
                    return next;
                  })} />
                  <span style={{ flex: 1 }}>{CHECK_LABELS[checkId] || String(item.check_name || checkId)}</span>
                  <span style={badgeStyle(meta.color, meta.bg)}>{meta.label}</span>
                </label>
              );
            })}
          </div>
          <button onClick={submitRecheck} disabled={submitting || selectedCheckIds.size === 0} style={{ marginTop: 10, display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 15px', borderRadius: 7, background: selectedCheckIds.size ? '#7c3aed' : '#cbd5e1', color: '#fff', border: 0, fontSize: 12, fontWeight: 600, cursor: submitting || !selectedCheckIds.size ? 'not-allowed' : 'pointer' }}>
            {submitting ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />} 重新检查并修复（已选 {selectedCheckIds.size} 项）
          </button>
        </div>
        </>
      )}

      {isPendingDecision && (
        <div style={{ ...cardStyle, marginBottom: 14, borderColor: '#fcd34d', background: '#fffbeb' }}>
          <div style={{ ...sectionTitleStyle, display: 'flex', alignItems: 'center', gap: 6, color: '#92400e' }}>
            <ShieldQuestion size={15} /> 需要你确认：是否接受本次投标建议
          </div>
          {!overrideMode ? (
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <button
                onClick={() => submitDecision('approve')}
                disabled={submitting}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 16px', borderRadius: 8, background: '#059669', color: '#fff', border: 'none', fontSize: 13, cursor: submitting ? 'not-allowed' : 'pointer', opacity: submitting ? 0.7 : 1 }}
              >
                <CheckCircle2 size={14} /> 批准建议{pkg?.level ? `（${pkg.level}）` : ''}
              </button>
              <button
                onClick={() => { setOverrideMode(true); setTargetLevel(''); setReason(''); setReasonTouched(false); setActionMsg(null); }}
                disabled={submitting}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 16px', borderRadius: 8, background: '#d97706', color: '#fff', border: 'none', fontSize: 13, cursor: submitting ? 'not-allowed' : 'pointer', opacity: submitting ? 0.7 : 1 }}
              >
                <XCircle size={14} /> 改判
              </button>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <span style={{ fontSize: 12, fontWeight: 600, color: '#92400e' }}>目标级别</span>
                {LEVEL_OPTIONS.filter((l) => l !== pkg?.level).map((l) => {
                  const m = LEVEL_META[l];
                  const selected = targetLevel === l;
                  return (
                    <button
                      key={l}
                      onClick={() => setTargetLevel(l)}
                      style={{ padding: '6px 14px', borderRadius: 8, border: selected ? `2px solid ${m.color}` : '1px solid #e2e8f0', background: selected ? m.bg : '#fff', color: m.color, fontSize: 12, fontWeight: 600, cursor: 'pointer' }}
                    >
                      {m.label}
                    </button>
                  );
                })}
              </div>
              <div>
                <span style={{ fontSize: 12, fontWeight: 600, color: '#92400e' }}>改判理由（必填，铁律5）</span>
                <textarea
                  value={reason}
                  onChange={(e) => { setReason(e.target.value); setReasonTouched(true); }}
                  placeholder="请填写改判理由，提交后将写入 override_reason 审计日志"
                  rows={3}
                  style={{ width: '100%', marginTop: 6, padding: '8px 10px', borderRadius: 8, border: reasonInvalid && reasonTouched ? '1px solid #dc2626' : '1px solid #e2e8f0', fontSize: 12, fontFamily: 'inherit', boxSizing: 'border-box' }}
                />
                {reasonTouched && reasonInvalid && (
                  <p style={{ fontSize: 11, color: '#dc2626', margin: '4px 0 0' }}>理由为空，禁止提交（铁律5：改判必带理由）</p>
                )}
              </div>
              <div style={{ display: 'flex', gap: 10 }}>
                <button
                  onClick={() => submitDecision('override')}
                  disabled={submitting || reasonInvalid || !targetLevel}
                  style={{ padding: '8px 16px', borderRadius: 8, background: submitting || reasonInvalid || !targetLevel ? '#cbd5e1' : '#1a56db', color: '#fff', border: 'none', fontSize: 13, cursor: submitting || reasonInvalid || !targetLevel ? 'not-allowed' : 'pointer' }}
                >
                  {submitting ? '提交中…' : '提交改判'}
                </button>
                <button
                  onClick={() => { setOverrideMode(false); setActionMsg(null); }}
                  style={{ padding: '8px 16px', borderRadius: 8, background: '#fff', color: '#475569', border: '1px solid #e2e8f0', fontSize: 13, cursor: 'pointer' }}
                >
                  取消
                </button>
              </div>
            </div>
          )}
          {actionMsg && <p style={{ fontSize: 12, color: '#b45309', margin: '8px 0 0' }}>{actionMsg}</p>}
        </div>
      )}

      {/* 最终建议只在规则检查确实产出后展示，避免资格阶段看起来像“跳到最后”。 */}
      {pkg && (
        <div style={{ ...cardStyle, marginBottom: 14 }}>
          <div style={sectionTitleStyle}>最终投标建议依据</div>
          <DecisionPackageCard pkg={pkg} />
        </div>
      )}

      {/* 人工决策 / 改判路径展示 */}
      {(humanDecision || overrideReason || finalLevel) && (
        <div style={{ ...cardStyle, marginBottom: 14 }}>
          <div style={sectionTitleStyle}>人工确认记录</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 12, color: '#334155' }}>
            <div>
              <span style={{ color: '#64748b' }}>确认操作：</span>
              {humanDecision ? (
                <span style={badgeStyle('#1d4ed8', '#dbeafe')}>
                  {humanDecision.action === 'approve' ? '接受系统建议' : humanDecision.action === 'override' ? '改判系统建议' : '已确认'}
                  {humanDecision.level ? ` → ${levelMeta(String(humanDecision.level)).label}` : ''}
                  {humanDecision.reviewer ? `（${String(humanDecision.reviewer)}）` : ''}
                </span>
              ) : '无'}
            </div>
            <div>
              <span style={{ color: '#64748b' }}>改判理由：</span>
              {overrideReason ? <span style={{ color: '#b45309' }}>{overrideReason}</span> : '无'}
            </div>
            <div>
              <span style={{ color: '#64748b' }}>最终投标建议：</span>
              {finalLevel ? <span style={badgeStyle(finalMeta.color, finalMeta.bg)}>{finalMeta.label}</span> : '—'}
            </div>
          </div>
        </div>
      )}

      {status === 'finalized' && (
        <div style={{ ...cardStyle, marginBottom: 14, borderColor: '#a7f3d0', background: '#f0fdf4' }}>
          <div style={{ ...sectionTitleStyle, display: 'flex', alignItems: 'center', gap: 6, color: '#166534' }}>
            <CheckCircle2 size={15} /> 全链路已完成，可直接交付
          </div>
          <p style={{ fontSize: 12, color: '#475569', margin: '0 0 10px' }}>
            解读、资格、正文生成、检查和决策均已在本次运行中完成。导出前请人工复核报价、签章和证件扫描件等线下要件。
          </p>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button onClick={exportDocx} disabled={exporting} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 13px', borderRadius: 7, background: '#15803d', color: '#fff', border: 0, fontSize: 12, fontWeight: 600, cursor: exporting ? 'wait' : 'pointer', opacity: exporting ? 0.7 : 1 }}>
              {exporting ? <Loader2 size={13} className="spin" /> : <Download size={13} />} 导出正文 DOCX
            </button>
            <button onClick={exportCheckReport} disabled={exportingReport} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 13px', borderRadius: 7, background: '#fff', color: '#166534', border: '1px solid #bbf7d0', fontSize: 12, cursor: exportingReport ? 'wait' : 'pointer' }}>
              <Download size={13} /> 导出检查报告
            </button>
            <button onClick={exportMissingMaterials} disabled={exportingReport} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 13px', borderRadius: 7, background: '#fff', color: '#166534', border: '1px solid #bbf7d0', fontSize: 12, cursor: exportingReport ? 'wait' : 'pointer' }}>
              <Download size={13} /> 导出缺料清单
            </button>
            <button onClick={() => { window.location.href = `/generate?project_id=${encodeURIComponent(detail?.project_id || '')}`; }} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 13px', borderRadius: 7, background: '#fff', color: '#475569', border: '1px solid #bbf7d0', fontSize: 12, cursor: 'pointer' }}>
              打开高级编辑工具
            </button>
          </div>
          {actionMsg && <p style={{ fontSize: 12, color: '#166534', margin: '8px 0 0' }}>{actionMsg}</p>}
        </div>
      )}

      {/* 成本面板 */}
      {status === 'finalized' && <CostPanel cost={cost} error={costError} />}
    </div>
  );
}
