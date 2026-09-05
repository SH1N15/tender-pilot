import { useState, useEffect, useRef, useCallback } from 'react';
import axios from 'axios';
import { PenTool, Loader2, ListTree, FileText, AlertTriangle, Shield, Play, Download, ChevronRight, ChevronDown, GripVertical, Plus, Trash2, Edit3, Check, X, ImageIcon, Copy, CheckCircle2, Info } from 'lucide-react';
import { generateApi, generateGraphApi, projectApi, aiImageApi, streamChapterGenerate, type Project, type OutlineNode, type GateInfo, type BizGraphRunDetail } from '../services/api';
import { useAppStore } from '../stores/appStore';
import RunStatusStrip from '../components/graph/RunStatusStrip';
import { CitationContentView, type CitationLedger } from '../components/common/CitationContentView';

const STRUCTURE_TEMPLATES = [
  { key: 'bid_letter', label: '投标函', desc: '投标函+授权书+承诺书' },
  { key: 'qualification', label: '资格审查', desc: '营业执照+资质+业绩+人员' },
  { key: 'technical', label: '技术标', desc: '方案+实施+质量+安全+售后' },
  { key: 'commercial', label: '商务标', desc: '报价+预算+成本分析' },
  { key: 'service', label: '售后服务', desc: '服务承诺+培训+应急+质保' },
];

function CopyableJson({ data, maxheight = '200px' }: { data: unknown; maxheight?: string }) {
  const [copied, setCopied] = useState(false);
  const jsonStr = JSON.stringify(data, null, 2);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(jsonStr);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = jsonStr;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };
  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={handleCopy}
        style={{
          position: 'absolute', top: '8px', right: '8px', zIndex: 1,
          padding: '4px 8px', background: copied ? '#ecfdf5' : 'white',
          border: `1px solid ${copied ? '#a7f3d0' : 'var(--color-border)'}`,
          borderRadius: '4px', cursor: 'pointer', fontSize: '11px',
          display: 'flex', alignItems: 'center', gap: '4px',
          color: copied ? '#059669' : '#64748b',
        }}
      >
        {copied ? <CheckCircle2 size={12} /> : <Copy size={12} />}
        {copied ? '已复制' : '复制'}
      </button>
      <pre style={{ background: '#f8fafc', padding: '12px', borderRadius: '8px', fontSize: '11px', overflow: 'auto', maxHeight: maxheight, marginTop: '8px', paddingRight: '60px' }}>
        {jsonStr}
      </pre>
    </div>
  );
}

type ChapterSummary = {
  id: string;
  title: string;
  level: number;
  status: string;
  word_count: number;
  has_content: boolean;
};

