// 图运行监视器容器：运行列表 ↔ 运行详情，新建运行。

import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Loader2, Network } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { graphApi, projectApi, type GraphRunSummary, type Project } from '../../services/api';
import GraphRunList from './GraphRunList';
import GraphRunDetail from './GraphRunDetail';
import ProjectPreparation from './ProjectPreparation';

export default function GraphMonitor({ projects }: { projects: Project[] }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [availableProjects, setAvailableProjects] = useState<Project[]>(projects);
  const [runs, setRuns] = useState<GraphRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createMsg, setCreateMsg] = useState<string | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState(searchParams.get('project_id') || '');
  const [preflightReady, setPreflightReady] = useState(false);

  const loadRuns = useCallback(async (): Promise<void> => {
    setLoading(true);
    let lastError: unknown = null;
    for (let attempt = 0; attempt <= 5; attempt += 1) {
      try {
        const r = await graphApi.list();
        setRuns(r.data.runs || []);
        setError(null);
        setLoading(false);
        return;
      } catch (e) {
        lastError = e;
        const err = e as { response?: { status?: number } };
        if (err.response?.status === 404 || attempt === 5) break;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    const err = lastError as { response?: { status?: number }; message?: string };
    setError(err?.response?.status === 404
      ? '后端暂无 /api/graph 路由（服务为旧代码，需重启后真机验收）'
      : `加载运行列表失败：${err?.message || '后端暂不可用'}`);
    setLoading(false);
  }, []);

  useEffect(() => { loadRuns(); }, [loadRuns]);
  useEffect(() => {
    if (projects.length) return;
    let cancelled = false;
    const loadProjects = async (attempt = 0): Promise<void> => {
      try {
        const response = await projectApi.list();
        if (!cancelled) setAvailableProjects(response.data.projects || []);
      } catch {
        if (!cancelled && attempt < 5) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          return loadProjects(attempt + 1);
        }
        if (!cancelled) setAvailableProjects([]);
      }
    };
    void loadProjects();
    return () => { cancelled = true; };
  }, [projects]);

  useEffect(() => {
    if (projects.length) setAvailableProjects(projects);
  }, [projects]);

  useEffect(() => {
    const requested = searchParams.get('project_id');
    if (requested && availableProjects.some((project) => project.id === requested)) {
      setSelectedProjectId(requested);
    } else if (!selectedProjectId && availableProjects[0]) {
      setSelectedProjectId(availableProjects[0].id);
    }
  }, [availableProjects, searchParams, selectedProjectId]);

  const selectedProject = useMemo(
    () => availableProjects.find((project) => project.id === selectedProjectId) || null,
    [availableProjects, selectedProjectId],
  );

  const selectProject = (projectId: string) => {
    setSelectedProjectId(projectId);
    setPreflightReady(false);
    if (projectId) setSearchParams({ project_id: projectId });
    else setSearchParams({});
  };

  const createRun = async (projectId: string, chapterIds?: string[]) => {
    if (creating || !projectId) return;
    setCreating(true);
    setCreateMsg(null);
    try {
      const r = await graphApi.start(projectId, chapterIds);
      setCreateMsg(
        `已创建运行 ${r.data.run_id}（${r.data.status}）${chapterIds?.length ? `，限定 ${chapterIds.length} 章` : ''}`,
      );
      await loadRuns();
      if (r.data.run_id) setSelectedRun(r.data.run_id);
    } catch (e) {
      const err = e as { response?: { status?: number; data?: { detail?: string | { reason?: string; next_action?: string } } } };
      const detail = err.response?.data?.detail;
      const readable = typeof detail === 'object' && detail
        ? `${detail.reason || '前置条件未满足'}${detail.next_action ? `。${detail.next_action}` : ''}`
        : detail || (e as Error).message;
      setCreateMsg(`创建失败${err.response?.status ? `（HTTP ${err.response.status}）` : ''}：${readable}`);
    } finally {
      setCreating(false);
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <Network size={16} color="#1a56db" />
        <span style={{ fontSize: 16, fontWeight: 700, color: '#0f172a' }}>全链路工作台</span>
        {selectedRun && (
          <button
            onClick={() => { setSelectedRun(null); loadRuns(); }}
            style={{ marginLeft: 'auto', fontSize: 12, color: '#1a56db', background: 'none', border: 'none', cursor: 'pointer' }}
          >
            ← 返回运行列表
          </button>
        )}
      </div>

      {createMsg && (
        <p style={{ fontSize: 12, color: '#b45309', background: '#fffbeb', border: '1px solid #fcd34d', borderRadius: 8, padding: '8px 10px', marginBottom: 12 }}>{createMsg}</p>
      )}

      {selectedRun ? (
        <GraphRunDetail runId={selectedRun} onBack={() => { setSelectedRun(null); loadRuns(); }} />
      ) : (
        <>
          <ProjectPreparation
            projects={availableProjects}
            projectId={selectedProjectId}
            onProjectChange={selectProject}
            onReadyChange={setPreflightReady}
          />
          {selectedProject && !preflightReady && (
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '10px 12px', marginBottom: 12, borderRadius: 9, background: '#fffbeb', border: '1px solid #fcd34d', color: '#92400e', fontSize: 12 }}>
              <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 1 }} />
              <span>当前进行到文件准备阶段。请先完成上传和解析，下一步按钮会在解析成功后出现。</span>
            </div>
          )}
          {loading && runs.length > 0 && (
            <p style={{ fontSize: 11, color: '#94a3b8', display: 'flex', alignItems: 'center', gap: 4, marginBottom: 8 }}>
              <Loader2 size={11} className="spin" /> 刷新中…
            </p>
          )}
          {selectedProject && preflightReady ? (
            <GraphRunList
              runs={runs.filter((run) => run.project_id === selectedProject.id)}
              loading={loading}
              error={error}
              projects={[selectedProject]}
              initialProjectId={selectedProject.id}
              onCreateRun={createRun}
              creating={creating}
              onEnterRun={setSelectedRun}
              onRefresh={loadRuns}
            />
          ) : selectedProject ? (
            <div style={{ background: '#fff', border: '1px solid #e2e8f0', borderRadius: 10, padding: 14, color: '#64748b', fontSize: 12 }}>
              完成上传和解析后，这里会出现“继续：开始 AI 解读”按钮和该项目的运行记录。
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
