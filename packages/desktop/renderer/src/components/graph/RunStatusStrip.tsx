// G-5 T1：业务子图运行状态条（复用 graphShared 的状态元数据与样式体系）。
// 四个业务页（解读/生成/检查/资格预审）共用：run 状态徽标 + 节点状态行 + HITL 审批门高亮。
// 各业务图节点名不同，node_status 按快照实际键渲染（未知节点做兼容追加）。

import { useEffect, useRef, useState } from 'react';
import { Loader2, Network, ShieldQuestion } from 'lucide-react';
import type { BizGraphRunDetail } from '../../services/api';
import { badgeStyle, GraphMonitorStyles, NODE_STATUS_META, RUN_STATUS_META } from './graphShared';

/** 各业务图节点展示顺序与中文名（未列出的 key 按快照出现顺序追加在最后） */
const NODE_LABELS: Record<string, string> = {
  // 解读子图
  interpret_dispatch: '调度',
  interpret_react: 'AI 解读',
  interpret_matrix: '评分矩阵',
  interpret_risk: '风险预警',
  interpret_finalize: '终态',
  // 检查图
  check_dispatch: '调度',
  check_execute: '检查执行',
  check_report: '报告生成',
  check_export: '导出',
  // 章节生成图
  outline_node: '大纲生成',
  chapter_nodes: '逐章生成',
  grounding_gate: 'Grounding 门',
  // 资格预审图
  qualification_extract: '资格要求提取',
  qualification_match: '凭证比对',
  qualification_hitl_gate: 'HITL 审批门',
  qualification_finalize: '终态',
};

/** 运行状态徽标口径（业务图状态 ∈ running/waiting_human/pending_decision/completed/finalized/failed） */
const BIZ_STATUS_META: Record<string, { label: string; color: string; bg: string }> = {
  ...RUN_STATUS_META,
  waiting_human: { label: '待人工审批', color: '#92400e', bg: '#fef3c7' },
  completed: { label: '已完成', color: '#15803d', bg: '#dcfce7' },
};

export default function RunStatusStrip({
  detail,
  loading,
  error,
  title = '图运行状态（LangGraph 子图）',
}: {
  detail: BizGraphRunDetail | null;
  loading: boolean;
  error: string | null;
  title?: string;
}) {
  const statusMeta = detail ? BIZ_STATUS_META[detail.status] || { label: detail.status, color: '#334155', bg: '#f1f5f9' } : null;
  const nodeStatus = detail?.snapshot?.node_status || {};
  const pendingGate = detail?.snapshot?.pending_gate || null;
  const gateNamespace = detail?.snapshot?.pending_gate_namespace || pendingGate;
  const nodeTimings = detail?.snapshot?.node_timings || {};

  // 未知节点 key 兼容：按快照顺序追加
  const orderedKeys = Object.keys(NODE_LABELS).filter((k) => k in nodeStatus);
  const extraKeys = Object.keys(nodeStatus).filter((k) => !(k in NODE_LABELS));
  const allKeys = [...orderedKeys, ...extraKeys];

  return (
    <div style={{ border: '1px solid #bfdbfe', borderRadius: 10, background: '#f8fbff', padding: '16px 18px', marginBottom: 16 }}>
      <GraphMonitorStyles />
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
        <Network size={15} color="#1a56db" />
        <span style={{ fontSize: 14, fontWeight: 700, color: '#0f172a' }}>{title}</span>
        {statusMeta && <span style={badgeStyle(statusMeta.color, statusMeta.bg)}>{statusMeta.label}</span>}
        {detail && (
          <span style={{ fontSize: 11, color: '#94a3b8', fontFamily: 'monospace' }}>{detail.run_id}</span>
        )}
        {loading && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, color: '#64748b' }}>
            <Loader2 size={11} className="spin" /> 轮询中…
          </span>
        )}
      </div>

      {error && (
        <p style={{ fontSize: 12, color: '#b91c1c', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 8, padding: '6px 10px', margin: 0 }}>
          {error}
        </p>
      )}

      {detail?.error && !error && (
        <p style={{ fontSize: 12, color: '#b91c1c', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 8, padding: '6px 10px', margin: 0 }}>
          运行错误：{detail.error}
        </p>
      )}

      {allKeys.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          {allKeys.map((k, idx) => {
            const meta = NODE_STATUS_META[nodeStatus[k]] || NODE_STATUS_META.pending;
            const pulsing = pendingGate === k;
            return (
              <span key={k} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                {idx > 0 && <span style={{ color: '#cbd5e1', fontWeight: 700 }}>→</span>}
                <span
                  title={`${NODE_LABELS[k] || k} · ${meta.label}${nodeTimings[k] ? ` · ${nodeTimings[k]}ms` : ''}`}
                  style={{
                    padding: '4px 10px',
                    borderRadius: 8,
                    border: `1px solid ${meta.color}55`,
                    background: meta.bg,
                    color: meta.color,
                    fontSize: 11,
                    fontWeight: 600,
                    animation: pulsing ? 'pd3-gate-pulse 1.6s ease-in-out infinite' : undefined,
                  }}
                >
                  {NODE_LABELS[k] || k}
                  {nodeTimings[k] !== undefined && <small style={{ fontWeight: 500, opacity: 0.75 }}>{nodeTimings[k]}ms</small>}
                </span>
              </span>
            );
          })}
        </div>
      )}

      {pendingGate && (
        <p style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, color: '#b45309', fontWeight: 700, margin: '8px 0 0' }}>
          <ShieldQuestion size={12} /> 人工门挂起：{gateNamespace} / {pendingGate} —— 请在下方完成决策
        </p>
      )}
    </div>
  );
}

/** 轮询 Hook：业务子图 run 详情 3s 轮询，终态停止。 */
export function useBizGraphRunPolling(
  fetcher: (() => Promise<BizGraphRunDetail | null>) | null,
  terminalStatuses: string[] = ['completed', 'finalized', 'failed'],
) {
  const [detail, setDetail] = useState<BizGraphRunDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    if (!fetcher) {
      setDetail(null);
      setLoading(false);
      setError(null);
      return;
    }
    setLoading(true);
    const tick = async () => {
      try {
        const d = await fetcher();
        if (!aliveRef.current) return;
        setDetail(d);
        setError(null);
        if (d && terminalStatuses.includes(d.status)) {
          setLoading(false);
          return;
        }
      } catch (e) {
        if (!aliveRef.current) return;
        setError(`图运行查询失败：${(e as Error).message}`);
      }
      if (aliveRef.current) timerRef.current = setTimeout(tick, 3000);
    };
    tick();
    return () => {
      aliveRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetcher]);

  return { detail, loading, error };
}
