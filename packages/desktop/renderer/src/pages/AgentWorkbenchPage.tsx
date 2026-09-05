import { useState, useEffect, useRef } from 'react';
import {
  Bot, Send, Loader2, AlertTriangle, Settings, CheckCircle2, XCircle, Wrench,
  ShieldQuestion, ChevronDown, ChevronRight, Play, Ban, Info, User,
} from 'lucide-react';
import { projectApi, llmApi, streamAguiRun, aguiApi, type Project } from '../services/api';
import GraphMonitor from '../components/graph/GraphMonitor';
import { useNavigate } from 'react-router-dom';

interface AgentOption { name: string; description: string }

const AGENT_OPTIONS: AgentOption[] = [
  { name: 'supervisor', description: '主管 Agent：编排完整投标生成流程' },
  { name: 'tender_interpret_agent', description: '招标解读 Agent' },
  { name: 'outline_agent', description: '大纲生成 Agent' },
  { name: 'content_agent', description: '内容生成 Agent' },
  { name: 'compliance_check_agent', description: '合规检查 Agent' },
  { name: 'format_agent', description: '格式排版 Agent' },
  { name: 'export_agent', description: '导出 Agent' },
];

interface ChatItem {
  kind: 'user' | 'assistant' | 'step' | 'tool' | 'error' | 'system' | 'interrupt';
  id: string;
  text?: string;
  step?: string;
  tool?: { name: string; result?: string };
  error?: string;
}

// 事件条目 ID 计数器：Date.now() 在流式高频事件下会产生重复 key
let itemSeq = 0;
const nextId = (prefix: string) => `${prefix}_${Date.now().toString(36)}_${++itemSeq}`;

interface ReviewItem {
  requirement_id: string;
  status: string;
  reason: string;
  decision?: string;
}

