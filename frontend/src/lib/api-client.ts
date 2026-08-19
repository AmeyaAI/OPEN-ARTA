/**
 * ARTA Platform — Typed API Client
 * Wraps fetch with JWT auth headers and base URL configuration.
 */

// API calls are proxied through Next.js rewrites (next.config.js)
// so we use same-origin (empty string). This avoids CORS issues and
// works with VS Code Remote SSH (only port 3001 needs forwarding).
// Override with NEXT_PUBLIC_ARTA_API_URL if you need direct API access.
const API_BASE = process.env.NEXT_PUBLIC_ARTA_API_URL ?? ''

let _accessToken: string | null = null

export function setToken(token: string | null) {
  _accessToken = token
  if (token) {
    localStorage.setItem('arta_token', token)
  } else {
    localStorage.removeItem('arta_token')
  }
}

export function getToken(): string | null {
  if (_accessToken) return _accessToken
  if (typeof window !== 'undefined') {
    _accessToken = localStorage.getItem('arta_token')
  }
  return _accessToken
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string> ?? {}),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  // Also send API key for endpoints that support it
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY
  if (apiKey) {
    headers['X-API-Key'] = apiKey
  }

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers })

  if (res.status === 401) {
    setToken(null)
    if (typeof window !== 'undefined') {
      window.location.href = '/login'
    }
    throw new Error('Unauthorized')
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    // F11-2: attach status code so callers can branch (e.g. 429 → friendly toast).
    // detail can be a string OR a structured dict (e.g. 409 dedup payload with
    // {error, message, workflow_id, ...}). Extract a human-readable message
    // either way so the thrown Error is actionable; pin the structured payload
    // onto the error itself so callers can branch on `.detail`.
    const rawDetail = body.detail ?? `HTTP ${res.status}`
    const message = typeof rawDetail === 'string'
      ? rawDetail
      : (rawDetail.message || rawDetail.error || JSON.stringify(rawDetail))
    const err = new Error(message) as Error & { status: number; detail?: any }
    err.status = res.status
    if (typeof rawDetail !== 'string') err.detail = rawDetail
    throw err
  }

  // 204 No Content (e.g. DELETE endpoints) has no body — res.json() would throw.
  if (res.status === 204) return undefined as T

  return res.json()
}

// ── Auth ─────────────────────────────────────────────────────────────────────

export interface UserPublic {
  id: string
  email: string
  full_name: string
  avatar_url: string | null
  is_admin: boolean
  is_active: boolean
  created_at: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: UserPublic
}

export async function login(email: string, password: string): Promise<TokenResponse> {
  const body = new URLSearchParams({ username: email, password })
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Login failed' }))
    throw new Error(err.detail)
  }
  const data: TokenResponse = await res.json()
  setToken(data.access_token)
  return data
}

export function logout() {
  setToken(null)
}

// ── Permissions ──────────────────────────────────────────────────────────────

export interface ProjectRoleEntry { project_id: string; role: string }

/** The current user's per-project roles (used to gate write actions in the UI). */
export async function fetchMyProjectRoles(userId: string): Promise<ProjectRoleEntry[]> {
  return request<ProjectRoleEntry[]>(`/api/users/${userId}/projects`)
}

// ── Dashboard ────────────────────────────────────────────────────────────────

export async function fetchHealth() {
  return request<{ status: string; platform: string; methodology: string }>('/health')
}

export async function fetchDashboard(project_id?: string) {
  const qs = project_id ? `?project_id=${project_id}` : ''
  return request<{
    quality_score: number
    coverage_pct: number
    pass_rate: number
    open_defects: number
    p0_defects: number
    total_scenarios: number
    active_agents: string[]
    pipeline: { build_id: string; stages: { name: string; status: string; duration_s?: number }[] }
  }>(`/api/dashboard${qs}`)
}

// ── Requirements ─────────────────────────────────────────────────────────────

export interface Requirement {
  id?: string
  req_id: string
  title: string
  description?: string
  priority: string
  risk_score: number
  impact?: number
  probability?: number
  status?: string
  acceptance_criteria?: {
    id: string; statement: string; covered: boolean
    given?: string | null; when?: string | null; then?: string | null
  }[]
  ac_count?: number
  test_count?: number
  coverage_pct?: number
  // Mirrors the backend's metadata.quality JSONB stamped at Jira import.
  metadata?: {
    quality?: {
      score: number
      band: 'clear' | 'weak' | 'unclear'
      measurable_ac_pct?: number | null
      ac_count?: number | null
      flags?: string[]
      ac_flags?: string[] // ids of ACs with no measurable criterion
    }
  }
}

export async function fetchRequirements(params?: { priority?: string; project_id?: string }) {
  const qs = new URLSearchParams()
  if (params?.priority) qs.set('priority', params.priority)
  if (params?.project_id) qs.set('project_id', params.project_id)
  const query = qs.toString() ? `?${qs}` : ''
  return request<{ requirements: Requirement[]; total: number }>(`/api/requirements${query}`)
}

export async function fetchRequirement(reqId: string) {
  return request<Requirement>(`/api/requirements/${reqId}`)
}

// FE-sync P3 — typed traceability-graph fetch. Routes through request<T>() so it
// carries the Bearer JWT + X-API-Key (the page's prior raw fetches used a dead
// `arta_api_key` localStorage key / no headers → 401 in keyed envs). Caller supplies
// the graph shape as T (the page's GraphData).
export async function fetchTraceabilityGraph<T = unknown>(
  params: { project_id?: string; run_id?: string } = {},
): Promise<T> {
  const qs = new URLSearchParams()
  if (params.project_id) qs.set('project_id', params.project_id)
  if (params.run_id) qs.set('run_id', params.run_id)
  const query = qs.toString() ? `?${qs}` : ''
  return request<T>(`/api/traceability/graph${query}`)
}

// ── Test Cases ───────────────────────────────────────────────────────────────

export interface TestCase {
  test_id: string
  id?: string
  title: string
  priority: string
  automation_tool: string
  tool?: string
  test_type: string
  last_status: string
  status?: string
  is_automated: boolean
  requirement_id?: string
  ac_id?: string
  gherkin_scenario?: string
  gherkin?: string
  script_content?: string
  script_path?: string
  duration_ms?: number
  test_data?: { columns: string[]; rows: string[][] } | null
  generated_at?: string
  // G2.3 — analytics + traceability fields (from Phase Gap-1 persistence)
  trace_id?: string
  model_version?: string
  prompt_version?: string
  red_phase_status?: 'RED' | 'GREEN_UNEXPECTED' | 'ERROR' | 'PENDING_VERIFICATION'
  judge_score?: number
  dataset_version?: string
  analytics_layer?: 'nl_to_query' | 'query_to_result' | 'result_to_insight' | 'insight_to_narrative' | 'e2e' | string
  tier?: 1 | 2 | 3
  adversarial_category?: string
  adversarial_input?: string
  error_message?: string
  generation_source?: 'llm' | 'fallback' | 'failed' | string
  // F7-3: Backend may include either / both — UI surfaces whichever is set
  generation_error?: string
  error?: string
}

