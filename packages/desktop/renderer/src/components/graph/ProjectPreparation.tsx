import { useEffect, useState } from 'react';
import { CheckCircle2, FileText, Loader2, Upload, AlertTriangle } from 'lucide-react';
import { interpretApi, type Project } from '../../services/api';

interface DocumentState {
  id: string;
  file_name: string;
  file_size?: number;
  parsed: boolean;
  type?: string;
}

interface ProjectPreparationProps {
  projects: Project[];
  projectId: string;
  onProjectChange: (projectId: string) => void;
  onReadyChange: (ready: boolean) => void;
}

const formatSize = (value?: number) => {
  if (!value) return '';
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
};

export default function ProjectPreparation({ projects, projectId, onProjectChange, onReadyChange }: ProjectPreparationProps) {
  const [documents, setDocuments] = useState<DocumentState[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [parsing, setParsing] = useState(false);
  const [message, setMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);
  const [dragging, setDragging] = useState(false);

  const loadDocuments = async (id: string) => {
    if (!id) {
      setDocuments([]);
      onReadyChange(false);
      return;
    }
    setLoading(true);
    setMessage(null);
    let lastError: unknown = null;
    for (let attempt = 0; attempt <= 5; attempt += 1) {
      try {
        const response = await interpretApi.listDocuments(id);
        const next = (response.data.documents || []) as DocumentState[];
        setDocuments(next);
        // 全链路的前置输入必须是招标文件本身已解析；企业参考资料不能替代招标文件。
        onReadyChange(next.some((doc) => doc.type === 'tender' && doc.parsed));
        setLoading(false);
        return;
      } catch (error) {
        lastError = error;
        if (attempt < 5) await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    setDocuments([]);
    onReadyChange(false);
    setMessage({ kind: 'error', text: `读取项目资料失败：${(lastError as Error)?.message || '后端暂不可用'}` });
    setLoading(false);
  };

  useEffect(() => { loadDocuments(projectId); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps

  // 文档列表是准备状态的唯一事实源。补一层派生同步，避免切换项目时
  // 父页面先把状态重置为 false，而异步加载完成回调被竞态覆盖。
  useEffect(() => {
    const tender = documents.find((doc) => doc.type === 'tender');
    onReadyChange(Boolean(tender?.parsed));
  }, [documents, onReadyChange]);

  const handleFiles = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (!projectId || !list.length) return;
    setUploading(true);
    setMessage(null);
    try {
      await interpretApi.upload(projectId, list);
      await loadDocuments(projectId);
      setMessage({ kind: 'ok', text: `第 1 步已完成：上传 ${list.length} 个文件。现在继续解析文件。` });
    } catch (error) {
      setMessage({ kind: 'error', text: `上传失败：${(error as Error).message}` });
    } finally {
      setUploading(false);
    }
  };

  const parse = async () => {
    if (!projectId || parsing) return;
    setParsing(true);
    setMessage(null);
    try {
      const response = await interpretApi.parse(projectId);
      await loadDocuments(projectId);
      setMessage({
        kind: 'ok',
        text: `第 2 步已完成：解析 ${response.data?.text_length || 0} 字，识别 ${response.data?.sections_count || 0} 个章节。可以继续 AI 解读。`,
      });
    } catch (error) {
      setMessage({ kind: 'error', text: `解析失败：${(error as Error).message}` });
    } finally {
      setParsing(false);
    }
  };

  const selected = documents.find((doc) => doc.type === 'tender');
  const hasFile = Boolean(selected);
  const ready = Boolean(selected?.parsed);

  return (
    <section style={{ background: '#fff', border: '1px solid #dbe4f0', borderRadius: 12, padding: 18, marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 14, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 16, fontWeight: 700, color: '#0f172a' }}>投标全流程</div>
        </div>
        <select
          value={projectId}
          onChange={(event) => onProjectChange(event.target.value)}
          aria-label="选择项目"
          style={{ minWidth: 240, padding: '8px 10px', border: '1px solid #cbd5e1', borderRadius: 7, fontSize: 13, background: '#fff' }}
        >
          <option value="">请选择项目</option>
          {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
        </select>
      </div>

      {!projectId ? (
        <div style={{ marginTop: 16, padding: 18, border: '1px dashed #cbd5e1', borderRadius: 10, color: '#64748b', fontSize: 13 }}>
          请选择一个项目，或先回到项目列表新建项目。
        </div>
      ) : (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 10, marginTop: 16 }}>
            {[
              { label: '1. 招标文件', done: hasFile, detail: hasFile ? `${selected?.file_name} ${formatSize(selected?.file_size)}` : '尚未上传' },
              { label: '2. 解析正文', done: ready, detail: ready ? '已解析，可供 AI 使用' : hasFile ? '等待解析' : '上传后可解析' },
              { label: '3. AI 解读', done: false, detail: ready ? '下一步：开始提取招标要求' : '解析完成后继续' },
            ].map((step) => (
              <div key={step.label} style={{ border: `1px solid ${step.done ? '#a7f3d0' : '#e2e8f0'}`, background: step.done ? '#ecfdf5' : '#f8fafc', borderRadius: 9, padding: '11px 12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 700, color: step.done ? '#047857' : '#475569' }}>
                  {step.done ? <CheckCircle2 size={14} /> : <FileText size={14} />}
                  {step.label}
                </div>
                <div style={{ fontSize: 11, color: '#64748b', marginTop: 5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{step.detail}</div>
              </div>
            ))}
          </div>

          <div
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => { event.preventDefault(); setDragging(false); handleFiles(event.dataTransfer.files); }}
            style={{ marginTop: 14, border: `1px dashed ${dragging ? '#2563eb' : '#b8c7dc'}`, background: dragging ? '#eff6ff' : '#f8fafc', borderRadius: 10, padding: '15px 16px', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}
          >
            <Upload size={18} color="#2563eb" />
            <div style={{ flex: 1, minWidth: 220 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: '#1e293b' }}>{hasFile ? '替换或补充招标文件' : '把招标文件放进这个项目'}</div>
              <div style={{ fontSize: 11, color: '#64748b', marginTop: 3 }}>支持 PDF、DOCX、DOC、WPS、TXT、MD；扫描件上传后可继续 OCR。</div>
            </div>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 11px', background: '#fff', color: '#1d4ed8', border: '1px solid #bfdbfe', borderRadius: 7, cursor: uploading ? 'wait' : 'pointer', fontSize: 12, fontWeight: 600 }}>
              {uploading ? <Loader2 size={13} className="spin" /> : <Upload size={13} />}
              {uploading ? '上传中…' : '选择文件'}
              <input type="file" multiple accept=".pdf,.docx,.doc,.wps,.txt,.md" hidden disabled={uploading} onChange={(event) => { if (event.target.files) handleFiles(event.target.files); event.currentTarget.value = ''; }} />
            </label>
            {hasFile && !ready && (
              <button onClick={parse} disabled={parsing || loading} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 12px', background: '#1d4ed8', color: '#fff', border: 0, borderRadius: 7, cursor: parsing ? 'wait' : 'pointer', fontSize: 12, fontWeight: 600 }}>
                {parsing ? <Loader2 size={13} className="spin" /> : <FileText size={13} />}
                {parsing ? '解析中…' : '解析招标文件'}
              </button>
            )}
          </div>
        </>
      )}

      {loading && <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#64748b', fontSize: 11, marginTop: 10 }}><Loader2 size={12} className="spin" /> 正在读取项目资料…</div>}
      {message && (
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 7, marginTop: 10, padding: '9px 11px', borderRadius: 8, background: message.kind === 'ok' ? '#ecfdf5' : '#fef2f2', color: message.kind === 'ok' ? '#047857' : '#b91c1c', fontSize: 12 }}>
          {message.kind === 'ok' ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
          <span>{message.text}</span>
        </div>
      )}
    </section>
  );
}