export default function AgentWorkbenchPage() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState('');
  const [agent, setAgent] = useState('supervisor');
  const [task, setTask] = useState('执行完整投标生成流程');
  const [running, setRunning] = useState(false);
  const [abortRef] = useState<AbortController | null>(null);
  const abortRef2 = useRef<AbortController | null>(null);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState('');

  // 资格预审 HITL 演示
  const [hitlMode, setHitlMode] = useState(false);
  const [reqText, setReqText] = useState('注册资金不低于 500 万元');
  const [credText, setCredText] = useState('300 万元');
  const [reviewItems, setReviewItems] = useState<ReviewItem[]>([]);
  const [activeWorkflowId, setActiveWorkflowId] = useState('');

  const bottomRef = useRef<HTMLDivElement>(null);

  // P-D3：默认展示图运行监视器；原旁路 ReAct 工作台收进次级 tab（功能保留不删）
  const [tab, setTab] = useState<'graph' | 'legacy'>('graph');
  const tabBtn = (active: boolean) => ({
    padding: '8px 18px',
    borderRadius: 8,
    border: active ? '1px solid #1a56db' : '1px solid #e2e8f0',
    background: active ? '#eff6ff' : '#fff',
    color: active ? '#1a56db' : '#64748b',
    fontSize: 13,
    fontWeight: 600,
    cursor: 'pointer',
  });

  useEffect(() => {
    projectApi.list().then((r) => {
      setProjects(r.data.projects || []);
      if (r.data.projects?.[0]) setProjectId(r.data.projects[0].id);
    }).catch(() => setMessage('无法加载项目列表（数据库可能未启动）'));
    return () => { abortRef2.current?.abort(); };
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [items]);

  const run = async () => {
    if (running) return;
    setRunning(true);
    setItems([]);
    setReviewItems([]);
    setActiveWorkflowId('');
    const controller = new AbortController();
    abortRef2.current = controller;
    const runId = `run_${Date.now().toString(36)}`;
    const threadId = `thread_${Date.now().toString(36)}`;
    try {
      await streamAguiRun({
        mode: 'agent',
        thread_id: threadId,
        run_id: runId,
        project_id: projectId,
        task: task,
        agent: agent === 'supervisor' ? undefined : agent,
      }, (ev) => {
        const type = ev.type as string;
        if (type === 'RUN_STARTED') {
          setItems([]);
          pushItem({ kind: 'system', id: nextId('sys'), text: 'Agent 已启动，正在执行…' });
        } else if (type === 'STEP_STARTED') {
          const step = (ev as { stepName: string }).stepName;
          pushItem({ kind: 'step', id: nextId('step'), step, text: `步骤开始: ${step}` });
        } else if (type === 'STEP_FINISHED') {
          const step = (ev as { stepName: string }).stepName;
          pushItem({ kind: 'step', id: nextId('stepf'), step, text: `步骤完成: ${step}` });
        } else if (type === 'TEXT_MESSAGE_CONTENT') {
          const delta = (ev as { delta?: string }).delta || '';
          pushItem({ kind: 'assistant', id: nextId('msg'), text: delta });
        } else if (type === 'TOOL_CALL_START') {
          const name = (ev as { toolCallName: string }).toolCallName;
          pushItem({ kind: 'tool', id: nextId('tool'), tool: { name }, text: `调用工具: ${name}` });
        } else if (type === 'TOOL_CALL_RESULT') {
          const name = (ev as { toolCallName?: string }).toolCallName || '工具';
          const content = (ev as { content?: string }).content || '';
          pushItem({ kind: 'tool', id: nextId('toolr'), tool: { name, result: content.slice(0, 500) }, text: `工具结果: ${name}` });
        } else if (type === 'RUN_FINISHED') {
          const outcome = (ev as { outcome?: { type: string } }).outcome;
          pushItem({ kind: 'system', id: nextId('done'), text: outcome?.type === 'interrupt' ? '任务等待人工确认（HITL）' : '任务完成' });
        } else if (type === 'RUN_ERROR') {
          pushItem({ kind: 'error', id: nextId('err'), error: (ev as { message: string }).message });
        }
      }, controller.signal);
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        pushItem({ kind: 'error', id: nextId('err'), error: (e as Error).message });
      }
    } finally {
      setRunning(false);
    }
  };

  const pushItem = (item: ChatItem) => {
    setItems((prev) => [...prev, item]);
  };

  const toggleTool = (id: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  // ── 资格预审 HITL（AG-UI interrupt/resume，无 LLM）──
  const runHitl = async () => {
    setRunning(true);
    setItems([]);
    setReviewItems([]);
    setActiveWorkflowId('');
    const runId = `run_${Date.now().toString(36)}`;
    try {
      await streamAguiRun({
        mode: 'qualification',
        thread_id: `thread_${Date.now().toString(36)}`,
        run_id: runId,
        project_id: 'hitl-demo',
        requirements: [{ requirement_id: 'r1', requirement_type: 'capital', description: reqText, min_amount: 5000000 }],
        credentials: credText ? [{ credential_id: 'c1', credential_type: 'capital', amount_text: credText, evidence_refs: ['e1'] }] : [],
      }, (ev) => {
        const type = ev.type as string;
        if (type === 'TEXT_MESSAGE_CONTENT') {
          const delta = (ev as { delta?: string }).delta || '';
          pushItem({ kind: 'assistant', id: nextId('msg'), text: delta });
        } else if (type === 'RUN_FINISHED') {
          const outcome = (ev as { outcome?: { type: string; interrupts?: Array<{ id: string; message?: string }> } }).outcome;
          if (outcome?.type === 'interrupt' && outcome.interrupts?.[0]) {
            setActiveWorkflowId(outcome.interrupts[0].id);
            setReviewItems([{ requirement_id: 'r1', status: 'insufficient', reason: '信息不足，需要人工确认', decision: undefined }]);
            pushItem({ kind: 'interrupt', id: nextId('int'), text: '需要人工审批以下资格项' });
          } else {
            pushItem({ kind: 'system', id: nextId('done'), text: '资格预审完成（无需人工）' });
          }
        } else if (type === 'RUN_ERROR') {
          pushItem({ kind: 'error', id: nextId('err'), error: (ev as { message: string }).message });
        }
      });
    } catch (e) {
      pushItem({ kind: 'error', id: nextId('err'), error: (e as Error).message });
    } finally {
      setRunning(false);
    }
  };

  const resumeHitl = async (decision: 'confirm' | 'reject' | 'mark_insufficient') => {
    if (!activeWorkflowId) return;
    setRunning(true);
    // resume 走独立端点（SSE）
    try {
      const resp = await fetch('/api/agui/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workflow_id: activeWorkflowId,
          thread_id: `thread_${Date.now().toString(36)}`,
          run_id: `run_${Date.now().toString(36)}`,
          decisions: [{ requirement_id: 'r1', decision, reviewer: 'admin', note: 'AG-UI 工作台审批' }],
        }),
      });
      const reader = (resp.body as ReadableStream).getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop() || '';
        for (const frame of frames) {
          const line = frame.trim();
          if (line.startsWith('data: ')) {
            const ev = JSON.parse(line.slice(6));
            if (ev.type === 'TEXT_MESSAGE_CONTENT') {
              pushItem({ kind: 'assistant', id: nextId('msg'), text: ev.delta || '' });
            } else if (ev.type === 'RUN_FINISHED') {
              pushItem({ kind: 'system', id: nextId('done'), text: '审批完成，流程已恢复' });
            } else if (ev.type === 'RUN_ERROR') {
              pushItem({ kind: 'error', id: nextId('err'), error: ev.message });
            }
          }
        }
      }
      setActiveWorkflowId('');
      setReviewItems([]);
    } catch (e) {
      pushItem({ kind: 'error', id: nextId('err'), error: (e as Error).message });
    } finally {
      setRunning(false);
    }
  };

  const legacyView = (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <div>
          <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>旁路 ReAct 工作台（旧）</h2>
          <p style={{ fontSize: 12, color: '#64748b', margin: '4px 0 0' }}>
            基于 AG-UI 协议（SSE）流式运行 Supervisor / 业务 Agent，支持工具调用展开与 HITL 人工确认
          </p>
        </div>
        <button
          onClick={() => navigate('/settings')}
          style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, padding: '8px 12px', borderRadius: 8, border: '1px solid #e2e8f0', background: '#fff', cursor: 'pointer', color: '#475569' }}
        >
          <Settings size={14} /> 前往设置配置 API Key
        </button>
      </div>

      {/* 配置栏 */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center', background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: '12px 16px', marginBottom: 16 }}>
        <div>
          <label style={{ fontSize: 11, color: '#64748b' }}>项目</label>
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)} style={{ marginLeft: 8, padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 13 }}>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            {projects.length === 0 && <option value="">（无项目，请先创建）</option>}
          </select>
        </div>
        <div>
          <label style={{ fontSize: 11, color: '#64748b' }}>Agent</label>
          <select value={agent} onChange={(e) => setAgent(e.target.value)} style={{ marginLeft: 8, padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 13 }}>
            {AGENT_OPTIONS.map((a) => <option key={a.name} value={a.name}>{a.name}</option>)}
          </select>
        </div>
        <div style={{ flex: 1, minWidth: 260 }}>
          <label style={{ fontSize: 11, color: '#64748b' }}>任务</label>
          <input
            value={task}
            onChange={(e) => setTask(e.target.value)}
            placeholder="输入任务描述，例如：解读招标文件并生成投标大纲"
            style={{ marginLeft: 8, padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 13, width: '70%' }}
          />
        </div>
        <button
          onClick={run}
          disabled={running || !projectId}
          style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 16px', borderRadius: 8, background: '#1a56db', color: '#fff', border: 'none', cursor: running || !projectId ? 'not-allowed' : 'pointer', opacity: running || !projectId ? 0.6 : 1, fontSize: 13 }}
        >
          {running ? <Loader2 size={14} className="spin" /> : <Send size={14} />} {running ? '运行中…' : '运行'}
        </button>
      </div>

      {/* 事件流 */}
      <div style={{ background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 16, minHeight: 260, marginBottom: 16 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: '#0f172a', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Bot size={15} color="#1a56db" /> AG-UI 事件流（SSE）
        </div>
        {items.length === 0 && (
          <p style={{ fontSize: 12, color: '#94a3b8' }}>尚未运行。点击「运行」开始；未配置 API Key 时后端会返回明确的 RUN_ERROR 事件并引导到设置页，不会崩溃。</p>
        )}
        {items.map((item) => {
          if (item.kind === 'user') {
            return (
              <div key={item.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 8 }}>
                <User size={14} color="#475569" style={{ marginTop: 2 }} />
                <span style={{ fontSize: 13, color: '#0f172a', whiteSpace: 'pre-wrap' }}>{item.text}</span>
              </div>
            );
          }
          if (item.kind === 'assistant') {
            return (
              <div key={item.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 8 }}>
                <Bot size={14} color="#1a56db" style={{ marginTop: 2 }} />
                <span style={{ fontSize: 13, color: '#334155', whiteSpace: 'pre-wrap' }}>{item.text}</span>
              </div>
            );
          }
          if (item.kind === 'step') {
            return (
              <div key={item.id} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6, color: '#059669' }}>
                <ChevronRight size={13} /> <span style={{ fontSize: 12, fontWeight: 600 }}>{item.text}</span>
              </div>
            );
          }
          if (item.kind === 'tool') {
            const expanded = expandedTools.has(item.id);
            return (
              <div key={item.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 6, background: '#f8fafc', borderRadius: 8, padding: '6px 10px' }}>
                <Wrench size={13} color="#d97706" style={{ marginTop: 2 }} />
                <div style={{ flex: 1 }}>
                  <button onClick={() => toggleTool(item.id)} style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 12, color: '#92400e', fontWeight: 600, padding: 0, display: 'flex', alignItems: 'center', gap: 4 }}>
                    {item.tool?.name} {item.tool?.result ? (expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />) : null}
                  </button>
                  {expanded && item.tool?.result && (
                    <pre style={{ fontSize: 11, color: '#64748b', whiteSpace: 'pre-wrap', margin: '4px 0 0', background: '#fff', padding: 8, borderRadius: 6, border: '1px solid #e2e8f0' }}>{item.tool.result}</pre>
                  )}
                </div>
              </div>
            );
          }
          if (item.kind === 'interrupt') {
            return (
              <div key={item.id} style={{ background: '#fffbeb', border: '1px solid #fcd34d', borderRadius: 8, padding: '10px 12px', marginBottom: 8 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, fontWeight: 600, color: '#92400e', marginBottom: 8 }}>
                  <ShieldQuestion size={15} /> {item.text}
                </div>
                {reviewItems.map((r) => (
                  <div key={r.requirement_id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#475569', marginBottom: 8, flexWrap: 'wrap' }}>
                    <span style={{ background: '#fef3c7', padding: '2px 8px', borderRadius: 6, fontWeight: 600 }}>{r.requirement_id}</span>
                    <span>{r.reason}</span>
                  </div>
                ))}
                <div style={{ display: 'flex', gap: 8 }}>
                  <button onClick={() => resumeHitl('confirm')} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '6px 12px', borderRadius: 6, background: '#059669', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}><CheckCircle2 size={13} /> 确认</button>
                  <button onClick={() => resumeHitl('reject')} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '6px 12px', borderRadius: 6, background: '#dc2626', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}><XCircle size={13} /> 驳回</button>
                  <button onClick={() => resumeHitl('mark_insufficient')} style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '6px 12px', borderRadius: 6, background: '#d97706', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}><AlertTriangle size={13} /> 标记不足</button>
                </div>
              </div>
            );
          }
          if (item.kind === 'error') {
            return (
              <div key={item.id} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 8, color: '#dc2626', background: '#fef2f2', borderRadius: 8, padding: '8px 10px' }}>
                <XCircle size={14} style={{ marginTop: 2 }} /> <span style={{ fontSize: 12 }}>{item.error}</span>
              </div>
            );
          }
          return (
            <div key={item.id} style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8, color: '#64748b' }}>
              <Info size={13} /> <span style={{ fontSize: 12 }}>{item.text}</span>
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>

      {/* 资格预审 HITL 演示（无 LLM） */}
      <div style={{ background: '#fff', border: '1px solid #e2e8f0', borderRadius: 12, padding: 16 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: '#0f172a', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
          <ShieldQuestion size={15} color="#0f766e" /> 资格预审 HITL（AG-UI Interrupt / Resume，无需 LLM / API Key）
        </div>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
          <input value={reqText} onChange={(e) => setReqText(e.target.value)} placeholder="资格要求" style={{ padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 12, width: 220 }} />
          <input value={credText} onChange={(e) => setCredText(e.target.value)} placeholder="企业材料金额" style={{ padding: '6px 10px', borderRadius: 8, border: '1px solid #e2e8f0', fontSize: 12, width: 160 }} />
          <button onClick={runHitl} disabled={running} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#0f766e', color: '#fff', border: 'none', fontSize: 12, cursor: running ? 'not-allowed' : 'pointer' }}>
            <Play size={13} /> 运行资格预审
          </button>
          <span style={{ fontSize: 11, color: '#94a3b8' }}>留空材料 → 生成 insufficient → 触发 Interrupt → 人工确认/驳回/标记不足 → Resume</span>
        </div>
      </div>
      {message && <p style={{ fontSize: 12, color: '#d97706', marginTop: 8 }}>{message}</p>}
    </div>
  );

  return (
    <div>
      {/* P-D3 顶层 tab：图运行监视器（默认）/ 旁路 ReAct 工作台（收次级） */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        <button onClick={() => setTab('graph')} style={tabBtn(tab === 'graph')}>图运行监视器</button>
        <button onClick={() => setTab('legacy')} style={tabBtn(tab === 'legacy')}>旁路工作台（旧）</button>
      </div>
      {tab === 'graph' ? <GraphMonitor projects={projects} /> : legacyView}
    </div>
  );
}
