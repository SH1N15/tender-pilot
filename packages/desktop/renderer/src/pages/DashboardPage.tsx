import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  FileSearch, PenTool, ShieldCheck, FileText, Plus, ArrowRight,
  CheckCircle2, Clock, Newspaper, FolderOpen,
  Zap, Activity, ChevronDown, Lightbulb, AlertCircle, ShieldAlert, FileCheck,
  Trash2, AlertTriangle, X,
} from 'lucide-react';
import { projectApi, type Project } from '../services/api';
import { useAppStore } from '../stores/appStore';

const pipelineSteps = [
  { path: '/interpret', icon: FileSearch, label: '招标解读', color: '#3b82f6', bg: '#eff6ff',
    desc: '上传招标文件，AI提取关键信息',
    input: '招标文件 (PDF/DOCX/TXT)',
    output: '15维度解读报告、评分矩阵、风险预警',
    rules: ['支持自动解析表格/图片', '评分标准自动映射', '强制性条款自动标注'] },
  { path: '/generate', icon: PenTool, label: '投标生成', color: '#059669', bg: '#ecfdf5',
    desc: '大纲编辑→AI生成正文',
    input: '解读结果 + 知识库资料',
    output: '完整投标文件正文、配图',
    rules: ['大纲可拖拽编辑调整', '闸门审核通过后进入下一步', 'AI配图自动去水印'] },
  { path: '/check', icon: ShieldCheck, label: '投标检查', color: '#d97706', bg: '#fffbeb',
    desc: '21项全面检查审核',
    input: '生成的投标文件',
    output: '检查报告 (合规/废标/资质/查重)',
    rules: ['21项检查可单独或批量运行', '支持上传已有标书直接检查', '★▲参数逐条响应对照'] },
  { path: '/format', icon: FileText, label: '文档输出', color: '#475569', bg: '#f8fafc',
    desc: '一键排版→PDF导出',
    input: '检查通过的投标文件',
    output: '格式化DOCX/PDF、修订痕迹',
    rules: ['4种模式：排版/检查/对比/美化', '7种标题编号格式', '60+排版配置项'] },
];

const quickActions = [
  { path: '/interpret', icon: FileSearch, label: '上传招标文件', desc: '开始解读', color: '#3b82f6', bg: '#eff6ff' },
  { path: '/check', icon: ShieldAlert, label: '上传标书检查', desc: '快速检查', color: '#d97706', bg: '#fffbeb' },
  { path: '/format', icon: FileCheck, label: '上传文档排版', desc: '一键排版', color: '#475569', bg: '#f8fafc' },
  { path: '/news', icon: Newspaper, label: '浏览今日商机', desc: '热点资讯', color: '#059669', bg: '#ecfdf5' },
];

const statusToStep: Record<string, number> = {
  created: 0, interpreting: 0, analyzing: 0,
  outlining: 1, generating: 1,
  checking: 2, formatting: 3,
  completed: 4, archived: 4,
};

const statusMap: Record<string, { label: string; color: string }> = {
  created: { label: '已创建', color: '#6b7280' },
  interpreting: { label: '解读中', color: '#3b82f6' },
  analyzing: { label: '分析中', color: '#3b82f6' },
  outlining: { label: '大纲中', color: '#059669' },
  generating: { label: '生成中', color: '#059669' },
  checking: { label: '检查中', color: '#d97706' },
  formatting: { label: '排版中', color: '#475569' },
  completed: { label: '已完成', color: '#059669' },
  archived: { label: '已归档', color: '#6b7280' },
};

