import { useState, useEffect } from 'react';
import { Activity, TrendingUp, Clock, Coins, AlertCircle, RefreshCw, Database } from 'lucide-react';
import { monitorApi } from '../services/api';

interface Metrics {
  overall?: { count: number; ok: number; error: number; success_rate: number; p50_ms: number; p95_ms: number; errors: Record<string, number>; total_tokens: number };
  by_kind?: Record<string, { count: number; success_rate: number; p50_ms: number; p95_ms: number; total_tokens: number; errors: Record<string, number> }>;
}

export default function MonitorPage() {
  const [metrics, setMetrics] = useState<Metrics>({});
  const [spans, setSpans] = useState<Array<Record<string, unknown>>>([]);
  const [status, setStatus] = useState<Record<string, unknown>>({});
  const [windowMinutes, setWindowMinutes] = useState(60);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [m, s, sp] = await Promise.all([
        monitorApi.metrics(windowMinutes),
        monitorApi.status(),
        monitorApi.spans(30),
      ]);
      setMetrics(m.data as Metrics);
      setStatus(s.data);
      setSpans((sp.data as { spans: Array<Record<string, unknown>> }).spans || []);
    } catch (e) {
      console.error('加载监控数据失败', e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [windowMinutes]);

  const overall = metrics.overall || { count: 0, ok: 0, error: 0, success_rate: 1, p50_ms: 0, p95_ms: 0, errors: {}, total_tokens: 0 };
  const byKind = metrics.by_kind || {};

  const cards = [
    { label: '总调用', value: String(overall.count), icon: Activity, color: '#1a56db' },
    { label: '成功率', value: `${(overall.success_rate * 100).toFixed(1)}%`, icon: TrendingUp, color: '#059669' },
    { label: 'P50 延迟', value: `${overall.p50_ms.toFixed(1)}ms`, icon: Clock, color: '#d97706' },
    { label: 'P95 延迟', value: `${overall.p95_ms.toFixed(1)}ms`, icon: Clock, color: '#dc2626' },
    { label: 'Token 消耗', value: String(overall.total_tokens), icon: Coins, color: '#7c3aed' },
    { label: '错误数', value: String(overall.error), icon: AlertCircle, color: '#dc2626' },
  ];

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>运行监控</h2>
          <p style={{ fontSize: 12, color: '#64748b', margin: '4px 0 0' }}>
            轻量 Tracing：FastAPI / Agent / LLM / Tool / Skill / OCR / A2A / AG-UI span（不记录 API Key 与完整投标正文）
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <select value={windowMinutes} onChange={(e) => setWindowMinutes(Number(e.target.value))} style={{ padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 12 }}>
            <option value={15}>近 15 分钟</option>
            <option value={60}>近 1 小时</option>
            <option value={1440}>近 24 小时</option>
          </select>
          <button onClick={load} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '7px 12px', borderRadius: 8, border: '1px solid #e2e8f0', background: '#fff', fontSize: 12, cursor: 'pointer' }}>
            <RefreshCw size={13} className={loading ? 'spin' : ''} /> 刷新
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
        {cards.map((card) => (
          <div key={card.label} style={{ flex: 1, minWidth: 130, background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: '14px 16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#64748b' }}>
              <card.icon size={15} color={card.color} /> {card.label}
            </div>
            <div style={{ fontSize: 22, fontWeight: 700, color: card.color, marginTop: 6 }}>{card.value}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        {/* 按 kind 分类 */}
        <div style={{ flex: 1, minWidth: 320, background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Database size={14} color="#475569" /> 按类型统计
          </div>
          <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ color: '#64748b', textAlign: 'left' }}>
                <th style={{ padding: '6px 4px' }}>类型</th><th>次数</th><th>成功率</th><th>P50</th><th>P95</th><th>Token</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(byKind).map(([kind, v]) => (
                <tr key={kind} style={{ borderTop: '1px solid #f1f5f9' }}>
                  <td style={{ padding: '6px 4px', fontWeight: 600 }}>{kind}</td>
                  <td>{v.count}</td>
                  <td>{(v.success_rate * 100).toFixed(0)}%</td>
                  <td>{v.p50_ms.toFixed(1)}ms</td>
                  <td>{v.p95_ms.toFixed(1)}ms</td>
                  <td>{v.total_tokens}</td>
                </tr>
              ))}
              {Object.keys(byKind).length === 0 && <tr><td colSpan={6} style={{ color: '#94a3b8', padding: 8 }}>暂无数据</td></tr>}
            </tbody>
          </table>
          <div style={{ marginTop: 10, fontSize: 11, color: '#64748b' }}>
            Tracing 状态: {String((status as { enabled?: boolean }).enabled ?? '')} · 内存 span: {String((status as { memory_spans?: number }).memory_spans ?? 0)} · OTLP: {String(Boolean((status as { otlp_endpoint?: boolean }).otlp_endpoint))}
          </div>
        </div>

        {/* 最近 span */}
        <div style={{ flex: 1.2, minWidth: 360, background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>最近运行 / Trace</div>
          {spans.map((s) => (
            <div key={String(s.span_id)} style={{ borderBottom: '1px solid #f1f5f9', padding: '6px 0', display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
              <span style={{ background: s.status === 'ok' ? '#ecfdf5' : '#fef2f2', color: s.status === 'ok' ? '#059669' : '#dc2626', padding: '2px 6px', borderRadius: 6, fontSize: 10, fontWeight: 600 }}>{String(s.status)}</span>
              <span style={{ fontWeight: 600, color: '#0f172a', flex: 1 }}>{String(s.name)}</span>
              <span style={{ color: '#94a3b8' }}>{String(s.kind)}</span>
              <span style={{ color: '#64748b' }}>{Number(s.duration_ms).toFixed(1)}ms</span>
              {s.error_type ? <span style={{ color: '#dc2626', fontSize: 10 }}>{String(s.error_type)}</span> : null}
            </div>
          ))}
          {spans.length === 0 && <p style={{ fontSize: 12, color: '#94a3b8' }}>暂无 span。运行一次 Agent / 请求即可看到数据。</p>}
        </div>
      </div>
    </div>
  );
}
