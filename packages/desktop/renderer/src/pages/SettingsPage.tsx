import { useState, useEffect, useCallback } from 'react';
import { Settings, Loader2, CheckCircle2, XCircle, Bot, Save, RotateCcw, ChevronDown, ChevronRight, Cpu, Shield, Plus, Trash2, Users, UserPlus, X, ScanText, PlugZap, Activity, RefreshCw, Plug, Server, Database, Image as ImageIcon } from 'lucide-react';
import { llmApi, skillApi, rbacApi, ocrApi, mcpApi, a2aApi, aguiApi, diagnosticsApi, secretsApi } from '../services/api';

type SettingsTab = 'agents' | 'llm' | 'embedding' | 'reranker' | 'rbac' | 'skills' | 'ocr' | 'image' | 'protocols' | 'diagnostics';

interface AgentModel {
  name: string;
  display_name: string;
  description: string;
  model: string;
  temperature: number;
  max_tokens: number;
  enabled: boolean;
}

interface RbacRole {
  id: string;
  name: string;
  display_name: string;
  description: string;
  permissions?: RbacPermission[];
  users?: RbacUser[];
}

interface RbacPermission {
  id: string;
  code: string;
  name: string;
  description: string;
  category: string;
}

interface RbacUser {
  id: string;
  name: string;
  email: string;
  roles?: Array<{ id: string; name: string; display_name: string }>;
}

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<SettingsTab>('agents');

  const [apiKey, setApiKey] = useState('');
  const [apiBase, setApiBase] = useState('https://api.deepseek.com');
  const [model, setModel] = useState('deepseek/deepseek-chat');
  const [providers, setProviders] = useState<Array<{ id: string; name: string; models: string[] }>>([]);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);
  const [tokenUsage, setTokenUsage] = useState<Record<string, unknown> | null>(null);
  const [savingDefault, setSavingDefault] = useState(false);
  const [keyMasked, setKeyMasked] = useState<string | null>(null);

  const [imageEnabled, setImageEnabled] = useState(false);
  const [imageProvider, setImageProvider] = useState('fallback');
  const [imageBase, setImageBase] = useState('');
  const [imageModel, setImageModel] = useState('');
  const [imageKey, setImageKey] = useState('');
  const [imageKeyMasked, setImageKeyMasked] = useState<string | null>(null);
  const [imageKeySource, setImageKeySource] = useState('missing');
  const [imageSaving, setImageSaving] = useState(false);
  const [imageTesting, setImageTesting] = useState(false);
  const [imageMessage, setImageMessage] = useState<{ success: boolean; text: string } | null>(null);

  const [agents, setAgents] = useState<AgentModel[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(false);
  const [agentsSaving, setAgentsSaving] = useState<string>('');
  const [agentsMessage, setAgentsMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [expandedAgent, setExpandedAgent] = useState<string>('');

  const [skills, setSkills] = useState<Array<Record<string, unknown>>>([]);

  const [rbacRoles, setRbacRoles] = useState<RbacRole[]>([]);
  const [rbacPermissions, setRbacPermissions] = useState<RbacPermission[]>([]);
  const [rbacUsers, setRbacUsers] = useState<RbacUser[]>([]);
  const [selectedRoleId, setSelectedRoleId] = useState<string>('');
  const [rbacLoading, setRbacLoading] = useState(false);
  const [rbacMessage, setRbacMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set());
  const [showCreateRoleDialog, setShowCreateRoleDialog] = useState(false);
  const [newRoleName, setNewRoleName] = useState('');
  const [newRoleDisplayName, setNewRoleDisplayName] = useState('');
  const [newRoleDescription, setNewRoleDescription] = useState('');
  const [showCreateUserDialog, setShowCreateUserDialog] = useState(false);
  const [newUserName, setNewUserName] = useState('');
  const [newUserEmail, setNewUserEmail] = useState('');
  const [newUserPassword, setNewUserPassword] = useState('');
  const [editingUserId, setEditingUserId] = useState<string>('');
  const [editUserName, setEditUserName] = useState('');
  const [editUserEmail, setEditUserEmail] = useState('');
  const [deleteConfirmId, setDeleteConfirmId] = useState<string>('');

  const loadData = useCallback(async () => {
    try {
      const [providersRes, skillsRes, usageRes, agentsRes, defaultModelRes] = await Promise.allSettled([
        llmApi.listProviders(),
        skillApi.list(),
        llmApi.getUsage(),
        llmApi.listAgentModels(),
        llmApi.getDefaultModel(),
      ]);

      if (providersRes.status === 'fulfilled') {
        setProviders(providersRes.value.data.providers || []);
      }
      if (defaultModelRes.status === 'fulfilled') {
        const d = defaultModelRes.value.data;
        if (d.model) {
          setModel(d.model);
          const prefix = d.model.split('/')[0];
          if (prefix) setSelectedProviderId(prefix);
        }
        if (d.api_base) setApiBase(d.api_base);
        setKeyMasked(d.api_key_masked ?? null);
      }
      if (skillsRes.status === 'fulfilled') {
        setSkills(skillsRes.value.data.skills || []);
      }
      if (usageRes.status === 'fulfilled') {
        setTokenUsage(usageRes.value.data);
      }
      if (agentsRes.status === 'fulfilled') {
        setAgents(agentsRes.value.data.agents || []);
        if (agentsRes.value.data.agents?.length > 0 && !expandedAgent) {
          setExpandedAgent(agentsRes.value.data.agents[0].name);
        }
      }
    } catch (e) {
      console.error('加载配置失败', e);
    }
  }, [expandedAgent]);

  const loadImageConfig = useCallback(async () => {
    try {
      const response = await secretsApi.getImageConfig();
      const data = response.data;
      setImageEnabled(Boolean(data.enabled));
      setImageProvider(data.provider || 'fallback');
      setImageBase(data.api_base || '');
      setImageModel(data.model || '');
      setImageKeyMasked(data.api_key_masked || null);
      setImageKeySource(data.api_key_source || 'missing');
    } catch (e) { console.error('加载 AI 配图配置失败', e); }
  }, []);

  const saveImageConfig = async () => {
    setImageSaving(true);
    setImageMessage(null);
    try {
      const response = await secretsApi.putImageConfig({ enabled: imageEnabled, provider: imageProvider, api_base: imageBase, model: imageModel });
      setImageMessage({ success: true, text: 'AI 配图配置已保存。配图不会阻塞正文生成。' });
    } catch (e) { setImageMessage({ success: false, text: `保存失败：${(e as Error).message}` }); }
    finally { setImageSaving(false); }
  };

  const saveImageKey = async () => {
    setImageSaving(true);
    setImageMessage(null);
    try {
      const response = await secretsApi.putImageKey(imageKey);
      setImageKeyMasked(response.data.masked);
      setImageKeySource(response.data.cleared ? 'missing' : 'keyring');
      setImageKey('');
      setImageMessage({ success: true, text: response.data.cleared ? 'AI 配图 Key 已清除。' : 'AI 配图 Key 已保存到本机凭据管理器。' });
    } catch (e) { setImageMessage({ success: false, text: `保存 Key 失败：${(e as Error).message}` }); }
    finally { setImageSaving(false); }
  };

  const testImage = async () => {
    setImageTesting(true);
    setImageMessage(null);
    try {
      const response = await fetch('/api/ai-image/providers', { headers: { Authorization: `Bearer ${localStorage.getItem('bidmaster_token') || ''}` } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json() as { providers?: Array<{ name: string; configured: boolean }> };
      const provider = data.providers?.find((item) => item.name === imageProvider);
      setImageMessage({ success: Boolean(provider?.configured), text: provider?.configured ? `供应商 ${imageProvider} 已就绪，可在导出时启用配图。` : `供应商 ${imageProvider} 尚未配置完整 Key。` });
    } catch (e) { setImageMessage({ success: false, text: `检测失败：${(e as Error).message}` }); }
    finally { setImageTesting(false); }
  };

  const loadRbacData = useCallback(async () => {
    setRbacLoading(true);
    try {
      const [rolesRes, permsRes, usersRes] = await Promise.allSettled([
        rbacApi.listRoles(),
        rbacApi.listPermissions(),
        rbacApi.listUsers(),
      ]);
      if (rolesRes.status === 'fulfilled') {
        setRbacRoles(rolesRes.value.data.roles || rolesRes.value.data || []);
      }
      if (permsRes.status === 'fulfilled') {
        let perms = (permsRes.value.data.permissions || permsRes.value.data || []) as RbacPermission[];
        if (!Array.isArray(perms)) {
          const flatPerms: RbacPermission[] = [];
          for (const [cat, items] of Object.entries(perms)) {
            if (Array.isArray(items)) {
              for (const p of items) {
                flatPerms.push({ ...p, category: p.category || cat });
              }
            }
          }
          perms = flatPerms;
        }
        setRbacPermissions(perms);
        const categories = new Set<string>(perms.map((p) => p.category));
        setExpandedCategories(categories);
      }
      if (usersRes.status === 'fulfilled') {
        setRbacUsers(usersRes.value.data.users || usersRes.value.data || []);
      }
    } catch (e) {
      console.error('加载RBAC数据失败', e);
    } finally {
      setRbacLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    if (activeTab === 'rbac') {
      loadRbacData();
    }
  }, [activeTab, loadRbacData]);

  const handleTestConnection = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await llmApi.testConnection({
        provider: model.split('/')[0],
        api_key: apiKey,
        api_base: apiBase,
        model: model,
      });
      setTestResult({
        success: res.data.success,
        message: res.data.success ? '连接成功！' : (res.data.error || '连接失败'),
      });
    } catch (e: unknown) {
      setTestResult({
        success: false,
        message: e instanceof Error ? e.message : '连接失败',
      });
    } finally {
      setTesting(false);
    }
  };

  const handleSaveDefaultModel = async () => {
    setSavingDefault(true);
    try {
      // apiKey 为空表示不修改已有 key（后端 if api_key: 支持），避免占位值覆盖真实 key
      const payload: Record<string, string> = { model, api_base: apiBase };
      if (apiKey.trim()) payload.api_key = apiKey.trim();
      await llmApi.setDefaultModel(payload);
      setTestResult({ success: true, message: '默认模型配置已保存' });
    } catch (e: unknown) {
      setTestResult({ success: false, message: e instanceof Error ? e.message : '保存失败' });
    } finally {
      setSavingDefault(false);
    }
  };

  const handleAgentChange = (name: string, field: string, value: unknown) => {
    setAgents(prev => prev.map(a => a.name === name ? { ...a, [field]: value } : a));
  };

  const handleSaveAgent = async (agent: AgentModel) => {
    setAgentsSaving(agent.name);
    setAgentsMessage(null);
    try {
      await llmApi.updateAgentModel(agent.name, {
        name: agent.name,
        display_name: agent.display_name,
        description: agent.description,
        model: agent.model,
        temperature: agent.temperature,
        max_tokens: agent.max_tokens,
        enabled: agent.enabled,
      });
      setAgentsMessage({ type: 'success', text: `${agent.display_name} 配置已保存` });
    } catch (e: unknown) {
      setAgentsMessage({ type: 'error', text: e instanceof Error ? e.message : '保存失败' });
    } finally {
      setAgentsSaving('');
    }
  };

  const handleResetAgents = async () => {
    setAgentsLoading(true);
    setAgentsMessage(null);
    try {
      await llmApi.resetAgentModels();
      const res = await llmApi.listAgentModels();
      setAgents(res.data.agents || []);
      setAgentsMessage({ type: 'success', text: '已重置所有Agent配置为默认值' });
    } catch (e: unknown) {
      setAgentsMessage({ type: 'error', text: e instanceof Error ? e.message : '重置失败' });
    } finally {
      setAgentsLoading(false);
    }
  };

  const [selectedProviderId, setSelectedProviderId] = useState('deepseek');
  const [customModel, setCustomModel] = useState('');
  const [useCustomModel, setUseCustomModel] = useState(false);

  const [agentCustomModel, setAgentCustomModel] = useState('');
  const [agentUseCustom, setAgentUseCustom] = useState<string>('');

  const allModelOptions = providers.flatMap(p =>
    p.models.map(m => ({ value: `${p.id}/${m}`, label: `${p.name} - ${m}`, providerId: p.id }))
  );

  const providerApiBases: Record<string, string> = {
    deepseek: 'https://api.deepseek.com',
    zhipu: 'https://open.bigmodel.cn/api/paas/v4',
    qianfan: 'https://aip.baidubce.com',
    dashscope: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    siliconflow: 'https://api.siliconflow.cn/v1',
    ollama: 'http://localhost:11434',
    openai: 'https://api.openai.com/v1',
  };

  const handleProviderChange = (providerId: string) => {
    setSelectedProviderId(providerId);
    // 仅当 API Base 仍为空或上一家供应商的预设值时才覆盖，用户自定义的 Base 保持不动
    const prevPresetBase = providerApiBases[selectedProviderId] || '';
    if (!apiBase || apiBase === prevPresetBase) {
      setApiBase(providerApiBases[providerId] || '');
    }
    // 不重置自定义模型开关与输入；仅当模型仍为空/上一家的预设首项时才切换到新供应商预设首项
    const prevProvider = providers.find(p => p.id === selectedProviderId);
    const prevPresetModel = prevProvider?.models[0] ? `${selectedProviderId}/${prevProvider.models[0]}` : '';
    if (!model || model === prevPresetModel) {
      const provider = providers.find(p => p.id === providerId);
      if (provider && provider.models.length > 0) {
        setModel(`${providerId}/${provider.models[0]}`);
      }
    }
  };

  const handlePresetModelChange = (modelValue: string) => {
    setModel(modelValue);
    setUseCustomModel(false);
  };

  const handleCustomModelConfirm = () => {
    if (customModel.trim()) {
      const prefix = selectedProviderId;
      const fullModel = customModel.includes('/') ? customModel : `${prefix}/${customModel.trim()}`;
      setModel(fullModel);
    }
  };

  const handleCreateRole = async () => {
    if (!newRoleName || !newRoleDisplayName) return;
    try {
      await rbacApi.createRole({ name: newRoleName, display_name: newRoleDisplayName, description: newRoleDescription });
      setShowCreateRoleDialog(false);
      setNewRoleName('');
      setNewRoleDisplayName('');
      setNewRoleDescription('');
      setRbacMessage({ type: 'success', text: '角色创建成功' });
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '创建角色失败' });
    }
  };

  const handleUpdateRole = async (role: RbacRole) => {
    try {
      await rbacApi.updateRole(role.id, { display_name: role.display_name, description: role.description });
      setRbacMessage({ type: 'success', text: '角色更新成功' });
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '更新角色失败' });
    }
  };

  const handleDeleteRole = async (roleId: string) => {
    try {
      await rbacApi.deleteRole(roleId);
      setSelectedRoleId('');
      setRbacMessage({ type: 'success', text: '角色已删除' });
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '删除角色失败' });
    }
  };

  const handleTogglePermission = async (roleId: string, permissionId: string, currentlyAssigned: boolean) => {
    try {
      if (currentlyAssigned) {
        await rbacApi.removePermission(roleId, permissionId);
      } else {
        const role = rbacRoles.find(r => r.id === roleId);
        const currentPermIds = (role?.permissions || []).map(p => p.id);
        const newPermIds = currentlyAssigned
          ? currentPermIds.filter(id => id !== permissionId)
          : [...currentPermIds, permissionId];
        await rbacApi.assignPermissions(roleId, newPermIds);
      }
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '权限操作失败' });
    }
  };

  const handleCreateUser = async () => {
    if (!newUserEmail || !newUserName || !newUserPassword) return;
    try {
      await rbacApi.createUser({ email: newUserEmail, name: newUserName, password: newUserPassword });
      setShowCreateUserDialog(false);
      setNewUserName('');
      setNewUserEmail('');
      setNewUserPassword('');
      setRbacMessage({ type: 'success', text: '用户创建成功' });
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '创建用户失败' });
    }
  };

  const handleUpdateUser = async () => {
    if (!editingUserId) return;
    try {
      await rbacApi.updateUser(editingUserId, { name: editUserName, email: editUserEmail });
      setEditingUserId('');
      setRbacMessage({ type: 'success', text: '用户更新成功' });
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '更新用户失败' });
    }
  };

  const handleDeleteUser = async (userId: string) => {
    try {
      await rbacApi.deleteUser(userId);
      setDeleteConfirmId('');
      setRbacMessage({ type: 'success', text: '用户已删除' });
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '删除用户失败' });
    }
  };

  const handleRemoveRoleFromUser = async (userId: string, roleId: string) => {
    try {
      await rbacApi.removeRole(userId, roleId);
      setRbacMessage({ type: 'success', text: '已移除用户角色' });
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '移除角色失败' });
    }
  };

  const handleInitRbac = async () => {
    try {
      await rbacApi.initRbac();
      setRbacMessage({ type: 'success', text: 'RBAC初始化成功' });
      loadRbacData();
    } catch (e: unknown) {
      setRbacMessage({ type: 'error', text: e instanceof Error ? e.message : '初始化失败' });
    }
  };

  const toggleCategory = (category: string) => {
    setExpandedCategories(prev => {
      const next = new Set(prev);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return next;
    });
  };

  const permissionsByCategory = rbacPermissions.reduce<Record<string, RbacPermission[]>>((acc, perm) => {
    if (!acc[perm.category]) acc[perm.category] = [];
    acc[perm.category].push(perm);
    return acc;
  }, {});

  const selectedRole = rbacRoles.find(r => r.id === selectedRoleId);
  const selectedRolePermissionIds = new Set((selectedRole?.permissions || []).map(p => p.id));

  const renderLLMTab = () => (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px' }}>
      <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>默认LLM供应商配置</h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <div>
            <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>选择供应商</label>
            <select
              value={selectedProviderId}
              onChange={(e) => handleProviderChange(e.target.value)}
              style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', background: 'white' }}
            >
              {providers.map(p => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>

          <div>
            <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>API Key</label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={keyMasked ? `已配置: ${keyMasked}（留空表示不修改）` : 'sk-...'}
              style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
            />
          </div>

          <div>
            <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>API Base URL</label>
            <input
              type="text"
              value={apiBase}
              onChange={(e) => setApiBase(e.target.value)}
              style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
            />
          </div>

          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>模型选择</label>
              <button
                onClick={() => setUseCustomModel(!useCustomModel)}
                style={{ fontSize: '12px', color: 'var(--color-primary)', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
              >
                {useCustomModel ? '选择预设模型' : '自定义模型'}
              </button>
            </div>

            {useCustomModel ? (
              <div style={{ display: 'flex', gap: '8px' }}>
                <input
                  type="text"
                  value={customModel}
                  onChange={(e) => setCustomModel(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleCustomModelConfirm()}
                  placeholder={selectedProviderId === 'siliconflow' ? '如: deepseek-ai/DeepSeek-V3' : '输入模型名称'}
                  style={{ flex: 1, padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
                />
                <button
                  onClick={handleCustomModelConfirm}
                  style={{ padding: '8px 14px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' }}
                >
                  确认
                </button>
              </div>
            ) : (
              <select
                value={model}
                onChange={(e) => handlePresetModelChange(e.target.value)}
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', background: 'white' }}
              >
                {providers.filter(p => p.id === selectedProviderId).flatMap(p =>
                  p.models.map(m => (
                    <option key={`${p.id}/${m}`} value={`${p.id}/${m}`}>
                      {m}
                    </option>
                  ))
                )}
              </select>
            )}

            <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '4px' }}>
              当前模型: <code style={{ background: '#f1f5f9', padding: '1px 4px', borderRadius: '3px' }}>{model}</code>
            </div>
          </div>

          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={handleTestConnection}
              disabled={testing}
              style={{ flex: 1, padding: '10px 12px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: testing ? 'not-allowed' : 'pointer', fontSize: '13px', fontWeight: 500, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}
            >
              {testing ? <Loader2 size={14} style={{ animation: 'spin 1s linear infinite' }} /> : null}
              {testing ? '测试中...' : '测试连接'}
            </button>
            <button
              onClick={handleSaveDefaultModel}
              disabled={savingDefault}
              style={{ flex: 1, padding: '10px 12px', background: '#059669', color: 'white', border: 'none', borderRadius: '6px', cursor: savingDefault ? 'not-allowed' : 'pointer', fontSize: '13px', fontWeight: 500, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}
            >
              {savingDefault ? <Loader2 size={14} style={{ animation: 'spin 1s linear infinite' }} /> : <Save size={14} />}
              {savingDefault ? '保存中...' : '保存为默认'}
            </button>
          </div>
        </div>

        {testResult && (
          <div style={{
            marginTop: '16px',
            padding: '12px',
            borderRadius: '8px',
            background: testResult.success ? '#ecfdf5' : '#fef2f2',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
          }}>
            {testResult.success ? <CheckCircle2 size={18} color="#059669" /> : <XCircle size={18} color="#dc2626" />}
            <span style={{ fontSize: '13px', color: testResult.success ? '#059669' : '#dc2626' }}>{testResult.message}</span>
          </div>
        )}

        {tokenUsage && (
          <div style={{ marginTop: '16px', padding: '12px', background: '#f8fafc', borderRadius: '8px' }}>
            <h4 style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px' }}>Token 使用统计</h4>
            <pre style={{ fontSize: '12px', margin: 0, overflow: 'auto' }}>{JSON.stringify(tokenUsage, null, 2)}</pre>
          </div>
        )}
      </div>

      <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>支持的供应商</h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {providers.map(p => (
            <div
              key={p.id}
              onClick={() => handleProviderChange(p.id)}
              style={{
                padding: '12px',
                border: `1px solid ${p.id === selectedProviderId ? 'var(--color-primary)' : 'var(--color-border)'}`,
                borderRadius: '8px',
                background: p.id === selectedProviderId ? '#eff6ff' : 'white',
                cursor: 'pointer',
                transition: 'all 0.2s',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '13px', fontWeight: 600 }}>{p.name}</span>
                {p.id === selectedProviderId && (
                  <span style={{ fontSize: '11px', color: 'var(--color-primary)', background: '#dbeafe', padding: '1px 8px', borderRadius: '10px' }}>当前</span>
                )}
              </div>
              <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '4px' }}>
                {providerApiBases[p.id] || ''}
              </div>
              <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: '6px', display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                {p.models.map(m => (
                  <span key={m} style={{ padding: '1px 6px', background: '#f0f9ff', borderRadius: '4px', fontSize: '11px' }}>{m}</span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );

  const renderAgentsTab = () => (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <div>
          <h3 style={{ fontSize: '16px', fontWeight: 600 }}>多智能体模型配置</h3>
          <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>为不同的Agent配置不同的模型，优化各环节效果</p>
        </div>
        <button
          onClick={handleResetAgents}
          disabled={agentsLoading}
          style={{ padding: '6px 14px', background: '#fffbeb', color: '#d97706', border: '1px solid #fde68a', borderRadius: '6px', cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}
        >
          <RotateCcw size={13} /> 重置全部
        </button>
      </div>

      {agentsMessage && (
        <div style={{
          marginBottom: '12px',
          padding: '8px 12px',
          borderRadius: '6px',
          fontSize: '13px',
          background: agentsMessage.type === 'success' ? '#ecfdf5' : '#fef2f2',
          color: agentsMessage.type === 'success' ? '#059669' : '#dc2626',
        }}>
          {agentsMessage.text}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: '20px' }}>
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '16px', border: '1px solid var(--color-border)' }}>
          <h4 style={{ fontSize: '13px', fontWeight: 600, marginBottom: '10px', color: 'var(--color-text-secondary)' }}>Agent 列表</h4>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {agents.map(agent => (
              <div
                key={agent.name}
                onClick={() => setExpandedAgent(agent.name)}
                style={{
                  padding: '8px 10px',
                  borderRadius: '6px',
                  cursor: 'pointer',
                  background: expandedAgent === agent.name ? '#eff6ff' : 'transparent',
                  borderLeft: expandedAgent === agent.name ? '3px solid var(--color-primary)' : '3px solid transparent',
                  opacity: agent.enabled ? 1 : 0.5,
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Bot size={13} color={agent.enabled ? 'var(--color-primary)' : '#9ca3af'} />
                  {agent.display_name}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: '2px', paddingLeft: '19px' }}>
                  {agent.model || '使用默认模型'}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          {agents.filter(a => a.name === expandedAgent).map(agent => (
            <div key={agent.name}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
                <div>
                  <h3 style={{ fontSize: '16px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Bot size={20} color="var(--color-primary)" />
                    {agent.display_name}
                  </h3>
                  <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: '2px' }}>{agent.description}</p>
                </div>
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                  <label style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer', fontSize: '13px' }}>
                    <input
                      type="checkbox"
                      checked={agent.enabled}
                      onChange={(e) => handleAgentChange(agent.name, 'enabled', e.target.checked)}
                      style={{ width: '16px', height: '16px', cursor: 'pointer' }}
                    />
                    启用
                  </label>
                  <button
                    onClick={() => handleSaveAgent(agent)}
                    disabled={agentsSaving === agent.name}
                    style={{
                      padding: '6px 14px',
                      background: agentsSaving === agent.name ? '#94a3b8' : '#059669',
                      color: 'white',
                      border: 'none',
                      borderRadius: '6px',
                      cursor: agentsSaving === agent.name ? 'not-allowed' : 'pointer',
                      fontSize: '13px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                    }}
                  >
                    <Save size={13} /> {agentsSaving === agent.name ? '保存中...' : '保存'}
                  </button>
                </div>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                    <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>使用模型</label>
                    <button
                      onClick={() => {
                        if (agentUseCustom === agent.name) {
                          setAgentUseCustom('');
                        } else {
                          setAgentUseCustom(agent.name);
                          setAgentCustomModel(agent.model || '');
                        }
                      }}
                      style={{ fontSize: '12px', color: 'var(--color-primary)', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
                    >
                      {agentUseCustom === agent.name ? '选择预设' : '自定义模型'}
                    </button>
                  </div>

                  {agentUseCustom === agent.name ? (
                    <div style={{ display: 'flex', gap: '8px' }}>
                      <input
                        type="text"
                        value={agentCustomModel}
                        onChange={(e) => setAgentCustomModel(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && agentCustomModel.trim()) {
                            handleAgentChange(agent.name, 'model', agentCustomModel.trim());
                          }
                        }}
                        placeholder="如: siliconflow/deepseek-ai/DeepSeek-V3"
                        style={{ flex: 1, padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
                      />
                      <button
                        onClick={() => {
                          if (agentCustomModel.trim()) {
                            handleAgentChange(agent.name, 'model', agentCustomModel.trim());
                          }
                        }}
                        style={{ padding: '8px 14px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' }}
                      >
                        确认
                      </button>
                    </div>
                  ) : (
                    <select
                      value={agent.model}
                      onChange={(e) => handleAgentChange(agent.name, 'model', e.target.value)}
                      style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', background: 'white' }}
                    >
                      <option value="">-- 使用默认模型 --</option>
                      {allModelOptions.map(opt => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  )}

                  <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '4px' }}>
                    当前: <code style={{ background: '#f1f5f9', padding: '1px 4px', borderRadius: '3px' }}>{agent.model || '默认'}</code>
                    {agent.model && (
                      <button
                        onClick={() => handleAgentChange(agent.name, 'model', '')}
                        style={{ marginLeft: '6px', fontSize: '11px', color: '#dc2626', background: 'none', border: 'none', cursor: 'pointer' }}
                      >
                        清除
                      </button>
                    )}
                  </div>
                </div>
                <div>
                  <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>Temperature</label>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <input
                      type="range"
                      min="0"
                      max="1"
                      step="0.1"
                      value={agent.temperature}
                      onChange={(e) => handleAgentChange(agent.name, 'temperature', parseFloat(e.target.value))}
                      style={{ flex: 1 }}
                    />
                    <span style={{ fontSize: '14px', fontWeight: 600, minWidth: '32px', textAlign: 'center' }}>{agent.temperature}</span>
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
                    低值=精确稳定，高值=创意多样
                  </div>
                </div>
                <div>
                  <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>最大Token数</label>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <select
                      value={[1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072].includes(agent.max_tokens) ? agent.max_tokens : 'custom'}
                      onChange={(e) => {
                        if (e.target.value !== 'custom') {
                          handleAgentChange(agent.name, 'max_tokens', parseInt(e.target.value));
                        }
                      }}
                      style={{ flex: 1, padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', background: 'white' }}
                    >
                      <option value={1024}>1,024</option>
                      <option value={2048}>2,048</option>
                      <option value={4096}>4,096</option>
                      <option value={8192}>8,192</option>
                      <option value={16384}>16,384</option>
                      <option value={32768}>32,768</option>
                      <option value={65536}>65,536</option>
                      <option value={131072}>131,072</option>
                      <option value="custom">自定义...</option>
                    </select>
                    {![1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072].includes(agent.max_tokens) && (
                      <input
                        type="number"
                        value={agent.max_tokens}
                        onChange={(e) => handleAgentChange(agent.name, 'max_tokens', parseInt(e.target.value) || 4096)}
                        min={256}
                        max={200000}
                        style={{ width: '100px', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px' }}
                      />
                    )}
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
                    不同模型支持的最大值不同，超出会被自动截断
                  </div>
                </div>
                <div>
                  <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>Agent名称</label>
                  <input
                    type="text"
                    value={agent.display_name}
                    onChange={(e) => handleAgentChange(agent.name, 'display_name', e.target.value)}
                    style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
                  />
                </div>
              </div>

              <div style={{ marginTop: '20px', padding: '12px', background: '#f8fafc', borderRadius: '8px', border: '1px solid var(--color-border)' }}>
                <h4 style={{ fontSize: '12px', fontWeight: 600, marginBottom: '6px', color: 'var(--color-text-secondary)' }}>配置建议</h4>
                <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', lineHeight: '1.8' }}>
                  {agent.name === 'interpret' && '招标解读需要精确提取信息，建议使用强推理模型(如deepseek-reasoner)，Temperature设为0.1-0.3'}
                  {agent.name === 'outline' && '大纲生成需要结构化思维，建议使用中等Temperature，模型可选qwen-max或glm-4-plus'}
                  {agent.name === 'content' && '内容生成需要创意和丰富度，建议使用高Temperature(0.6-0.8)，模型可选deepseek-chat或qwen-max'}
                  {agent.name === 'check' && '质量检查需要精确判断，建议使用低Temperature(0.1-0.2)，模型可选deepseek-chat或gpt-4o'}
                  {agent.name === 'format' && '格式排版为规则性任务，建议使用最低Temperature，模型可选任意稳定模型'}
                  {agent.name === 'final_check' && '终审需要全面严谨，建议使用强推理模型，Temperature设为0.1'}
                  {agent.name === 'export' && '导出为确定性任务，Temperature设为0即可，模型可选任意稳定模型'}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );

  const renderRbacTab = () => (
    <div>
      {rbacLoading && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px', fontSize: '13px', color: 'var(--color-text-secondary)' }}>
          <Loader2 size={14} className="animate-spin" /> 加载中...
        </div>
      )}

      {rbacMessage && (
        <div style={{
          marginBottom: '12px',
          padding: '8px 12px',
          borderRadius: '6px',
          fontSize: '13px',
          background: rbacMessage.type === 'success' ? '#ecfdf5' : '#fef2f2',
          color: rbacMessage.type === 'success' ? '#059669' : '#dc2626',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}>
          <span>{rbacMessage.text}</span>
          <button onClick={() => setRbacMessage(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px' }}>
            <X size={14} />
          </button>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: '20px', marginBottom: '24px' }}>
        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '16px', border: '1px solid var(--color-border)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <h4 style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text-secondary)' }}>角色列表</h4>
            <button
              onClick={() => setShowCreateRoleDialog(true)}
              style={{ padding: '4px 10px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}
            >
              <Plus size={12} /> 新建角色
            </button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {rbacRoles.map(role => (
              <div
                key={role.id}
                onClick={() => setSelectedRoleId(role.id)}
                style={{
                  padding: '8px 10px',
                  borderRadius: '6px',
                  cursor: 'pointer',
                  background: selectedRoleId === role.id ? '#eff6ff' : 'transparent',
                  borderLeft: selectedRoleId === role.id ? '3px solid var(--color-primary)' : '3px solid transparent',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Shield size={13} color={selectedRoleId === role.id ? 'var(--color-primary)' : '#64748b'} />
                  {role.display_name}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: '2px', paddingLeft: '19px' }}>
                  {role.description || '无描述'} · {(role.permissions || []).length}项权限
                </div>
              </div>
            ))}
            {rbacRoles.length === 0 && (
              <div style={{ textAlign: 'center', padding: '20px', color: 'var(--color-text-secondary)', fontSize: '13px' }}>
                暂无角色
                <button
                  onClick={handleInitRbac}
                  style={{ display: 'block', margin: '8px auto 0', padding: '4px 12px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '12px' }}
                >
                  初始化默认角色
                </button>
              </div>
            )}
          </div>
        </div>

        <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
          {selectedRole ? (
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '20px' }}>
                <div style={{ flex: 1 }}>
                  <div style={{ marginBottom: '12px' }}>
                    <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>角色名称</label>
                    <input
                      type="text"
                      value={selectedRole.display_name}
                      onChange={(e) => {
                        setRbacRoles(prev => prev.map(r => r.id === selectedRole.id ? { ...r, display_name: e.target.value } : r));
                      }}
                      style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>角色描述</label>
                    <input
                      type="text"
                      value={selectedRole.description}
                      onChange={(e) => {
                        setRbacRoles(prev => prev.map(r => r.id === selectedRole.id ? { ...r, description: e.target.value } : r));
                      }}
                      style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
                    />
                  </div>
                </div>
                <div style={{ display: 'flex', gap: '8px', marginLeft: '12px', flexShrink: 0 }}>
                  <button
                    onClick={() => handleUpdateRole(selectedRole)}
                    style={{ padding: '6px 14px', background: '#059669', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '4px' }}
                  >
                    <Save size={13} /> 保存
                  </button>
                  <button
                    onClick={() => handleDeleteRole(selectedRole.id)}
                    style={{ padding: '6px 14px', background: '#dc2626', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '4px' }}
                  >
                    <Trash2 size={13} /> 删除
                  </button>
                </div>
              </div>

              <div style={{ marginBottom: '20px' }}>
                <h4 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '12px' }}>权限配置</h4>
                <div style={{ border: '1px solid var(--color-border)', borderRadius: '8px', overflow: 'hidden' }}>
                  {Object.entries(permissionsByCategory).map(([category, perms]) => (
                    <div key={category}>
                      <div
                        onClick={() => toggleCategory(category)}
                        style={{
                          padding: '10px 12px',
                          background: '#f8fafc',
                          borderBottom: '1px solid var(--color-border)',
                          cursor: 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          gap: '8px',
                          fontSize: '13px',
                          fontWeight: 600,
                        }}
                      >
                        {expandedCategories.has(category) ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        {category}
                        <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)', fontWeight: 400 }}>
                          ({perms.filter(p => selectedRolePermissionIds.has(p.id)).length}/{perms.length})
                        </span>
                      </div>
                      {expandedCategories.has(category) && (
                        <div style={{ padding: '8px 12px 8px 32px', borderBottom: '1px solid var(--color-border)' }}>
                          {perms.map(perm => (
                            <label
                              key={perm.id}
                              style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: '8px',
                                padding: '4px 0',
                                cursor: 'pointer',
                                fontSize: '13px',
                              }}
                            >
                              <input
                                type="checkbox"
                                checked={selectedRolePermissionIds.has(perm.id)}
                                onChange={() => handleTogglePermission(selectedRole.id, perm.id, selectedRolePermissionIds.has(perm.id))}
                                style={{ width: '15px', height: '15px', cursor: 'pointer' }}
                              />
                              <span>{perm.name}</span>
                              <span style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>- {perm.description}</span>
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                  {Object.keys(permissionsByCategory).length === 0 && (
                    <div style={{ padding: '20px', textAlign: 'center', color: 'var(--color-text-secondary)', fontSize: '13px' }}>
                      暂无权限数据
                    </div>
                  )}
                </div>
              </div>

              <div>
                <h4 style={{ fontSize: '14px', fontWeight: 600, marginBottom: '12px' }}>
                  <Users size={14} style={{ verticalAlign: 'middle', marginRight: '4px' }} />
                  拥有此角色的用户 ({(selectedRole.users || []).length})
                </h4>
                {(selectedRole.users || []).length > 0 ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {(selectedRole.users || []).map(user => (
                      <div
                        key={user.id}
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          padding: '8px 12px',
                          border: '1px solid var(--color-border)',
                          borderRadius: '6px',
                          fontSize: '13px',
                        }}
                      >
                        <div>
                          <span style={{ fontWeight: 500 }}>{user.name}</span>
                          <span style={{ color: 'var(--color-text-secondary)', marginLeft: '8px', fontSize: '12px' }}>{user.email}</span>
                        </div>
                        <button
                          onClick={() => handleRemoveRoleFromUser(user.id, selectedRole.id)}
                          style={{ background: 'none', border: '1px solid #fca5a5', color: '#dc2626', borderRadius: '4px', cursor: 'pointer', padding: '2px 8px', fontSize: '12px' }}
                        >
                          移除
                        </button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div style={{ textAlign: 'center', padding: '16px', color: 'var(--color-text-secondary)', fontSize: '13px' }}>
                    暂无用户拥有此角色
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div style={{ textAlign: 'center', padding: '60px', color: 'var(--color-text-secondary)', fontSize: '14px' }}>
              <Shield size={40} style={{ marginBottom: '12px', opacity: 0.3 }} />
              <div>请从左侧选择一个角色查看详情</div>
            </div>
          )}
        </div>
      </div>

      <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <h3 style={{ fontSize: '16px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Users size={18} /> 用户管理
          </h3>
          <button
            onClick={() => setShowCreateUserDialog(true)}
            style={{ padding: '6px 14px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '4px' }}
          >
            <UserPlus size={14} /> 新建用户
          </button>
        </div>

        {rbacUsers.length > 0 ? (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
            <thead>
              <tr style={{ borderBottom: '2px solid var(--color-border)' }}>
                <th style={{ textAlign: 'left', padding: '8px 12px', fontWeight: 600, color: 'var(--color-text-secondary)', fontSize: '12px' }}>姓名</th>
                <th style={{ textAlign: 'left', padding: '8px 12px', fontWeight: 600, color: 'var(--color-text-secondary)', fontSize: '12px' }}>邮箱</th>
                <th style={{ textAlign: 'left', padding: '8px 12px', fontWeight: 600, color: 'var(--color-text-secondary)', fontSize: '12px' }}>角色</th>
                <th style={{ textAlign: 'right', padding: '8px 12px', fontWeight: 600, color: 'var(--color-text-secondary)', fontSize: '12px' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {rbacUsers.map(user => (
                <tr key={user.id} style={{ borderBottom: '1px solid var(--color-border)' }}>
                  <td style={{ padding: '10px 12px', fontWeight: 500 }}>{user.name}</td>
                  <td style={{ padding: '10px 12px', color: 'var(--color-text-secondary)' }}>{user.email}</td>
                  <td style={{ padding: '10px 12px' }}>
                    <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                      {(user.roles || []).map(role => (
                        <span key={role.id} style={{ padding: '2px 8px', background: '#eff6ff', color: '#1a56db', borderRadius: '10px', fontSize: '11px' }}>
                          {role.display_name}
                        </span>
                      ))}
                      {(user.roles || []).length === 0 && (
                        <span style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>无角色</span>
                      )}
                    </div>
                  </td>
                  <td style={{ padding: '10px 12px', textAlign: 'right' }}>
                    <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                      <button
                        onClick={() => {
                          setEditingUserId(user.id);
                          setEditUserName(user.name);
                          setEditUserEmail(user.email);
                        }}
                        style={{ padding: '4px 10px', background: '#f0f9ff', color: '#1a56db', border: '1px solid #bfdbfe', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' }}
                      >
                        编辑
                      </button>
                      {deleteConfirmId === user.id ? (
                        <div style={{ display: 'flex', gap: '4px' }}>
                          <button
                            onClick={() => handleDeleteUser(user.id)}
                            style={{ padding: '4px 10px', background: '#dc2626', color: 'white', border: 'none', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' }}
                          >
                            确认
                          </button>
                          <button
                            onClick={() => setDeleteConfirmId('')}
                            style={{ padding: '4px 10px', background: '#f1f5f9', color: '#64748b', border: '1px solid var(--color-border)', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' }}
                          >
                            取消
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setDeleteConfirmId(user.id)}
                          style={{ padding: '4px 10px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: '4px', cursor: 'pointer', fontSize: '12px' }}
                        >
                          删除
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div style={{ textAlign: 'center', padding: '40px', color: 'var(--color-text-secondary)', fontSize: '13px' }}>
            暂无用户数据
          </div>
        )}
      </div>

      {showCreateRoleDialog && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
          <div style={{ background: 'white', borderRadius: '12px', padding: '24px', width: '400px', maxWidth: '90vw' }}>
            <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>新建角色</h3>
            <div style={{ marginBottom: '12px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>角色标识 (name)</label>
              <input
                type="text"
                value={newRoleName}
                onChange={(e) => setNewRoleName(e.target.value)}
                placeholder="如: editor"
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ marginBottom: '12px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>显示名称</label>
              <input
                type="text"
                value={newRoleDisplayName}
                onChange={(e) => setNewRoleDisplayName(e.target.value)}
                placeholder="如: 编辑者"
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ marginBottom: '16px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>描述</label>
              <input
                type="text"
                value={newRoleDescription}
                onChange={(e) => setNewRoleDescription(e.target.value)}
                placeholder="角色描述"
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button
                onClick={() => { setShowCreateRoleDialog(false); setNewRoleName(''); setNewRoleDisplayName(''); setNewRoleDescription(''); }}
                style={{ padding: '6px 14px', background: '#f1f5f9', color: '#64748b', border: '1px solid var(--color-border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' }}
              >
                取消
              </button>
              <button
                onClick={handleCreateRole}
                disabled={!newRoleName || !newRoleDisplayName}
                style={{ padding: '6px 14px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: !newRoleName || !newRoleDisplayName ? 'not-allowed' : 'pointer', fontSize: '13px' }}
              >
                创建
              </button>
            </div>
          </div>
        </div>
      )}

      {showCreateUserDialog && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
          <div style={{ background: 'white', borderRadius: '12px', padding: '24px', width: '400px', maxWidth: '90vw' }}>
            <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>新建用户</h3>
            <div style={{ marginBottom: '12px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>姓名</label>
              <input
                type="text"
                value={newUserName}
                onChange={(e) => setNewUserName(e.target.value)}
                placeholder="用户姓名"
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ marginBottom: '12px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>邮箱</label>
              <input
                type="email"
                value={newUserEmail}
                onChange={(e) => setNewUserEmail(e.target.value)}
                placeholder="user@example.com"
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ marginBottom: '16px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>密码</label>
              <input
                type="password"
                value={newUserPassword}
                onChange={(e) => setNewUserPassword(e.target.value)}
                placeholder="设置密码"
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button
                onClick={() => { setShowCreateUserDialog(false); setNewUserName(''); setNewUserEmail(''); setNewUserPassword(''); }}
                style={{ padding: '6px 14px', background: '#f1f5f9', color: '#64748b', border: '1px solid var(--color-border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' }}
              >
                取消
              </button>
              <button
                onClick={handleCreateUser}
                disabled={!newUserName || !newUserEmail || !newUserPassword}
                style={{ padding: '6px 14px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: !newUserName || !newUserEmail || !newUserPassword ? 'not-allowed' : 'pointer', fontSize: '13px' }}
              >
                创建
              </button>
            </div>
          </div>
        </div>
      )}

      {editingUserId && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
          <div style={{ background: 'white', borderRadius: '12px', padding: '24px', width: '400px', maxWidth: '90vw' }}>
            <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>编辑用户</h3>
            <div style={{ marginBottom: '12px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>姓名</label>
              <input
                type="text"
                value={editUserName}
                onChange={(e) => setEditUserName(e.target.value)}
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ marginBottom: '16px' }}>
              <label style={{ fontSize: '13px', color: 'var(--color-text-secondary)', display: 'block', marginBottom: '6px' }}>邮箱</label>
              <input
                type="email"
                value={editUserEmail}
                onChange={(e) => setEditUserEmail(e.target.value)}
                style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: '6px', fontSize: '14px', boxSizing: 'border-box' }}
              />
            </div>
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button
                onClick={() => setEditingUserId('')}
                style={{ padding: '6px 14px', background: '#f1f5f9', color: '#64748b', border: '1px solid var(--color-border)', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' }}
              >
                取消
              </button>
              <button
                onClick={handleUpdateUser}
                style={{ padding: '6px 14px', background: 'var(--color-primary)', color: 'white', border: 'none', borderRadius: '6px', cursor: 'pointer', fontSize: '13px' }}
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );

  const renderSkillsTab = () => (
    <div style={{ background: 'var(--color-surface)', borderRadius: '12px', padding: '24px', border: '1px solid var(--color-border)' }}>
      <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '16px' }}>已注册 Skill ({skills.length})</h3>
      {skills.length === 0 ? (
        <p style={{ color: 'var(--color-text-secondary)', fontSize: '13px' }}>暂无注册的 Skill</p>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
          {skills.map((skill, i) => (
            <div
              key={i}
              style={{
                padding: '10px 12px',
                border: '1px solid var(--color-border)',
                borderRadius: '8px',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <div>
                <div style={{ fontSize: '13px', fontWeight: 600 }}>{(skill as Record<string, unknown>).name as string || '-'}</div>
                <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)' }}>{(skill as Record<string, unknown>).description as string || '-'}</div>
              </div>
              <span style={{
                fontSize: '11px',
                padding: '2px 8px',
                borderRadius: '10px',
                background: '#eff6ff',
                color: '#1a56db',
              }}>
                {(skill as Record<string, unknown>).category as string || '-'}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );


  // ── vNext: MinerU OCR 配置 ──────────────────────────────
  const [ocrConfig, setOcrConfig] = useState<Record<string, unknown> | null>(null);
  const [ocrMode, setOcrMode] = useState('off');
  const [ocrEndpoint, setOcrEndpoint] = useState('https://api.mineru.net/v4');
  const [ocrKey, setOcrKey] = useState('');
  const [ocrTimeout, setOcrTimeout] = useState(60);
  const [ocrPoll, setOcrPoll] = useState(3);
  const [ocrMaxPolls, setOcrMaxPolls] = useState(120);
  const [ocrTesting, setOcrTesting] = useState(false);
  const [ocrResult, setOcrResult] = useState<{ success: boolean; message: string } | null>(null);

  const loadOcrConfig = useCallback(async () => {
    try {
      const r = await ocrApi.getConfig();
      setOcrConfig(r.data as unknown as Record<string, unknown>);
      setOcrMode(r.data.mode || 'off');
      setOcrEndpoint(r.data.endpoint || '');
      setOcrTimeout(r.data.timeout ?? 60);
      setOcrPoll(r.data.poll_interval ?? 3);
      setOcrMaxPolls(r.data.max_polls ?? 120);
    } catch (e) { console.error('加载 OCR 配置失败', e); }
  }, []);

  const saveOcrConfig = async () => {
    try {
      const r = await ocrApi.updateConfig({ mode: ocrMode, endpoint: ocrEndpoint, api_key: ocrKey || undefined, timeout: ocrTimeout, poll_interval: ocrPoll, max_polls: ocrMaxPolls });
      setOcrConfig(r.data.config as unknown as Record<string, unknown>);
      setOcrKey('');
      setOcrResult({ success: true, message: 'OCR 配置已保存（Key 仅保存在本地 .env，已掩码）' });
    } catch (e) {
      setOcrResult({ success: false, message: (e as Error).message });
    }
  };

  const testOcr = async () => {
    setOcrTesting(true);
    setOcrResult(null);
    try {
      const r = await ocrApi.testConnection({ mode: ocrMode, endpoint: ocrEndpoint, api_key: ocrKey || undefined });
      setOcrResult({ success: r.data.success, message: r.data.success ? (r.data.message || '连接成功') : (r.data.error || '连接失败') });
    } catch (e) {
      setOcrResult({ success: false, message: (e as Error).message });
    } finally { setOcrTesting(false); }
  };

  const renderOcrTab = () => (
    <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '20px' }}>
      <h3 style={{ fontSize: '15px', fontWeight: 600, margin: '0 0 4px', display: 'flex', alignItems: 'center', gap: 6 }}>
        <ScanText size={16} color="#0ea5e9" /> MinerU OCR
      </h3>
      <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: '0 0 16px' }}>
        官方 MinerU 云 API（api.mineru.net/v4）或自定义 self-hosted endpoint。API Key 只保存在本地 .env，日志与响应一律掩码。
      </p>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '12px 24px', maxWidth: 720 }}>
        <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>模式
          <select value={ocrMode} onChange={(e) => setOcrMode(e.target.value)} style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13 }}>
            <option value="off">关闭</option>
            <option value="mock">Mock（无 Key 测试）</option>
            <option value="cloud">官方云 API</option>
            <option value="selfhosted">自建 endpoint</option>
          </select>
        </label>
        <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>Endpoint
          <input value={ocrEndpoint} onChange={(e) => setOcrEndpoint(e.target.value)} placeholder="https://api.mineru.net/v4" style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13 }} />
        </label>
        <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>API Key {ocrConfig && (ocrConfig as { api_key_set?: boolean }).api_key_set && <span style={{ color: '#059669' }}>（已配置: {(ocrConfig as { api_key_masked?: string }).api_key_masked}）</span>}
          <input type="password" value={ocrKey} onChange={(e) => setOcrKey(e.target.value)} placeholder="输入新 Key（留空表示不修改）" autoComplete="off" style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13 }} />
        </label>
        <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>超时(秒)
          <input type="number" value={ocrTimeout} onChange={(e) => setOcrTimeout(Number(e.target.value))} style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13 }} />
        </label>
        <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>轮询间隔(秒)
          <input type="number" step="0.5" value={ocrPoll} onChange={(e) => setOcrPoll(Number(e.target.value))} style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13 }} />
        </label>
        <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)' }}>最大轮询次数
          <input type="number" value={ocrMaxPolls} onChange={(e) => setOcrMaxPolls(Number(e.target.value))} style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13 }} />
        </label>
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
        <button onClick={testOcr} disabled={ocrTesting} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#0ea5e9', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
          {ocrTesting ? <Loader2 size={13} className="spin" /> : <Plug size={13} />} 测试连接
        </button>
        <button onClick={saveOcrConfig} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#1a56db', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}><Save size={13} /> 保存配置</button>
      </div>
      {ocrResult && (
        <div style={{ marginTop: 12, padding: '8px 12px', borderRadius: 8, fontSize: 12, background: ocrResult.success ? '#ecfdf5' : '#fef2f2', color: ocrResult.success ? '#059669' : '#dc2626' }}>{ocrResult.message}</div>
      )}
      <p style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: 12 }}>
        提示：Mock 模式无需真实 Key，可确定性验证 OCR 链路；真实 Key 请到 MinerU 官网申请。错误分类：auth / rate_limit / format / timeout / upstream。
      </p>
    </div>
  );

  // ── vNext: Embedding / 知识库密钥配置 ────────────────────
  const [embKey, setEmbKey] = useState('');
  const [embModel, setEmbModel] = useState('');
  const [embApiBase, setEmbApiBase] = useState('');
  const [embEffectiveModel, setEmbEffectiveModel] = useState('');
  const [embStatus, setEmbStatus] = useState<{ configured: boolean; source: string; masked: string | null } | null>(null);
  const [embSaving, setEmbSaving] = useState(false);
  const [embTesting, setEmbTesting] = useState(false);
  const [embResult, setEmbResult] = useState<{ success: boolean; message: string } | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  const axiosErrMsg = (e: unknown): string => {
    const resp = (e as { response?: { data?: { detail?: string } } })?.response;
    return resp?.data?.detail || (e instanceof Error ? e.message : '操作失败');
  };

  const loadEmbStatus = useCallback(async () => {
    try {
      const r = await secretsApi.status();
      setEmbStatus(r.data.secrets?.BMP_EMBEDDING_API_KEY || null);
    } catch (e) { console.error('加载密钥状态失败', e); }
    try {
      const r = await secretsApi.getEmbeddingConfig();
      setEmbModel(r.data.model || '');
      setEmbApiBase(r.data.api_base || '');
      setEmbEffectiveModel(r.data.model || '');
    } catch (e) { console.error('加载 Embedding 配置失败', e); }
  }, []);

  const saveEmbConfig = async () => {
    setEmbSaving(true);
    setEmbResult(null);
    try {
      const r = await secretsApi.putEmbeddingConfig({ model: embModel, api_base: embApiBase });
      const savedModel = r.data?.model || embModel;
      setEmbEffectiveModel(savedModel);
      setEmbResult({
        success: true,
        message: r.data.needs_restart
          ? `已保存模型与接口：模型「${savedModel}」/ API Base（重启服务后生效）`
          : `已保存模型与接口：模型「${savedModel}」/ API Base（立即生效）`,
      });
      await loadEmbStatus();
    } catch (e) {
      setEmbResult({ success: false, message: axiosErrMsg(e) });
    } finally { setEmbSaving(false); }
  };

  const saveEmbKey = async () => {
    setEmbSaving(true);
    setEmbResult(null);
    try {
      await secretsApi.put('BMP_EMBEDDING_API_KEY', embKey);
      setEmbKey('');
      setEmbResult({ success: true, message: '已保存 API Key（仅 Key，模型/Base 未改动；存入系统凭据管理器，立即生效）' });
      await loadEmbStatus();
    } catch (e) {
      setEmbResult({ success: false, message: axiosErrMsg(e) });
    } finally { setEmbSaving(false); }
  };

  const testEmb = async () => {
    setEmbTesting(true);
    setEmbResult(null);
    try {
      const r = await secretsApi.testEmbedding({
        model: embModel || undefined,
        api_base: embApiBase || undefined,
      });
      setEmbResult({ success: r.data.ok, message: r.data.ok ? `连接成功：${r.data.model}（${r.data.latency_ms}ms）` : '连接失败' });
    } catch (e) {
      setEmbResult({ success: false, message: axiosErrMsg(e) });
    } finally { setEmbTesting(false); }
  };

  const clearEmbKey = async () => {
    setEmbSaving(true);
    setEmbResult(null);
    try {
      await secretsApi.put('BMP_EMBEDDING_API_KEY', '');
      setEmbResult({ success: true, message: '已清除（回退到环境变量 / .env 配置）' });
      await loadEmbStatus();
    } catch (e) {
      setEmbResult({ success: false, message: axiosErrMsg(e) });
    } finally { setEmbSaving(false); setConfirmClear(false); }
  };

  const sourceLabel: Record<string, string> = { env: '环境变量', keyring: '凭据管理器', envfile: '.env 文件', missing: '未配置' };

  // ── P-C: Reranker 配置（走 env_store 审计）────────────────
  const [rrEnabled, setRrEnabled] = useState(false);
  const [rrKey, setRrKey] = useState('');
  const [rrModel, setRrModel] = useState('');
  const [rrApiBase, setRrApiBase] = useState('');
  const [rrStatus, setRrStatus] = useState<{ configured: boolean; source: string; masked: string | null } | null>(null);
  const [rrSaving, setRrSaving] = useState(false);
  const [rrTesting, setRrTesting] = useState(false);
  const [rrResult, setRrResult] = useState<{ success: boolean; message: string } | null>(null);
  const [rrConfirmClear, setRrConfirmClear] = useState(false);

  const loadRerankerConfig = useCallback(async () => {
    try {
      const r = await secretsApi.getRerankerConfig();
      setRrEnabled(r.data.enabled);
      setRrModel(r.data.model || '');
      setRrApiBase(r.data.api_base || '');
      setRrStatus({ configured: r.data.api_key_set, source: r.data.api_key_source, masked: r.data.api_key_masked });
    } catch (e) { console.error('加载 Reranker 配置失败', e); }
  }, []);

  const saveRerankerConfig = async () => {
    setRrSaving(true); setRrResult(null);
    try {
      await secretsApi.putRerankerConfig({ enabled: rrEnabled, model: rrModel, api_base: rrApiBase });
      setRrResult({ success: true, message: `已保存（${rrEnabled ? '已启用' : '已关闭'}）：模型「${rrModel}」/ API Base（立即生效）` });
      await loadRerankerConfig();
    } catch (e) {
      setRrResult({ success: false, message: axiosErrMsg(e) });
    } finally { setRrSaving(false); }
  };

  const saveRerankerKey = async () => {
    setRrSaving(true); setRrResult(null);
    try {
      await secretsApi.putRerankerKey(rrKey);
      setRrKey('');
      setRrResult({ success: true, message: '已保存 API Key（仅 Key；写入 .env，有审计日志）' });
      await loadRerankerConfig();
    } catch (e) {
      setRrResult({ success: false, message: axiosErrMsg(e) });
    } finally { setRrSaving(false); }
  };

  const testReranker = async () => {
    setRrTesting(true); setRrResult(null);
    try {
      const r = await secretsApi.testReranker({ model: rrModel || undefined, api_base: rrApiBase || undefined });
      setRrResult({ success: r.data.ok, message: r.data.ok ? `连接成功：${r.data.model}（${r.data.latency_ms}ms）` : '连接失败' });
    } catch (e) {
      setRrResult({ success: false, message: axiosErrMsg(e) });
    } finally { setRrTesting(false); }
  };

  const clearRerankerKey = async () => {
    setRrSaving(true); setRrResult(null);
    try {
      await secretsApi.putRerankerKey('');
      setRrResult({ success: true, message: '已清除（未配置 Key 时重排自动降级跳过）' });
      await loadRerankerConfig();
    } catch (e) {
      setRrResult({ success: false, message: axiosErrMsg(e) });
    } finally { setRrSaving(false); setRrConfirmClear(false); }
  };

  const renderRerankerTab = () => (
    <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '20px', maxWidth: 720 }}>
      <h3 style={{ fontSize: '15px', fontWeight: 600, margin: '0 0 4px', display: 'flex', alignItems: 'center', gap: 6 }}>
        <Cpu size={16} color="#8b5cf6" /> Reranker 检索重排
      </h3>
      <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: '0 0 16px' }}>
        混合检索（向量+关键词）之后接 BGE-Reranker 相关性重排。默认关闭；未配置 Key 或调用失败时自动降级跳过重排，主检索链路不受影响。
      </p>
      <div style={{ fontSize: '12px', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 12 }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
          <input type="checkbox" checked={rrEnabled} onChange={(e) => setRrEnabled(e.target.checked)} />
          <span style={{ fontWeight: 600, color: rrEnabled ? '#059669' : 'var(--color-text-secondary)' }}>{rrEnabled ? '已启用' : '已关闭'}</span>
        </label>
        <span>
          <span style={{ color: 'var(--color-text-secondary)' }}>Key 状态：</span>
          {rrStatus?.configured
            ? <span style={{ color: '#059669' }}>已配置: {rrStatus.masked}（{sourceLabel[rrStatus.source] || rrStatus.source}）</span>
            : <span style={{ color: '#d97706' }}>未配置</span>}
        </span>
      </div>
      <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)', display: 'block' }}>API Key
        <input
          type="password"
          value={rrKey}
          onChange={(e) => setRrKey(e.target.value)}
          placeholder="sk-...（留空提交表示不修改）"
          autoComplete="off"
          style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }}
        />
      </label>
      <div style={{ display: 'flex', gap: 8, marginTop: 10, paddingBottom: 12, borderBottom: '1px dashed var(--color-border)' }}>
        <button onClick={saveRerankerKey} disabled={rrSaving} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#1a56db', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
          <Save size={13} /> 保存 Key（仅 Key）
        </button>
        {rrConfirmClear ? (
          <>
            <button onClick={clearRerankerKey} disabled={rrSaving} style={{ padding: '8px 14px', borderRadius: 8, background: '#dc2626', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>确认清除</button>
            <button onClick={() => setRrConfirmClear(false)} style={{ padding: '8px 14px', borderRadius: 8, background: '#f1f5f9', color: '#64748b', border: '1px solid var(--color-border)', fontSize: 12, cursor: 'pointer' }}>取消</button>
          </>
        ) : (
          <button onClick={() => setRrConfirmClear(true)} disabled={rrSaving} style={{ padding: '8px 14px', borderRadius: 8, background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', fontSize: 12, cursor: 'pointer' }}>清除</button>
        )}
      </div>
      <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)', marginTop: 16, marginBottom: 8 }}>模型与接口</div>
      <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)', display: 'block' }}>Reranker 模型名
        <input
          type="text"
          value={rrModel}
          onChange={(e) => setRrModel(e.target.value)}
          placeholder="如: BAAI/bge-reranker-v2-m3"
          style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }}
        />
      </label>
      <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)', display: 'block', marginTop: 12 }}>Reranker API Base
        <input
          type="text"
          value={rrApiBase}
          onChange={(e) => setRrApiBase(e.target.value)}
          placeholder="如: https://api.siliconflow.cn/v1"
          style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }}
        />
      </label>
      <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
        <button onClick={saveRerankerConfig} disabled={rrSaving} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#059669', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
          <Save size={13} /> 保存启用/模型/Base
        </button>
        <button onClick={testReranker} disabled={rrTesting} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#0ea5e9', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
          {rrTesting ? <Loader2 size={13} className="spin" /> : <Plug size={13} />} 测试连接
        </button>
      </div>
      {rrResult && (
        <div style={{ marginTop: 12, padding: '8px 12px', borderRadius: 8, fontSize: 12, background: rrResult.success ? '#ecfdf5' : '#fef2f2', color: rrResult.success ? '#059669' : '#dc2626' }}>{rrResult.message}</div>
      )}
      <p style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: 12 }}>
        提示：仅系统管理员可修改；配置写入 .env（审计见 .dev/env_writes.log）。
      </p>
    </div>
  );

  const renderEmbeddingTab = () => (
    <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '20px', maxWidth: 720 }}>
      <h3 style={{ fontSize: '15px', fontWeight: 600, margin: '0 0 4px', display: 'flex', alignItems: 'center', gap: 6 }}>
        <Database size={16} color="#0ea5e9" /> Embedding / 知识库
      </h3>
      <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: '0 0 16px' }}>
        用于 RAG 知识库向量化。Key 保存在本地系统凭据管理器（keyring），优先于 .env；保存后立即生效，无需重启。日志与响应只显示末 4 位掩码。
      </p>
      <div style={{ fontSize: '12px', marginBottom: 12 }}>
        <span style={{ color: 'var(--color-text-secondary)' }}>当前状态：</span>
        {embStatus?.configured
          ? <span style={{ color: '#059669' }}>已配置: {embStatus.masked}（{sourceLabel[embStatus.source] || embStatus.source}）</span>
          : <span style={{ color: '#d97706' }}>未配置</span>}
        {embEffectiveModel && (
          <span style={{ marginLeft: 12 }}>
            <span style={{ color: 'var(--color-text-secondary)' }}>当前模型：</span>
            <span style={{ color: '#0ea5e9', fontWeight: 600 }}>{embEffectiveModel}</span>
          </span>
        )}
      </div>
      <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)', display: 'block' }}>API Key
        <input
          type="password"
          value={embKey}
          onChange={(e) => setEmbKey(e.target.value)}
          placeholder="sk-...（留空提交表示不修改）"
          autoComplete="off"
          style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }}
        />
      </label>
      <div style={{ display: 'flex', gap: 8, marginTop: 10, paddingBottom: 12, borderBottom: '1px dashed var(--color-border)' }}>
        <button onClick={saveEmbKey} disabled={embSaving} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#1a56db', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
          <Save size={13} /> 保存 Key（仅 Key）
        </button>
        {confirmClear ? (
          <>
            <button onClick={clearEmbKey} disabled={embSaving} style={{ padding: '8px 14px', borderRadius: 8, background: '#dc2626', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>确认清除</button>
            <button onClick={() => setConfirmClear(false)} style={{ padding: '8px 14px', borderRadius: 8, background: '#f1f5f9', color: '#64748b', border: '1px solid var(--color-border)', fontSize: 12, cursor: 'pointer' }}>取消</button>
          </>
        ) : (
          <button onClick={() => setConfirmClear(true)} disabled={embSaving} style={{ padding: '8px 14px', borderRadius: 8, background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', fontSize: 12, cursor: 'pointer' }}>清除</button>
        )}
      </div>
      <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--color-text)', marginTop: 16, marginBottom: 8 }}>模型与接口（影响 Embedding 调用目标）</div>
      <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)', display: 'block' }}>Embedding 模型名
        <input
          type="text"
          value={embModel}
          onChange={(e) => setEmbModel(e.target.value)}
          placeholder="如: text-embedding-v3"
          style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }}
        />
      </label>
      <label style={{ fontSize: '12px', color: 'var(--color-text-secondary)', display: 'block', marginTop: 12 }}>Embedding API Base
        <input
          type="text"
          value={embApiBase}
          onChange={(e) => setEmbApiBase(e.target.value)}
          placeholder="如: https://dashscope.aliyuncs.com/compatible-mode/v1"
          style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }}
        />
      </label>
      <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
        <button onClick={saveEmbConfig} disabled={embSaving} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#059669', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
          <Save size={13} /> 保存模型/Base（仅模型与接口）
        </button>
        <button onClick={testEmb} disabled={embTesting} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#0ea5e9', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer' }}>
          {embTesting ? <Loader2 size={13} className="spin" /> : <Plug size={13} />} 测试连接
        </button>
      </div>
      {embResult && (
        <div style={{ marginTop: 12, padding: '8px 12px', borderRadius: 8, fontSize: 12, background: embResult.success ? '#ecfdf5' : '#fef2f2', color: embResult.success ? '#059669' : '#dc2626' }}>{embResult.message}</div>
      )}
      <p style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: 12 }}>
        提示：仅系统管理员可修改；清除后回退到环境变量 / .env 中的 BMP_EMBEDDING_API_KEY（若有）。
      </p>
    </div>
  );

  const renderImageTab = () => (
    <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '20px', maxWidth: 720 }}>
      <h3 style={{ fontSize: '15px', fontWeight: 600, margin: '0 0 5px', display: 'flex', alignItems: 'center', gap: 6 }}>
        <ImageIcon size={16} color="#7c3aed" /> AI 配图
      </h3>
      <p style={{ fontSize: '12px', color: 'var(--color-text-secondary)', margin: '0 0 16px', lineHeight: 1.6 }}>
        配图是正文导出的增强项，不会阻塞解读、生成或检查。默认关闭；开启后，系统会根据章节建议在 DOCX 排版/导出阶段插入图片。
      </p>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: 'var(--color-text)', marginBottom: 14, cursor: 'pointer' }}>
        <input type="checkbox" checked={imageEnabled} onChange={(event) => setImageEnabled(event.target.checked)} />
        导出时启用 AI 配图
      </label>
      <label style={{ fontSize: 12, color: 'var(--color-text-secondary)', display: 'block' }}>供应商
        <select value={imageProvider} onChange={(event) => setImageProvider(event.target.value)} style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, background: '#fff' }}>
          <option value="fallback">默认服务（不可达时使用离线占位图）</option>
          <option value="volcengine">火山方舟</option>
          <option value="google">Google Imagen</option>
          <option value="custom">自定义 OpenAI 兼容端点</option>
        </select>
      </label>
      <label style={{ fontSize: 12, color: 'var(--color-text-secondary)', display: 'block', marginTop: 12 }}>API Base（自定义端点填写）
        <input type="text" value={imageBase} onChange={(event) => setImageBase(event.target.value)} placeholder="https://api.example.com/v1" style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }} />
      </label>
      <label style={{ fontSize: 12, color: 'var(--color-text-secondary)', display: 'block', marginTop: 12 }}>模型名
        <input type="text" value={imageModel} onChange={(event) => setImageModel(event.target.value)} placeholder="如 doubao-seedream / dall-e-3" style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }} />
      </label>
      <label style={{ fontSize: 12, color: 'var(--color-text-secondary)', display: 'block', marginTop: 12 }}>API Key
        <input type="password" value={imageKey} onChange={(event) => setImageKey(event.target.value)} placeholder="留空表示不修改" autoComplete="off" style={{ display: 'block', width: '100%', marginTop: 4, padding: '7px 10px', borderRadius: 8, border: '1px solid var(--color-border)', fontSize: 13, boxSizing: 'border-box' }} />
      </label>
      <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', marginTop: 8 }}>
        当前 Key：{imageKeyMasked || '未配置'}（来源：{imageKeySource === 'keyring' ? '本机凭据管理器' : imageKeySource === 'envfile' || imageKeySource === 'env' ? '环境配置' : '未配置'}）
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
        <button onClick={saveImageConfig} disabled={imageSaving} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#059669', color: '#fff', border: 0, fontSize: 12, cursor: imageSaving ? 'wait' : 'pointer' }}><Save size={13} /> 保存配图设置</button>
        <button onClick={saveImageKey} disabled={imageSaving || !imageKey.trim()} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#1a56db', color: '#fff', border: 0, fontSize: 12, cursor: imageSaving || !imageKey.trim() ? 'not-allowed' : 'pointer', opacity: imageSaving || !imageKey.trim() ? 0.6 : 1 }}><Save size={13} /> 保存 Key</button>
        <button onClick={testImage} disabled={imageTesting} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#0ea5e9', color: '#fff', border: 0, fontSize: 12, cursor: imageTesting ? 'wait' : 'pointer' }}>{imageTesting ? <Loader2 size={13} className="spin" /> : <Plug size={13} />} 检查供应商</button>
      </div>
      {imageMessage && <div style={{ marginTop: 12, padding: '8px 12px', borderRadius: 8, fontSize: 12, background: imageMessage.success ? '#ecfdf5' : '#fef2f2', color: imageMessage.success ? '#047857' : '#b91c1c' }}>{imageMessage.text}</div>}
    </div>
  );

  // ── vNext: 协议服务状态（MCP / A2A / AG-UI）─────────────
  const [mcpStatus, setMcpStatus] = useState<Record<string, unknown> | null>(null);
  const [a2aStatus, setA2aStatus] = useState<Record<string, unknown> | null>(null);
  const [aguiStatus, setAguiStatus] = useState<Record<string, unknown> | null>(null);
  const [protoTesting, setProtoTesting] = useState(false);
  const [protoResult, setProtoResult] = useState<{ success: boolean; message: string } | null>(null);

  const loadProtocols = useCallback(async () => {
    try {
      const [m, a, g] = await Promise.allSettled([mcpApi.status(), a2aApi.status(), aguiApi.status()]);
      if (m.status === 'fulfilled') setMcpStatus(m.value.data);
      if (a.status === 'fulfilled') setA2aStatus(a.value.data);
      if (g.status === 'fulfilled') setAguiStatus(g.value.data);
    } catch (e) { console.error('加载协议状态失败', e); }
  }, []);

  const testMcp = async () => {
    setProtoTesting(true);
    setProtoResult(null);
    try {
      const r = await mcpApi.test();
      setProtoResult({ success: r.data.success, message: r.data.success ? 'MCP 工具调用成功' : (r.data.error || '失败') });
    } catch (e) {
      setProtoResult({ success: false, message: (e as Error).message });
    } finally { setProtoTesting(false); }
  };

  const renderProtocolsTab = () => (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '20px' }}>
        <h3 style={{ fontSize: '15px', fontWeight: 600, margin: '0 0 8px', display: 'flex', alignItems: 'center', gap: 6 }}>
          <PlugZap size={16} color="#8b5cf6" /> MCP（Model Context Protocol）
        </h3>
        <StatusRow label="启用状态" value={mcpStatus ? String((mcpStatus as { enabled?: boolean }).enabled) : '…'} ok={Boolean(mcpStatus && (mcpStatus as { enabled?: boolean }).enabled)} />
        <StatusRow label="工具 / 资源" value={mcpStatus ? `${String((mcpStatus as { tools_count?: number }).tools_count)} / ${String((mcpStatus as { resources_count?: number }).resources_count)}` : '…'} />
        <StatusRow label="SDK" value={mcpStatus ? String((mcpStatus as { version?: string }).version || '') : ''} />
        <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginTop: 4 }}>
          地址：{mcpStatus ? String((mcpStatus as { addresses?: Record<string, string> }).addresses?.streamable_http || '') : '…'}
        </div>
        {mcpStatus && (mcpStatus as { tools?: Array<{ name: string }> }).tools && (
          <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: 8 }}>
            工具：{(mcpStatus as { tools?: Array<{ name: string }> }).tools?.map((t) => t.name).join('、')}
          </div>
        )}
        <button onClick={testMcp} disabled={protoTesting} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 14px', borderRadius: 8, background: '#8b5cf6', color: '#fff', border: 'none', fontSize: 12, cursor: 'pointer', marginTop: 12 }}>
          {protoTesting ? <Loader2 size={13} className="spin" /> : <Plug size={13} />} MCP 健康测试
        </button>
        {protoResult && (
          <div style={{ marginTop: 8, padding: '8px 12px', borderRadius: 8, fontSize: 12, background: protoResult.success ? '#ecfdf5' : '#fef2f2', color: protoResult.success ? '#059669' : '#dc2626' }}>{protoResult.message}</div>
        )}
      </div>

      <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '20px' }}>
        <h3 style={{ fontSize: '15px', fontWeight: 600, margin: '0 0 8px', display: 'flex', alignItems: 'center', gap: 6 }}>
          <Server size={16} color="#0f766e" /> A2A（Agent2Agent，官方 a2a-sdk 1.1.2）
        </h3>
        <StatusRow label="启用状态" value={a2aStatus ? String((a2aStatus as { enabled?: boolean }).enabled) : '…'} ok={Boolean(a2aStatus && (a2aStatus as { enabled?: boolean }).enabled)} />
        <StatusRow label="Agent Card" value={a2aStatus ? String((a2aStatus as { agent_card_url?: string }).agent_card_url || '') : '…'} />
        <StatusRow label="Skills" value={a2aStatus ? String((a2aStatus as { skills_count?: number }).skills_count) : '…'} />
        <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: 6 }}>
          Skills：{a2aStatus && (a2aStatus as { skills?: string[] }).skills ? (a2aStatus as { skills?: string[] }).skills?.join('、') : '…'}
        </div>
      </div>

      <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '20px' }}>
        <h3 style={{ fontSize: '15px', fontWeight: 600, margin: '0 0 8px', display: 'flex', alignItems: 'center', gap: 6 }}>
          <Activity size={16} color="#d97706" /> AG-UI（Agent-User Interaction，官方 ag-ui-protocol 0.1.21）
        </h3>
        <StatusRow label="启用状态" value={aguiStatus ? String((aguiStatus as { enabled?: boolean }).enabled) : '…'} ok={Boolean(aguiStatus && (aguiStatus as { enabled?: boolean }).enabled)} />
        <StatusRow label="Run 端点" value={aguiStatus ? String((aguiStatus as { endpoints?: Record<string, string> }).endpoints?.run || '') : '…'} />
        <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: 6 }}>
          能力：streaming / tool_call_lifecycle / step_events / hitl_interrupt / resume
        </div>
      </div>
    </div>
  );

  // ── vNext: 环境诊断 ─────────────────────────────────────
  const [diagChecks, setDiagChecks] = useState<Array<Record<string, unknown>>>([]);
  const [diagLoading, setDiagLoading] = useState(false);

  const loadDiagnostics = useCallback(async () => {
    setDiagLoading(true);
    try {
      const r = await diagnosticsApi.get();
      setDiagChecks(r.data.checks || []);
    } catch (e) { console.error('加载诊断失败', e); }
    finally { setDiagLoading(false); }
  }, []);

  useEffect(() => {
    if (activeTab === 'ocr') { loadOcrConfig(); }
    if (activeTab === 'image') { loadImageConfig(); }
    if (activeTab === 'embedding') { loadEmbStatus(); }
    if (activeTab === 'reranker') { loadRerankerConfig(); }
    if (activeTab === 'protocols') { loadProtocols(); }
    if (activeTab === 'diagnostics') { loadDiagnostics(); }
  }, [activeTab, loadOcrConfig, loadImageConfig, loadEmbStatus, loadRerankerConfig, loadProtocols, loadDiagnostics]);

  const renderDiagnosticsTab = () => (
    <div style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: '12px', padding: '20px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <h3 style={{ fontSize: '15px', fontWeight: 600, margin: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Activity size={16} color="#1a56db" /> 环境诊断
        </h3>
        <button onClick={loadDiagnostics} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '7px 12px', borderRadius: 8, border: '1px solid var(--color-border)', background: '#fff', fontSize: 12, cursor: 'pointer' }}>
          <RefreshCw size={13} className={diagLoading ? 'spin' : ''} /> 重新检测
        </button>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 10 }}>
        {diagChecks.map((check, i) => {
          const status = String(check.status || '?');
          const ok = ['ok', 'configured', 'enabled', 'off', 'disabled', 'not_configured'].includes(status);
          const color = status === 'ok' || status === 'configured' || status === 'enabled' ? '#059669' : status === 'off' || status === 'disabled' || status === 'not_configured' ? '#d97706' : '#dc2626';
          return (
            <div key={i} style={{ border: '1px solid var(--color-border)', borderRadius: 10, padding: '12px 14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span style={{ fontSize: '13px', fontWeight: 600 }}>{String(check.name)}</span>
                <span style={{ fontSize: '11px', fontWeight: 700, color, background: `${color}15`, padding: '2px 8px', borderRadius: 6 }}>{status}</span>
              </div>
              <div style={{ fontSize: '11px', color: 'var(--color-text-secondary)', marginTop: 6 }}>
                {Object.entries(check).filter(([k]) => !['name', 'status'].includes(k)).map(([k, v]) => `${k}=${typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v)}`).join('  ')}
              </div>
              {Boolean(check.error) && <div style={{ fontSize: '11px', color: '#dc2626', marginTop: 4 }}>{String(check.error)}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );

  function StatusRow({ label, value, ok }: { label: string; value: string; ok?: boolean }) {
    return (
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: '12px', marginTop: 4 }}>
        <span style={{ color: 'var(--color-text-secondary)', minWidth: 80 }}>{label}</span>
        {ok !== undefined && (ok ? <CheckCircle2 size={13} color="#059669" /> : <XCircle size={13} color="#dc2626" />)}
        <span style={{ color: 'var(--color-text)', wordBreak: 'break-all' }}>{value}</span>
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: '32px' }}>
        <h2 style={{ fontSize: '24px', fontWeight: 700 }}>平台设置</h2>
        <p style={{ fontSize: '14px', color: 'var(--color-text-secondary)', marginTop: '4px' }}>
          多智能体模型配置、LLM供应商管理、权限管理、Skill管理
        </p>
      </div>

      <div style={{ display: 'flex', gap: '8px', marginBottom: '20px' }}>
        {([
          { key: 'agents' as SettingsTab, label: '智能体模型配置', icon: <Bot size={14} /> },
          { key: 'llm' as SettingsTab, label: 'LLM供应商', icon: <Cpu size={14} /> },
          { key: 'embedding' as SettingsTab, label: 'Embedding/知识库', icon: <Database size={14} /> },
          { key: 'reranker' as SettingsTab, label: 'Reranker重排', icon: <Cpu size={14} /> },
          { key: 'ocr' as SettingsTab, label: 'OCR识别', icon: <ScanText size={14} /> },
          { key: 'image' as SettingsTab, label: 'AI 配图', icon: <ImageIcon size={14} /> },
          { key: 'protocols' as SettingsTab, label: 'MCP/A2A/AG-UI', icon: <PlugZap size={14} /> },
          { key: 'diagnostics' as SettingsTab, label: '环境诊断', icon: <Activity size={14} /> },
          { key: 'rbac' as SettingsTab, label: '权限管理', icon: <Shield size={14} /> },
          { key: 'skills' as SettingsTab, label: 'Skill管理', icon: <Settings size={14} /> },
        ]).map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            style={{
              padding: '8px 16px',
              background: activeTab === tab.key ? 'var(--color-primary)' : 'var(--color-surface)',
              color: activeTab === tab.key ? 'white' : 'var(--color-text)',
              border: `1px solid ${activeTab === tab.key ? 'var(--color-primary)' : 'var(--color-border)'}`,
              borderRadius: '8px',
              cursor: 'pointer',
              fontSize: '13px',
              fontWeight: 500,
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            {tab.icon} {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'llm' && renderLLMTab()}
      {activeTab === 'embedding' && renderEmbeddingTab()}
      {activeTab === 'reranker' && renderRerankerTab()}
      {activeTab === 'agents' && renderAgentsTab()}
      {activeTab === 'ocr' && renderOcrTab()}
      {activeTab === 'image' && renderImageTab()}
      {activeTab === 'protocols' && renderProtocolsTab()}
      {activeTab === 'diagnostics' && renderDiagnosticsTab()}
      {activeTab === 'rbac' && renderRbacTab()}
      {activeTab === 'skills' && renderSkillsTab()}
    </div>
  );
}