export default function DashboardPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  // Worker K：删除项目（二次确认对话框 + toast）
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const { setCurrentProject, currentProjectId } = useAppStore();
  const navigate = useNavigate();

  useEffect(() => { loadProjects(); }, []);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 5000);
    return () => clearTimeout(t);
  }, [toast]);

  const loadProjects = async (): Promise<void> => {
    setLoading(true);
    let lastError: unknown = null;
    for (let attempt = 0; attempt <= 5; attempt += 1) {
      try {
        const res = await projectApi.list();
        setProjects(res.data.projects || []);
        setLoading(false);
        return;
      } catch (e) {
        lastError = e;
        if (attempt < 5) await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    console.error('加载项目列表失败', lastError);
    setLoading(false);
  };

  const handleCreateProject = async () => {
    if (!newProjectName.trim()) return;
    try {
      const res = await projectApi.create(newProjectName.trim());
      setNewProjectName('');
      setShowCreate(false);
      await loadProjects();
      const newId = res.data?.project_id || res.data?.id;
      if (newId) {
        setCurrentProject(newId);
        navigate(`/graph?project_id=${encodeURIComponent(newId)}`);
      }
    } catch (e) {
      console.error('创建项目失败', e);
    }
  };

  const currentProject = projects.find(p => p.id === currentProjectId);
  const currentStep = currentProject ? (statusToStep[currentProject.status] ?? 0) : 0;

  const openDeleteDialog = (project: Project) => {
    setDeleteTarget(project);
    setDeleteArmed(false);
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    if (!deleteArmed) {
      // 防呆：第一次点击只武装按钮，第二次点击才真正执行
      setDeleteArmed(true);
      return;
    }
    setDeleting(true);
    try {
      const res = await projectApi.remove(deleteTarget.id);
      const deletedTables = Object.values(res.data?.deleted ?? {}).reduce(
        (acc, v) => acc + (typeof v === 'number' ? v : 0), 0
      );
      if (currentProjectId === deleteTarget.id) setCurrentProject(null);
      await loadProjects();
      setDeleteTarget(null);
      setToast({ kind: 'ok', text: `项目「${deleteTarget.name}」已删除（级联清理 ${deletedTables} 条数据）` });
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      setToast({ kind: 'err', text: `删除失败：${typeof detail === 'string' ? detail : '请稍后重试'}` });
    } finally {
      setDeleting(false);
      setDeleteArmed(false);
    }
  };

  const totalProjects = projects.length;
  const completedProjects = projects.filter(p => p.status === 'completed' || p.status === 'archived').length;
  const inProgressProjects = projects.filter(p => !['completed', 'archived', 'created'].includes(p.status)).length;
  const pendingProjects = projects.filter(p => p.status === 'created').length;

  return (
    <div className="page-fade-in">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
        <div>
          <h2 style={{ fontSize: '22px', fontWeight: 700, color: '#0f172a' }}>工作台</h2>
          <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginTop: '2px' }}>
            项目与总图运行状态
          </p>
        </div>
        <button
          onClick={() => { setShowCreate(true); setNewProjectName(''); }}
          style={{
            padding: '8px 18px', background: 'var(--color-primary)', color: 'white',
            border: 'none', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 600,
            display: 'flex', alignItems: 'center', gap: '6px',
            boxShadow: '0 2px 8px rgba(26,86,219,0.25)',
          }}
        >
          <Plus size={15} /> 新建项目
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '12px', marginBottom: '18px' }}>
        {[
          { icon: FolderOpen, label: '总项目', value: totalProjects, color: '#1a56db', bg: '#eff6ff' },
          { icon: Activity, label: '进行中', value: inProgressProjects, color: '#059669', bg: '#ecfdf5' },
          { icon: Clock, label: '待启动', value: pendingProjects, color: '#d97706', bg: '#fffbeb' },
          { icon: CheckCircle2, label: '已完成', value: completedProjects, color: '#475569', bg: '#f8fafc' },
        ].map(stat => (
          <div key={stat.label} style={{
            background: 'var(--color-surface)', borderRadius: '12px', padding: '14px 16px',
            border: '1px solid var(--color-border)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
              <div style={{
                width: '30px', height: '30px', borderRadius: '8px', background: stat.bg,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
                <stat.icon size={15} color={stat.color} />
              </div>
              <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{stat.label}</span>
            </div>
            <div style={{ fontSize: '22px', fontWeight: 700, color: '#0f172a', lineHeight: 1 }}>{stat.value}</div>
          </div>
        ))}
      </div>

      <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '18px 20px', marginBottom: '18px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '16px', flexWrap: 'wrap' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
            <Activity size={16} color="#1a56db" />
            <h3 style={{ fontSize: '15px', fontWeight: 700, color: '#0f172a', margin: 0 }}>全链路运行</h3>
          </div>
          <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: 0 }}>一次发起，统一查看四段图节点、耗时与人工决策门。</p>
        </div>
        <button onClick={() => navigate('/graph')} style={{ padding: '8px 14px', background: '#1a56db', color: '#fff', border: 0, borderRadius: '7px', cursor: 'pointer', fontSize: '12px', fontWeight: 600, display: 'inline-flex', alignItems: 'center', gap: '5px' }}>
          打开总图工作台 <ArrowRight size={13} />
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '18px' }}>
        <div style={{
          background: 'var(--color-surface)', borderRadius: '14px', padding: '18px',
          border: '1px solid var(--color-border)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
            <Zap size={15} color="#1a56db" />
            <h3 style={{ fontSize: '13px', fontWeight: 600, color: '#0f172a' }}>快捷入口</h3>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
            {quickActions.map(action => (
              <div
                key={action.path}
                onClick={() => navigate(action.path)}
                style={{
                  padding: '10px', borderRadius: '8px', background: action.bg,
                  cursor: 'pointer', transition: 'all 0.15s', border: '1px solid transparent',
                }}
                onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.borderColor = action.color; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.borderColor = 'transparent'; }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '2px' }}>
                  <action.icon size={14} color={action.color} />
                  <span style={{ fontSize: '12px', fontWeight: 600, color: '#0f172a' }}>{action.label}</span>
                </div>
                <span style={{ fontSize: '10px', color: 'var(--color-text-secondary)' }}>{action.desc}</span>
              </div>
            ))}
          </div>
        </div>

        <div style={{
          background: 'var(--color-surface)', borderRadius: '14px', padding: '18px',
          border: '1px solid var(--color-border)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' }}>
            <FolderOpen size={15} color="#1a56db" />
            <h3 style={{ fontSize: '13px', fontWeight: 600, color: '#0f172a' }}>项目列表</h3>
            <span style={{ fontSize: '10px', color: 'var(--color-text-secondary)', background: '#f1f5f9', padding: '1px 6px', borderRadius: '8px' }}>
              {totalProjects} 个
            </span>
          </div>

          {showCreate && (
            <div style={{
              display: 'flex', gap: '6px', alignItems: 'center',
              padding: '8px 10px', marginBottom: '8px',
              background: '#f0f7ff', borderRadius: '8px', border: '1px dashed #3b82f6',
            }}>
              <Plus size={14} color="#3b82f6" style={{ flexShrink: 0 }} />
              <input
                type="text"
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                placeholder="输入项目名称"
                style={{ flex: 1, padding: '5px 10px', border: '1px solid #bfdbfe', borderRadius: '5px', fontSize: '12px', background: 'white' }}
                onKeyDown={(e) => e.key === 'Enter' && handleCreateProject()}
                autoFocus
              />
              <button onClick={handleCreateProject} style={{
                padding: '5px 12px', background: '#059669', color: 'white', border: 'none',
                borderRadius: '5px', cursor: 'pointer', fontSize: '11px', fontWeight: 500,
              }}>确认</button>
              <button onClick={() => { setShowCreate(false); setNewProjectName(''); }} style={{
                padding: '5px 10px', background: 'white', color: 'var(--color-text)',
                border: '1px solid var(--color-border)', borderRadius: '5px', cursor: 'pointer', fontSize: '11px',
              }}>取消</button>
            </div>
          )}

          {loading ? (
            <p style={{ color: 'var(--color-text-secondary)', textAlign: 'center', padding: '20px', fontSize: '12px' }}>加载中...</p>
          ) : projects.length === 0 && !showCreate ? (
            <div style={{ textAlign: 'center', padding: '24px' }}>
              <div style={{
                width: '40px', height: '40px', borderRadius: '10px', background: '#eff6ff',
                display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 8px',
              }}>
                <Zap size={18} color="#1a56db" />
              </div>
              <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginBottom: '10px' }}>
                暂无项目，点击新建
              </p>
              <button
                onClick={() => { setShowCreate(true); setNewProjectName(''); }}
                style={{ padding: '5px 14px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '12px', fontWeight: 500, display: 'inline-flex', alignItems: 'center', gap: '4px' }}
              >
                <Plus size={12} /> 创建项目
              </button>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '240px', overflowY: 'auto' }}>
              {projects.map((project) => {
                const st = statusMap[project.status] || { label: project.status, color: '#6b7280' };
                const pStep = statusToStep[project.status] ?? 0;
                const isActive = project.id === currentProjectId;
                const stepIdx = Math.min(pStep, 3);
                const StepIcon = pipelineSteps[stepIdx].icon;
                const stepColor = pipelineSteps[stepIdx].color;

                return (
                  <div
                    key={project.id}
                    style={{
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      padding: '8px 10px',
                      border: `1px solid ${isActive ? 'var(--color-primary)' : 'var(--color-border)'}`,
                      borderRadius: '8px', cursor: 'pointer',
                      background: isActive ? '#eff6ff' : 'transparent',
                      transition: 'all 0.15s',
                    }}
                    onClick={() => setCurrentProject(project.id)}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 1, minWidth: 0 }}>
                      <div style={{
                        width: '28px', height: '28px', borderRadius: '6px',
                        background: isActive ? 'var(--color-primary)' : pipelineSteps[stepIdx].bg,
                        display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                      }}>
                        {isActive ? <FolderOpen size={12} color="white" /> : <StepIcon size={12} color={stepColor} />}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: '12px', fontWeight: 500, color: '#0f172a', display: 'flex', alignItems: 'center', gap: '4px' }}>
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{project.name}</span>
                          {isActive && (
                            <span style={{ fontSize: '8px', padding: '0px 4px', borderRadius: '3px', background: 'var(--color-primary)', color: 'white', fontWeight: 600, flexShrink: 0 }}>
                              当前
                            </span>
                          )}
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginTop: '1px' }}>
                          {pipelineSteps.map((s, i) => (
                            <div key={i} title={s.label} style={{
                              width: pStep > i ? '7px' : pStep === i ? '7px' : '4px',
                              height: '4px', borderRadius: '2px',
                              background: pStep >= i ? s.color : '#e2e8f0',
                              opacity: pStep >= i ? 1 : 0.4,
                            }} />
                          ))}
                        </div>
                      </div>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
                      <span style={{ fontSize: '9px', padding: '1px 5px', borderRadius: '4px', fontWeight: 500, background: `${st.color}10`, color: st.color }}>
                        {st.label}
                      </span>
                      <button
                        onClick={(e) => { e.stopPropagation(); setCurrentProject(project.id); navigate(`/graph?project_id=${encodeURIComponent(project.id)}`); }}
                        style={{
                          padding: '3px 8px', background: stepColor, color: 'white',
                          border: 'none', borderRadius: '4px', cursor: 'pointer',
                          fontSize: '10px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '2px',
                        }}
                      >
                        进入 <ArrowRight size={9} />
                      </button>
                      <button
                        title="删除项目"
                        aria-label={`删除项目 ${project.name}`}
                        onClick={(e) => { e.stopPropagation(); openDeleteDialog(project); }}
                        style={{
                          padding: '3px', background: 'transparent', color: '#94a3b8',
                          border: 'none', borderRadius: '4px', cursor: 'pointer',
                          display: 'flex', alignItems: 'center',
                        }}
                        onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.color = '#dc2626'; }}
                        onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.color = '#94a3b8'; }}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {deleteTarget && (
        <div
          onClick={() => { if (!deleting) setDeleteTarget(null); }}
          style={{
            position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: 'var(--color-surface)', borderRadius: '14px', padding: '22px 24px',
              width: '380px', maxWidth: '90vw', boxShadow: '0 12px 40px rgba(15,23,42,0.25)',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
              <div style={{
                width: '34px', height: '34px', borderRadius: '9px', background: '#fef2f2',
                display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
              }}>
                <AlertTriangle size={17} color="#dc2626" />
              </div>
              <h3 style={{ fontSize: '15px', fontWeight: 700, color: '#0f172a', margin: 0, flex: 1 }}>
                删除项目
              </h3>
              <button
                onClick={() => setDeleteTarget(null)}
                aria-label="关闭"
                style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: '2px', color: '#94a3b8', display: 'flex' }}
              >
                <X size={15} />
              </button>
            </div>
            <p style={{ fontSize: '12.5px', color: 'var(--color-text)', lineHeight: 1.7, margin: '0 0 6px' }}>
              将永久删除项目
              <b style={{ color: '#dc2626' }}>「{deleteTarget.name}」</b>
              及其全部关联数据（章节、文档、解读、大纲、检查报告、图运行 checkpoint、长期记忆与上传文件）。
            </p>
            <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: '0 0 16px' }}>
              此操作不可恢复。{deleteArmed ? '请再次点击「确认删除」执行。' : '请点击「确认删除」，然后再次点击以二次确认。'}
            </p>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
              <button
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
                style={{
                  padding: '7px 14px', background: 'white', color: 'var(--color-text)',
                  border: '1px solid var(--color-border)', borderRadius: '7px', cursor: 'pointer', fontSize: '12px',
                }}
              >
                取消
              </button>
              <button
                onClick={handleConfirmDelete}
                disabled={deleting}
                style={{
                  padding: '7px 14px', background: deleteArmed ? '#dc2626' : '#f87171', color: 'white',
                  border: 'none', borderRadius: '7px', cursor: deleting ? 'wait' : 'pointer',
                  fontSize: '12px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '5px',
                  opacity: deleting ? 0.7 : 1,
                }}
              >
                <Trash2 size={12} />
                {deleting ? '删除中...' : deleteArmed ? '确认删除（第二次点击执行）' : '确认删除'}
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div
          style={{
            position: 'fixed', bottom: '24px', left: '50%', transform: 'translateX(-50%)',
            padding: '9px 16px', borderRadius: '8px', fontSize: '12.5px', fontWeight: 500,
            background: toast.kind === 'ok' ? '#059669' : '#dc2626', color: 'white',
            boxShadow: '0 6px 20px rgba(15,23,42,0.25)', zIndex: 1100,
            display: 'flex', alignItems: 'center', gap: '6px', maxWidth: '80vw',
          }}
        >
          {toast.kind === 'ok' ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{toast.text}</span>
        </div>
      )}
    </div>
  );
}
