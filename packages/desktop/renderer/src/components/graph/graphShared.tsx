import type { CSSProperties } from 'react';
import type { GraphRunStatus } from '../../services/api';

// ── 图运行监视器共用样式/工具 ──

export const RUN_STATUS_META: Record<GraphRunStatus, { label: string; color: string; bg: string }> = {
  running: { label: '运行中', color: '#1d4ed8', bg: '#dbeafe' },
  pending_decision: { label: '需要人工确认', color: '#92400e', bg: '#fef3c7' },
  finalized: { label: '已定案', color: '#15803d', bg: '#dcfce7' },
  failed: { label: '失败', color: '#b91c1c', bg: '#fee2e2' },
};

export const LEVEL_META: Record<string, { label: string; color: string; bg: string }> = {
  BID: { label: 'BID 投标', color: '#15803d', bg: '#dcfce7' },
  CAUTION: { label: 'CAUTION 谨慎', color: '#b45309', bg: '#fef3c7' },
  NO_BID: { label: 'NO_BID 放弃', color: '#b91c1c', bg: '#fee2e2' },
};

export const NODE_STATUS_META: Record<string, { color: string; bg: string; label: string }> = {
  done: { color: '#15803d', bg: '#dcfce7', label: '完成' },
  truncated: { color: '#b45309', bg: '#fef3c7', label: '截断' },
  pending: { color: '#64748b', bg: '#f1f5f9', label: '等待' },
  skipped: { color: '#64748b', bg: '#f1f5f9', label: '跳过' },
  failed: { color: '#b91c1c', bg: '#fee2e2', label: '失败' },
  running: { color: '#1d4ed8', bg: '#dbeafe', label: '运行中' },
};

export function levelMeta(level?: string | null) {
  if (!level) return { label: '—', color: '#64748b', bg: '#f1f5f9' };
  return LEVEL_META[level] || { label: level, color: '#334155', bg: '#f1f5f9' };
}

export function formatDuration(ms?: number) {
  if (ms == null) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms.toFixed(1)}ms`;
}

export function formatToken(n?: number) {
  if (n == null) return '—';
  return n.toLocaleString('en-US');
}

/** P-D3 脉冲高亮动画（决策门挂起时使用）。全组件只注入一次。 */
export function GraphMonitorStyles() {
  return (
    <style>{`
      @keyframes pd3-gate-pulse {
        0%, 100% { box-shadow: 0 0 0 0 rgba(217, 119, 6, 0.55); }
        50% { box-shadow: 0 0 0 8px rgba(217, 119, 6, 0); }
      }
    `}</style>
  );
}

export const cardStyle: CSSProperties = {
  background: '#fff',
  border: '1px solid #e2e8f0',
  borderRadius: 12,
  padding: 16,
};

export const sectionTitleStyle: CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: '#0f172a',
  marginBottom: 10,
};

export const badgeStyle = (color: string, bg: string): CSSProperties => ({
  display: 'inline-flex',
  alignItems: 'center',
  padding: '2px 10px',
  borderRadius: 999,
  fontSize: 11,
  fontWeight: 700,
  color,
  background: bg,
});