export async function fetchTests(params?: { priority?: string; tool?: string; project_id?: string; flag?: string }) {
  const qs = new URLSearchParams()
  if (params?.priority) qs.set('priority', params.priority)
  if (params?.tool) qs.set('tool', params.tool)
  if (params?.project_id) qs.set('project_id', params.project_id)
  // R330 P1d — gate-stamped metadata filter (potentially_incorrect | guess | needs_attention)
  if (params?.flag) qs.set('flag', params.flag)
  const query = qs.toString() ? `?${qs}` : ''
  return request<{ tests: TestCase[]; total: number }>(`/api/tests${query}`)
}

// ── Execution / Runs ─────────────────────────────────────────────────────────

export interface TestRun {
  run_id: string
  build_id: string
  environment: string
  status: string
  gate_decision?: string
  total_tests: number
  passed: number
  failed: number
  pass_rate: number
  duration_ms?: number
  started_at?: string
  completed_at?: string
  // F7-3: Backend execution router populates these on real runs; the
  // integrations + use-execution-healing pages read them. Optional because
  // older runs / mock data may not include them.
  id?: string
  total?: number
  trigger?: string
  triggered_by?: string
  branch?: string
}

export async function fetchRuns(params?: { project_id?: string }) {
  const qs = new URLSearchParams()
  if (params?.project_id) qs.set('project_id', params.project_id)
  const query = qs.toString() ? `?${qs}` : ''
  return request<{ runs: TestRun[] }>(`/api/execution/runs${query}`)
}

// ── FE-sync P3 — SUT-quality drill-downs (backend computed these but the dashboard
// never consumed them). All run-scoped; the mission-report actionable_ctas point here.
export interface RunByRequirementRow {
  requirement_id: string
  total: number
  passed: number
  failed: number
  blocked: number
  skipped: number
  executed_pass_pct: number | null
  by_tool?: Record<string, Record<string, number>>
}
export interface RunByRequirement {
  run_id: string
  requirements?: RunByRequirementRow[]
  attributed_requirements?: number
  note?: string
  error?: string
}
export async function fetchRunByRequirement(runId: string) {
  return request<RunByRequirement>(`/api/execution/runs/${runId}/by-requirement`)
}

export interface AuthScopePrefix { prefix: string; count: number; sample_paths?: string[] }
export interface RunAuthScopeSummary {
  run_id: string
  prefixes?: AuthScopePrefix[]
  total_401s?: number
  warning?: string
}
export async function fetchRunAuthScopeSummary(runId: string) {
  return request<RunAuthScopeSummary>(`/api/execution/runs/${runId}/auth-scope-summary`)
}

export interface TopEndpoint5xx {
  endpoint_template: string
  count_5xx: number
  total_requests: number
  pct_5xx: number
}
export interface RunTopEndpoints5xx {
  run_id: string
  endpoints?: TopEndpoint5xx[]
  arta_attributed_excluded?: number
  note?: string
}
export async function fetchRunTopEndpoints5xx(runId: string) {
  return request<RunTopEndpoints5xx>(`/api/execution/runs/${runId}/top-endpoints-5xx`)
}

