import axios from 'axios';
import type { CitationLedger } from '../components/common/CitationContentView';

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
  headers: {
    'Content-Type': 'application/json',
  },
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('bidmaster_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const storeData = localStorage.getItem('bidmaster-app-store');
      if (storeData) {
        try {
          const parsed = JSON.parse(storeData);
          if (parsed?.state?.token) {
            localStorage.removeItem('bidmaster_token');
            localStorage.removeItem('bidmaster_user');
            const cleanState = { ...parsed, state: { ...parsed.state, token: null, user: null } };
            localStorage.setItem('bidmaster-app-store', JSON.stringify(cleanState));
          }
        } catch { /* ignore */ }
      }
      if (window.location.pathname !== '/login') {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  }
);

export interface Project {
  id: string;
  name: string;
  status: string;
  created_at: string;
}

export interface InterpretResult {
  success: boolean;
  data?: Record<string, unknown>;
  error?: string;
  warnings?: string[];
}

export interface CheckReport {
  id: string;
  type: string;
  risk_level: string;
  summary: Record<string, unknown>;
  created_at: string;
}

export interface OutlineNode {
  id: string;
  title: string;
  level: number;
  children: OutlineNode[];
  score_mapping?: string[];
  page_target?: number;
  status?: 'pending' | 'generating' | 'done';
}

export interface GateInfo {
  stage: string;
  label: string;
  status: 'pending' | 'confirmed' | 'blocked';
  items?: string[];
}

export interface LoginUser {
  id: string;
  email: string;
  name: string;
  role: string;
  avatar: string | null;
  roles: Array<{ id: string; name: string; display_name: string }>;
}

export const authApi = {
  login: (email: string, password: string) =>
    api.post<{ token: string; user: LoginUser }>('/auth/login', { email, password }),
  me: () => {
    const token = localStorage.getItem('bidmaster_token');
    return api.get<LoginUser>(`/auth/me?token=${token}`);
  },
  logout: () => {
    const token = localStorage.getItem('bidmaster_token');
    return api.post(`/auth/logout?token=${token}`);
  },
  changePassword: (oldPassword: string, newPassword: string) => {
    const token = localStorage.getItem('bidmaster_token');
    return api.put('/auth/change-password', { token, old_password: oldPassword, new_password: newPassword });
  },
};

export const projectApi = {
  list: () => api.get<{ projects: Project[] }>('/projects/'),
  create: (name: string) => api.post(`/projects/?name=${encodeURIComponent(name)}`),
  get: (id: string) => api.get(`/projects/${id}`),
  updateStatus: (id: string, status: string) => api.patch(`/projects/${id}/status?status=${status}`),
  confirmGate: (id: string, stage: string) => api.post(`/projects/${id}/gate/${stage}`),
  listGates: (id: string) => api.get(`/projects/${id}/gate`),
  remove: (id: string) => api.delete<{ success: boolean; deleted: Record<string, number> }>(
    `/projects/${id}?confirm=true`
  ),
};

