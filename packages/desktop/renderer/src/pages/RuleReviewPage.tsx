import { useState, useEffect } from 'react';
import { RefreshCw, Check, X, Package, Rocket, Undo2, ScrollText, ShieldCheck, Loader2 } from 'lucide-react';
import { rulesApi, type RuleProposal, type RulePack } from '../services/api';

export default function RuleReviewPage() {
  const [proposals, setProposals] = useState<RuleProposal[]>([]);
  const [packs, setPacks] = useState<RulePack[]>([]);
  const [audit, setAudit] = useState<Array<Record<string, unknown>>>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [packName, setPackName] = useState('规则包');

  const load = async () => {
    setLoading(true);
    try {
      const [p, pk, a] = await Promise.all([rulesApi.proposals(), rulesApi.packs(), rulesApi.audit()]);
      setProposals(p.data.proposals || []);
      setPacks(pk.data.packs || []);
      setAudit((a.data as { events: Array<Record<string, unknown>> }).events || []);
    } catch (e) {
      setMessage({ type: 'error', text: `加载失败: ${(e as Error).message}` });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const generate = async () => {
    try {
      const r = await rulesApi.generate(5);
      setMessage({ type: 'success', text: `已生成 ${r.data.created.length} 条候选` });
      await load();
    } catch (e) {
      setMessage({ type: 'error', text: `候选生成失败: ${(e as Error).message}` });
    }
  };

  const decide = async (id: string, action: 'approve' | 'reject') => {
    try {
      if (action === 'approve') await rulesApi.approve(id, 'admin', '审核通过');
      else await rulesApi.reject(id, 'admin', '审核驳回');
      setMessage({ type: 'success', text: `候选已${action === 'approve' ? '审批' : '驳回'}` });
      await load();
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message });
    }
  };

  const createPack = async () => {
    if (selected.size === 0) { setMessage({ type: 'error', text: '请先选择已审批候选' }); return; }
    try {
      const r = await rulesApi.createPack(packName, Array.from(selected));
      setMessage({ type: 'success', text: `已创建规则包 v${r.data.version}` });
      setSelected(new Set());
      await load();
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message });
    }
  };

  const publish = async (id: string) => {
    try {
      const r = await rulesApi.publish(id);
      setMessage({ type: 'success', text: `规则包 v${r.data.version} 已发布（通过基准+回归门禁）` });
      await load();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: { message?: string } } } }).response?.data?.detail;
      setMessage({ type: 'error', text: `发布被阻止: ${detail?.message || (e as Error).message}` });
    }
  };

  const rollback = async (id: string) => {
    try {
      await rulesApi.rollback(id);
      setMessage({ type: 'success', text: '已回滚到上一已发布版本' });
      await load();
    } catch (e) {
      setMessage({ type: 'error', text: (e as Error).message });
    }
  };

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const pending = proposals.filter((p) => p.status === 'pending');
  const approved = proposals.filter((p) => p.status === 'approved');

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>规则审核（受控学习）</h2>
          <p style={{ fontSize: 12, color: '#64748b', margin: '4px 0 0' }}>
            数据飞轮 → 规则候选 → 人工审批 → 版本化规则包 → 发布（基准+回归门禁）/ 回滚。默认永不自动发布。
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={generate} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, border: '1px solid #e2e8f0', background: '#fff', fontSize: 12, cursor: 'pointer' }}>
            <RefreshCw size={13} /> 从数据飞轮生成候选
          </button>
          <button onClick={load} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, border: '1px solid #e2e8f0', background: '#fff', fontSize: 12, cursor: 'pointer' }}>
            <RefreshCw size={13} /> 刷新
          </button>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '10px 12px', marginBottom: 12, borderRadius: 9, background: '#f8fafc', border: '1px solid #e2e8f0', color: '#475569', fontSize: 12, lineHeight: 1.55 }}>
        <ShieldCheck size={15} color="#1a56db" style={{ flexShrink: 0, marginTop: 1 }} />
        <span>这里不是日常投标操作页。它用于管理员把人工改判和资格判断沉淀成“候选规则”，审核后才会进入后续自动匹配；普通项目不需要进入这里。</span>
      </div>

      {message && (
        <div style={{ padding: '10px 14px', borderRadius: 8, marginBottom: 12, fontSize: 12, background: message.type === 'success' ? '#ecfdf5' : '#fef2f2', color: message.type === 'success' ? '#059669' : '#dc2626' }}>
          {message.text}
        </div>
      )}

      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        {/* 待审候选 */}
        <div style={{ flex: 1, minWidth: 340, background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
            <ScrollText size={14} color="#d97706" /> 待审候选（{pending.length}）
          </div>
          {pending.map((p) => (
            <div key={p.proposal_id} style={{ border: '1px solid #f1f5f9', borderRadius: 8, padding: '10px 12px', marginBottom: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 13, fontWeight: 600 }}>{p.template}</span>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button onClick={() => decide(p.proposal_id, 'approve')} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '4px 10px', borderRadius: 6, background: '#059669', color: '#fff', border: 'none', fontSize: 11, cursor: 'pointer' }}><Check size={12} /> 审批</button>
                  <button onClick={() => decide(p.proposal_id, 'reject')} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '4px 10px', borderRadius: 6, background: '#dc2626', color: '#fff', border: 'none', fontSize: 11, cursor: 'pointer' }}><X size={12} /> 驳回</button>
                </div>
              </div>
              <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>{p.rationale}</div>
              <div style={{ fontSize: 10, color: '#94a3b8', marginTop: 4 }}>类型: {p.requirement_type || '通用'} · 参数: {JSON.stringify(p.params)} · 统计: {JSON.stringify(p.statistics)}</div>
            </div>
          ))}
          {pending.length === 0 && <p style={{ fontSize: 12, color: '#94a3b8' }}>暂无待审候选。点击「从数据飞轮生成候选」（需要先有资格预审 Trace 数据）。</p>}
        </div>

        {/* 已审批 -> 建包 */}
        <div style={{ flex: 1, minWidth: 340, background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
            <ShieldCheck size={14} color="#059669" /> 已审批候选 → 创建规则包
          </div>
          {approved.map((p) => (
            <label key={p.proposal_id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0', fontSize: 12, cursor: 'pointer' }}>
              <input type="checkbox" checked={selected.has(p.proposal_id)} onChange={() => toggle(p.proposal_id)} />
              <span style={{ fontWeight: 600 }}>{p.template}</span>
              <span style={{ color: '#94a3b8', fontSize: 11 }}>{p.requirement_type || '通用'}</span>
            </label>
          ))}
          {approved.length === 0 && <p style={{ fontSize: 12, color: '#94a3b8' }}>暂无已审批候选。</p>}
          <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
            <input value={packName} onChange={(e) => setPackName(e.target.value)} placeholder="规则包名称" style={{ flex: 1, padding: '7px 10px', borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 12 }} />
            <button onClick={createPack} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#0f766e', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}><Package size={13} /> 创建规则包</button>
          </div>
        </div>
      </div>

      {/* 规则包 */}
      <div style={{ background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 16, marginTop: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Package size={14} color="#1a56db" /> 规则包 / 版本（{packs.length}）
        </div>
        <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ color: '#64748b', textAlign: 'left' }}>
              <th style={{ padding: '6px 4px' }}>名称</th><th>版本</th><th>状态</th><th>规则数</th><th>发布者</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {packs.map((pk) => (
              <tr key={pk.pack_id} style={{ borderTop: '1px solid #f1f5f9' }}>
                <td style={{ padding: '8px 4px', fontWeight: 600 }}>{pk.name}</td>
                <td>v{pk.version}</td>
                <td>
                  <span style={{ padding: '2px 8px', borderRadius: 6, fontSize: 10, fontWeight: 600, background: pk.status === 'published' ? '#ecfdf5' : pk.status === 'rolled_back' ? '#f1f5f9' : '#fffbeb', color: pk.status === 'published' ? '#059669' : pk.status === 'rolled_back' ? '#64748b' : '#d97706' }}>
                    {pk.status}
                  </span>
                </td>
                <td>{pk.rules?.length ?? 0}</td>
                <td>{pk.published_by || '-'}</td>
                <td>
                  <div style={{ display: 'flex', gap: 6 }}>
                    {pk.status === 'draft' && (
                      <button onClick={() => publish(pk.pack_id)} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '4px 10px', borderRadius: 6, background: '#1a56db', color: '#fff', border: 'none', fontSize: 11, cursor: 'pointer' }}><Rocket size={12} /> 发布</button>
                    )}
                    {pk.status === 'published' && (
                      <button onClick={() => rollback(pk.pack_id)} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '4px 10px', borderRadius: 6, background: '#dc2626', color: '#fff', border: 'none', fontSize: 11, cursor: 'pointer' }}><Undo2 size={12} /> 回滚</button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {packs.length === 0 && <tr><td colSpan={6} style={{ color: '#94a3b8', padding: 8 }}>暂无规则包</td></tr>}
          </tbody>
        </table>
      </div>

      {/* 审计 */}
      <div style={{ background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 16, marginTop: 16 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <ScrollText size={14} color="#64748b" /> 审计记录（{audit.length}）
        </div>
        <div style={{ maxHeight: 220, overflow: 'auto' }}>
          {audit.map((e, i) => (
            <div key={i} style={{ display: 'flex', gap: 10, fontSize: 11, color: '#475569', borderBottom: '1px solid #f8fafc', padding: '5px 0' }}>
              <span style={{ color: '#94a3b8' }}>{String(e.occurred_at).slice(0, 19)}</span>
              <span style={{ fontWeight: 600 }}>{String(e.action)}</span>
              <span>{String(e.target_id)}</span>
              <span style={{ color: '#94a3b8' }}>{String(e.actor)}</span>
            </div>
          ))}
          {audit.length === 0 && <p style={{ fontSize: 12, color: '#94a3b8' }}>暂无审计记录</p>}
        </div>
      </div>
      {loading && <div style={{ position: 'fixed', right: 20, bottom: 20, display: 'flex', alignItems: 'center', gap: 6, background: '#0f172a', color: '#fff', padding: '8px 14px', borderRadius: 8, fontSize: 12 }}><Loader2 size={13} className="spin" /> 加载中…</div>}
    </div>
  );
}