// evidence-package is a zip FileResponse — fetch as a blob + trigger a download
// (can't use request<T>, which parses JSON). Reuses api-client's own token logic.
export async function downloadEvidencePackage(runId: string): Promise<void> {
  const token = getToken()
  const headers: Record<string, string> = {}
  if (token) headers['Authorization'] = `Bearer ${token}`
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY
  if (apiKey) headers['X-API-Key'] = apiKey
  const res = await fetch(`${API_BASE}/api/execution/runs/${runId}/evidence-package`, { headers })
  if (!res.ok) throw new Error(`evidence-package: ${res.status}`)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `evidence-${runId}.zip`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export async function triggerRun(params: {
  suite_type: string; environment: string; tools?: string[]; project_id?: string;
  test_ids?: string[];
}): Promise<{ run_id: string; status: string }> {
  return request<{ run_id: string; status: string }>('/api/execution/run', {
    method: 'POST',
    body: JSON.stringify(params),
  })
}

export interface EnvironmentSummary {
  id: string;
  name: string;
  base_url: string;
  filename: string;
  has_storage_state: boolean;
  project_match: boolean;
}

// Fix HHH — extended to accept operator-supplied key/value pairs. When a
// `Record<string,string>` is passed in `namesOrValues`, the API persists each
// pair (overwriting only placeholder values). Backwards-compatible: passing
// a `string[]` falls back to the original "add blank placeholders" behavior.
export async function bulkAddEnvironmentVariables(
  projectId: string,
  envName: string,
  namesOrValues: string[] | Record<string, string>,
  defaultValue: string = "",
): Promise<{ added: string[]; skipped: string[]; total_variables: number }> {
  const body: Record<string, unknown> = { env_name: envName, default_value: defaultValue }
  if (Array.isArray(namesOrValues)) {
    body.names = namesOrValues
  } else {
    body.values = namesOrValues
  }
  return request(`/api/projects/${projectId}/environments/${envName}/variables/bulk`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// Fix HHH — runs the SUT Onboarding Agent (Phase G's GGG) and returns the
// discovered config plus a list of fields that still need operator input.
// The "Connect to SUT" wizard step (renderStep5_ConnectSUT) calls this and
// surfaces the three coloured panels: auto-discovered, probed, needs-input.
export async function runSutOnboarding(projectId: string): Promise<{
  onboarding_config: Record<string, unknown>;
  needs_operator: Array<{ field: string; reason: string; suggestion?: string; alternatives?: string[] }>;
  auto_resolved_count: number;
}> {
  return request(`/api/projects/${projectId}/sut/onboard`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export interface InFlightGeneration {
  requirement_id: string;
  ac_id: string | null;
  workflow_id: string;
  started_seconds_ago: number;
}

/** Returns currently-running generation pipelines so the UI can show
 *  "Generating…" state that survives page reloads and cross-tab nav. */
export async function fetchInFlightGenerations(): Promise<InFlightGeneration[]> {
  const res = await request<{ items: InFlightGeneration[]; count: number }>(
    '/api/tests/generations/in-flight',
  )
  return res.items || []
}

export async function fetchEnvironments(projectId: string): Promise<EnvironmentSummary[]> {
  const res = await request<{ environments: EnvironmentSummary[] }>(
    `/api/projects/${projectId}/environments`,
  )
  return res.environments || []
}

// ── Defects ──────────────────────────────────────────────────────────────────

export interface DefectItem {
  defect_id: string
  title: string
  severity: string
  priority: string
  status: string
  root_cause?: string
  suggested_fix?: string
  // F8-16: surfaced by POST /api/defects/{id}/analyze (DefectIntelAgent).
  // Typing them removes the (selected as any) escape-hatches in defects/page.tsx
  // and tells consumers what the backend may populate post-analysis.
  suggested_fix_language?: string
  impacted_components?: string[]
  heal_proposal?: {
    description?: string
    diff?: string
    confidence?: number
    [k: string]: unknown
  }
  // FE-sync P1 — the 5-level RCA deep-dive + preventive action, now carried through
  // POST /api/defects/{id}/analyze (previously discarded by the backend router).
  deep_dive?: {
    symptom?: string
    immediate_cause?: string
    upstream_cause?: string
    architectural_cause?: string
    process_cause?: string
  }
  preventive_action?: string
}

export async function fetchDefects(params?: { severity?: string; status?: string; priority?: string; project_id?: string; run_id?: string; limit?: number }) {
  const qs = new URLSearchParams()
  if (params?.severity)   qs.set('severity', params.severity)
  if (params?.status)     qs.set('status', params.status)
  if (params?.priority)   qs.set('priority', params.priority)
  if (params?.project_id) qs.set('project_id', params.project_id)
  // R28.4 — when run_id is supplied, response is scoped to defects
  // produced during that specific run (operator's "navigate back to
  // prior run" flow).
  if (params?.run_id)     qs.set('run_id', params.run_id)
  if (params?.limit)      qs.set('limit', String(params.limit))
  const query = qs.toString() ? `?${qs}` : ''
  return request<{ defects: DefectItem[]; total: number; run_id?: string | null }>(`/api/defects${query}`)
}

// FE-sync P1 — DefectIntelAgent RCA on demand. The response now carries the 5-level
// deep_dive + preventive_action (the router used to discard them). Merge the result
// into the selected defect to render the deep-dive.
export interface DefectAnalysis {
  defect_id: string
  status: string
  root_cause?: string
  suggested_fix?: string
  confidence?: number
  deep_dive?: DefectItem['deep_dive']
  preventive_action?: string
}
export async function analyzeDefect(defectId: string) {
  return request<DefectAnalysis>(`/api/defects/${defectId}/analyze`, { method: 'POST' })
}

// ── Projects ─────────────────────────────────────────────────────────────────

export interface RepositoryEntry {
  name: string
  repo: string
  branch: string
  provider?: 'github' | 'gitlab' | 'bitbucket'
}

export interface Project {
  id: string
  name: string
  description: string
  color: string
  icon: string
  project_type?: string
  engagement_model?: string
  stack_type?: string
  is_greenfield?: boolean
  llm_config: Record<string, unknown>
  integrations: Record<string, unknown>
}

export async function fetchProjects() {
  return request<{ projects: Project[]; total: number }>('/api/projects')
}

export async function createProject(data: Partial<Project>) {
  return request<Project>('/api/projects', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

export async function updateProject(projectId: string, data: Partial<Project>) {
  return request<Project>(`/api/projects/${projectId}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })
}

export async function testIntegration(projectId: string, integrationType: string) {
  // R104.A — payload key matches backend Pydantic schema `IntegrationTestRequest`
  // at src/api/routers/projects.py:1260 which expects `integration` (not `type`).
  // Pre-R104.A the UI showed a Pydantic validation error on every Test Connection
  // click: `{"type":"missing","loc":["body","integration"],"msg":"Field required",...}`.
  return request<{ status: string; latency_ms: number; message: string }>(
    `/api/projects/${projectId}/integrations/test`,
    { method: 'POST', body: JSON.stringify({ integration: integrationType }) },
  )
}

export async function fetchAISuggestions(context: string, type: string) {
  return request<{ suggestions: string[] }>('/api/assistant/command', {
    method: 'POST',
    body: JSON.stringify({ command: `/${type}`, args: context }),
  })
}

// ── AI Assistant ─────────────────────────────────────────────────────────────

export async function sendCommand(command: string, args: string, project_id?: string) {
  return request<Record<string, unknown>>('/api/assistant/command', {
    method: 'POST',
    body: JSON.stringify({ command, args, project_id }),
  })
}

// ── R320 Test-Case Refinement Copilot ───────────────────────────────────────
export interface RefinePayload {
  project_id?: string
  test_id?: string
  requirement_id?: string
  ac_id?: string
  tool?: string
  kind: string                 // endpoint | field_value | shape | intent
  method?: string
  path?: string
  field?: string
  from_value?: string
  to_value?: string
  correction_text: string
  corrected_by?: string
  confirm: boolean
}

export interface RefineResult {
  mode: string                 // draft | confirmed
  verdict?: string             // arta_knew | human_knowledge
  matched_source?: string | null
  proposed_fact?: Record<string, unknown>
  explain?: string
  correction_id?: string
  grounding_written?: boolean
  grounding_fact_ref?: string | null
  regen?: Record<string, unknown>
}

export async function refineTest(payload: RefinePayload) {
  return request<RefineResult>('/api/assistant/refine', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export interface TestCorrection {
  correction_id: string
  project_id?: string
  test_id?: string
  requirement_id?: string
  tool?: string
  kind: string
  correction_text: string
  from_value?: string
  to_value?: string
  verdict?: string
  grounding_fact_ref?: string
  verify_status?: string
  corrected_by?: string
  created_at?: string
}

export async function listCorrections(projectId?: string) {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return request<{ corrections: TestCorrection[]; total: number }>(`/api/assistant/corrections${qs}`)
}

export async function revertCorrection(correctionId: string) {
  return request<{ reverted: string; grounding_removed: number }>(
    `/api/assistant/corrections/${encodeURIComponent(correctionId)}`, { method: 'DELETE' })
}

export interface CorrectionsAnalytics {
  total: number
  arta_knew: number
  human_knowledge: number
  arta_defect_rate: number
  by_kind: Record<string, number>
  by_tool: Record<string, number>
  by_verify_status: Record<string, number>
  top_corrected_endpoints: { fact: string; count: number }[]
}

export async function correctionsAnalytics(projectId?: string) {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return request<CorrectionsAnalytics>(`/api/assistant/corrections/analytics${qs}`)
}

export async function verifyCorrection(correctionId: string) {
  return request<{ correction_id: string; verify_status: string; run_id?: string | null }>(
    `/api/assistant/corrections/${encodeURIComponent(correctionId)}/verify`, { method: 'POST' })
}

export async function reconcileVerify(correctionId: string) {
  return request<{ correction_id: string; verify_status: string }>(
    `/api/assistant/corrections/${encodeURIComponent(correctionId)}/verify-status`)
}

// R320 — the corrected test's artifact + latest failure, for the RefineWorkspace.
export async function fetchTest(testId: string, projectId?: string) {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return request<any>(`/api/tests/${encodeURIComponent(testId)}${qs}`)
}

export interface LatestResult {
  status: string | null
  error_message: string | null
  triage_category: string | null
  run_id: string | null
  executed_at?: string | null
}

export async function fetchTestLatestResult(testId: string) {
  return request<LatestResult>(`/api/assistant/tests/${encodeURIComponent(testId)}/latest-result`)
}

// ── Quality Gates ────────────────────────────────────────────────────────────

export async function checkGate(buildId: string) {
  return request<Record<string, unknown>>(`/api/gates/${buildId}`)
}

// ── SSE Streaming ───────────────────────────────────────────────────────────

export interface StreamEvent {
  type: string
  run_id?: string
  test_id?: string
  title?: string
  status?: string
  duration_ms?: number
  progress?: string
  passed?: number
  failed?: number
  total?: number
  error?: string
}

/**
 * Subscribe to live execution results via SSE.
 * Returns a cleanup function to close the connection.
 */
export function streamRunResults(
  runId: string,
  onEvent: (event: StreamEvent) => void,
  onDone: () => void,
): () => void {
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY || ''
  const url = `${API_BASE}/api/execution/runs/${runId}/stream${apiKey ? `?api_key=${apiKey}` : ''}`
  const es = new EventSource(url)

  es.onmessage = (e) => {
    if (e.data === '[DONE]') {
      onDone()
      es.close()
      return
    }
    try {
      const data: StreamEvent = JSON.parse(e.data)
      if (data.type === 'run_completed') {
        onEvent(data)
        onDone()
        es.close()
      } else {
        onEvent(data)
      }
    } catch {
      // ignore parse errors
    }
  }

  es.onerror = () => {
    es.close()
    onDone()
  }

  return () => es.close()
}

// ── Defect Creation ─────────────────────────────────────────────────────────

export async function createDefect(data: {
  title: string
  severity: string
  description?: string
  requirement_id?: string
  test_case_id?: string
}) {
  return request<Record<string, unknown>>('/api/defects', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

// ── Jira Integration ─────────────────────────────────────────────────────────

export async function createJiraTicket(defectId: string) {
  return request<{ jira_id: string; jira_url: string; defect_id: string; message: string }>(`/api/defects/${defectId}/file-jira`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
}

// R306.E: Exploratory Testing client helpers removed — the /api/exploratory
// router was deprovisioned (broken DB-mode SBTM prototype, disconnected from all
// ARTA pillars). Kept FE↔BE in sync so test_fe_be_api_sync stays green; restore
// alongside the router if the feature is reintroduced.

// ── Report Export ────────────────────────────────────────────────────────────

export type ReportFormat = 'pdf' | 'xlsx'

export interface ReportExportResponse {
  report_type: string
  format: string
  html?: string
  run_id?: string
  sheets?: Record<string, { columns: string[]; rows: unknown[][] }>
}

export async function exportRunReport(runId: string, format: ReportFormat = 'pdf') {
  return request<ReportExportResponse>(`/api/reports/runs/${runId}/export?format=${format}`)
}

export async function exportCoverageReport(format: ReportFormat = 'pdf') {
  return request<ReportExportResponse>(`/api/reports/coverage/export?format=${format}`)
}

export async function exportComplianceReport(format: ReportFormat = 'pdf') {
  return request<ReportExportResponse>(`/api/reports/compliance/export?format=${format}`)
}

// ── Self-Healing Stats ──────────────────────────────────────────────────────

export interface HealingStats {
  total_proposals: number
  total_healed: number
  total_rejected: number
  total_pending: number
  approval_rate: number
  avg_confidence: number
  hours_saved: number
  maintenance_reduction_pct: number
  strategy_breakdown: Record<string, number>
  weekly_trends: { date: string; healed: number; rejected: number; pending: number; avg_confidence: number }[]
}

export interface HealingProposal {
  id: string
  test_id: string
  defect_id: string | null
  strategy: string
  confidence: number
  ai_reasoning: string
  current_selector: string
  proposed_selector: string
  diff_preview: string
  status: string
  created_at: string
}

export async function fetchHealingStats(project_id?: string, run_id?: string) {
  // R68.4 — run_id scopes stats to one execution run (backend matches
  // proposal.metadata.last_seen_run_id).
  const params = new URLSearchParams()
  if (project_id) params.set('project_id', project_id)
  if (run_id) params.set('run_id', run_id)
  const qs = params.toString() ? `?${params}` : ''
  return request<HealingStats>(`/api/healing/stats${qs}`)
}

export async function fetchHealingQueue(status?: string, project_id?: string, run_id?: string) {
  // R68.4 — run_id scopes the heal queue to proposals from one run.
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  if (project_id) params.set('project_id', project_id)
  if (run_id) params.set('run_id', run_id)
  const qs = params.toString() ? `?${params}` : ''
  return request<{ proposals: HealingProposal[]; total: number; pending: number }>(`/api/healing/queue${qs}`)
}

export async function approveHealingProposal(proposalId: string, overrideSelector?: string) {
  return request<{ ok: boolean; proposal_id: string; applied_selector: string; github_pr: string }>(`/api/healing/${proposalId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ override_selector: overrideSelector || null }),
  })
}

export async function rejectHealingProposal(proposalId: string) {
  return request<{ ok: boolean; proposal_id: string; status: string }>(`/api/healing/${proposalId}/reject`, {
    method: 'POST',
  })
}

export async function requestHeal(
  testId: string,
  errorMessage: string,
  failureClass?: string,
  projectId?: string,
  testTitle?: string,
) {
  return request<{ proposal_id: string; status: string; redirect_url: string; message: string }>('/api/healing/propose', {
    method: 'POST',
    body: JSON.stringify({
      test_id: testId,
      error_message: errorMessage,
      failure_class: failureClass,
      project_id: projectId ?? null,
      test_title: testTitle ?? null,
    }),
  })
}

export async function runHealingFix(proposalId: string) {
  return request<{ proposal_id: string; status: string }>(`/api/healing/${proposalId}/run`, {
    method: 'POST',
  })
}

export async function getHealingRunResult(proposalId: string) {
  return request<{ status: 'running' | 'pass' | 'fail' | 'not_started'; output?: string; error?: string; error_count?: number }>(
    `/api/healing/${proposalId}/run/result`,
  )
}

export async function fetchHealingProposal(proposalId: string) {
  return request<HealingProposal & { _analyzing?: boolean }>(
    `/api/healing/${proposalId}`,
  )
}

// ── Dashboard Trends ────────────────────────────────────────────────────────

export interface DashboardTrends {
  quality_trends: { day: string; quality: number; coverage: number }[]
  pass_fail_by_type: { name: string; passed: number; failed: number }[]
}

export async function fetchDashboardTrends(days: number = 7, project_id?: string) {
  const params = new URLSearchParams({ days: String(days) })
  if (project_id) params.set('project_id', project_id)
  return request<DashboardTrends>(`/api/dashboard/trends?${params}`)
}

// ── Requirement Corrections & History ────────────────────────────────────────

export interface RequirementCorrection {
  title?: string;
  description?: string;
  priority?: string;
  acceptance_criteria?: { id: string; title: string }[];
}

export interface RequirementChange {
  version: number;
  changed_at: string;
  changed_by: string;
  trigger: string;
  summary: string;
}

export async function correctRequirement(reqId: string, corrections: RequirementCorrection) {
  const token = getToken();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/api/requirements/${reqId}`, {
    method: 'PATCH',
    headers,
    body: JSON.stringify(corrections),
  });
  if (!res.ok) throw new Error('Failed to save correction');
  return res.json();
}

export async function fetchRequirementChanges(reqId: string): Promise<RequirementChange[]> {
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/api/requirements/${reqId}/changes`, { headers });
  if (!res.ok) throw new Error('Failed to fetch changes');
  return res.json();
}

export async function previewUpload(file: File): Promise<any[]> {
  const formData = new FormData();
  formData.append('file', file);
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/api/requirements/upload?preview=true`, {
    method: 'POST',
    headers,
    body: formData,
  });
  if (!res.ok) throw new Error('Failed to preview upload');
  return res.json();
}

// R320 S3 — real requirement import (commit, not preview). Persists parsed
// requirements to the project (upload endpoint without ?preview).
export async function commitUpload(file: File, projectId?: string): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY;
  if (apiKey) headers['X-API-Key'] = apiKey;
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
  const res = await fetch(`${API_BASE}/api/requirements/upload${qs}`, {
    method: 'POST', headers, body: formData,
  });
  if (!res.ok) throw new Error(`Import failed (${res.status})`);
  return res.json();
}

export async function resyncJira(projectId: string) {
  const token = getToken();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/api/requirements/import/jira`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ project_id: projectId }),
  });
  if (!res.ok) throw new Error('Jira sync failed');
  return res.json();
}

