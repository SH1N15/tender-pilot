import { AlertCircle, CheckCircle2, Circle, Clock3, Loader2, UserRoundCheck } from 'lucide-react';
import type { GraphRunSnapshot } from '../../services/api';
import { GraphMonitorStyles } from './graphShared';

type VisibleStatus = 'done' | 'running' | 'action' | 'error' | 'pending';

interface StageDef {
  key: string;
  label: string;
  description: string;
}

const STAGES: StageDef[] = [
  { key: 'upload', label: '上传招标文件', description: '把本项目的招标文件纳入流程' },
  { key: 'parse', label: '解析文件', description: '提取正文、章节和表格，供后续 AI 使用' },
  { key: 'interpret', label: '招标解读', description: '提取资格要求、评分项、时间节点和风险' },
  { key: 'qualification', label: '资格核对', description: '对照企业材料，确认资格条件是否有证据支撑' },
  { key: 'outline', label: '生成大纲', description: '按招标结构和评分要求形成投标文件目录' },
  { key: 'scope', label: '确认生成范围', description: '由你勾选本次需要生成的章节' },
  { key: 'generate', label: '分批生成正文', description: '每批最多 15 章，逐批保存并显示进度' },
  { key: 'check', label: '检查与修复', description: '执行合规检查并自动修复可处理的问题' },
  { key: 'decision', label: '确认投标建议', description: '结合风险、缺料和检查结果做最终确认' },
  { key: 'export', label: '复核并导出', description: '下载正文、检查报告和缺料清单' },
];

const STATUS_META: Record<VisibleStatus, { label: string; color: string; bg: string; border: string }> = {
  done: { label: '已完成', color: '#047857', bg: '#ecfdf5', border: '#a7f3d0' },
  running: { label: '正在处理', color: '#1d4ed8', bg: '#eff6ff', border: '#93c5fd' },
  action: { label: '需要你确认', color: '#92400e', bg: '#fffbeb', border: '#fcd34d' },
  error: { label: '需要处理', color: '#b91c1c', bg: '#fef2f2', border: '#fecaca' },
  pending: { label: '尚未开始', color: '#64748b', bg: '#f8fafc', border: '#e2e8f0' },
};

function visibleStatus(stage: string, snapshot: GraphRunSnapshot | null): VisibleStatus {
  const raw = snapshot?.node_status?.[stage];
  const namespace = snapshot?.pending_gate_namespace;
  const decisionPending = snapshot?.pending_gate === 'decision_hitl_gate' || namespace === 'decision';
  if ((stage === 'qualification' && namespace === 'qualification') ||
      (stage === 'scope' && namespace === 'scope') ||
      (stage === 'decision' && namespace === 'decision')) return 'action';
  if (stage === 'decision') {
    if (snapshot?.node_status?.decision_hitl_gate === 'done') return 'done';
    if (snapshot?.node_status?.rule_gate === 'done') return 'action';
  }
  if (stage === 'export') {
    if (snapshot?.current_stage === 'finalized') return 'done';
    if (snapshot?.node_status?.finalize === 'error') return 'error';
    if (decisionPending) return 'action';
  }
  if (raw === 'done' || raw === 'skipped') return 'done';
  if (raw === 'running') return 'running';
  if (raw === 'error' || raw === 'failed') return 'error';
  return 'pending';
}

function StatusIcon({ status }: { status: VisibleStatus }) {
  if (status === 'done') return <CheckCircle2 size={18} />;
  if (status === 'running') return <Loader2 size={18} className="spin" />;
  if (status === 'action') return <UserRoundCheck size={18} />;
  if (status === 'error') return <AlertCircle size={18} />;
  return <Circle size={18} />;
}

export default function NodeTopology({ snapshot }: { snapshot: GraphRunSnapshot | null }) {
  const progress = snapshot?.progress as Record<string, unknown> | undefined;
  const elapsed = Number(progress?.elapsed_seconds || 0);
  return (
    <div>
      <GraphMonitorStyles />
      {Boolean(progress?.message) && snapshot?.current_stage !== 'finalized' && (
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 9, marginBottom: 12, padding: '10px 12px', border: '1px solid #bfdbfe', background: '#eff6ff', borderRadius: 7, color: '#1e40af' }}>
          <Clock3 size={16} style={{ marginTop: 1, flexShrink: 0 }} />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 12, fontWeight: 700 }}>{String(progress?.stage_label || '流程运行中')}{elapsed > 0 ? ` · 已运行 ${elapsed} 秒` : ''}</div>
            <div style={{ fontSize: 11, marginTop: 2, color: '#475569' }}>{String(progress?.message || '')}</div>
          </div>
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 8 }}>
        {STAGES.map((stage, index) => {
          const status = visibleStatus(stage.key, snapshot);
          const meta = STATUS_META[status];
          return (
            <div key={stage.key} style={{ display: 'grid', gridTemplateColumns: '28px minmax(0, 1fr)', gap: 8, minHeight: 76, padding: '10px 11px', border: `1px solid ${meta.border}`, background: meta.bg, borderRadius: 7, color: meta.color }}>
              <div style={{ paddingTop: 1 }}><StatusIcon status={status} /></div>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8 }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: status === 'pending' ? '#475569' : meta.color }}>{index + 1}. {stage.label}</span>
                  <span style={{ fontSize: 10, whiteSpace: 'nowrap' }}>{meta.label}</span>
                </div>
                <div style={{ fontSize: 11, lineHeight: 1.45, color: '#64748b', marginTop: 4 }}>{stage.description}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
