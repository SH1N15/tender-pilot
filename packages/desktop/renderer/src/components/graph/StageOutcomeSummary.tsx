import { CheckCircle2, Circle, FileCheck2, FileText, ShieldCheck, Sparkles, UserRoundCheck } from 'lucide-react';
import type { GraphRunSnapshot } from '../../services/api';

const STAGES = [
  { key: 'interpret', label: '招标解读', icon: FileText, description: '提取资格要求、评分点、风险与关键时间' },
  { key: 'qualification', label: '资格核对', icon: ShieldCheck, description: '把企业材料与招标资格要求逐条比对' },
  { key: 'outline', label: '投标大纲', icon: Sparkles, description: '根据招标结构和评分要求形成章节计划' },
  { key: 'generate', label: '正文生成', icon: Sparkles, description: '按确认范围生成章节正文与引用台账' },
  { key: 'check', label: '检查修复', icon: FileCheck2, description: '执行合规检查、修复可自动修复的问题' },
];

function stageDetail(key: string, value: Record<string, unknown> | undefined): string {
  if (!value) return '尚未开始';
  if (value.error) return `失败：${String(value.error)}`;
  if (value.skipped) return '已跳过（使用已有产物）';
  if (key === 'interpret') {
    const data = (value.interpretation as Record<string, unknown> | undefined)?.data as Record<string, unknown> | undefined;
    const dimensions = data?.dimensions;
    return dimensions && typeof dimensions === 'object' ? `已形成 ${Object.keys(dimensions).length} 个解读维度` : '解读结果已写入项目';
  }
  if (key === 'qualification') {
    const items = Array.isArray(value.review_items) ? value.review_items.length : 0;
    return items ? `有 ${items} 条资格项等待人工确认` : '资格规则已完成，无待确认项';
  }
  if (key === 'outline') {
    const count = Array.isArray(value.chapters_plan) ? value.chapters_plan.length : 0;
    return count ? `已形成 ${count} 个章节，可在范围确认阶段勾选` : '大纲结果已写入项目';
  }
  if (key === 'generate') {
    const count = Array.isArray(value.chapters) ? value.chapters.length : Number(value.completed_chapters || 0);
    const total = Number(value.total_chapters || 0);
    return count ? `已生成 ${count}${total ? `/${total}` : ''} 个章节` : '正文产物将在范围确认后逐批写入';
  }
  if (key === 'check') {
    const report = (value.report as Record<string, unknown> | undefined) || value;
    const results = Array.isArray(report.check_results) ? report.check_results.length : 0;
    const missing = Array.isArray(report.missing_material_findings)
      ? report.missing_material_findings.length
      : Array.isArray(value.missing_materials) ? value.missing_materials.length : 0;
    return results ? `已完成 ${results} 项检查${missing ? `，${missing} 项需要补材料` : ''}` : '检查结果已写入项目';
  }
  return '已完成';
}

export default function StageOutcomeSummary({ snapshot }: { snapshot: GraphRunSnapshot | null }) {
  const statuses = snapshot?.node_status || {};
  const results = snapshot?.stage_results || {};
  const decisionPending = snapshot?.pending_gate === 'decision_hitl_gate'
    || snapshot?.pending_gate_namespace === 'decision';
  return (
    <section style={{ background: '#fff', border: '1px solid #dbe4f0', borderRadius: 12, padding: 16, marginBottom: 14 }}>
      <div style={{ fontSize: 14, fontWeight: 700, color: '#0f172a', marginBottom: 10 }}>阶段产物</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 9 }}>
        {STAGES.map((stage) => {
          const Icon = stage.icon;
          const status = statuses[stage.key] || 'pending';
          const waitingForHuman = stage.key === 'qualification' && snapshot?.pending_gate_namespace === 'qualification';
          const waitingForDecision = stage.key === 'export' && decisionPending;
          const done = !waitingForHuman && !waitingForDecision && (status === 'done' || status === 'skipped');
          const running = status === 'running';
          return (
            <div key={stage.key} style={{ border: `1px solid ${waitingForHuman || waitingForDecision ? '#fcd34d' : done ? '#a7f3d0' : running ? '#bfdbfe' : '#e2e8f0'}`, background: waitingForHuman || waitingForDecision ? '#fffbeb' : done ? '#f0fdf4' : running ? '#eff6ff' : '#f8fafc', borderRadius: 9, padding: '10px 11px', minHeight: 82 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: waitingForHuman || waitingForDecision ? '#92400e' : done ? '#047857' : running ? '#1d4ed8' : '#64748b', fontSize: 12, fontWeight: 700 }}>
                {waitingForHuman || waitingForDecision ? <UserRoundCheck size={14} /> : done ? <CheckCircle2 size={14} /> : running ? <Icon size={14} /> : <Circle size={14} />}
                {stage.label}
              </div>
              <div style={{ fontSize: 11, color: '#64748b', marginTop: 6, lineHeight: 1.45 }}>
                {waitingForDecision ? '等待确认投标建议后即可导出最终文件' : done || running || waitingForHuman ? stageDetail(stage.key, results[stage.key]) : stage.description}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