// ── Review Gate ─────────────────────────────────────────────────────────────

export interface PendingReview {
  workflow_id: string;
  project_id?: string;   // F10-3: stamped on writes; absent on legacy entries
  requirement_id: string;
  status: string;
  created_at: string;
  gherkin_scenarios: string[];
  test_data: { fixture_id: string; columns: string[]; rows: Record<string, any>[] };
  risk_profile: { risk_score: number; priority: string; test_types: string[] };
}

export async function fetchPendingReviews(projectId?: string): Promise<PendingReview[]> {
  // F10-3: scope to the active project so the demo seed (or another
  // project's pending reviews) doesn't leak into this view.
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
  const res = await fetch(`${API_BASE}/api/tests/pending-reviews${qs}`);
  if (!res.ok) throw new Error('Failed to fetch pending reviews');
  return res.json();
}

export async function submitReviewDecision(
  workflowId: string,
  decision: 'approve' | 'reject',
  reason?: string,
  editedGherkin?: string[]
) {
  const res = await fetch(`${API_BASE}/api/tests/pending-reviews/${workflowId}/decide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, reason: reason || '', edited_gherkin: editedGherkin }),
  });
  if (!res.ok) throw new Error('Review decision failed');
  return res.json();
}

export async function editPendingGherkin(workflowId: string, scenarios: string[]) {
  const res = await fetch(`${API_BASE}/api/tests/pending-reviews/${workflowId}/gherkin`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ gherkin_scenarios: scenarios }),
  });
  if (!res.ok) throw new Error('Edit failed');
  return res.json();
}

export async function submitTestFeedback(testId: string, rating: number, corrections: string, regenerate: boolean) {
  const res = await fetch(`${API_BASE}/api/tests/${testId}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating, corrections, regenerate }),
  });
  if (!res.ok) throw new Error('Feedback submission failed');
  return res.json();
}

