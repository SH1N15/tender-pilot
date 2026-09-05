// 运行列表：GET /api/graph/runs —— run_id、项目、状态、创建时间、最终定级徽标；刷新 + 进入详情。
// 生成范围在大纲生成后由主流程内的“范围确认”阶段选择。

import { useEffect, useMemo, useState } from 'react';
import { History, Loader2, Play, Plus, RefreshCw } from 'lucide-react';
import { graphApi, type GraphRunSummary, type Project } from '../../services/api';
import { badgeStyle, cardStyle, levelMeta, RUN_STATUS_META, sectionTitleStyle } from './graphShared';

export default function GraphRunList({
  runs, loading, error, projects, initialProjectId, onCreateRun, creating, onEnterRun, onRefresh,
}: {
  runs: GraphRunSummary[];
  loading: boolean;
  error: string | null;
  projects: Project[];
  initialProjectId?: string;
  onCreateRun: (projectId: string, chapterIds?: string[]) => void;
  creating: boolean;
  onEnterRun: (runId: string) => void;
  onRefresh: () => void;
}) {
  const [projectId, setProjectId] = useState(initialProjectId || '');
  const [showHistory, setShowHistory] = useState(false);

  useEffect(() => {
    setProjectId(initialProjectId || '');
  }, [initialProjectId]);

  const projectName = (id: string) => projects.find((p) => p.id === id)?.name || id;
  const latestRun = useMemo(
    () => runs.find((run) => run.is_latest !== false) || runs[0] || null,
    [runs],
  );
  const visibleRuns = useMemo(
    () => (showHistory ? runs : runs.filter((run) => run.is_latest !== false)),
    [runs, showHistory],
  );
  return (
    <div style={cardStyle}>
      <div style={{ ...sectionTitleStyle, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span>全链路运行记录</span>
        <span style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {projects.length === 1 ? (
            <span style={{ display: 'inline-flex', alignItems: 'center', padding: '6px 9px', borderRadius: 7, background: '#f8fafc', border: '1px solid #e2e8f0', color: '#475569', fontSize: 12 }}>
              项目：{projects[0].name}
            </span>
          ) : (
            <select
              id="pd3-new-run-project"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              style={{ padding: '5px 8px', borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 12 }}
            >
              <option value="">选择项目新建运行…</option>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          )}
          <button
            onClick={() => {
              if (projectId) onCreateRun(projectId);
            }}
            disabled={creating || !projectId}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '6px 10px', borderRadius: 8, background: '#1a56db', color: '#fff', border: 'none', fontSize: 12, cursor: creating || !projectId ? 'not-allowed' : 'pointer', opacity: creating || !projectId ? 0.7 : 1 }}
          >
            {creating ? <Loader2 size={12} className="spin" /> : <Plus size={12} />} 继续：开始 AI 解读
          </button>
          <button
            onClick={onRefresh}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', background: '#fff', cursor: 'pointer', fontSize: 12, color: '#475569' }}
          >
            <RefreshCw size={12} /> 刷新
          </button>
          {runs.some((run) => run.is_latest === false) && (
            <button
              onClick={() => setShowHistory((value) => !value)}
              title={showHistory ? '隐藏历史运行' : '显示历史运行'}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', background: showHistory ? '#f8fafc' : '#fff', cursor: 'pointer', fontSize: 12, color: '#475569' }}
            >
              <History size={12} /> {showHistory ? '隐藏历史' : '历史运行'}
            </button>
          )}
        </span>
      </div>

      {latestRun && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '9px 11px', marginBottom: 10, borderRadius: 8, background: '#eff6ff', border: '1px solid #bfdbfe', color: '#1e3a8a', fontSize: 12 }}>
          <span>当前项目最新运行：<strong style={{ fontFamily: 'monospace' }}>{latestRun.run_id}</strong>，状态「{RUN_STATUS_META[latestRun.status]?.label || latestRun.status}」</span>
          {latestRun.final_level && <span style={badgeStyle(levelMeta(latestRun.final_level).color, levelMeta(latestRun.final_level).bg)}>{levelMeta(latestRun.final_level).label}</span>}
        </div>
      )}

      {error && (
        <p style={{ fontSize: 12, color: '#b91c1c', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 8, padding: '8px 10px' }}>
          {error}
        </p>
      )}
      {loading && runs.length === 0 && (
        <p style={{ fontSize: 12, color: '#94a3b8', display: 'flex', alignItems: 'center', gap: 6 }}>
          <Loader2 size={12} className="spin" /> 加载中…
        </p>
      )}
      {!loading && !error && visibleRuns.length === 0 && (
        <p style={{ fontSize: 12, color: '#94a3b8' }}>还没有运行记录。准备区完成后，点击「启动全链路」一次跑完解读、资格、正文、检查和决策。</p>
      )}
      {visibleRuns.length > 0 && (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['Run ID', '项目', '状态', '创建时间', '最终定级', ''].map((h) => (
                <th key={h} style={{ textAlign: 'left', fontSize: 11, fontWeight: 600, color: '#64748b', padding: '6px 10px', borderBottom: '1px solid #e2e8f0' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRuns.map((r) => {
              const m = RUN_STATUS_META[r.status] || { label: r.status, color: '#334155', bg: '#f1f5f9' };
              const lm = levelMeta(r.final_level);
              return (
                <tr key={r.run_id} style={{ cursor: 'pointer' }} onClick={() => onEnterRun(r.run_id)}>
                  <td style={{ fontSize: 12, color: '#334155', padding: '8px 10px', borderBottom: '1px solid #f1f5f9', fontFamily: 'monospace' }}>
                    {r.run_id}
                    {r.is_latest === false && <span style={{ marginLeft: 7, fontFamily: 'inherit', fontSize: 10, color: '#64748b' }}>历史快照</span>}
                  </td>
                  <td style={{ fontSize: 12, color: '#334155', padding: '8px 10px', borderBottom: '1px solid #f1f5f9' }}>{projectName(r.project_id)}</td>
                  <td style={{ padding: '8px 10px', borderBottom: '1px solid #f1f5f9' }}>
                    <span style={badgeStyle(m.color, m.bg)}>{m.label}</span>
                  </td>
                  <td style={{ fontSize: 12, color: '#64748b', padding: '8px 10px', borderBottom: '1px solid #f1f5f9' }}>
                    {r.created_at ? new Date(Number(r.created_at) < 1e12 ? Number(r.created_at) * 1000 : Number(r.created_at)).toLocaleString('zh-CN', { hour12: false }) : '—'}
                  </td>
                  <td style={{ padding: '8px 10px', borderBottom: '1px solid #f1f5f9' }}>
                    {r.final_level ? <span style={badgeStyle(lm.color, lm.bg)}>{lm.label}</span> : <span style={{ fontSize: 12, color: '#94a3b8' }}>—</span>}
                  </td>
                  <td style={{ padding: '8px 10px', borderBottom: '1px solid #f1f5f9' }}>
                    <button
                      onClick={(e) => { e.stopPropagation(); onEnterRun(r.run_id); }}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '4px 10px', borderRadius: 6, border: '1px solid #e2e8f0', background: '#fff', cursor: 'pointer', fontSize: 11, color: '#1a56db' }}
                    >
                      <Play size={11} /> 详情
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
