// 成本面板：GET /api/graph/runs/{id}/cost —— 每节点 LLM 调用数 / token / 耗时表格 + 合计行。

import type { CSSProperties } from 'react';
import type { GraphCostReport } from '../../services/api';
import { cardStyle, formatDuration, formatToken, sectionTitleStyle } from './graphShared';

const th: CSSProperties = {
  textAlign: 'left',
  fontSize: 11,
  fontWeight: 600,
  color: '#64748b',
  padding: '6px 10px',
  borderBottom: '1px solid #e2e8f0',
};
const td: CSSProperties = {
  fontSize: 12,
  color: '#334155',
  padding: '6px 10px',
  borderBottom: '1px solid #f1f5f9',
};

const NODE_LABELS: Record<string, string> = {
  interpret: '招标解读',
  qualification: '资格核对',
  outline: '生成大纲',
  generate: '正文生成',
  check: '检查与修复',
  rule_gate: '投标建议计算',
  finalize: '整理交付结果',
};

export default function CostPanel({ cost, error }: { cost: GraphCostReport | null; error?: string | null }) {
  const nodes = Object.entries(cost?.nodes || {});
  return (
    <div style={cardStyle}>
      <div style={{ ...sectionTitleStyle, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>运行资源统计</span>
        {cost && (
          <span style={{ fontSize: 11, color: '#64748b', fontWeight: 500 }}>
            合计：LLM 调用 {formatToken(cost.total_llm_calls)} 次 · token {formatToken(cost.total_tokens)} · 耗时 {formatDuration(cost.total_duration_ms)}
          </span>
        )}
      </div>
      {error && <p style={{ fontSize: 12, color: '#d97706', margin: 0 }}>{error}</p>}
      {!error && nodes.length === 0 && <p style={{ fontSize: 12, color: '#94a3b8', margin: 0 }}>暂无成本数据</p>}
      {nodes.length > 0 && (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={th}>节点</th>
              <th style={th}>LLM 调用数</th>
              <th style={th}>文本处理量</th>
              <th style={th}>耗时</th>
            </tr>
          </thead>
          <tbody>
            {nodes.map(([name, c]) => (
              <tr key={name}>
                <td style={td}>{NODE_LABELS[name] || '处理阶段'}</td>
                <td style={td}>{c.llm_calls ?? 0}</td>
                <td style={td}>{formatToken(c.tokens)}</td>
                <td style={td}>{formatDuration(c.duration_ms)}</td>
              </tr>
            ))}
            <tr>
              <td style={{ ...td, fontWeight: 700 }}>合计</td>
              <td style={{ ...td, fontWeight: 700 }}>{cost?.total_llm_calls ?? '—'}</td>
              <td style={{ ...td, fontWeight: 700 }}>{cost ? formatToken(cost.total_tokens) : '—'}</td>
              <td style={{ ...td, fontWeight: 700 }}>{cost ? formatDuration(cost.total_duration_ms) : '—'}</td>
            </tr>
          </tbody>
        </table>
      )}
    </div>
  );
}