/** Rebuild the editable tree from persisted chapters when the page is opened from a graph run. */
function chaptersToOutline(chapters: ChapterSummary[]): OutlineNode[] {
  const nodes = new Map<string, OutlineNode>();
  const roots: OutlineNode[] = [];
  for (const chapter of chapters) {
    const status: OutlineNode['status'] = chapter.status === 'done' || chapter.has_content
      ? 'done'
      : chapter.status === 'generating'
        ? 'generating'
        : 'pending';
    nodes.set(chapter.id, {
      id: chapter.id,
      title: chapter.title,
      level: chapter.level,
      children: [],
      status,
    });
  }
  for (const chapter of chapters) {
    const node = nodes.get(chapter.id);
    if (!node) continue;
    const parts = chapter.id.split('.');
    const parent = parts.length > 1 ? nodes.get(parts.slice(0, -1).join('.')) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

export default function GeneratePage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [outlineSaving, setOutlineSaving] = useState(false);
  const [outlineMessage, setOutlineMessage] = useState('');
  const [outlineResult, setOutlineResult] = useState<Record<string, unknown> | null>(null);
  const [structureResult, setStructureResult] = useState<{ name?: string; total_pages?: number; total_sections?: number; sections?: Array<{ id: string; title: string; level: number; page_target?: number; content_hint?: string }> } | null>(null);
  const [mandatoryResult, setMandatoryResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string>('');
  const [outlineMode, setOutlineMode] = useState<string>('aligned');
  const [outlineNodes, setOutlineNodes] = useState<OutlineNode[]>([]);
  const [editingNode, setEditingNode] = useState<string | null>(null);
  const [editText, setEditText] = useState<string>('');
  const [gates, setGates] = useState<GateInfo[]>([]);
  const [streamContent, setStreamContent] = useState<string>('');
  const [groundingInfo, setGroundingInfo] = useState<string>('');
  // G-2：引用对照表（【n】点查来源联动：流式末尾事件 / 已落库章节均可加载）
  const [citationLedger, setCitationLedger] = useState<CitationLedger | null>(null);
  const [savedChapter, setSavedChapter] = useState<{ id: string; title: string; content: string } | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [scoreCoverage, setScoreCoverage] = useState<Record<string, unknown> | null>(null);
  const [activeSection, setActiveSection] = useState<'outline' | 'generate' | 'gate' | 'structure' | 'coverage' | 'aiimage'>('outline');
  const [outlineBasis, setOutlineBasis] = useState<{ mode: string; scoringItems: Array<{ category: string; item: string; score: unknown }>; docLength: number; matchedCount: number; totalItems: number } | null>(null);
  const streamAbortRef = useRef<AbortController | null>(null);

  const [aiImagePrompt, setAiImagePrompt] = useState('');
  const [aiImageProvider, setAiImageProvider] = useState('default');
  const [aiImageSize, setAiImageSize] = useState('landscape_16_9');
  const [aiImageLoading, setAiImageLoading] = useState(false);
  const [aiImageError, setAiImageError] = useState('');
  const [aiImageResult, setAiImageResult] = useState<string>('');
  const [aiImageHistory, setAiImageHistory] = useState<Array<{ prompt: string; url: string; timestamp: number }>>([]);
  const [enableIllustration, setEnableIllustration] = useState(false);
  const [removeWatermark, setRemoveWatermark] = useState(true);
  const [aiImageProviders, setAiImageProviders] = useState<Array<{ name: string; display_name?: string; configured?: boolean; description?: string }>>([]);
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [apiKeySaving, setApiKeySaving] = useState(false);
  const [apiKeyMessage, setApiKeyMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const { currentProjectId, setCurrentProject } = useAppStore();
  const projectIdFromUrl = new URLSearchParams(window.location.search).get('project_id') || '';

  useEffect(() => {
    loadProjects();
    loadAiImageProviders();
  }, []);

  useEffect(() => {
    const preferredProjectId = projectIdFromUrl || currentProjectId || '';
    if (preferredProjectId && selectedProjectId !== preferredProjectId) {
      setSelectedProjectId(preferredProjectId);
      setCurrentProject(preferredProjectId);
    }
  }, [currentProjectId, projectIdFromUrl, selectedProjectId, setCurrentProject]);

  useEffect(() => {
    if (selectedProjectId || currentProjectId) {
      loadGates();
    }
  }, [currentProjectId, selectedProjectId]);

  const loadProjects = async () => {
    try {
      const res = await projectApi.list();
      setProjects(res.data.projects || []);
    } catch (e) {
      console.error('加载项目列表失败', e);
    }
  };

  const loadAiImageProviders = async () => {
    try {
      const res = await aiImageApi.listProviders();
      setAiImageProviders(res.data?.providers || []);
    } catch (e) {
      console.error('加载配图供应商配置失败', e);
    }
  };

  const handleSaveApiKey = async () => {
    if (aiImageProvider === 'default' || !apiKeyInput.trim()) return;
    setApiKeySaving(true);
    setApiKeyMessage(null);
    try {
      const res = await aiImageApi.saveConfig(aiImageProvider, apiKeyInput.trim());
      const masked = res.data?.masked_key || res.data?.api_key_masked || '';
      setApiKeyMessage({ type: 'success', text: masked ? `API Key 已保存（${masked}）` : 'API Key 保存成功' });
      setApiKeyInput('');
      await loadAiImageProviders();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      const detail = err?.response?.data?.detail;
      setApiKeyMessage({ type: 'error', text: detail || (e instanceof Error ? e.message : 'API Key 保存失败') });
    } finally {
      setApiKeySaving(false);
    }
  };

  const GATE_STAGES: Array<{ stage: string; label: string }> = [
    { stage: 'interpret', label: '招标解读闸门' },
    { stage: 'generate', label: '投标生成闸门' },
    { stage: 'check', label: '投标检查闸门' },
    { stage: 'format', label: '文档输出闸门' },
  ];

  const loadGates = async () => {
    const projectId = selectedProjectId || currentProjectId || '';
    if (!projectId) return;
    try {
      const res = await projectApi.listGates(projectId);
      // 后端返回 { passed_stages: string[] }；据此派生各阶段闸门状态
      const passed: string[] = res.data?.passed_stages || [];
      const list: GateInfo[] = GATE_STAGES.map(({ stage, label }) => ({
        stage,
        label,
        status: passed.includes(stage) ? 'confirmed' : 'pending',
      }));
      setGates(list);
    } catch (e) {
      console.error('加载闸门信息失败', e);
    }
  };

  const [generatingProgress, setGeneratingProgress] = useState<string>('');
  // G-5 T1：章节生成图运行状态条（大纲/正文运行时展示节点拓扑与逐章进度）
  const [graphRunDetail, setGraphRunDetail] = useState<BizGraphRunDetail | null>(null);

  // BUG-16：章节下拉数据源 + 手动输入切换
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [chapterId, setChapterId] = useState<string>('');
  const [manualChapterMode, setManualChapterMode] = useState(false);
  const [manualChapterId, setManualChapterId] = useState('');

  const loadChapters = useCallback(async (pid: string) => {
    if (!pid) return;
    try {
      const res = await generateApi.chapters(pid);
      const loaded = (res.data?.chapters || []) as ChapterSummary[];
      setChapters(loaded);
      setOutlineNodes(prev => prev.length > 0 ? prev : chaptersToOutline(loaded));
    } catch (e) {
      console.error('加载章节列表失败', e);
      setChapters([]);
    }
  }, []);

  useEffect(() => {
    setChapters([]);
    setChapterId('');
    setManualChapterId('');
    setOutlineNodes([]);
    setOutlineResult(null);
    setOutlineBasis(null);
    const projectId = selectedProjectId || currentProjectId || '';
    if (projectId) {
      loadChapters(projectId);
    }
  }, [currentProjectId, selectedProjectId, loadChapters]);

  useEffect(() => {
    const projectId = selectedProjectId || currentProjectId || '';
    if (projectId && activeSection === 'generate') {
      loadChapters(projectId);
    }
  }, [activeSection, currentProjectId, selectedProjectId, loadChapters]);

  const handleGenerateOutline = async () => {
    if (!selectedProjectId) return;
    setLoading(true);
    setError('');
    setOutlineBasis(null);
    setGeneratingProgress('提交图运行...');

    try {
      // G-5 T1：大纲生成走章节生成图运行；outline_only=true 只跑到大纲（正文由用户逐章/批量另行触发），
      // 避免大项目（上百章）一次 run 连跑数小时、前端 6 分钟轮询窗口必超时。
      const startRes = await generateGraphApi.start(selectedProjectId, {
        outline_mode: outlineMode,
        run_outline: true,
        outline_only: true,
        chapter_ids: [],
        chapter_modes: {},
      });
      const runId = startRes.data?.run_id;
      if (!runId) {
        setError('创建图运行失败');
        return;
      }

      const maxPolls = 120; // 最多轮询120次（6分钟，与旧 task 轮询窗口一致）
      for (let i = 0; i < maxPolls; i++) {
        await new Promise(r => setTimeout(r, 3000));
        try {
          const res = await generateGraphApi.get(selectedProjectId, runId);
          const run = res.data;
          setGraphRunDetail(run);
          const stage = (run.snapshot as { current_stage?: string } | null)?.current_stage || '';
          setGeneratingProgress(stage ? `图运行中：${stage}` : '大纲生成中，请耐心等待（可能需要2-5分钟）...');
          if (run.status === 'completed' || run.status === 'failed') {
            const snap = (run.snapshot || {}) as Record<string, unknown>;
            const errors = (snap.errors as string[] | undefined) || [];
            const outlineData =
              ((snap.outline_result as Record<string, unknown> | null)?.data as Record<string, unknown> | undefined)?.outline ||
              (snap.outline as Record<string, unknown> | undefined) ||
              (snap.outline_tree as Record<string, unknown> | undefined) ||
              {};
            const result = {
              success: run.status === 'completed',
              data: {
                outline: outlineData,
                chapters: snap.chapters || [],
              },
              error: run.error || errors[0] || undefined,
              warnings: (snap.warnings as string[] | undefined) || [],
            };
            if (result.success === false) {
              setGeneratingProgress('');
              setError(result.error || '大纲生成结果为空，请重试');
              return;
            }
            setGeneratingProgress('');
            _processOutlineResult(result);
            return;
          }
        } catch {
          // 轮询请求失败，继续尝试
        }
      }

      // 超时
      setGeneratingProgress('');
      setError('大纲生成超时，请重试');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '大纲生成失败');
    } finally {
      setLoading(false);
    }
  };

  const _processOutlineResult = (data: Record<string, unknown> | null) => {
    setOutlineResult(data);
    // 兼容多种历史形态：{chapters} / {outline:{chapters}} / {data:{chapters}} / {success,data:{outline:{chapters}}}
    const payload = (data && typeof data.data === 'object' && data.data) ? data.data as Record<string, unknown> : (data || {});
    const outline = payload.outline || payload;
    const resultMode = (data?.mode as string) || (payload.mode as string) || outlineMode;

    // Extract basis info
    let scoringItems: Array<{ category: string; item: string; score: unknown }> = [];
    let matchedCount = 0;
    if (outline && typeof outline === 'object') {
      const outlineObj = outline as Record<string, unknown>;
      const scoreMapping = outlineObj.score_mapping as Record<string, string> | undefined;
      if (scoreMapping && typeof scoreMapping === 'object' && !Array.isArray(scoreMapping)) {
        scoringItems = Object.entries(scoreMapping).map(([name, chapterId]) => ({
          category: '', item: name, score: `→ 章节 ${chapterId}`,
        }));
        matchedCount = Object.keys(scoreMapping).length;
      }
    }

    setOutlineBasis({
      mode: resultMode,
      scoringItems,
      docLength: 0,
      matchedCount,
      totalItems: scoringItems.length,
    });

    if (Array.isArray(outline)) {
      setOutlineNodes(outline as OutlineNode[]);
    } else if (outline && typeof outline === 'object') {
      const outlineObj = outline as Record<string, unknown>;
      const nodes = (outlineObj.chapters || outlineObj.sections || []) as OutlineNode[];
      const scoreMapping = outlineObj.score_mapping as Record<string, string> | undefined;
      if (scoreMapping && typeof scoreMapping === 'object' && !Array.isArray(scoreMapping)) {
        const nodeScoreMap: Record<string, string[]> = {};
        for (const [scoreKey, chapterId] of Object.entries(scoreMapping)) {
          const cid = String(chapterId);
          if (!nodeScoreMap[cid]) nodeScoreMap[cid] = [];
          nodeScoreMap[cid].push(scoreKey);
        }
        const assignScoreMapping = (nodeList: OutlineNode[]): OutlineNode[] => {
          return nodeList.map(node => ({
            ...node,
            score_mapping: nodeScoreMap[node.id] || node.score_mapping || undefined,
            children: node.children ? assignScoreMapping(node.children) : [],
          }));
        };
        setOutlineNodes(assignScoreMapping(nodes));
      } else {
        setOutlineNodes(nodes);
      }
    }
    // BUG-16：大纲生成完成后章节已物化，刷新章节下拉数据源
    if (selectedProjectId) loadChapters(selectedProjectId);
    setLoading(false);
  };

  const handleExtractMandatory = async () => {
    if (!selectedProjectId) return;
    setLoading(true);
    setError('');
    try {
      const res = await generateApi.mandatoryExtract(selectedProjectId);
      setMandatoryResult(res.data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '提取失败');
    } finally {
      setLoading(false);
    }
  };

  // Worker I 任务3：直连导出可选 AI 配图嵌入（后端读 settings BMP_ILLUSTRATION_ENABLED 等，
  // 开关关/无 key 时后端自动降级为无图=现状）
  const [exportWithIllustrations, setExportWithIllustrations] = useState(false);
  const [illustrationChapterId, setIllustrationChapterId] = useState('');
  const [exportStatus, setExportStatus] = useState('');

  const handleExportDocx = async () => {
    const projectId = selectedProjectId || currentProjectId || '';
    if (!projectId) {
      setError('请先选择项目后再导出');
      return;
    }
    setLoading(true);
    setError('');
    setExportStatus(exportWithIllustrations ? '正在导出正文并嵌入已保存的 AI 配图，请稍候…' : '正在导出正文 DOCX…');
    try {
      const res = await generateApi.exportDocxDirect(projectId, exportWithIllustrations, {
        provider: aiImageProvider,
        size: aiImageSize,
        force: false,
        storedOnly: exportWithIllustrations,
      });
      const disposition: string = (res.headers?.['content-disposition'] as string) || '';
      const m = /filename\*=UTF-8''([^;]+)/.exec(disposition);
      const filename = m ? decodeURIComponent(m[1]) : '正文直连导出.docx';
      const contentType = String(res.headers?.['content-type'] || 'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
      const url = URL.createObjectURL(new Blob([res.data], { type: contentType }));
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      const imageCount = Number(res.headers?.['x-tenderpilot-illustration-count'] || res.headers?.['x-bidmaster-illustration-count'] || 0);
      const requestedImageCount = Number(res.headers?.['x-tenderpilot-illustration-requested'] || res.headers?.['x-bidmaster-illustration-requested'] || imageCount);
      setExportStatus(exportWithIllustrations
        ? (requestedImageCount > imageCount
          ? `导出完成，已嵌入 ${imageCount} 张 AI 配图（${requestedImageCount - imageCount} 张图片服务未返回可嵌入文件）`
          : `导出完成，已嵌入 ${imageCount} 张 AI 配图`)
        : '导出完成');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '导出 DOCX 失败');
      setExportStatus('导出失败');
    } finally {
      setLoading(false);
    }
  };

  const handleGenerateStructure = async (structureType: string) => {
    if (!selectedProjectId) return;
    setLoading(true);
    setError('');
    try {
      const res = await generateApi.generateStructure(selectedProjectId, structureType);
      setOutlineResult(res.data);
      // 就地渲染：从 {success,data:{structures:{[type]:{name,sections,total_pages,total_sections}}}} 提取
      const payload = res.data?.data || res.data || {};
      const tpl = payload.structures?.[structureType] || null;
      setStructureResult(tpl);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '结构模板生成失败');
    } finally {
      setLoading(false);
    }
  };

  const handleScoreCoverage = async () => {
    if (!selectedProjectId) return;
    setLoading(true);
    setError('');
    try {
              const res = await generateApi.scoreCoverage(selectedProjectId);
              const covData = res.data;
              // 兼容 {success,data:{...}} 包装
              const covPayload = (covData && typeof covData === 'object' && typeof covData.data === 'object' && covData.data) ? covData.data : (covData || {});
              setScoreCoverage(covPayload);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '覆盖率计算失败');
    } finally {
      setLoading(false);
    }
  };

  const handleConfirmGate = async (stage: string) => {
    if (!selectedProjectId) return;
    try {
      await projectApi.confirmGate(selectedProjectId, stage);
      await loadGates();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '闸门确认失败');
    }
  };

  const handleStreamGenerate = useCallback(async (chapterId: string, mode: string = 'A') => {
    if (!selectedProjectId) return;
    setIsStreaming(true);
    setStreamContent('');
    setGroundingInfo('');
    setCitationLedger(null);
    setSavedChapter(null);
    setError('');

    const controller = new AbortController();
    streamAbortRef.current = controller;

    try {
      // BUG-18 修正：后端该路由为 SSE 流式返回，改用 fetch + ReadableStream 真流式渲染
      const result = await streamChapterGenerate(
        selectedProjectId,
        chapterId,
        mode,
        (acc) => setStreamContent(acc),
        controller.signal,
      );
      // P3：B 模式末尾事件携带 grounding/引用统计，最小展示（【n】锚点保留在正文原样渲染）
      if (mode.toUpperCase() === 'B' && result.grounding && typeof result.grounding === 'object') {
        const g = result.grounding as { total?: number; passed?: number; rejected?: number };
        const rate = result.citationRate as { rate?: number } | undefined;
        setGroundingInfo(
          `引用统计：硬事实 ${g.total ?? 0} 处 · 通过 ${g.passed ?? 0} · 拒绝 ${g.rejected ?? 0}` +
            (rate && typeof rate.rate === 'number' ? ` · 引用有效率 ${(rate.rate * 100).toFixed(0)}%` : ''),
        );
      }
      if (result.error) {
        setError(result.error || '正文生成失败，请重试');
        setStreamContent('');
      } else if (result.content) {
        setStreamContent(result.content);
      } else {
        setStreamContent('');
        setError('生成结果为空，请重试');
      }
      // G-2：末尾事件携带 citation_ledger → 正文【n】可点查来源
      if (result.citationLedger && typeof result.citationLedger === 'object') {
        setCitationLedger(result.citationLedger as CitationLedger);
      }
      // BUG-16：生成完成后刷新章节下拉（标注 ✅ 与字数）
      if (selectedProjectId) loadChapters(selectedProjectId);
    } catch (e: unknown) {
      if (axios.isCancel(e)) {
        setError('已取消生成');
      } else {
        const err = e as { response?: { data?: { detail?: string; error?: string } } };
        const detail = err?.response?.data?.detail || err?.response?.data?.error;
        setError(detail || (e instanceof Error ? e.message : '正文生成失败'));
      }
      setStreamContent('');
    } finally {
      streamAbortRef.current = null;
      setIsStreaming(false);
    }
  }, [selectedProjectId, loadChapters]);

  const handleStopStream = () => {
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    setIsStreaming(false);
  };

  const updateNodeTitle = (nodes: OutlineNode[], nodeId: string, newTitle: string): OutlineNode[] => {
    return nodes.map(node => {
      if (node.id === nodeId) {
        return { ...node, title: newTitle };
      }
      if (node.children) {
        return { ...node, children: updateNodeTitle(node.children, nodeId, newTitle) };
      }
      return node;
    });
  };

  const removeNode = (nodes: OutlineNode[], nodeId: string): OutlineNode[] => {
    return nodes
      .filter(node => node.id !== nodeId)
      .map(node => ({
        ...node,
        children: node.children ? removeNode(node.children, nodeId) : [],
      }));
  };

  const addChildNode = (nodes: OutlineNode[], parentId: string, child: OutlineNode): OutlineNode[] => {
    return nodes.map(node => {
      if (node.id === parentId) {
        return { ...node, children: [...(node.children || []), child] };
      }
      if (node.children) {
        return { ...node, children: addChildNode(node.children, parentId, child) };
      }
      return node;
    });
  };

  const handleSaveEdit = (nodeId: string) => {
    setOutlineNodes(prev => updateNodeTitle(prev, nodeId, editText));
    setEditingNode(null);
    setEditText('');
    setOutlineMessage('');
  };

  const handleSaveOutline = async () => {
    if (!selectedProjectId || outlineNodes.length === 0) return;
    setOutlineSaving(true);
    setOutlineMessage('');
    setError('');
    try {
      const result = await generateApi.saveOutline(selectedProjectId, { chapters: outlineNodes });
      await loadChapters(selectedProjectId);
      setOutlineMessage(`大纲已保存，当前共 ${result.data?.chapters ?? outlineNodes.length} 个章节`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '大纲保存失败');
    } finally {
      setOutlineSaving(false);
    }
  };

  const handleCancelEdit = () => {
    setEditingNode(null);
    setEditText('');
  };

  const renderOutlineNode = (node: OutlineNode, depth: number = 0) => {
    const isEditing = editingNode === node.id;
    const indent = depth * 24;

    return (
      <div key={node.id}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            padding: '6px 8px',
            marginLeft: `${indent}px`,
            borderRadius: '6px',
            background: isEditing ? '#eff6ff' : 'transparent',
            border: '1px solid transparent',
            fontSize: '13px',
          }}
          onMouseEnter={(e) => {
            if (!isEditing) (e.currentTarget as HTMLDivElement).style.background = '#f8fafc';
          }}
          onMouseLeave={(e) => {
            if (!isEditing) (e.currentTarget as HTMLDivElement).style.background = 'transparent';
          }}
        >
          <GripVertical size={14} color="#94a3b8" style={{ cursor: 'grab', flexShrink: 0 }} />

          {node.children && node.children.length > 0 ? (
            <ChevronDown size={14} color="#64748b" style={{ flexShrink: 0 }} />
          ) : (
            <ChevronRight size={14} color="#cbd5e1" style={{ flexShrink: 0 }} />
          )}

          {isEditing ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '4px', flex: 1 }}>
              <input
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleSaveEdit(node.id);
                  if (e.key === 'Escape') handleCancelEdit();
                }}
                style={{
                  flex: 1,
                  padding: '4px 8px',
                  border: '1px solid var(--color-primary)',
                  borderRadius: '4px',
                  fontSize: '13px',
                  outline: 'none',
                }}
                autoFocus
              />
              <button onClick={() => handleSaveEdit(node.id)} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px' }}>
                <Check size={14} color="#059669" />
              </button>
              <button onClick={handleCancelEdit} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px' }}>
                <X size={14} color="#dc2626" />
              </button>
            </div>
          ) : (
            <>
              <span style={{ flex: 1, fontWeight: depth < 2 ? 600 : 400 }}>
                {node.title}
              </span>
              {node.score_mapping && node.score_mapping.length > 0 && (
                <span style={{ fontSize: '11px', color: '#059669', background: '#ecfdf5', padding: '2px 6px', borderRadius: '4px' }}>
                  {node.score_mapping.length}项评分
                </span>
              )}
              {node.page_target && (
                <span style={{ fontSize: '11px', color: '#475569', background: '#f1f5f9', padding: '2px 6px', borderRadius: '4px' }}>
                  ~{node.page_target}页
                </span>
              )}
              <button
                onClick={() => { setEditingNode(node.id); setEditText(node.title); }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px', opacity: 0.5 }}
                title="编辑"
              >
                <Edit3 size={12} />
              </button>
              <button
                onClick={() => setOutlineNodes(prev => removeNode(prev, node.id))}
                style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px', opacity: 0.5, color: '#dc2626' }}
                title="删除"
              >
                <Trash2 size={12} />
              </button>
              <button
                onClick={() => {
                  const child: OutlineNode = {
                    id: `new_${Date.now()}`,
                    title: '新章节',
                    level: node.level + 1,
                    children: [],
                    status: 'pending',
                  };
                  setOutlineNodes(prev => addChildNode(prev, node.id, child));
                }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px', opacity: 0.5, color: '#059669' }}
                title="添加子章节"
              >
                <Plus size={12} />
              </button>
            </>
          )}
        </div>
        {node.children && node.children.map(child => renderOutlineNode(child, depth + 1))}
      </div>
    );
  };

  const sectionTabs = [
    { key: 'outline', label: '大纲编辑', icon: <ListTree size={16} /> },
    { key: 'generate', label: '正文生成', icon: <PenTool size={16} /> },
    { key: 'gate', label: '闸门审核', icon: <Shield size={16} /> },
    { key: 'structure', label: '结构模板', icon: <FileText size={16} /> },
    { key: 'coverage', label: '评分覆盖', icon: <AlertTriangle size={16} /> },
  ];

  const handleGenerateAiImage = async () => {
    if (!aiImagePrompt.trim()) return;
    setAiImageLoading(true);
    setAiImageResult('');
    setError('');
    setAiImageError('');
    try {
      const res = await aiImageApi.generate(aiImagePrompt, aiImageProvider, aiImageSize);
      const imageUrl = res.data?.image_url || res.data?.url || '';
      setAiImageResult(imageUrl);
      if (imageUrl) {
        setAiImageHistory(prev => [{ prompt: aiImagePrompt, url: imageUrl, timestamp: Date.now() }, ...prev].slice(0, 5));
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      const detail = err?.response?.data?.detail;
      setAiImageError(detail ? `配图生成失败：${detail}` : (err?.message ? `配图生成失败：${err.message}` : 'AI配图生成失败'));
    } finally {
      setAiImageLoading(false);
    }
  };

  const handleGenerateChapterIllustrations = async () => {
    const projectId = selectedProjectId || currentProjectId || '';
    if (!projectId || !illustrationChapterId) {
      setError('请先选择要生成配图的章节');
      return;
    }
    setLoading(true);
    setError('');
    setExportStatus('正在生成并保存本章配图，请稍候…');
    try {
      const res = await generateApi.generateChapterIllustrations(projectId, illustrationChapterId, {
        provider: aiImageProvider,
        imageSize: aiImageSize,
      });
      const count = Number(res.data?.generated || 0);
      setExportStatus(res.data?.reused ? `本章已存在 ${count} 张已保存配图` : `本章已保存 ${count} 张 AI 配图，可继续生成其他章节`);
      await loadChapters(projectId);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '本章配图生成失败');
      setExportStatus('本章配图生成失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-fade-in">
      <div style={{
        background: 'var(--color-surface)',
        borderRadius: '12px',
        padding: '20px 24px',
        border: '1px solid var(--color-border)',
        marginBottom: '20px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <PenTool size={22} color="#059669" />
          <div>
            <h2 style={{ fontSize: '18px', fontWeight: 700, color: 'var(--color-text)', margin: 0 }}>高级编辑与修订</h2>
            <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', margin: '4px 0 0' }}>编辑已有大纲、章节正文和检查结果</p>
          </div>
        </div>
      </div>

      <div style={{ marginBottom: '20px' }}>
        <select
          value={selectedProjectId || currentProjectId || ''}
          onChange={(e) => setSelectedProjectId(e.target.value)}
          style={{
            width: '100%',
            padding: '8px 12px',
            border: '1px solid var(--color-border)',
            borderRadius: '6px',
            fontSize: '14px',
            background: 'var(--color-surface)',
          }}
        >
          <option value="">请选择项目</option>
          {(selectedProjectId || currentProjectId) && !projects.some(p => p.id === (selectedProjectId || currentProjectId)) && (
            <option value={selectedProjectId || currentProjectId || ''}>当前项目（已恢复）</option>
          )}
          {projects.map(p => (
            <option key={p.id} value={p.id}>{p.name} ({p.status})</option>
          ))}
        </select>
      </div>

      <div style={{ display: 'flex', gap: '8px', marginBottom: '20px' }}>
        {sectionTabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveSection(tab.key as typeof activeSection)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              padding: '8px 14px',
              background: activeSection === tab.key ? 'var(--color-primary)' : 'var(--color-surface)',
              color: activeSection === tab.key ? 'white' : 'var(--color-text)',
              border: `1px solid ${activeSection === tab.key ? 'var(--color-primary)' : 'var(--color-border)'}`,
              borderRadius: '8px',
              cursor: 'pointer',
              fontSize: '13px',
              fontWeight: 500,
            }}
          >
            {tab.icon}
            {tab.label}
          </button>
        ))}
      </div>

      {activeSection === 'outline' && (
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '16px', fontWeight: 600 }}>大纲编辑器</h3>
            <div style={{ display: 'flex', gap: '8px' }}>
              <select
                value={outlineMode}
                onChange={(e) => setOutlineMode(e.target.value)}
                style={{ padding: '6px 10px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '13px' }}
              >
                <option value="aligned">对齐评分项模式</option>
                <option value="free">自由模式</option>
              </select>
              <button
                onClick={handleGenerateOutline}
                disabled={loading || !selectedProjectId}
                style={{
                  padding: '6px 14px',
                  background: 'var(--color-primary)',
                  color: 'white',
                  border: 'none',
                  borderRadius: '6px',
                  cursor: loading || !selectedProjectId ? 'not-allowed' : 'pointer',
                  fontSize: '13px',
                }}
              >
                {loading ? (generatingProgress || '生成中...') : '生成大纲'}
              </button>
              <button
                onClick={handleSaveOutline}
                disabled={outlineSaving || loading || !selectedProjectId || outlineNodes.length === 0}
                style={{
                  padding: '6px 14px',
                  background: outlineSaving || loading || !selectedProjectId || outlineNodes.length === 0 ? '#94a3b8' : '#2563eb',
                  color: 'white',
                  border: 'none',
                  borderRadius: '6px',
                  cursor: outlineSaving || loading || !selectedProjectId || outlineNodes.length === 0 ? 'not-allowed' : 'pointer',
                  fontSize: '13px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                }}
              >
                <Check size={14} /> {outlineSaving ? '保存中...' : '保存大纲'}
              </button>
              {loading && graphRunDetail && (
                <RunStatusStrip detail={graphRunDetail} loading={false} error={null} title="章节生成图运行" />
              )}
              <button
                onClick={() => {
                  const newNode: OutlineNode = {
                    id: `new_${Date.now()}`,
                    title: '新章节',
                    level: 1,
                    children: [],
                    status: 'pending',
                  };
                  setOutlineNodes(prev => [...prev, newNode]);
                }}
                style={{
                  padding: '6px 14px',
                  background: '#059669',
                  color: 'white',
                  border: 'none',
                  borderRadius: '6px',
                  cursor: 'pointer',
                  fontSize: '13px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                }}
              >
                <Plus size={14} /> 添加章节
              </button>
            </div>
          </div>

          {outlineNodes.length > 0 ? (
            <div style={{ border: '1px solid var(--color-border)', borderRadius: '8px', padding: '12px', maxHeight: '500px', overflow: 'auto' }}>
              {outlineNodes.map(node => renderOutlineNode(node))}
            </div>
          ) : (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--color-text-secondary)', fontSize: '14px' }}>
              {outlineResult ? '大纲数据格式不匹配，请查看原始结果' : '点击"生成大纲"开始'}
            </div>
          )}

          {outlineMessage && (
            <div style={{ marginTop: '10px', fontSize: '12px', color: '#059669' }}>{outlineMessage}</div>
          )}

          {outlineResult && (
            <details style={{ marginTop: '12px' }}>
              <summary style={{ fontSize: '12px', color: 'var(--color-text-secondary)', cursor: 'pointer' }}>查看原始JSON结果</summary>
              <CopyableJson data={outlineResult} maxheight="200px" />
            </details>
          )}

          {outlineBasis && (
            <div style={{ marginTop: '16px', padding: '14px', background: '#f0f9ff', borderRadius: '8px', border: '1px solid #bfdbfe' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
                <Info size={16} color="#2563eb" />
                <span style={{ fontSize: '14px', fontWeight: 600, color: '#1e40af' }}>生成依据</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px', marginBottom: outlineBasis.scoringItems.length > 0 ? '12px' : '0' }}>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '13px', fontWeight: 600, color: '#1e40af' }}>
                    {outlineBasis.mode === 'aligned' ? '对齐评分项模式' : '自由模式'}
                  </div>
                  <div style={{ fontSize: '11px', color: '#64748b' }}>生成模式</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '13px', fontWeight: 600, color: '#059669' }}>
                    {outlineBasis.matchedCount} / {outlineBasis.totalItems}
                  </div>
                  <div style={{ fontSize: '11px', color: '#64748b' }}>评分项匹配</div>
                </div>
                <div style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: '13px', fontWeight: 600, color: '#64748b' }}>
                    {outlineNodes.length} 章
                  </div>
                  <div style={{ fontSize: '11px', color: '#64748b' }}>大纲章节数</div>
                </div>
              </div>
              {outlineBasis.scoringItems.length > 0 && (
                <div>
                  <div style={{ fontSize: '12px', fontWeight: 600, color: '#475569', marginBottom: '6px' }}>评分项→章节映射</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {outlineBasis.scoringItems.map((item, idx) => (
                      <div key={idx} style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', padding: '4px 8px', background: 'white', borderRadius: '4px', border: '1px solid #e2e8f0' }}>
                        <span style={{ color: '#059669', fontWeight: 500, minWidth: '120px' }}>{item.item}</span>
                        <span style={{ color: '#94a3b8' }}>→</span>
                        <span style={{ color: '#1e40af' }}>{String(item.score)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {outlineBasis.mode === 'free' && (
                <div style={{ fontSize: '12px', color: '#64748b', marginTop: '4px' }}>
                  自由模式：大纲仅基于招标文件内容生成，未对齐评分项。如需对齐，请选择"对齐评分项模式"。
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {activeSection === 'generate' && (
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>正文生成</h3>

          <div style={{ padding: '16px', background: '#f0f9ff', borderRadius: '10px', border: '1px solid #bfdbfe', marginBottom: '16px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <ImageIcon size={18} color="#2563eb" />
                <span style={{ fontSize: '14px', fontWeight: 600, color: '#1e40af' }}>可选配图</span>
              </div>
              <label style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer', fontSize: '13px', fontWeight: 500 }}>
                <input
                  type="checkbox"
                  checked={enableIllustration}
                  onChange={(e) => setEnableIllustration(e.target.checked)}
                  style={{ width: '16px', height: '16px', cursor: 'pointer' }}
                />
                启用AI配图
              </label>
            </div>

            {enableIllustration && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px' }}>
                <div>
                  <label style={{ fontSize: '12px', color: '#1e40af', display: 'block', marginBottom: '4px' }}>配图供应商</label>
                  <select
                    value={aiImageProvider}
                    onChange={(e) => setAiImageProvider(e.target.value)}
                    style={{ width: '100%', padding: '6px 10px', border: '1px solid #bfdbfe', borderRadius: '6px', fontSize: '13px', background: 'white' }}
                  >
                    <option value="default">默认(内置)</option>
                    <option value="volcengine">火山方舟</option>
                    <option value="google">Google AI Studio</option>
                  </select>
                </div>
                <div>
                  <label style={{ fontSize: '12px', color: '#1e40af', display: 'block', marginBottom: '4px' }}>配图尺寸</label>
                  <select
                    value={aiImageSize}
                    onChange={(e) => setAiImageSize(e.target.value)}
                    style={{ width: '100%', padding: '6px 10px', border: '1px solid #bfdbfe', borderRadius: '6px', fontSize: '13px', background: 'white' }}
                  >
                    <option value="landscape_16_9">横版 16:9（推荐）</option>
                    <option value="landscape_4_3">横版 4:3</option>
                    <option value="portrait_4_3">竖版 4:3</option>
                    <option value="portrait_16_9">竖版 16:9</option>
                    <option value="square_hd">方形 高清</option>
                    <option value="square">方形</option>
                  </select>
                </div>
                <div>
                  <label style={{ fontSize: '12px', color: '#1e40af', display: 'block', marginBottom: '4px' }}>去水印</label>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 0' }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer', fontSize: '13px' }}>
                      <input
                        type="checkbox"
                        checked={removeWatermark}
                        onChange={(e) => setRemoveWatermark(e.target.checked)}
                        style={{ width: '16px', height: '16px', cursor: 'pointer' }}
                      />
                      自动去水印
                    </label>
                    <span style={{ fontSize: '11px', color: '#6b7280' }}>
                      {removeWatermark ? '✓ 将自动检测并移除水印' : '保留原始图片'}
                    </span>
                  </div>
                </div>
              </div>
            )}

            {enableIllustration && (
              <div style={{ marginTop: '12px', display: 'flex', gap: '8px', alignItems: 'center' }}>
                <input
                  type="text"
                  value={aiImagePrompt}                  onChange={(e) => setAiImagePrompt(e.target.value)}
                  placeholder="自定义配图描述（留空则根据章节标题自动生成）"
                  style={{ flex: 1, padding: '6px 10px', border: '1px solid #bfdbfe', borderRadius: '6px', fontSize: '13px', background: 'white' }}
                />
                <button
                  onClick={handleGenerateAiImage}
                  disabled={aiImageLoading || !aiImagePrompt.trim()}
                  style={{
                    padding: '6px 14px',
                    background: aiImageLoading || !aiImagePrompt.trim() ? '#94a3b8' : '#2563eb',
                    color: 'white',
                    border: 'none',
                    borderRadius: '6px',
                    cursor: aiImageLoading || !aiImagePrompt.trim() ? 'not-allowed' : 'pointer',
                    fontSize: '12px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {aiImageLoading ? <Loader2 size={12} /> : <ImageIcon size={12} />}
                  {aiImageLoading ? '生成中...' : '预览配图'}
                </button>
              </div>
            )}
            {aiImageError && (
              <div style={{ marginTop: '8px', padding: '8px 10px', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: '6px', color: '#dc2626', fontSize: '12px' }}>
                {aiImageError}
              </div>
            )}

            {aiImageResult && enableIllustration && (
              <div style={{ marginTop: '12px', display: 'flex', gap: '12px', alignItems: 'flex-start' }}>
                <img
                  src={aiImageResult}
                  alt="AI配图预览"
                  style={{ width: '200px', height: '120px', objectFit: 'cover', borderRadius: '6px', border: '1px solid #bfdbfe' }}
                />
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: '12px', color: '#059669', marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                    {removeWatermark ? '✓ 已启用去水印' : '○ 未启用去水印'}
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>
                    此配图将在正文生成时自动插入到对应章节中
                  </div>
                  {aiImageHistory.length > 0 && (
                    <div style={{ marginTop: '8px', display: 'flex', gap: '6px' }}>
                      {aiImageHistory.slice(0, 4).map((item, idx) => (
                        <img
                          key={idx}
                          src={item.url}
                          alt={item.prompt}
                          onClick={() => setAiImageResult(item.url)}
                          style={{ width: '48px', height: '36px', objectFit: 'cover', borderRadius: '4px', cursor: 'pointer', border: '1px solid #bfdbfe' }}
                        />
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          {/* BUG-15：配图供应商 API Key 配置入口（选中的供应商展示配置状态） */}
          {enableIllustration && (
            <div style={{ marginTop: '12px', padding: '12px', background: '#f8fafc', borderRadius: '8px', border: '1px solid #e2e8f0' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                <span style={{ fontSize: '12px', fontWeight: 600, color: '#475569' }}>API Key 配置</span>
                <span style={{ fontSize: '11px', color: aiImageProvider === 'default' ? '#94a3b8' : (aiImageProviders.find(p => p.name === aiImageProvider)?.configured ? '#059669' : '#d97706') }}>
                  {aiImageProvider === 'default'
                    ? '默认(内置)无需配置'
                    : (aiImageProviders.find(p => p.name === aiImageProvider)?.configured ? '✅ 已配置' : '⚠ 未配置，请填写 Key')}
                </span>
              </div>
              {aiImageProvider !== 'default' && (
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                  <input
                    type="password"
                    value={apiKeyInput}
                    onChange={(e) => setApiKeyInput(e.target.value)}
                    placeholder="填写 API Key（留空不修改）"
                    style={{ flex: 1, padding: '6px 10px', border: '1px solid #bfdbfe', borderRadius: '6px', fontSize: '12px', background: 'white' }}
                  />
                  <button
                    onClick={handleSaveApiKey}
                    disabled={apiKeySaving || !apiKeyInput.trim()}
                    style={{
                      padding: '6px 14px',
                      background: apiKeySaving || !apiKeyInput.trim() ? '#94a3b8' : '#2563eb',
                      color: 'white',
                      border: 'none',
                      borderRadius: '6px',
                      cursor: apiKeySaving || !apiKeyInput.trim() ? 'not-allowed' : 'pointer',
                      fontSize: '12px',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {apiKeySaving ? '保存中...' : '保存 Key'}
                  </button>
                </div>
              )}
              {apiKeyMessage && (
                <div style={{ marginTop: '6px', fontSize: '11px', color: apiKeyMessage.type === 'success' ? '#059669' : '#dc2626' }}>
                  {apiKeyMessage.text}
                </div>
              )}
            </div>
          )}
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '16px' }}>
            <div>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>
                章节
                <span
                  onClick={() => setManualChapterMode(v => !v)}
                  style={{ marginLeft: '8px', fontSize: '11px', color: '#2563eb', cursor: 'pointer' }}
                >
                  {manualChapterMode ? '← 返回章节选择' : '手动输入 ID'}
                </span>
              </label>
              {manualChapterMode ? (
                <input
                  value={manualChapterId}
                  onChange={(e) => setManualChapterId(e.target.value)}
                  placeholder="输入章节ID，如 ch1"
                  style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px' }}
                />
              ) : chapters.length > 0 ? (
                <select
                  value={chapterId}
                  onChange={(e) => setChapterId(e.target.value)}
                  style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', background: 'var(--color-surface)' }}
                >
                  <option value="">请选择章节</option>
                  {chapters.map(c => (
                    <option key={c.id} value={c.id}>
                      {'　'.repeat(Math.max(0, c.level - 1))}{c.id} {c.title}{c.has_content ? ' ✅' : ''}
                    </option>
                  ))}
                </select>
              ) : (
                <div style={{ padding: '10px 12px', border: '1px dashed var(--color-border)', borderRadius: '6px', fontSize: '12px', color: '#d97706', background: '#fffbeb' }}>
                  请先回到「大纲编辑」生成大纲（章节会自动出现在这里）
                </div>
              )}
              {chapterId && chapters.find(c => c.id === chapterId && c.has_content) && (
                <button
                  onClick={async () => {
                    try {
                      const res = await generateApi.chapterContent(selectedProjectId!, chapterId);
                      setSavedChapter({ id: res.data?.chapter_id || chapterId, title: res.data?.title || '', content: res.data?.content || '' });
                      setCitationLedger((res.data?.citation_ledger as CitationLedger) || null);
                    } catch (e) {
                      setError(e instanceof Error ? e.message : '章节读取失败');
                    }
                  }}
                  style={{ marginTop: '6px', padding: '4px 10px', fontSize: '12px', border: '1px solid var(--color-border)', borderRadius: '6px', background: 'var(--color-surface)', cursor: 'pointer' }}
                >
                  查看已存正文与引用来源（【n】可点查）
                </button>
              )}
              {savedChapter && (
                <div style={{ marginTop: '8px', border: '1px solid var(--color-border)', borderRadius: '8px', padding: '10px', maxHeight: '320px', overflow: 'auto' }}>
                  <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginBottom: '6px' }}>
                    已存章节：{savedChapter.title}
                    <button onClick={() => setSavedChapter(null)} style={{ float: 'right', border: 'none', background: 'none', cursor: 'pointer', fontSize: '12px' }}>关闭</button>
                  </div>
                  <CitationContentView content={savedChapter.content} citationLedger={citationLedger} maxHeight="140px" />
                </div>
              )}
            </div>
            <div>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>生成模式</label>
              <select
                id="gen-mode-select"
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px' }}
              >
                <option value="A">A模式(知识库+大纲)</option>
                <option value="B">B模式(纯知识库)</option>
                <option value="C">C模式(纯大纲)</option>
                <option value="D">D模式(自由生成)</option>
              </select>
            </div>
          </div>

          <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
            <button
              onClick={() => {
                const mode = (document.getElementById('gen-mode-select') as HTMLSelectElement)?.value || 'A';
                const cid = manualChapterMode ? manualChapterId.trim() : chapterId;
                if (!cid) return;
                handleStreamGenerate(cid, mode);
              }}
              disabled={isStreaming || !selectedProjectId || (!manualChapterMode && !chapterId) || (manualChapterMode && !manualChapterId.trim()) || (!manualChapterMode && chapters.length === 0)}
              style={{
                padding: '8px 16px',
                background: 'var(--color-primary)',
                color: 'white',
                border: 'none',
                borderRadius: '8px',
                cursor: isStreaming || !selectedProjectId ? 'not-allowed' : 'pointer',
                fontSize: '13px',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
              }}
            >
              <Play size={14} /> {isStreaming ? '生成中...' : '生成正文'}
            </button>
            {isStreaming && (
              <button
                onClick={handleStopStream}
                style={{
                  padding: '8px 16px',
                  background: '#dc2626',
                  color: 'white',
                  border: 'none',
                  borderRadius: '8px',
                  cursor: 'pointer',
                  fontSize: '13px',
                }}
              >
                停止
              </button>
            )}
            <button
              onClick={handleExtractMandatory}
              disabled={loading || !selectedProjectId}
              style={{
                padding: '8px 16px',
                background: '#d97706',
                color: 'white',
                border: 'none',
                borderRadius: '8px',
                cursor: loading || !selectedProjectId ? 'not-allowed' : 'pointer',
                fontSize: '13px',
              }}
            >
              提取实质性要求
            </button>
            <button
              onClick={handleExportDocx}
              disabled={loading || !selectedProjectId}
              style={{
                padding: '8px 16px',
                background: '#059669',
                color: 'white',
                border: 'none',
                borderRadius: '8px',
                cursor: loading || !selectedProjectId ? 'not-allowed' : 'pointer',
                fontSize: '13px',
              }}
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
              {loading ? '导出正文中…' : '导出正文 DOCX（直连）'}
            </button>
            {/* 配图生成与正文导出是两个独立动作；此开关只决定导出是否带入已保存配图。 */}
            <label style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '12px', color: 'var(--color-text-secondary)', cursor: 'pointer', alignSelf: 'center' }}>
              <input
                type="checkbox"
                checked={exportWithIllustrations}
                onChange={(e) => setExportWithIllustrations(e.target.checked)}
                disabled={loading}
              />
              导出时包含已生成配图
              </label>
            <>
                <select
                  aria-label="选择配图章节"
                  value={illustrationChapterId}
                  onChange={(e) => setIllustrationChapterId(e.target.value)}
                  disabled={loading}
                  style={{ maxWidth: '260px', padding: '5px 8px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '12px', background: 'white' }}
                >
                  <option value="">选择要生成配图的章节</option>
                  {chapters.map((chapter) => (
                    <option key={chapter.id} value={chapter.id}>{chapter.id} {chapter.title}</option>
                  ))}
                </select>
                <button
                  onClick={handleGenerateChapterIllustrations}
                  disabled={loading || !illustrationChapterId}
                  style={{ padding: '6px 10px', background: loading || !illustrationChapterId ? '#cbd5e1' : '#2563eb', color: 'white', border: 'none', borderRadius: '6px', cursor: loading || !illustrationChapterId ? 'not-allowed' : 'pointer', fontSize: '12px', whiteSpace: 'nowrap' }}
                >
                  {loading ? '生成中…' : '生成本章配图'}
                </button>
            </>
            {exportStatus && (
              <span style={{ alignSelf: 'center', fontSize: '12px', color: exportStatus.includes('失败') ? '#dc2626' : '#475569' }}>
                {exportStatus}
              </span>
            )}
          </div>

          {(streamContent || isStreaming) && (
            <div style={{ border: '1px solid var(--color-border)', borderRadius: '8px', padding: '16px', background: '#f8fafc', maxHeight: '400px', overflow: 'auto' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                  {isStreaming ? '正在生成...' : '生成完成'}
                </span>
                {isStreaming && <Loader2 size={14} className="animate-spin" />}
              </div>
              <div style={{ fontSize: '14px', lineHeight: '1.8' }}>
                <CitationContentView content={streamContent} citationLedger={citationLedger} maxHeight="160px" />
                {isStreaming && <span style={{ animation: 'blink 1s infinite' }}>▊</span>}
              </div>
              {!isStreaming && groundingInfo && (
                <div style={{ marginTop: '8px', fontSize: '12px', color: 'var(--color-text-secondary)' }}>
                  {groundingInfo}
                </div>
              )}
            </div>
          )}

          {mandatoryResult && (
            <div style={{ marginTop: '16px' }}>
              <h4 style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>实质性要求提取结果</h4>
              <CopyableJson data={mandatoryResult} maxheight="300px" />
            </div>
          )}
        </div>
      )}

      {activeSection === 'gate' && (
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>闸门审核确认</h3>
          <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginBottom: '16px' }}>
            每个阶段完成后需确认审核，通过后才能进入下一阶段
          </p>

          {gates.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {gates.map((gate, idx) => (
                <div
                  key={gate.stage}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    padding: '16px',
                    border: '1px solid var(--color-border)',
                    borderRadius: '8px',
                    background: gate.status === 'confirmed' ? '#ecfdf5' : gate.status === 'blocked' ? '#fef2f2' : '#f8fafc',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                    <div style={{
                      width: '32px',
                      height: '32px',
                      borderRadius: '50%',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      background: gate.status === 'confirmed' ? '#059669' : gate.status === 'blocked' ? '#dc2626' : '#94a3b8',
                      color: 'white',
                      fontSize: '14px',
                      fontWeight: 600,
                    }}>
                      {idx + 1}
                    </div>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: '14px' }}>{gate.label}</div>
                      {gate.items && gate.items.length > 0 && (
                        <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
                          检查项: {gate.items.join('、')}
                        </div>
                      )}
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{
                      fontSize: '12px',
                      padding: '4px 10px',
                      borderRadius: '12px',
                      background: gate.status === 'confirmed' ? '#d1fae5' : gate.status === 'blocked' ? '#fee2e2' : '#e2e8f0',
                      color: gate.status === 'confirmed' ? '#059669' : gate.status === 'blocked' ? '#dc2626' : '#64748b',
                    }}>
                      {gate.status === 'confirmed' ? '已确认' : gate.status === 'blocked' ? '已阻断' : '待确认'}
                    </span>
                    {gate.status === 'pending' && (
                      <button
                        onClick={() => handleConfirmGate(gate.stage)}
                        style={{
                          padding: '6px 14px',
                          background: '#059669',
                          color: 'white',
                          border: 'none',
                          borderRadius: '6px',
                          cursor: 'pointer',
                          fontSize: '12px',
                        }}
                      >
                        确认通过
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--color-text-secondary)', fontSize: '14px' }}>
              暂无闸门信息，请先生成大纲
            </div>
          )}
        </div>
      )}

      {activeSection === 'structure' && (
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>标书5大结构模板</h3>
          <p style={{ fontSize: '13px', color: 'var(--color-text-secondary)', marginBottom: '16px' }}>
            自动生成标书标准结构章节，确保不遗漏任何必要部分
          </p>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: '12px' }}>
            {STRUCTURE_TEMPLATES.map(tpl => (
              <div
                key={tpl.key}
                style={{
                  padding: '16px',
                  border: '1px solid var(--color-border)',
                  borderRadius: '8px',
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                }}
                onClick={() => handleGenerateStructure(tpl.key)}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--color-primary)';
                  (e.currentTarget as HTMLDivElement).style.background = '#eff6ff';
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--color-border)';
                  (e.currentTarget as HTMLDivElement).style.background = 'transparent';
                }}
              >
                <div style={{ fontWeight: 600, fontSize: '14px', marginBottom: '4px' }}>{tpl.label}</div>
                <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>{tpl.desc}</div>
              </div>
            ))}
          </div>
          {loading && <div style={{ marginTop: '12px', fontSize: '13px', color: 'var(--color-text-secondary)' }}>结构模板生成中...</div>}
          {structureResult && (
            <div style={{ marginTop: '16px', border: '1px solid var(--color-border)', borderRadius: '8px', padding: '16px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <div style={{ fontWeight: 600, fontSize: '14px' }}>
                  {structureResult.name || '结构模板'} · 共 {structureResult.total_sections ?? structureResult.sections?.length ?? 0} 节 / 约 {structureResult.total_pages ?? 0} 页
                </div>
                <button
                  onClick={() => setStructureResult(null)}
                  style={{ background: 'transparent', border: 'none', cursor: 'pointer', fontSize: '12px', color: 'var(--color-text-secondary)' }}
                >
                  关闭
                </button>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {(structureResult.sections || []).map(sec => (
                  <div key={sec.id} style={{ paddingLeft: `${Math.max(0, (sec.level - 1)) * 16}px`, fontSize: '13px', display: 'flex', gap: '8px', alignItems: 'baseline' }}>
                    <span style={{ fontWeight: sec.level === 1 ? 600 : 400 }}>
                      {sec.level === 1 ? '▶ ' : '· '}{sec.title}
                    </span>
                    {typeof sec.page_target === 'number' && <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>约{sec.page_target}页</span>}
                    {sec.content_hint && <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>{sec.content_hint}</span>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {activeSection === 'coverage' && (
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3 style={{ fontSize: '16px', fontWeight: 600 }}>评分矩阵覆盖率</h3>
            <button
              onClick={handleScoreCoverage}
              disabled={loading || !selectedProjectId}
              style={{
                padding: '6px 14px',
                background: 'var(--color-primary)',
                color: 'white',
                border: 'none',
                borderRadius: '6px',
                cursor: loading || !selectedProjectId ? 'not-allowed' : 'pointer',
                fontSize: '13px',
              }}
            >
              {loading ? '计算中...' : '计算覆盖率'}
            </button>
          </div>

          {scoreCoverage ? (
            <div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px', marginBottom: '16px' }}>
                <div style={{ padding: '16px', background: '#ecfdf5', borderRadius: '8px', textAlign: 'center' }}>
                  <div style={{ fontSize: '24px', fontWeight: 700, color: '#059669' }}>
                    {String((scoreCoverage as Record<string, unknown>).coverage_rate || '0')}%
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>覆盖率</div>
                </div>
                <div style={{ padding: '16px', background: '#eff6ff', borderRadius: '8px', textAlign: 'center' }}>
                  <div style={{ fontSize: '24px', fontWeight: 700, color: '#2563eb' }}>
                    {String((scoreCoverage as Record<string, unknown>).covered_items || '0')}
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>已覆盖评分项</div>
                </div>
                <div style={{ padding: '16px', background: '#fef2f2', borderRadius: '8px', textAlign: 'center' }}>
                  <div style={{ fontSize: '24px', fontWeight: 700, color: '#dc2626' }}>
                    {String((scoreCoverage as Record<string, unknown>).uncovered_items || '0')}
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>未覆盖评分项</div>
                </div>
              </div>
              <CopyableJson data={scoreCoverage} maxheight="300px" />
            </div>
          ) : (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--color-text-secondary)', fontSize: '14px' }}>
              点击"计算覆盖率"查看评分项覆盖情况
            </div>
          )}
        </div>
      )}

      {error && (
        <div style={{ marginTop: '16px', padding: '12px', background: '#fef2f2', borderRadius: '8px', color: '#dc2626', fontSize: '13px' }}>
          {error}
        </div>
      )}

      <style>{`
        @keyframes blink {
          0%, 50% { opacity: 1; }
          51%, 100% { opacity: 0; }
        }
        .animate-spin {
          animation: spin 1s linear infinite;
        }
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