export const interpretApi = {
  upload: (projectId: string, files: File[], documentType: 'tender' | 'reference' | 'bid' = 'tender') => {
    const formData = new FormData();
    for (const f of files) {
      formData.append('files', f);
    }
    return api.post(`/interpret/upload/${projectId}?document_type=${documentType}`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  listDocuments: (projectId: string) => api.get(`/interpret/documents/${projectId}`),
  searchEvidence: (projectId: string, query: string, topK = 8) =>
    api.get(`/interpret/evidence/${projectId}/search`, { params: { query, top_k: topK } }),
  getDocument: (documentId: string) => api.get(`/interpret/document/${documentId}`),
  deleteDocument: (documentId: string) => api.delete(`/interpret/document/${encodeURIComponent(documentId)}`),
  getAnalysis: (projectId: string) => api.get<{
    has_documents: boolean;
    has_parsed: boolean;
    has_analysis: boolean;
    analysis: {
      dimensions: Record<string, unknown> | null;
      scoring_matrix: Record<string, unknown> | null;
      risk_flags: Record<string, unknown> | null;
      sections: unknown[] | null;
    } | null;
    parse_info: {
      text_length: number;
      doc_metadata: Record<string, unknown> | null;
    } | null;
  }>(`/interpret/analysis/${projectId}`),
  parse: (projectId: string) => api.post(`/interpret/parse/${projectId}`),
  parseDocument: (documentId: string) => api.post(`/interpret/parse-document/${encodeURIComponent(documentId)}`),
  interpret: (projectId: string) => api.post(`/interpret/interpret/${projectId}`, null, { timeout: 600000 }),
  scoringMatrix: (projectId: string) => api.post(`/interpret/scoring-matrix/${projectId}`, null, { timeout: 600000 }),
  riskAlert: (projectId: string) => api.post(`/interpret/risk-alert/${projectId}`, null, { timeout: 600000 }),
  exportReport: (projectId: string, format: string = 'markdown') =>
    api.post(`/interpret/export/${projectId}?format=${format}`, null, { responseType: 'blob' }),
};

export const generateApi = {
  // BUG-18：走现有 axios 实例（自动带 Bearer token）。后端此路由实际为一次性 JSON 返回，非 SSE 流。
  generateChapter: (projectId: string, chapterId: string, mode: string = 'A', signal?: AbortSignal) =>
    api.post(`/generate/${projectId}/content/stream/${chapterId}?mode=${mode}`, {}, { timeout: 300000, signal }),
  mandatoryExtract: (projectId: string) =>
    api.post(`/generate/${projectId}/mandatory-extract`),
  generateStructure: (projectId: string, structureType: string) =>
    api.post(`/generate/${projectId}/structure/${structureType}`),
  scoreCoverage: (projectId: string) =>
    api.get(`/generate/${projectId}/score-coverage`),
  chapters: (projectId: string) =>
    api.get<{
      chapters: Array<{
        id: string;
        title: string;
        level: number;
        status: string;
        word_count: number;
        has_content: boolean;
        citation_ledger?: CitationLedger | null;
      }>
    }>(`/generate/${projectId}/chapters`),
  saveOutline: (projectId: string, tree: unknown) =>
    api.put<{ success: boolean; chapters: number; removed: number }>(`/generate/${projectId}/outline`, { tree }),
  // Worker G：章节正文直连导出 DOCX（不经排版管线，blob 下载）
  // 导出只读取已保存配图；章节配图通过 generateChapterIllustrations 单独完成。
  exportDocxDirect: (projectId: string, illustrations = false, imageOptions?: { provider?: string; size?: string; force?: boolean; chapterId?: string; storedOnly?: boolean }) => {
    const params = new URLSearchParams();
    if (illustrations) params.set('illustrations', 'true');
    if (imageOptions?.force) params.set('force_illustrations', 'true');
    if (imageOptions?.provider) params.set('illustration_provider', imageOptions.provider);
    if (imageOptions?.size) params.set('illustration_size', imageOptions.size);
    if (imageOptions?.chapterId) params.set('illustration_chapter_id', imageOptions.chapterId);
    if (imageOptions?.storedOnly) params.set('stored_illustrations_only', 'true');
    const query = params.toString();
    return api.get(`/generate/${projectId}/export-docx${query ? `?${query}` : ''}`, { responseType: 'blob', timeout: 300000 });
  },
  // G-2：章节正文 + 引用对照表（正文页【n】点查来源的数据源）
  chapterContent: (projectId: string, chapterId: string) =>
    api.get<{
      chapter_id: string;
      title: string;
      mode: string;
      status: string;
      word_count: number;
      content: string;
      citation_ledger: CitationLedger;
      sources: Array<{ n: number; chunk_id: string; source: string }>;
    }>(`/generate/${projectId}/chapters/${chapterId}`),
  generateChapterIllustrations: (projectId: string, chapterId: string, options?: { provider?: string; imageSize?: string; regenerate?: boolean }) =>
    api.post(`/generate/${projectId}/chapters/${chapterId}/illustrations`, {
      provider: options?.provider,
      image_size: options?.imageSize || 'landscape_16_9',
      regenerate: Boolean(options?.regenerate),
    }, { timeout: 300000 }),
};

export const checkApi = {
  compliance: (projectId: string) => api.post(`/check/${projectId}/compliance`),
  disqualification: (projectId: string) => api.post(`/check/${projectId}/disqualification`),
  qualification: (projectId: string) => api.post(`/check/${projectId}/qualification`),
  pricing: (projectId: string) => api.post(`/check/${projectId}/pricing`),
  fitScore: (projectId: string) => api.post(`/check/${projectId}/fit-score`),
  selfcheck: (projectId: string) => api.post(`/check/${projectId}/selfcheck`),
  fullCheck: (projectId: string) => api.post(`/check/${projectId}/full-check`, {}, { timeout: 1800000 }),
  deposit: (projectId: string) => api.post(`/check/${projectId}/deposit`),
  signature: (projectId: string) => api.post(`/check/${projectId}/signature`),
  validity: (projectId: string) => api.post(`/check/${projectId}/validity`),
  consistency: (projectId: string) => api.post(`/check/${projectId}/consistency`),
  duplicate: (projectId: string) => api.post(`/check/${projectId}/duplicate`),
  mandatoryReq: (projectId: string) => api.post(`/check/${projectId}/mandatory-req`),
  docIntegrity: (projectId: string) => api.post(`/check/${projectId}/doc-integrity`),
  aiTextCheck: (projectId: string) => api.post(`/check/${projectId}/ai-text-check`),
  riskScore: (projectId: string) => api.post(`/check/${projectId}/risk-score`),
  crossCheck: (projectId: string) => api.post(`/check/${projectId}/cross-check`),
  sampleReport: (projectId: string) => api.post(`/check/${projectId}/sample-report`),
  jointBid: (projectId: string) => api.post(`/check/${projectId}/joint-bid`),
  ebidSubmit: (projectId: string) => api.post(`/check/${projectId}/ebid-submit`),
  pricingLogic: (projectId: string) => api.post(`/check/${projectId}/pricing-logic`),
  listReports: (projectId: string) => api.get(`/check/${projectId}/reports`),
  exportReport: (projectId: string, reportId: string, format: string = 'markdown') =>
    api.get(`/check/${projectId}/reports/${reportId}/export?format=${format}`, { responseType: 'blob' }),
  // Worker I 任务2：《需补充材料清单》导出（docx 表格 / markdown，确定性提取无 LLM）
  exportMissingMaterials: (projectId: string, format: 'docx' | 'markdown' = 'docx') =>
    api.get(`/check/${projectId}/missing-materials-docx?format=${format}`, { responseType: 'blob', timeout: 120000 }),
  uploadCheck: (bidFile: File, tenderFile: File | null, checkType: string = 'fullCheck') => {
    const formData = new FormData();
    formData.append('bid_file', bidFile);
    if (tenderFile) {
      formData.append('tender_file', tenderFile);
    }
    formData.append('check_type', checkType);
    return api.post('/check/upload-check', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    });
  },
};

export const formatApi = {
  format: (file: File, template: string = 'default', mode: string = 'format') => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post(`/format/format?template=${template}&mode=${mode}`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  checkFormat: (file: File, template: string = 'default') => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post(`/format/check-format?template=${template}`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  diffFormat: (file: File, template: string = 'default') => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post(`/format/diff-format?template=${template}`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  beautify: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post('/format/beautify', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  listTemplates: () => api.get('/format/templates'),
  getTemplate: (name: string) => api.get(`/format/templates/${name}`),
  saveTemplate: (name: string, config: Record<string, unknown>) => api.put(`/format/templates/${name}`, config),
  deleteTemplate: (name: string) => api.delete(`/format/templates/${name}`),
};

export const skillApi = {
  list: () => api.get('/skills/'),
  get: (name: string) => api.get(`/skills/${name}`),
  execute: (name: string, parameters: Record<string, unknown>, projectId: string = '') =>
    api.post(`/skills/${name}/execute?project_id=${projectId}`, { parameters }),
};

export const llmApi = {
  listProviders: () => api.get('/llm/providers'),
  testConnection: (config: Record<string, string>) => api.post('/llm/test', config),
  getUsage: () => api.get('/llm/usage'),
  listAgentModels: () => api.get('/llm/agent-models'),
  updateAgentModel: (name: string, config: Record<string, unknown>) => api.put(`/llm/agent-models/${name}`, config),
  resetAgentModels: () => api.post('/llm/agent-models/reset'),
  setDefaultModel: (config: Record<string, string>) => api.put('/llm/default-model', config),
  getDefaultModel: () =>
    api.get<{
      model: string;
      api_base: string;
      api_key_masked: string | null;
      api_key_set: boolean;
    }>('/llm/default-model'),
};

export const secretsApi = {
  status: () =>
    api.get<{ secrets: Record<string, { configured: boolean; source: string; masked: string | null }> }>(
      '/secrets/status'
    ),
  put: (field: string, value: string) =>
    api.put<{ success: boolean; masked: string | null; cleared: boolean }>(`/secrets/${field}`, { value }),
  getImageConfig: () =>
    api.get<{
      enabled: boolean;
      provider: string;
      api_base: string;
      model: string;
      api_key_masked: string | null;
      api_key_set: boolean;
      api_key_source: string;
    }>('/secrets/image/config'),
  putImageConfig: (config: { enabled?: boolean; provider?: string; api_base?: string; model?: string }) =>
    api.put<{ success: boolean; enabled: boolean; provider: string; api_base: string; model: string }>(
      '/secrets/image/config', config
    ),
  putImageKey: (value: string) =>
    api.put<{ success: boolean; masked: string | null; cleared: boolean }>('/secrets/BMP_IMAGE_API_KEY', { value }),
  getEmbeddingConfig: () =>
    api.get<{
      model: string;
      api_base: string;
      api_key_masked: string | null;
      api_key_set: boolean;
      source: string;
    }>('/secrets/embedding/config'),
  putEmbeddingConfig: (config: { model?: string; api_base?: string }) =>
    api.put<{ success: boolean; model: string; api_base: string; needs_restart: boolean }>(
      '/secrets/embedding/config',
      config
    ),
  testEmbedding: (config?: { model?: string; api_base?: string }) =>
    api.post<{ ok: boolean; latency_ms?: number; model?: string }>('/secrets/embedding/test', config ?? {}),
  // P-C: Reranker 配置
  getRerankerConfig: () =>
    api.get<{
      enabled: boolean;
      model: string;
      api_base: string;
      api_key_masked: string | null;
      api_key_set: boolean;
      api_key_source: string;
      source: string;
    }>('/secrets/reranker/config'),
  putRerankerConfig: (config: { enabled?: boolean; model?: string; api_base?: string }) =>
    api.put<{ success: boolean; enabled: boolean; model: string; api_base: string }>(
      '/secrets/reranker/config',
      config
    ),
  putRerankerKey: (value: string) =>
    api.put<{ success: boolean; masked: string | null; cleared: boolean }>('/secrets/reranker/key', { value }),
  testReranker: (config?: { model?: string; api_base?: string }) =>
    api.post<{ ok: boolean; latency_ms?: number; model?: string }>('/secrets/reranker/test', config ?? {}),
};

export const newsApi = {
  listTasks: () => api.get('/news/tasks'),
  createTask: (data: { name: string; keywords: string; sites?: string[]; interval_minutes?: number }) =>
    api.post('/news/tasks', data),
  updateTask: (id: string, data: { enabled?: boolean; name?: string; keywords?: string }) =>
    api.patch(`/news/tasks/${id}`, data),
  deleteTask: (id: string) => api.delete(`/news/tasks/${id}`),
  runTask: (id: string) => api.post(`/news/tasks/${id}/run`),
  semanticFilter: (taskId: string, companyProfile: string = '', threshold: number = 0.6) =>
    api.post(`/news/tasks/${taskId}/semantic-filter?company_profile=${encodeURIComponent(companyProfile)}&threshold=${threshold}`),
  listResults: (taskId: string) => api.get(`/news/tasks/${taskId}/results`),
  todayHot: (category: string = 'all', limit: number = 50) =>
    api.get(`/news/today-hot?category=${category}&limit=${limit}`),
  refreshHot: () => api.post('/news/refresh-hot'),
};

export const knowledgeApi = {
  list: () => api.get('/knowledge/'),
  create: (data: { name: string; embedding_model?: string; kb_type?: string }) => api.post('/knowledge/', data),
  delete: (id: string) => api.delete(`/knowledge/${id}`),
  upload: (id: string, file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post(`/knowledge/${id}/upload`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },
  search: (id: string, query: string, topK: number = 5) =>
    api.post(`/knowledge/${id}/search?query=${encodeURIComponent(query)}&top_k=${topK}`),
};

export const rbacApi = {
  listRoles: () => api.get('/rbac/roles'),
  createRole: (data: { name: string; display_name: string; description?: string }) => api.post('/rbac/roles', data),
  updateRole: (id: string, data: { display_name?: string; description?: string }) => api.put(`/rbac/roles/${id}`, data),
  deleteRole: (id: string) => api.delete(`/rbac/roles/${id}`),
  listPermissions: () => api.get('/rbac/permissions'),
  assignPermissions: (roleId: string, permissionIds: string[]) => api.post(`/rbac/roles/${roleId}/permissions`, { permission_ids: permissionIds }),
  removePermission: (roleId: string, permissionId: string) => api.delete(`/rbac/roles/${roleId}/permissions/${permissionId}`),
  listUsers: () => api.get('/rbac/users'),
  createUser: (data: { email: string; name: string; password: string }) => api.post('/rbac/users', data),
  updateUser: (id: string, data: { name?: string; email?: string }) => api.put(`/rbac/users/${id}`, data),
  deleteUser: (id: string) => api.delete(`/rbac/users/${id}`),
  assignRoles: (userId: string, roleIds: string[]) => api.post(`/rbac/users/${userId}/roles`, { role_ids: roleIds }),
  removeRole: (userId: string, roleId: string) => api.delete(`/rbac/users/${userId}/roles/${roleId}`),
  checkPermission: (userId: string, permissionCode: string) => api.post('/rbac/check-permission', { user_id: userId, permission_code: permissionCode }),
  initRbac: () => api.post('/rbac/init'),
};

export const aiImageApi = {
  generate: (prompt: string, provider: string = 'default', imageSize: string = 'landscape_16_9') =>
    api.post('/ai-image/generate', { prompt, provider, image_size: imageSize }, { timeout: 120000 }),
  listProviders: () => api.get('/ai-image/providers'),
  saveConfig: (provider: string, apiKey: string) =>
    api.put(
      '/ai-image/config',
      provider === 'google' ? { provider, google_api_key: apiKey } : { provider, volcengine_api_key: apiKey },
    ),
};

// ===== 资格预审（Qualification Match + HITL Workflow）=====

export type RequirementType = 'certificate' | 'capital' | 'project_experience' | 'personnel' | 'region';
export type MatchStatus = 'met' | 'unmet' | 'insufficient';
export type WorkflowStatus = 'waiting_human' | 'resumed' | 'completed';
export type ReviewDecision = 'confirm' | 'reject' | 'mark_insufficient';

export interface Requirement {
  requirement_id: string;
  requirement_type: RequirementType;
  description?: string;
  certificate_name?: string | null;
  valid_until?: string | null;
  min_amount?: number | string | null;
  currency?: string;
  min_count?: number | null;
  date_from?: string | null;
  date_to?: string | null;
  personnel_title?: string | null;
  region?: string | null;
  source_refs?: string[];
  source_text?: string | null;
  source_path?: string | null;
}

export interface Credential {
  credential_id: string;
  credential_type: string;
  name?: string | null;
  certificate_name?: string | null;
  issue_date?: string | null;
  expiry_date?: string | null;
  amount?: number | string | null;
  amount_text?: string | null;
  currency?: string;
  project_name?: string | null;
  contract_amount?: number | string | null;
  contract_amount_text?: string | null;
  start_date?: string | null;
  completion_date?: string | null;
  personnel_title?: string | null;
  certificate_number?: string | null;
  region?: string | null;
  evidence_refs: string[];
  source?: string | null;
}

export interface MatchResult {
  requirement_id: string;
  requirement_type: string;
  status: MatchStatus;
  reason: string;
  evidence_refs: string[];
  matched_credential_ids: string[];
  warnings: string[];
}

export interface MatchSummary {
  total: number;
  met: number;
  unmet: number;
  insufficient: number;
}

export interface MatchReport {
  overall_status: MatchStatus;
  summary: MatchSummary;
  results: MatchResult[];
  warnings: string[];
}

export interface ReviewItem {
  requirement_id: string;
  status: string;
  reason: string;
  evidence_refs: string[];
  decision?: ReviewDecision | null;
}

export interface WorkflowDecision {
  requirement_id: string;
  decision: ReviewDecision;
  reviewer?: string;
  note?: string;
}

export interface WorkflowDecisionRecord {
  requirement_id: string;
  decision: ReviewDecision;
  reviewer: string;
  note: string;
  decided_at: string;
}

export interface QualificationWorkflow {
  workflow_id: string;
  status: WorkflowStatus;
  report: MatchReport;
  review_items: ReviewItem[];
  decisions: WorkflowDecisionRecord[];
  warnings: string[];
}

export interface UnresolvedItem {
  source_field: string;
  source_text: string;
  reason: string;
  source_path?: string | null;
}

export interface AdapterResult {
  requirements: Requirement[];
  unresolved_items: UnresolvedItem[];
  warnings: string[];
}

export interface RunFromAnalysisResponse extends QualificationWorkflow {
  adapter: AdapterResult;
}

export interface ProjectAdapter extends AdapterResult {
  project_id: string;
}

export interface RunFromProjectResponse extends RunFromAnalysisResponse {
  project_id: string;
}

export interface FlywheelMetrics {
  run_count: number;
  approval_run_count: number;
  auto_complete_rate: number;
  human_intervention_rate: number;
  insufficient_rate: number;
  human_override_rate: number;
  entrypoint_counts: Record<string, number>;
  status_counts: Record<string, number>;
}

export interface CredentialCandidate {
  candidate_id: string;
  credential_type: string;
  name?: string | null;
  certificate_name?: string | null;
  certificate_number?: string | null;
  issue_date?: string | null;
  expiry_date?: string | null;
  amount?: number | string | null;
  amount_text?: string | null;
  currency?: string;
  project_name?: string | null;
  contract_amount?: number | string | null;
  contract_amount_text?: string | null;
  completion_date?: string | null;
  personnel_title?: string | null;
  person_ref?: string | null;
  region?: string | null;
  source_path?: string | null;
  source_excerpt: string;
  confidence_level: 'high' | 'medium' | 'low';
  needs_human_confirmation: boolean;
  warnings: string[];
}

export interface CredentialAdapterResult {
  candidates: CredentialCandidate[];
  unresolved_items: UnresolvedItem[];
  warnings: string[];
}

export interface EvalDatasetInfo {
  name: string;
  version: string;
  description: string;
  case_count: number;
}

export interface FailedCaseSummary {
  case_id: string;
  requirement_id: string;
  expected: string;
  actual: string;
  reason: string;
}

export interface EvalReport {
  dataset_name: string;
  dataset_version: string;
  matcher_version: string;
  case_count: number;
  valid_case_count: number;
  invalid_case_count: number;
  requirement_accuracy: number;
  overall_accuracy: number;
  confusion_matrix: Record<string, Record<string, number>>;
  evidence_invariant_violations: Array<{ case_id: string; requirement_id: string }>;
  by_requirement_type: Record<string, { total: number; correct: number; accuracy: number }>;
  by_tag: Record<string, { case_count: number; overall_correct: number; overall_accuracy: number }>;
  failed_cases: FailedCaseSummary[];
  invalid_cases?: Array<{ case_id?: string; line_no?: number; reason: string }>;
}

export interface FlywheelTraceEvent {
  event_id: string;
  event_type: 'run' | 'approval';
  occurred_at: string;
  trace_id: string;
  project_ref?: string | null;
  entrypoint?: string;
  matcher_version?: string;
  adapter_version?: string | null;
  workflow_status?: string | null;
  overall_status?: string | null;
  summary?: Record<string, number>;
  review_item_count?: number;
  warning_count?: number;
  unresolved_count?: number | null;
  latency_ms?: number;
  decision_counts?: Record<string, number>;
  reviewer_filled?: boolean;
  human_override?: boolean;
}

export const qualificationApi = {
  match: (requirements: Requirement[], credentials: Credential[]) =>
    api.post<MatchReport>('/qualification/match', { requirements, credentials }),
  fromAnalysis: (dimensions: Record<string, unknown>) =>
    api.post<AdapterResult>('/qualification/from-analysis', { dimensions }),
  fromProject: (projectId: string) =>
    api.post<ProjectAdapter>(`/qualification/from-project/${projectId}`),
  flywheelMetrics: () => api.get<FlywheelMetrics>('/qualification/flywheel/metrics'),
  flywheelTraces: (limit = 50) =>
    api.get<{ traces: FlywheelTraceEvent[]; count: number }>(`/qualification/flywheel/traces?limit=${limit}`),
  flywheelExport: () => api.get('/qualification/flywheel/export', { responseType: 'blob' }),
  evalDatasets: () => api.get<{ datasets: EvalDatasetInfo[] }>('/qualification/flywheel/eval/datasets'),
  evalRun: (datasetName?: string) =>
    api.post<EvalReport>('/qualification/flywheel/eval/run', { dataset_name: datasetName ?? 'synthetic_baseline' }),
  evalLatest: () => api.get<EvalReport>('/qualification/flywheel/eval/latest'),
  credentialsFromText: (text: string, sourceLabel?: string) =>
    api.post<CredentialAdapterResult>('/qualification/credentials/from-text', { text, source_label: sourceLabel ?? 'manual' }),
  credentialsFromProject: (projectId: string) =>
    api.post<CredentialAdapterResult>(`/qualification/credentials/from-project/${projectId}`),
  confirmCredential: (candidate: CredentialCandidate, evidenceRef: string) =>
    api.post<Credential>('/qualification/credentials/confirm', { candidate, evidence_ref: evidenceRef }),
};


// ─────────────────────────────────────────────────────────────
// vNext: OCR / MCP / A2A / AG-UI / 规则治理 / 监控 / 诊断
// ─────────────────────────────────────────────────────────────

export interface OcrConfig {
  mode: string;
  endpoint: string;
  api_key_masked: string;
  api_key_set: boolean;
  timeout: number;
  poll_interval: number;
  max_polls: number;
}

export interface OcrTaskInfo {
  task_id: string;
  project_id: string;
  document_id: string;
  state: string;
  error_class: string | null;
  error_message: string | null;
  polls: number;
  has_result: boolean;
  result_length: number;
}

export const ocrApi = {
  getConfig: () => api.get<OcrConfig>('/ocr/config'),
  updateConfig: (body: Record<string, unknown>) => api.post<{ success: boolean; config: OcrConfig }>('/ocr/config', body),
  testConnection: (body?: Record<string, unknown>) => api.post<{ success: boolean; message?: string; error?: string; error_class?: string }>('/ocr/test', body ?? {}),
  scan: (projectId: string) => api.post(`/ocr/scan/${projectId}`),
  status: (projectId: string) => api.get<{ project_id: string; tasks: OcrTaskInfo[]; summary: Record<string, number> }>(`/ocr/status/${projectId}`),
  poll: (projectId: string) => api.post(`/ocr/poll/${projectId}`),
  applyResults: (projectId: string) => api.post(`/ocr/result/${projectId}`),
  run: (projectId: string) => api.post(`/ocr/run/${projectId}`),
};

export const mcpApi = {
  status: () => api.get<Record<string, unknown>>('/mcp/status'),
  capabilities: () => api.get<Record<string, unknown>>('/mcp/capabilities'),
  test: () => api.post<{ success: boolean; message?: string; error?: string }>('/mcp/test'),
};

export const a2aApi = {
  status: () => api.get<Record<string, unknown>>('/a2a/status'),
  agentCard: () => api.get<Record<string, unknown>>('/a2a/agent-card'),
};

export const aguiApi = {
  status: () => api.get<Record<string, unknown>>('/agui/status'),
  run: (body: Record<string, unknown>) => api.post('/agui/run', body),
  resume: (body: Record<string, unknown>) => api.post('/agui/resume', body),
};

/**
 * 流式生成章节正文（SSE）。逐步回调累积的正文文本；流结束时 resolve 最终全文。
 * 后端 POST /generate/{project_id}/content/stream/{chapter_id}?mode=X 返回 text/event-stream。
 */
export async function streamChapterGenerate(
  projectId: string,
  chapterId: string,
  mode: string,
  onContent: (accumulated: string) => void,
  signal?: AbortSignal,
): Promise<{
  content: string;
  wordCount?: number;
  error?: string;
  citationLedger?: unknown;
  grounding?: unknown;
  citationRate?: unknown;
}> {
  const token = localStorage.getItem('bidmaster_token') || '';
  const resp = await fetch(`/api/generate/${projectId}/content/stream/${chapterId}?mode=${encodeURIComponent(mode)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: '{}',
    signal,
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const j = await resp.json();
      detail = j?.detail || j?.error || detail;
    } catch { /* ignore */ }
    throw new Error(`正文生成失败：${detail}`);
  }
  if (!resp.body) throw new Error('正文生成失败：无响应流');

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let content = '';
  let wordCount: number | undefined;
  let error: string | undefined;
  let citationLedger: unknown;
  let grounding: unknown;
  let citationRate: unknown;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\n\n');
    buffer = frames.pop() || '';
    for (const frame of frames) {
      const line = frame.trim();
      if (!line.startsWith('data:')) continue;
      try {
        const evt = JSON.parse(line.slice(5).trim());
        if (typeof evt.content === 'string') {
          content += evt.content;
          onContent(content);
        }
        if (evt.done) {
          wordCount = evt.word_count;
          // P3：B 模式末尾事件携带引用账本/grounding 统计
          if (evt.citation_ledger !== undefined) citationLedger = evt.citation_ledger;
          if (evt.grounding !== undefined) grounding = evt.grounding;
          if (evt.citation_rate !== undefined) citationRate = evt.citation_rate;
        }
        if (evt.error) error = evt.error;
      } catch { /* 忽略坏帧 */ }
    }
  }
  return { content, wordCount, error, citationLedger, grounding, citationRate };
}

/** 读取 AG-UI SSE 事件流（fetch + ReadableStream）。返回事件对象数组。 */
export async function streamAguiRun(
  body: Record<string, unknown>,
  onEvent: (event: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = localStorage.getItem('bidmaster_token') || '';
  const resp = await fetch('/api/agui/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`AG-UI 请求失败: HTTP ${resp.status}`);
  }
  const reader = resp.body.getReader();
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
        try {
          onEvent(JSON.parse(line.slice(6)));
        } catch { /* 忽略坏帧 */ }
      }
    }
  }
}

export interface RuleProposal {
  proposal_id: string;
  source: string;
  template: string;
  requirement_type: string;
  params: Record<string, unknown>;
  statistics: Record<string, unknown>;
  rationale: string;
  status: string;
  created_at: string;
}

export interface RulePack {
  pack_id: string;
  version: string;
  name: string;
  status: string;
  rules: Array<Record<string, unknown>>;
  proposal_ids: string[];
  created_at: string;
  published_at: string | null;
  published_by: string;
  rolled_back_to: string | null;
  eval_summary: Record<string, unknown>;
}

export const rulesApi = {
  proposals: (status?: string) => api.get<{ proposals: RuleProposal[] }>('/rules/proposals', { params: status ? { status } : {} }),
  generate: (limit = 5) => api.post<{ created: RuleProposal[] }>('/rules/proposals/generate', null, { params: { limit } }),
  approve: (id: string, reviewer = '', note = '') => api.post<RuleProposal>(`/rules/proposals/${id}/approve`, { reviewer, note }),
  reject: (id: string, reviewer = '', note = '') => api.post<RuleProposal>(`/rules/proposals/${id}/reject`, { reviewer, note }),
  packs: () => api.get<{ packs: RulePack[] }>('/rules/packs'),
  createPack: (name: string, proposalIds: string[]) => api.post<RulePack>('/rules/packs', { name, proposal_ids: proposalIds }),
  publish: (id: string) => api.post<RulePack>(`/rules/packs/${id}/publish`, { published_by: 'admin' }),
  rollback: (id: string) => api.post<RulePack>(`/rules/packs/${id}/rollback`, { rolled_back_by: 'admin' }),
  audit: () => api.get<{ events: Array<Record<string, unknown>> }>('/rules/audit'),
};

export const monitorApi = {
  status: () => api.get<Record<string, unknown>>('/monitor/status'),
  metrics: (windowMinutes = 60, kind?: string) => api.get<Record<string, unknown>>('/monitor/metrics', { params: { window_minutes: windowMinutes, kind } }),
  spans: (limit = 50, kind?: string) => api.get<{ spans: Array<Record<string, unknown>> }>('/monitor/spans', { params: { limit, kind } }),
};

export const diagnosticsApi = {
  get: () => api.get<{ checks: Array<Record<string, unknown>>; overall_ok: boolean }>('/diagnostics'),
};

// ── /api/graph（P-D1 LangGraph 主编排图运行监视器，契约见 core/agent_engine/README.md）──

export type GraphRunStatus = 'running' | 'pending_decision' | 'finalized' | 'failed';

export interface GraphRunSummary {
  run_id: string;
  project_id: string;
  status: GraphRunStatus;
  created_at: string;
  final_level?: string | null;
  /** 同一项目中按创建时间排序的最新运行；旧服务未返回时按最新兼容处理。 */
  is_latest?: boolean;
}

export interface GraphDecisionPackage {
  level?: string | null;
  rationale?: string | null;
  evidence?: Array<Record<string, unknown> | string>;
  risks?: Array<Record<string, unknown> | string>;
  [key: string]: unknown;
}

export interface GraphRunSnapshot {
  node_status: Record<string, string>;
  pending_gate: string | null;
  pending_gate_namespace?: 'qualification' | 'scope' | 'decision' | null;
  stage_results?: Record<string, Record<string, unknown>>;
  decision_package: GraphDecisionPackage | null;
  human_decision: Record<string, unknown> | null;
  override_reason: string | null;
  final_level?: string | null;
  [key: string]: unknown;
}

export interface GraphRunDetail {
  success: boolean;
  run_id: string;
  project_id: string;
  status: GraphRunStatus;
  error?: string | null;
  snapshot: GraphRunSnapshot | null;
  pending_gate_namespace?: 'qualification' | 'scope' | 'decision' | null;
  override_reason?: string | null;
  decision_package?: GraphDecisionPackage | null;
}

export interface GraphNodeCost {
  llm_calls: number;
  tokens: number;
  duration_ms: number;
  [key: string]: unknown;
}

export interface GraphCostReport {
  run_id: string;
  nodes: Record<string, GraphNodeCost>;
  total_llm_calls: number;
  total_tokens: number;
  total_duration_ms: number;
  [key: string]: unknown;
}

export const graphApi = {
  // POST /api/graph/runs：异步启动全图运行（chapterIds 可选：限定正文生成章节，缺省全部）
  start: (projectId: string, chapterIds?: string[]) =>
    api.post<{ success: boolean; run_id: string; status: string }>(
      '/graph/runs',
      chapterIds && chapterIds.length ? { project_id: projectId, chapter_ids: chapterIds } : { project_id: projectId },
    ),
  // GET /api/graph/runs：运行列表
  list: () => api.get<{ success: boolean; runs: GraphRunSummary[] }>('/graph/runs'),
  // GET /api/graph/runs/{run_id}：运行详情（含 snapshot）
  get: (runId: string) => api.get<GraphRunDetail>(`/graph/runs/${encodeURIComponent(runId)}`),
  // 图运行检查报告导出：没有旧 CheckReport 记录时仍可从当前运行快照导出
  exportReport: (runId: string, format: string = 'markdown') =>
    api.get(`/graph/runs/${encodeURIComponent(runId)}/export-report?format=${format}`, { responseType: 'blob', timeout: 120000 }),
  // POST /api/graph/runs/{run_id}/decision：决策门（approve / override+level+reason）
  decide: (runId: string, body:
    | { action: 'approve'; namespace?: 'decision' | 'qualification'; decisions?: Array<Record<string, unknown>> }
    | { action: 'refresh'; namespace: 'qualification' }
    | { action: 'approve'; namespace: 'scope'; chapter_ids: string[] }
    | { action: 'recheck'; namespace?: 'decision'; check_ids?: string[] }
    | { action: 'override'; level: string; reason: string; namespace?: 'decision' }
  ) =>
    api.post<{ success: boolean; snapshot: GraphRunSnapshot }>(`/graph/runs/${encodeURIComponent(runId)}/decision`, body),
  // GET /api/graph/runs/{run_id}/cost：成本报告
  cost: (runId: string) => api.get<{ success: boolean; cost: GraphCostReport }>(`/graph/runs/${encodeURIComponent(runId)}/cost`),
};

// ── G-5 T1：业务子图运行视图（解读/生成/检查/资格预审四页共用）──

/** 业务子图运行条目（解读/生成/检查均为 {run_id,project_id,status,created_at,...} 形状） */
export interface BizGraphRunSummary {
  run_id: string;
  project_id: string;
  status: string;
  created_at: number | string;
  error?: string | null;
  workflow_status?: string;
}

/** 业务子图运行快照（各图 node_status 键不同，统一宽松渲染） */
export interface BizGraphRunDetail {
  success: boolean;
  run_id: string;
  project_id: string;
  status: string;
  error?: string | null;
  snapshot: {
    node_status: Record<string, string>;
    node_timings?: Record<string, number>;
    pending_gate?: string | null;
    pending_gate_namespace?: string | null;
    current_stage?: string;
    next_nodes?: string[];
    workflow_status?: string;
    review_items?: Array<Record<string, unknown>>;
    report?: Record<string, unknown> | null;
    chapters?: Array<Record<string, unknown>>;
    grounding?: Record<string, unknown>;
    timing?: Record<string, unknown>;
    errors?: string[];
    warnings?: string[];
    has_result?: boolean;
    mode?: string;
    [key: string]: unknown;
  } | null;
}

export const interpretGraphApi = {
  // GET /api/interpret/{p}/graph/runs：解读子图运行列表（G-5 新增）
  list: (projectId: string) =>
    api.get<{ success: boolean; runs: BizGraphRunSummary[] }>(`/interpret/${projectId}/graph/runs`),
  // GET /api/interpret/{p}/graph/runs/{run_id}：解读子图快照
  get: (projectId: string, runId: string) =>
    api.get<BizGraphRunDetail>(`/interpret/${projectId}/graph/runs/${encodeURIComponent(runId)}`),
};

export const generateGraphApi = {
  // POST /api/generate/{p}/graph/run：创建章节生成图运行
  start: (projectId: string, body?: Record<string, unknown>) =>
    api.post<{ success: boolean; run_id: string; status: string }>(`/generate/${projectId}/graph/run`, body ?? {}),
  // GET /api/generate/{p}/graph/runs：运行列表
  list: (projectId: string) =>
    api.get<{ success: boolean; runs: BizGraphRunSummary[] }>(`/generate/${projectId}/graph/runs`),
  // GET /api/generate/{p}/graph/runs/{run_id}：快照（含逐章进度/grounding/timing）
  get: (projectId: string, runId: string) =>
    api.get<BizGraphRunDetail>(`/generate/${projectId}/graph/runs/${encodeURIComponent(runId)}`),
};

export const checkGraphApi = {
  // POST /api/check/{p}/graph：创建检查图运行（单项/全量）
  start: (projectId: string, body?: { check_ids?: string[] | null; formats?: string[] }) =>
    api.post<{ success: boolean; run_id: string; status: string }>(`/check/${projectId}/graph`, body ?? {}),
  // GET /api/check/{p}/graph/{run_id}：快照
  get: (projectId: string, runId: string) =>
    api.get<BizGraphRunDetail>(`/check/${projectId}/graph/${encodeURIComponent(runId)}`),
};

export const qualificationGraphApi = {
  // POST /api/qualification/graph/run：图模式资格预审（requirements/dimensions/project_id）
  start: (body: Record<string, unknown>) =>
    api.post<{ success: boolean; run_id: string; status: string; error?: string }>('/qualification/graph/run', body),
  // GET /api/qualification/graph/runs：运行列表
  list: () =>
    api.get<{ success: boolean; runs: BizGraphRunSummary[] }>('/qualification/graph/runs'),
  // GET /api/qualification/graph/runs/{run_id}：快照（node_status/pending_gate/report/review_items）
  get: (runId: string) =>
    api.get<BizGraphRunDetail>(`/qualification/graph/runs/${encodeURIComponent(runId)}`),
  // POST /api/qualification/graph/runs/{run_id}/decision：HITL 审批（confirm/reject/mark_insufficient）
  decide: (runId: string, decisions: Array<{ requirement_id: string; decision: string; reviewer?: string; note?: string }>) =>
    api.post<{ success: boolean; snapshot: Record<string, unknown> }>(
      `/qualification/graph/runs/${encodeURIComponent(runId)}/decision`,
      { decisions }
    ),
};

export default api;