export async function pushTestToGitHub(testId: string) {
  const res = await fetch(`${API_BASE}/api/tests/${testId}/push-to-github`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error('Push to GitHub failed');
  return res.json();
}

// ── Test Script / Data / Versions ───────────────────────────────────────────

export async function fetchTestScript(testId: string) {
  return request<{ content: string; language: string }>(`/api/tests/${testId}/script`)
}

export async function fetchTestData(testId: string) {
  return request<{ rows: Record<string, string>[]; columns: string[] }>(`/api/tests/${testId}/data`)
}

// Frozen-dataset preview — file-based fixtures referenced by Gherkin steps.
export interface FixtureColumnSummary {
  name: string
  dtype: 'number' | 'string'
  null_pct: number
  distinct: number
  min?: number
  max?: number
}

export interface FixtureDataset {
  path: string
  absolute_path: string
  exists: boolean
  format: string
  size_bytes?: number
  modified_at?: number
  row_count?: number
  columns?: string[]
  sample_rows?: Record<string, unknown>[]
  column_summary?: FixtureColumnSummary[]
  preview_unavailable?: boolean
  message?: string
  error?: string
  alternatives?: string[]
  purpose?: 'primary' | 'alternative'
  alternative_for?: string
}

export interface FixtureDataResponse {
  test_id: string
  requirement_id: string
  datasets: FixtureDataset[]
  sample_row_limit: number
}

export async function fetchFixtureData(testId: string): Promise<FixtureDataResponse> {
  return request<FixtureDataResponse>(`/api/tests/${testId}/fixture-data`)
}

export async function materialiseFixture(
  testId: string,
  body?: { columns?: string[]; row_count?: number; version?: string; fmt?: string },
): Promise<{ ok: boolean; path: string; preview: FixtureDataset }> {
  return request(`/api/tests/${testId}/fixture-data/materialise`, {
    method: 'POST',
    body: JSON.stringify(body || {}),
  })
}

export interface FixtureReviewFinding {
  level: 'info' | 'warn' | 'error'
  step: string
  message: string
}

export interface FixtureReviewResponse {
  verdict: 'match' | 'partial' | 'mismatch' | 'info'
  summary: string
  findings: FixtureReviewFinding[]
  dataset_path?: string
}

export async function reviewFixtureData(testId: string): Promise<FixtureReviewResponse> {
  return request<FixtureReviewResponse>(`/api/tests/${testId}/fixture-data/review`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

// Build a download URL that the browser can fetch directly via an <a> tag —
// includes the API key as a query string because the <a download> request
// won't carry the Authorization/X-API-Key header that `request()` adds.
export function fixtureDownloadUrl(testId: string, path: string): string {
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY ?? ''
  const params = new URLSearchParams({ path })
  if (apiKey) params.set('api_key', apiKey)
  return `${API_BASE}/api/tests/${testId}/fixture-data/download?${params.toString()}`
}

export async function fetchTestVersions(testId: string) {
  return request<{ versions: { version: number; date: string; summary: string; author: string }[] }>(`/api/tests/${testId}/versions`)
}

export async function fetchTestVersionDiff(testId: string, v1: number, v2: number) {
  return request<{ diff: string; additions: number; deletions: number }>(`/api/tests/${testId}/versions/${v1}/diff/${v2}`)
}

// ── NFR Assessment ──────────────────────────────────────────────────────────

export async function fetchNFRAssessment(
  buildId?: string,
  project_id?: string,
  run_id?: string,
) {
  // R68.5 — run_id pins the assessment to a specific run; absent → backend
  // auto-selects the latest completed run (pre-R68.5 behaviour preserved).
  const params = new URLSearchParams()
  if (buildId) params.set('build_id', buildId)
  if (project_id) params.set('project_id', project_id)
  if (run_id) params.set('run_id', run_id)
  const qs = params.toString() ? `?${params}` : ''
  return request<any>(`/api/gates/nfr${qs}`)
}

// ── Quality Score ───────────────────────────────────────────────────────────

export async function fetchQualityScore(project_id?: string) {
  const qs = project_id ? `?project_id=${project_id}` : ''
  return request<any>(`/api/gates/latest${qs}`)
}

// R37.6 — SUT quality score (separate from gate-pass quality_score).
// Measures SUT health (real backend bug surface) over time, not test-
// suite pass rate. Surfaces sut_health_pct, MTTR, new/fixed counts +
// 30-day trend.
export interface SutQualityScore {
  project_id: string
  window_days: number
  sut_health_pct: number | null
  open_p0_count: number
  open_p1_count: number
  new_regressions_count: number
  fixed_count: number
  mttr_hours_p50: number | null
  mttr_hours_p95: number | null
  trend: { date: string; new_regressions: number }[]
}

export async function fetchSutQualityScore(
  project_id: string,
  days: number = 30,
): Promise<SutQualityScore> {
  const qs = `?project_id=${encodeURIComponent(project_id)}&days=${days}`
  return request<SutQualityScore>(`/api/sut/quality-score${qs}`)
}

// R74.1 — per-endpoint SUT health rollup. Reads R72.4's
// /api/sut/endpoint-health backend endpoint. Returns granular
// per-endpoint pass-rate + component rollup + worst-10 list so
// operators see "which SUT endpoints need fixing" rather than a
// flat defect list.
export interface EndpointHealthRow {
  endpoint_key: string
  pass_count: number
  fail_count: number
  blocked_count: number
  judge_total: number
  pass_pct: number
  coverage_test_count: number
  last_seen: string | null
}
export interface ComponentHealth {
  component: string
  pass_count: number
  fail_count: number
  endpoint_count: number
  pass_pct: number
}
export interface EndpointHealthPayload {
  project_id: string
  window_days: number
  endpoints: EndpointHealthRow[]
  worst_10: EndpointHealthRow[]
  components: ComponentHealth[]
  totals: {
    endpoints_seen: number
    endpoints_above_95: number
    endpoints_below_50: number
  }
}

export async function fetchEndpointHealth(
  project_id: string,
  days: number = 7,
  min_runs: number = 1,
): Promise<EndpointHealthPayload> {
  const qs = `?project_id=${encodeURIComponent(project_id)}&days=${days}&min_runs=${min_runs}`
  return request<EndpointHealthPayload>(`/api/sut/endpoint-health${qs}`)
}

// R74.2 — auth-staleness signal. Reads R72.5's
// /api/discovery/projects/{pid}/auth-staleness backend endpoint.
// Used by the global badge to surface stale_soon / expired cookies
// before they break the autonomous loop.
export type AuthStalenessState = 'fresh' | 'stale_soon' | 'expired' | 'unknown'
export interface AuthStaleness {
  project_id: string
  environment: string
  resolved_env_name: string
  state: AuthStalenessState
  // R306.C — auth-method awareness. `pipeline_blocking` is false for
  // bearer/api_key SUTs whose runtime mints its own token; the badge stays
  // silent for those. `cookie_state` preserves the raw cookie TTL state for a
  // future discovery-status panel when the banner is suppressed.
  auth_method?: string
  pipeline_blocking?: boolean
  cookie_state?: AuthStalenessState
  ttl_remaining_seconds: number | null
  ttl_remaining_hours: number | null
  ttl_pct_remaining: number | null
  expires_at_iso: string | null
  hint: string | null
}

export async function fetchAuthStaleness(
  project_id: string,
  environment: string = 'staging',
): Promise<AuthStaleness> {
  const qs = `?environment=${encodeURIComponent(environment)}`
  return request<AuthStaleness>(`/api/discovery/projects/${project_id}/auth-staleness${qs}`)
}

// R39.3 — discovery + auth-state diagnostic for the Run Pipeline modal.
// Single source-of-truth: cookie/storage-state presence + last harvest
// signal + unfilled-var count. Used by R39.2 to render the Paste Auth
// CTA when the platform can detect auth is missing.
export interface DiscoveryStatus {
  project_id: string
  environment: string
  auth_state_present: boolean
  cookie_status: 'set' | 'redacted_placeholder' | 'missing'
  bearer_status: 'set' | 'redacted_placeholder' | 'missing'
  storage_state_file_present: boolean
  storage_state_path: string
  storage_state_mtime: number | null
  last_har_mtime: number | null
  auth_failed_flag_present: boolean
  auth_failed_diagnosis: { reason?: string; sample?: string } | null
  unfilled_count: number
  unfilled_vars: string[]
  filled_count: number
}

export async function fetchDiscoveryStatus(
  project_id: string,
  environment: string = 'staging',
): Promise<DiscoveryStatus> {
  const qs = `?environment=${encodeURIComponent(environment)}`
  return request<DiscoveryStatus>(
    `/api/discovery/projects/${encodeURIComponent(project_id)}/discovery-status${qs}`,
  )
}

// ── Admin ───────────────────────────────────────────────────────────────────

export async function fetchAdminHealth() {
  // F3-5: /api/admin/health returns {services, degraded, checked_at}.
  // /api/health is a thin liveness probe and only returns {status: "ok"}.
  return request<any>('/api/admin/health')
}

// ── User Management (Admin) ─────────────────────────────────────────────────

export async function fetchUsers(): Promise<UserPublic[]> {
  return request<UserPublic[]>('/api/users')
}

/** Soft-delete a user (sets is_active=false). Admin only. Reversible via reactivateUser.
 *  Throws with .status=400 when the backend refuses (self / last-admin). */
export async function deactivateUser(userId: string): Promise<void> {
  await request<void>(`/api/users/${userId}`, { method: 'DELETE' })
}

/** Restore a soft-deleted user (sets is_active=true). Admin only. */
export async function reactivateUser(userId: string): Promise<{ id: string; is_active: boolean }> {
  return request(`/api/users/${userId}/reactivate`, { method: 'POST' })
}

export interface InviteResponse {
  invite_id: string
  user_id: string
  email: string
  expires_at: string
  invite_url: string
  email_sent: boolean
  email_subject: string
  email_body: string
}

export async function inviteUser(body: {
  email: string
  full_name: string
  is_admin?: boolean
  project_id?: string | null
  project_role?: string | null
}): Promise<InviteResponse> {
  return request<InviteResponse>('/api/users/invite', {
    method: 'POST',
    body: JSON.stringify({
      email: body.email,
      full_name: body.full_name,
      is_admin: body.is_admin ?? false,
      project_id: body.project_id || null,
      project_role: body.project_role || null,
    }),
  })
}

export async function acceptInvite(token: string, password: string): Promise<TokenResponse> {
  const res = await fetch(`${API_BASE}/api/auth/accept-invite`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, password }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Invite acceptance failed' }))
    throw new Error(err.detail || 'Invite acceptance failed')
  }
  const data: TokenResponse = await res.json()
  setToken(data.access_token)
  return data
}

// ── Test Generation ───────────────────────────────────────────────────────────

export async function generateTests(
  requirementId: string,
  tools?: string[],
  force?: boolean,
  acId?: string,
) {
  return request<{
    workflow_id: string
    requirement_id: string
    status: string
    gherkin_scenarios: string[]
    test_data_fixtures: any[]
    scripts: string[]
    test_count: number
    tests: any[]
  }>('/api/tests/generate', {
    method: 'POST',
    body: JSON.stringify({
      requirement_id: requirementId,
      tools: tools || ['playwright', 'newman'],
      force: !!force,
      ...(acId ? { ac_id: acId } : {}),
    }),
  })
}

export interface ToolDetail {
  status: 'generated' | 'failed' | 'skipped'
  test_count: number
  ac_covered?: string[]
  error?: string
}

export interface GenerateAllJob {
  job_id: string
  project_id: string
  status: 'running' | 'completed' | 'interrupted' | 'none' | 'failed' | 'aborted' | 'crashed'
  total_requirements: number
  completed: number
  failed: number
  // F11-4: distinct counter for "no-op skipped because hash matched"
  // so the UI can render "All up to date" instead of misreading 0 generated as N errors.
  skipped?: number
  current_requirement: string | null
  // F11-3: per-stage progress so the UI doesn't sit at the previous count
  // for the 70-115s an Ollama requirement takes.
  current_stage?: 'risk_scoring' | 'atdd' | 'automation' | 'writing' | null
  current_stage_started_at?: string | null
  // F11-6: rolling stats for ETA estimate.
  per_req_avg_seconds?: number | null
  eta_seconds?: number | null
  total_tests_generated: number
  started_at?: string
  completed_at?: string
  retry_of?: string
  rate_limit_reset?: number
  results: {
    requirement_id: string
    status: 'completed' | 'skipped'
    test_count: number
    tests_retained?: number
    tools: Record<string, ToolDetail>
  }[]
  errors: {
    requirement_id: string
    error: string
    is_rate_limited?: boolean
  }[]
}

export async function generateAllTests(
  projectId: string,
  force: boolean = false,
  requirementIds?: string[],
) {
  // R298 — optional SCOPED regen: pass a subset of requirement slugs (e.g.
  // regen. Backend matches case-insensitively against req_id/id; omit for a full run.
  let qs = `project_id=${projectId}${force ? '&force=true' : ''}`
  if (requirementIds && requirementIds.length > 0) {
    qs += `&requirement_ids=${encodeURIComponent(requirementIds.join(','))}`
  }
  return request<GenerateAllJob>(`/api/tests/generate-all?${qs}`, {
    method: 'POST',
  })
}

// F12-8: pollGenerateAllStatus removed — replaced by streamGenerateAllStatus
// (SSE-based) in F11. Polling endpoint on the backend can stay for ad-hoc
// curl/debugging but no frontend code path uses it anymore.

/**
 * F1: Stream generate-all job progress via SSE.
 *
 * Replaces the 3-second polling loop with a single persistent EventSource
 * connection. The backend sends a "status" event every 2s until the job
 * reaches a terminal state (completed/failed/aborted/crashed).
 *
 * Usage:
 *   const stop = streamGenerateAllStatus(jobId, (job) => setProgress(job),
 *                                               (err) => console.warn(err))
 *   // later: stop()  (component unmount)
 *
 * Returns a cleanup function that closes the EventSource.
 */
export function streamGenerateAllStatus(
  jobId: string,
  onStatus: (job: GenerateAllJob) => void,
  onError?: (msg: string) => void,
): () => void {
  let cancelled = false
  // F12-1: Browser EventSource cannot send X-API-Key or Authorization
  // headers, so the backend `require_api_key` dep also accepts the
  // credential as a query param. Prefer the env-baked API key (CI/admin)
  // and fall back to the logged-in JWT.
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY
  const jwt = getToken()
  const params = new URLSearchParams()
  if (apiKey) params.set('api_key', apiKey)
  else if (jwt) params.set('token', jwt)
  const qs = params.toString() ? `?${params.toString()}` : ''
  const url = `/api/tests/generate-all/stream/${encodeURIComponent(jobId)}${qs}`
  const es = new EventSource(url, { withCredentials: false })

  es.addEventListener('status', (e: MessageEvent) => {
    if (cancelled) return
    try {
      const job = JSON.parse(e.data) as GenerateAllJob
      onStatus(job)
      const terminal = new Set(['completed', 'failed', 'aborted', 'crashed'])
      if (terminal.has(job.status as string)) {
        es.close()
      }
    } catch (err) {
      onError?.(String(err))
    }
  })

  es.addEventListener('error', () => {
    if (cancelled) return
    // EventSource auto-reconnects by default; log once then let browser retry.
    onError?.('stream error; attempting reconnect')
  })

  return () => {
    cancelled = true
    try { es.close() } catch {}
  }
}

export async function getActiveGenerateJob(projectId: string) {
  return request<GenerateAllJob & { status: string }>(`/api/tests/generate-all/active?project_id=${projectId}`)
}

export async function getGenerationHistory(projectId: string) {
  return request<{ jobs: GenerateAllJob[] }>(`/api/tests/generate-all/history?project_id=${projectId}`)
}

export async function getLastCompletedJob(projectId: string) {
  return request<GenerateAllJob>(`/api/tests/generate-all/last?project_id=${projectId}`)
}

export async function retryFailedTests(jobId: string, fromRequirement?: string) {
  const params = new URLSearchParams({ job_id: jobId })
  if (fromRequirement) params.set('from_requirement', fromRequirement)
  return request<GenerateAllJob>(`/api/tests/generate-all/retry?${params}`, { method: 'POST' })
}

export async function regenerateTests(requirementId: string, force?: boolean, feedback?: string) {
  const params = new URLSearchParams({ requirement_id: requirementId })
  if (force) params.set('force', 'true')
  if (feedback) params.set('feedback', feedback)
  return request<any>(`/api/tests/regenerate?${params}`, { method: 'POST' })
}

// R54.2 — focused per-tool regen helper. Calls the R53 backend endpoint
// /api/tests/regenerate-by-tool which iterates a project's requirements
// (or just one) and regenerates only the specified automation tool's
// specs. Use cases: re-trigger k6 after R51 brace-balance autofix; regen
// pytest after R47.4a grounding retry-with-hint; etc.
export interface RegenerateByToolResult {
  status: 'completed' | 'no_work'
  job_id: string
  project_id: string
  tool: string
  force: boolean
  requirements_targeted: number
  succeeded: number
  failed: number
  results: Array<{ requirement_id: string; status: string; tests_generated: number }>
  errors: Array<{ requirement_id: string; error: string }>
}

export async function regenerateByTool(params: {
  projectId: string
  tool: string
  force?: boolean
  requirementId?: string
}): Promise<RegenerateByToolResult> {
  const qs = new URLSearchParams({
    project_id: params.projectId,
    tool: params.tool,
    force: String(params.force ?? true),
  })
  if (params.requirementId) qs.set('requirement_id', params.requirementId)
  return request<RegenerateByToolResult>(
    `/api/tests/regenerate-by-tool?${qs.toString()}`,
    { method: 'POST' },
  )
}

// WS4 — EXECUTE one tool (optionally one requirement), the run-side counterpart
// to regenerateByTool. Hits the thin /api/execution/execute-by-tool wrapper.
export async function executeByTool(params: {
  projectId: string
  tool: string
  environment?: string
  suiteType?: string
  requirementId?: string
}): Promise<{ run_id: string; status: string }> {
  const qs = new URLSearchParams({
    project_id: params.projectId,
    tool: params.tool,
    environment: params.environment ?? 'staging',
    suite_type: params.suiteType ?? 'full',
  })
  if (params.requirementId) qs.set('requirement_id', params.requirementId)
  return request<{ run_id: string; status: string }>(
    `/api/execution/execute-by-tool?${qs.toString()}`,
    { method: 'POST' },
  )
}

export async function getActiveRun(projectId?: string) {
  const qs = projectId ? `?project_id=${projectId}` : ''
  return request<any>(`/api/execution/runs/active${qs}`)
}

// R57.11 — tool-inventory diagnostic. ARTA expects 6 automation tools;
// when any has 0 generated specs the operator likely needs to trigger
// per-tool regen (R53 backend / R54 modal). Used by the test-explorer
// and architecture pages to render a proactive banner instead of letting
// the operator dispatch a 38min run that produces 0 results for the
// missing tool.
export const EXPECTED_TOOLS = ['playwright', 'newman', 'k6', 'zap', 'axe', 'pytest'] as const
export type ExpectedTool = typeof EXPECTED_TOOLS[number]

export interface ToolInventoryGap {
  tool: ExpectedTool
  count: number
}

/** R57.11 — compute per-tool inventory gaps (tools with 0 specs).
 *
 * Counts the `automation_tool` (or fallback `tool`) field of each
 * TestCase. Tools with count > 0 are excluded from the result; only
 * empty-inventory tools surface for operator action.
 *
 * Cheap O(N) over the test list — safe to call on every render via
 * `useMemo`. Returns [] when no gaps.
 */
export function computeToolInventoryGaps(tests: TestCase[]): ToolInventoryGap[] {
  const counts: Record<string, number> = {}
  for (const t of tests || []) {
    const tool = String(t.automation_tool || t.tool || '').toLowerCase()
    if (!tool) continue
    counts[tool] = (counts[tool] || 0) + 1
  }
  return EXPECTED_TOOLS
    .map(tool => ({ tool, count: counts[tool] || 0 }))
    .filter(g => g.count === 0)
}

// ── Requirement Discovery ─────────────────────────────────────────────────

export async function discoverRequirements(projectId: string, sources?: string[]) {
  return request<any>('/api/requirements/discover', {
    method: 'POST',
    body: JSON.stringify({
      project_id: projectId,
      sources: sources || ['github_readme', 'github_issues', 'github_code'],
    }),
  })
}

export async function testConnectivity(projectId: string, environment?: string) {
  return request<any>(`/api/projects/${projectId}/test-connectivity`, {
    method: 'POST',
    body: JSON.stringify({ environment: environment || 'local' }),
  })
}

// ── Phase H — Discovery / Chains / Steps ─────────────────────────────────────

export interface DiscoverySummary {
  project_id: string
  last_discovery_at: string | null
  envvars_harvested_count: number
  captured_endpoint_count: number
  captured_chain_count: number
  discovery_pending: boolean
  stage_2_5_enabled: boolean
  is_api_only?: boolean
}

export interface CapturedChain {
  chain_id: string
  semantic_hash: string
  project_id: string
  source_test_id: string | null
  captured_at: string
  occurrence_count: number
  source_har: string | null
  nodes: Array<{
    sequence_index: number
    method: string
    path_template: string
    concrete_url: string
    status: number
    started_at: string | null
    duration_ms: number
    request_body_shape: any
    response_body_shape: any
    provides: Record<string, string>
    consumes: Record<string, number>
  }>
}

export interface CapturedEndpoint {
  method: string
  path: string
  status: number
  content_type?: string
  request_body_shape?: any
  response_body_shape?: any
  evidence_count: number
  discovered_at?: string | null
  source_har?: string | null
}

export interface RunStep {
  test_id: string
  seq: number
  method: string
  path: string
  status: number
  duration_ms: number
  error: string | null
  cascade_skip: boolean
  cascade_reason: string | null
  provider_contract_violation: boolean
  ts: string
}

export async function fetchDiscoverySummary(projectId: string) {
  return request<DiscoverySummary>(`/api/discovery/projects/${projectId}/summary`)
}

// FE-sync P2/P4 — the Architecture-Discovery summary: protocol distribution +
// non-REST inventory + protocol_warnings (e.g. graphql_introspection_failed). Emitted
// by src/agents/architecture_discovery.py; served at /architecture. Had ZERO FE consumer.
export interface ProtocolWarning {
  protocol: string
  endpoint?: string
  signal: string
  detail: string
}
export interface ArchitectureSummary {
  project_id: string
  graphs?: Record<string, unknown>
  protocol_counts?: Record<string, number>
  coverage_gaps?: {
    non_rest_endpoints?: string[]
    [k: string]: unknown
  }
  protocol_warnings?: ProtocolWarning[]
  generated_at?: string
}
export async function fetchArchitectureSummary(projectId: string) {
  return request<ArchitectureSummary>(`/api/discovery/projects/${projectId}/architecture`)
}

// R330 (SUT-Understanding P1/P1d) — how well-grounded ARTA's SUT understanding is.
export interface GroundingCoverage {
  project_id: string
  total_endpoints: number
  by_provenance: Record<string, number>
  grounded_endpoints: number
  grounded_pct: number
  source_grounded_endpoints?: number
  source_grounded_pct?: number
  tests?: {
    test_count?: number
    grounded_by?: Record<string, number>
    potentially_incorrect_count?: number
    traceability_pct?: number | null
    // P1d — gen-time fail-loud statuses ("unavailable:no_github_token" → count)
    source_grounding?: Record<string, number>
    // P1d — distinct guess ∪ flagged (the old guess+flagged sum double-counted)
    needs_attention_count?: number
    // Code→API spine: tests whose endpoints resolve to a real SUT source file
    source_component_count?: number
    // API→Data spine: tests touching a named SUT domain entity + distinct entities
    data_object_count?: number
    entities_under_test?: string[]
  }
  source_grounding_available?: boolean
  // P1d — component flags behind source_grounding_available
  source_grounding?: { token_available?: boolean; repo_configured?: boolean }
  note?: string
  disabled?: boolean
}

export async function fetchGroundingCoverage(projectId: string) {
  return request<GroundingCoverage>(`/api/discovery/projects/${projectId}/grounding-coverage`)
}

// SUT-Understanding — derived AUTHORIZATION model summary. Mirrors the backend
// /api/discovery/projects/{id}/authz-model response (discovery.py). All fields
// optional so the panel renders defensively when a field is absent/degraded.
export interface AuthzModel {
  project_id: string
  built?: boolean
  disabled?: boolean
  operation_count?: number
  summary?: {
    by_scope?: Record<string, number>
    authz_gated?: number
    exempt_auth_only?: number
    domains?: string[]
  }
  role_count?: number
  principal_count?: number
  principal_by_type?: Record<string, number>
  mechanism?: string
}

export async function fetchAuthzModel(projectId: string) {
  return request<AuthzModel>(`/api/discovery/projects/${projectId}/authz-model`)
}

// R330 P4 — one Architecture Discovery graph artifact (reuses the pre-existing
// fetchArchitectureSummary/ArchitectureSummary above for the graph list).
export interface RawGraphNode {
  id: string
  kind?: string
  name?: string
  path?: string
  method?: string
  protocol?: string
  has_request_shape?: boolean
  has_response_shape?: boolean
  auth_required?: boolean
  source?: string
  [k: string]: unknown
}
export interface RawGraphEdge {
  from?: string
  to?: string
  kind?: string
  [k: string]: unknown
}
export interface RawArchitectureGraph {
  nodes?: RawGraphNode[]
  edges?: RawGraphEdge[]
  graph?: string
  protocol_counts?: Record<string, number>
  [k: string]: unknown
}

export async function fetchArchitectureGraph(projectId: string, graph: string) {
  return request<RawArchitectureGraph>(`/api/discovery/projects/${projectId}/architecture/${graph}`)
}

export async function fetchCapturedChains(projectId: string, limit = 50) {
  return request<{ project_id: string; count: number; chains: CapturedChain[] }>(
    `/api/discovery/projects/${projectId}/chains?limit=${limit}`,
  )
}

export async function fetchCapturedEndpoints(projectId: string, limit = 200) {
  return request<{ project_id: string; count: number; endpoints: CapturedEndpoint[] }>(
    `/api/discovery/projects/${projectId}/endpoints?limit=${limit}`,
  )
}

export async function fetchRunSteps(runId: string) {
  return request<{
    run_id: string
    count: number
    steps: RunStep[]
    per_endpoint_p95: Record<string, { count: number; p50: number; p95: number; p99: number; error_rate: number }>
  }>(`/api/discovery/runs/${runId}/steps`)
}

export async function refreshDiscovery(projectId: string) {
  return request<{ project_id: string; discovery_pending: boolean; note: string }>(
    `/api/discovery/refresh`,
    { method: 'POST', body: JSON.stringify({ project_id: projectId }) },
  )
}
