import { useState, useEffect, useCallback, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import { Database, Plus, Trash2, Upload, Search, Loader2, FileUp, ArrowLeft } from 'lucide-react';
import { interpretApi, knowledgeApi } from '../services/api';

interface KnowledgeBase {
  id: string;
  name: string;
  doc_count: number;
  embedding_model: string;
  collection_name: string;
  kb_type?: string; // legal=法规合规库 | enterprise=企业私有库
  review_status?: string; // draft | reviewed | published
  valid_until?: string | null;
  created_at?: string | null;
}

interface SearchResult {
  text: string;
  score: number;
  metadata?: Record<string, unknown>;
}

interface ProjectEvidenceDocument {
  id: string;
  file_name: string;
  file_size?: number;
  type: string;
  parsed: boolean;
  created_at?: string | null;
  evidence_collection?: string | null;
  indexed_chunks?: number;
}

export default function KnowledgePage() {
  const location = useLocation();
  // Worker I 任务1：检查页"上传补充资料"跳转携带 preselectType=enterprise → 自动预选企业私有库
  const preselectType = (location.state as { preselectType?: string } | null)?.preselectType as 'enterprise' | 'legal' | undefined;
  const fromCheck = (location.state as { from?: string } | null)?.from === 'check-recheck';
  const projectId = (location.state as { projectId?: string } | null)?.projectId
    || new URLSearchParams(location.search).get('project_id') || '';
  const preselectDoneRef = useRef(false);

  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [selected, setSelected] = useState<KnowledgeBase | null>(null);
  const [newName, setNewName] = useState('');
  const [newKbType, setNewKbType] = useState<'enterprise' | 'legal'>('enterprise');
  const [creating, setCreating] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [projectDocs, setProjectDocs] = useState<ProjectEvidenceDocument[]>([]);
  const [projectCollection, setProjectCollection] = useState('');
  const [projectQuery, setProjectQuery] = useState('');
  const [projectResults, setProjectResults] = useState<SearchResult[]>([]);
  const [projectSearching, setProjectSearching] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const r = await knowledgeApi.list();
      setKbs(r.data.knowledge_bases || []);
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const loadProjectEvidence = useCallback(async () => {
    if (!projectId) return;
    try {
      const r = await interpretApi.listDocuments(projectId);
      setProjectDocs((r.data.documents || []).filter((doc: ProjectEvidenceDocument) => doc.type === 'reference' || doc.type === 'bid'));
      setProjectCollection(r.data.evidence_collection || '');
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    }
  }, [projectId]);

  useEffect(() => { loadProjectEvidence(); }, [loadProjectEvidence]);

  // Worker I 任务1：来自检查页补料引导时，自动预选第一个企业私有库并提示返回复检
  useEffect(() => {
    if (!preselectType || preselectDoneRef.current || kbs.length === 0) return;
    preselectDoneRef.current = true;
    const target = kbs.filter((k) => (k.kb_type || 'enterprise') === preselectType)
      .sort((a, b) => (b.doc_count || 0) - (a.doc_count || 0))[0];
    if (target) {
      setSelected(target);
      setMessage({ ok: true, text: `已预选「${target.name}」（企业私有库）——上传补充资料后，回到检查页点击"资料已补充，重新检查修复"` });
    } else {
      setMessage({ ok: false, text: '未找到企业私有库，请先创建一个（类型选"企业库"）再上传资料' });
    }
  }, [kbs, preselectType]);

  const createKb = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    setMessage(null);
    try {
      const r = await knowledgeApi.create({ name: newName.trim(), kb_type: newKbType });
      setNewName('');
      await load();
      const created: KnowledgeBase = { id: r.data.id, name: r.data.name, doc_count: 0, embedding_model: '', collection_name: r.data.collection_name };
      setSelected(created);
      setMessage({ ok: true, text: `知识库「${r.data.name}」已创建` });
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    } finally { setCreating(false); }
  };

  const uploadFile = async (file: File) => {
    if (!selected) return;
    setUploading(true);
    setMessage(null);
    try {
      const r = await knowledgeApi.upload(selected.id, file);
      setMessage({ ok: true, text: `已入库 ${r.data.chunks_added} 个 chunk（向量已保存，可检索）` });
      await load();
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    } finally { setUploading(false); if (fileRef.current) fileRef.current.value = ''; }
  };

  const doSearch = async () => {
    if (!selected || !query.trim()) return;
    setSearching(true);
    setMessage(null);
    try {
      const r = await knowledgeApi.search(selected.id, query.trim(), 5);
      setResults(r.data.results || []);
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    } finally { setSearching(false); }
  };

  const searchProjectEvidence = async () => {
    if (!projectId || !projectQuery.trim()) return;
    setProjectSearching(true);
    try {
      const r = await interpretApi.searchEvidence(projectId, projectQuery.trim(), 8);
      setProjectResults(r.data.results || []);
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    } finally { setProjectSearching(false); }
  };

  const deleteProjectDocument = async (doc: ProjectEvidenceDocument) => {
    if (!projectId || !window.confirm(`删除项目资料“${doc.file_name}”？其 RAG 分块也会一并移除。`)) return;
    try {
      await interpretApi.deleteDocument(doc.id);
      await loadProjectEvidence();
      setProjectResults([]);
      setMessage({ ok: true, text: `已删除 ${doc.file_name} 及其项目 RAG 分块` });
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    }
  };

  const deleteKb = async (kb: KnowledgeBase) => {
    try {
      await knowledgeApi.delete(kb.id);
      if (selected?.id === kb.id) { setSelected(null); setResults([]); }
      await load();
      setMessage({ ok: true, text: `知识库「${kb.name}」已删除` });
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 18, margin: '0 0 4px' }}>
        <Database size={18} color="#0ea5e9" /> 知识中心
      </h2>
      <p style={{ fontSize: 12, color: 'var(--color-text-secondary)', margin: '0 0 16px' }}>
        创建知识库 → 上传文档（自动切块并向量化入库）→ 检索返回相关片段与相似度。Embedding 使用平台设置中配置的模型与 Key。
        {fromCheck && (
          <span style={{ marginLeft: 8, color: '#1a56db', cursor: 'pointer' }} onClick={() => window.history.back()}>
            <ArrowLeft size={11} style={{ verticalAlign: -1 }} /> 返回检查页继续复检
          </span>
        )}
      </p>
      {projectId && (
        <div style={{ marginBottom: 18, padding: 14, border: '1px solid #bfdbfe', borderRadius: 8, background: '#f8fbff' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 10 }}>
            <div>
              <div style={{ fontSize: 14, fontWeight: 700, color: '#1e3a8a' }}>当前项目证据资料</div>
              <div style={{ fontSize: 11, color: '#64748b', marginTop: 3 }}>{projectCollection || '项目独立证据集合'} · {projectDocs.length} 份资料</div>
            </div>
            <div style={{ display: 'flex', gap: 6, minWidth: 280, flex: 1, maxWidth: 520 }}>
              <input value={projectQuery} onChange={(e) => setProjectQuery(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') searchProjectEvidence(); }} placeholder="检索当前项目证据" style={{ flex: 1, padding: '7px 10px', border: '1px solid #bfdbfe', borderRadius: 6, fontSize: 12 }} />
              <button onClick={searchProjectEvidence} disabled={projectSearching} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '7px 11px', border: 0, borderRadius: 6, background: '#1d4ed8', color: '#fff', fontSize: 12, cursor: 'pointer' }}>{projectSearching ? <Loader2 size={13} className="spin" /> : <Search size={13} />} 检索</button>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(260px, 0.9fr) minmax(320px, 1.1fr)', gap: 10 }}>
            <div style={{ maxHeight: 190, overflowY: 'auto', background: '#fff', border: '1px solid #dbeafe', borderRadius: 6 }}>
              {projectDocs.length === 0 && <div style={{ padding: 10, fontSize: 12, color: '#64748b' }}>当前项目暂无已解析的补充资料</div>}
              {projectDocs.map((doc) => (
                <div key={doc.id} style={{ padding: '8px 10px', borderBottom: '1px solid #eff6ff', fontSize: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
                    <div style={{ fontWeight: 600, color: '#334155' }}>{doc.file_name}</div>
                    <button title="删除项目资料及其 RAG 分块" onClick={() => deleteProjectDocument(doc)} style={{ border: 0, background: 'transparent', color: '#b91c1c', cursor: 'pointer', padding: 2 }}><Trash2 size={13} /></button>
                  </div>
                  <div style={{ color: '#64748b', marginTop: 3 }}>{doc.parsed ? '已解析' : '待解析'} · {doc.indexed_chunks || 0} 个 RAG 分块</div>
                </div>
              ))}
            </div>
            <div style={{ maxHeight: 190, overflowY: 'auto' }}>
              {projectResults.map((r, i) => <div key={i} style={{ padding: '8px 10px', marginBottom: 6, background: '#fff', border: '1px solid #dbeafe', borderRadius: 6, fontSize: 11, whiteSpace: 'pre-wrap' }}>{r.text}</div>)}
              {!projectResults.length && <div style={{ padding: 10, fontSize: 12, color: '#64748b' }}>输入关键词即可查看修复与复检实际使用的项目证据片段</div>}
            </div>
          </div>
        </div>
      )}
      <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>
        <div style={{ width: 320, flexShrink: 0 }}>
          <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
            <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="新知识库名称"
              style={{ flex: 1, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13 }} />
            <select value={newKbType} onChange={(e) => setNewKbType(e.target.value as 'enterprise' | 'legal')}
              style={{ padding: '7px 6px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 12 }}>
              <option value="enterprise">企业库</option>
              <option value="legal">法规库</option>
            </select>
            <button onClick={createKb} disabled={creating}
              style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '7px 12px', borderRadius: 8, background: '#059669', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
              {creating ? <Loader2 size={13} className="spin" /> : <Plus size={13} />} 创建
            </button>
          </div>
          {kbs.length === 0 && <div style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>暂无知识库，先创建一个。</div>}
          {kbs.map((kb) => (
            <div key={kb.id} onClick={() => { setSelected(kb); setResults([]); }}
              style={{ padding: '10px 12px', borderRadius: 8, border: `1px solid ${selected?.id === kb.id ? '#0ea5e9' : 'var(--color-border)'}`, marginBottom: 8, cursor: 'pointer', background: 'var(--color-surface)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: 13, fontWeight: 600 }}>{kb.name}</span>
                <button onClick={(e) => { e.stopPropagation(); deleteKb(kb); }}
                  style={{ background: 'none', border: 'none', color: '#dc2626', cursor: 'pointer', padding: 2 }}>
                  <Trash2 size={13} />
                </button>
              </div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 2 }}>
                {kb.kb_type && (
                  <span style={{ fontSize: 10, padding: '1px 6px', borderRadius: 6, background: kb.kb_type === 'legal' ? '#eff6ff' : '#f0fdf4', color: kb.kb_type === 'legal' ? '#1d4ed8' : '#059669' }}>
                    {kb.kb_type === 'legal' ? '法规合规' : '企业私有'}
                  </span>
                )}
                {kb.review_status && kb.review_status !== 'draft' && (
                  <span style={{ fontSize: 10, color: kb.review_status === 'published' ? '#059669' : '#b45309' }}>
                    {kb.review_status === 'published' ? '已发布' : '已复核'}
                  </span>
                )}
              </div>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', marginTop: 2 }}>
                {kb.doc_count} 个 chunk{kb.embedding_model ? ` · ${kb.embedding_model}` : ''}
              </div>
            </div>
          ))}
        </div>
        <div style={{ flex: 1 }}>
          {!selected ? (
            <div style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>← 选择或创建一个知识库</div>
          ) : (
            <>
              <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
                <button onClick={() => fileRef.current?.click()} disabled={uploading}
                  style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#1a56db', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
                  {uploading ? <Loader2 size={13} className="spin" /> : <Upload size={13} />} 上传文档入库
                </button>
                <input ref={fileRef} type="file" accept=".pdf,.docx,.doc,.txt,.md,.html" hidden
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadFile(f); }} />
                <span style={{ fontSize: 11, color: 'var(--color-text-secondary)', alignSelf: 'center' }}>
                  <FileUp size={11} /> 支持 pdf/docx/txt/md/html
                </span>
              </div>
              <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
                <input value={query} onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') doSearch(); }} placeholder="输入检索问题"
                  style={{ flex: 1, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13 }} />
                <button onClick={doSearch} disabled={searching}
                  style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '7px 12px', borderRadius: 8, background: '#0ea5e9', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
                  {searching ? <Loader2 size={13} className="spin" /> : <Search size={13} />} 检索
                </button>
              </div>
              {message && (
                <div style={{ marginBottom: 12, padding: '8px 12px', borderRadius: 8, fontSize: 12, background: message.ok ? '#ecfdf5' : '#fef2f2', color: message.ok ? '#059669' : '#dc2626' }}>{message.text}</div>
              )}
              {results.map((r, i) => (
                <div key={i} style={{ padding: '10px 12px', borderRadius: 8, border: '1px solid var(--color-border)', marginBottom: 8, background: 'var(--color-surface)' }}>
                  <div style={{ fontSize: 11, color: '#0ea5e9', marginBottom: 4 }}>相似度: {(() => { const top = results[0]?.score || 0; const pct = top > 0 ? (r.score / top) * 100 : 0; return `${pct.toFixed(0)}%（相关度得分 ${r.score.toFixed(4)}）`; })()}</div>
                  <div style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>{r.text}</div>
                </div>
              ))}
              {results.length === 0 && query && !searching && (
                <div style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>暂无结果（先上传文档，或换个问法）</div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
