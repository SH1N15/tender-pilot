// 决策包四要素卡片：建议定级 / 理由 / 证据列表 / 风险清单（severity 分级）。
// evidence / risks 元素做兼容渲染：字符串直接显示；对象取常见字段（check/description/summary/severity/…），兜底 JSON。

import type { GraphDecisionPackage } from '../../services/api';
import { badgeStyle, cardStyle, levelMeta, sectionTitleStyle } from './graphShared';

const SEVERITY_META: Record<string, { label: string; color: string; bg: string }> = {
  high: { label: '高', color: '#b91c1c', bg: '#fee2e2' },
  medium: { label: '中', color: '#b45309', bg: '#fef3c7' },
  low: { label: '低', color: '#15803d', bg: '#dcfce7' },
};

function pickString(obj: Record<string, unknown>, keys: string[]): string | undefined {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === 'string' && v) return v;
    if (typeof v === 'number') return String(v);
  }
  return undefined;
}

function summarizeObject(obj: Record<string, unknown>): string {
  const direct = pickString(obj, ['summary', 'description', 'detail', 'reason', 'message', 'result']);
  if (direct) return summarizeString(direct);
  const total = Number(obj.total ?? obj.total_checks ?? obj.total_requirements ?? obj.total_clauses ?? obj.total_items);
  const passed = Number(obj.passed ?? obj.compliant ?? obj.fully_responded ?? obj.answered);
  const failed = Number(obj.failed ?? obj.non_compliant ?? obj.missing);
  const warning = Number(obj.warning ?? obj.warnings);
  if (Number.isFinite(total) && total > 0) {
    const parts = [`共 ${total} 项`];
    if (Number.isFinite(passed)) parts.push(`通过 ${passed}`);
    if (Number.isFinite(failed)) parts.push(`未通过/缺失 ${failed}`);
    if (Number.isFinite(warning)) parts.push(`警告 ${warning}`);
    return parts.join('，');
  }
  const coverage = pickString(obj, ['coverage_rate', 'fit_score', 'risk_level']);
  if (coverage) return coverage;
  return '已生成结构化检查结果，请查看下方检查明细。';
}

function summarizeString(value: string): string {
  const text = value.trim();
  if (!/^[\[{]/.test(text)) return text;
  const read = (names: string[]) => {
    for (const name of names) {
      const match = text.match(new RegExp(`[\\'\"]?${name}[\\'\"]?\\s*:\\s*[\\'\"]?([^,}\\n]+)`));
      if (match?.[1]) return match[1].replace(/[\\'\"]+$/g, '').trim();
    }
    return undefined;
  };
  const total = read(['total', 'total_checks', 'total_requirements', 'total_clauses', 'total_items']);
  const passed = read(['passed', 'compliant', 'fully_responded', 'answered']);
  const failed = read(['failed', 'non_compliant', 'missing']);
  const warning = read(['warning', 'warnings']);
  if (total) {
    const parts = [`共 ${total} 项`];
    if (passed) parts.push(`通过 ${passed}`);
    if (failed) parts.push(`未通过/缺失 ${failed}`);
    if (warning) parts.push(`警告 ${warning}`);
    return parts.join('，');
  }
  const fit = read(['fit_score', 'score']);
  if (fit) return `匹配度评分：${fit}`;
  const note = read(['analysis_note', 'detail', 'reason', 'message']);
  if (note) return note.length > 220 ? `${note.slice(0, 220)}…` : note;
  return text.length > 220 ? `${text.slice(0, 220)}…` : text;
}

export function renderEvidenceItem(item: Record<string, unknown> | string, i: number) {
  if (typeof item === 'string') {
    const summary = summarizeString(item);
    const failed = /['\"](?:failed|non_compliant|missing)['\"]\s*:\s*[1-9]/.test(item);
    return <li key={i} style={{ fontSize: 12, color: '#334155', marginBottom: 4 }}><span style={{ color: failed ? '#b91c1c' : '#15803d', fontWeight: 700, marginRight: 6 }}>{failed ? '✗' : '✓'}</span>{summary}</li>;
  }
  const check = pickString(item, ['check', 'name', 'rule_id', 'id', 'title']) || `证据 ${i + 1}`;
  const summary = summarizeObject(item);
  const passed = item.passed === true || item.status === 'pass' || item.status === 'passed'
    || (Number(item.failed ?? 0) === 0 && Number(item.non_compliant ?? 0) === 0 && Number(item.missing ?? 0) === 0);
  const mark = item.passed === false || item.status === 'fail' || item.status === 'failed'
    || Number(item.failed ?? 0) > 0 || Number(item.non_compliant ?? 0) > 0 || Number(item.missing ?? 0) > 0 ? '✗' : '✓';
  return (
    <li key={i} style={{ fontSize: 12, color: '#334155', marginBottom: 4 }}>
      <span style={{ color: passed ? '#15803d' : '#b45309', fontWeight: 700, marginRight: 6 }}>{mark}</span>
      <span style={{ fontWeight: 600 }}>{check}</span> — {summary}
    </li>
  );
}

export function renderRiskItem(item: Record<string, unknown> | string, i: number) {
  if (typeof item === 'string') {
    const summary = summarizeString(item);
    return (
      <li key={i} style={{ fontSize: 12, color: '#334155', marginBottom: 4 }}>
        <span style={badgeStyle('#64748b', '#f1f5f9')}>风险</span> <span style={{ marginLeft: 6 }}>{summary}</span>
      </li>
    );
  }
  const severity = (pickString(item, ['severity', 'level']) || '').toLowerCase();
  const meta = SEVERITY_META[severity] || { label: severity || '风险', color: '#64748b', bg: '#f1f5f9' };
  const desc = summarizeObject(item);
  return (
    <li key={i} style={{ fontSize: 12, color: '#334155', marginBottom: 4, display: 'flex', alignItems: 'baseline', gap: 6 }}>
      <span style={badgeStyle(meta.color, meta.bg)}>{meta.label}</span>
      <span>{desc}</span>
    </li>
  );
}

export default function DecisionPackageCard({ pkg }: { pkg: GraphDecisionPackage | null }) {
  const level = levelMeta(pkg?.level);
  const evidence = pkg?.evidence || [];
  const risks = pkg?.risks || [];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12 }}>
      <div style={cardStyle}>
        <div style={sectionTitleStyle}>建议定级</div>
        <span style={badgeStyle(level.color, level.bg)}>{level.label}</span>
      </div>
      <div style={cardStyle}>
        <div style={sectionTitleStyle}>理由</div>
        <p style={{ fontSize: 12, color: '#334155', margin: 0, whiteSpace: 'pre-wrap' }}>{pkg?.rationale || '—'}</p>
      </div>
      <div style={cardStyle}>
        <div style={sectionTitleStyle}>证据列表（{evidence.length}）</div>
        {evidence.length === 0 ? (
          <p style={{ fontSize: 12, color: '#94a3b8', margin: 0 }}>无</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 16 }}>{evidence.map(renderEvidenceItem)}</ul>
        )}
      </div>
      <div style={cardStyle}>
        <div style={sectionTitleStyle}>风险清单（{risks.length}）</div>
        {risks.length === 0 ? (
          <p style={{ fontSize: 12, color: '#94a3b8', margin: 0 }}>无</p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 16, listStyle: 'none' }}>{risks.map(renderRiskItem)}</ul>
        )}
      </div>
    </div>
  );
}
