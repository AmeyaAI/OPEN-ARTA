'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { fetchProjects, updateProject, createProject, testIntegration, testConnectivity, type Project } from '@/lib/api-client'
import EnvVariablesEditor from '@/components/EnvVariablesEditor'
import { useTasks } from '@/lib/task-context'

// ── Environment types ────────────────────────────────────────────────────────
type AuthMethod = 'cookie' | 'bearer' | 'basic' | 'oauth2' | 'none'

interface LocalStorageEntry { key: string; value: string }
interface TestRole { roleName: string; fieldName: string; roleValue: string }
interface EnvVariable { key: string; value: string }

interface EnvironmentConfig {
  base_url: string
  api_base_url: string
  auth_method: AuthMethod
  auth: {
    cookie_name?: string
    cookie_value?: string
    local_storage?: LocalStorageEntry[]
    bearer_token?: string
    basic_username?: string
    basic_password?: string
    oauth2_client_id?: string
    oauth2_client_secret?: string
    oauth2_token_url?: string
  }
  has_roles: boolean
  roles: TestRole[]
  variables: EnvVariable[]
}

const LLM_PROVIDERS = ['anthropic', 'claude_code', 'openai', 'gemini', 'ollama', 'azure'] as const

const PROVIDER_LABELS: Record<string, string> = {
  anthropic: 'Anthropic (Claude)',
  claude_code: 'Claude Code',
  openai: 'OpenAI',
  gemini: 'Google Gemini',
  ollama: 'Ollama (Local)',
  azure: 'Azure OpenAI',
}
const ENGAGEMENT_MODELS = ['tea_solo', 'tea_lite', 'integrated'] as const
const STACK_TYPES = ['frontend', 'backend', 'fullstack'] as const
const PROJECT_TYPES = [
  { id: 'web_app', label: 'Web Application', desc: 'Playwright + Newman + k6' },
  { id: 'mobile_app', label: 'Mobile Application', desc: 'Appium + device farms' },
  { id: 'api_microservice', label: 'API / Microservices', desc: 'Newman + k6 + contract tests' },
  { id: 'analytics', label: 'Analytics AI', desc: 'TEA-extended 8-principle methodology' },
] as const

const INTEGRATION_ITEMS = [
  { key: 'github', label: 'GitHub', desc: 'PR creation, status checks, self-healing commits' },
  { key: 'jira', label: 'Jira', desc: 'Import stories, auto-file defects' },
  { key: 'slack', label: 'Slack', desc: 'Pipeline notifications, gate decisions' },
  { key: 'confluence', label: 'Confluence', desc: 'Ingest PRDs and architecture docs' },
  { key: 'neo4j', label: 'Neo4j', desc: 'Traceability graph database' },
  { key: 'chromadb', label: 'ChromaDB', desc: 'RAG vector store for context retrieval' },
]

const INTEGRATION_FIELDS: Record<string, { key: string; label: string; type?: string }[]> = {
  github: [
    { key: 'repository_url', label: 'Repository URL' },
    { key: 'personal_access_token', label: 'Personal Access Token', type: 'password' },
    { key: 'default_branch', label: 'Default Branch' },
  ],
  jira: [
    { key: 'server_url', label: 'Server URL' },
    { key: 'email', label: 'Email' },
    { key: 'api_token', label: 'API Token', type: 'password' },
    { key: 'project_key', label: 'Project Key' },
  ],
  slack: [
    { key: 'webhook_url', label: 'Webhook URL' },
    { key: 'channel', label: 'Channel' },
  ],
  confluence: [
    { key: 'server_url', label: 'Server URL' },
    { key: 'email', label: 'Email' },
    { key: 'api_token', label: 'API Token', type: 'password' },
  ],
  neo4j: [
    { key: 'uri', label: 'URI' },
    { key: 'username', label: 'Username' },
    { key: 'password', label: 'Password', type: 'password' },
  ],
  chromadb: [
    { key: 'url', label: 'URL' },
  ],
}

const PROJECT_COLORS = [
  { name: 'indigo', value: '#6366f1' },
  { name: 'cyan', value: '#06b6d4' },
  { name: 'emerald', value: '#10b981' },
  { name: 'amber', value: '#f59e0b' },
  { name: 'rose', value: '#f43f5e' },
]

const NEW_PROJECT_LLM_PROVIDERS = ['Anthropic', 'Claude Code', 'Google Gemini', 'Ollama', 'OpenAI', 'Azure OpenAI'] as const

export default function SettingsPage() {
  const router = useRouter()
  const { addTask, completeTask, failTask } = useTasks()
  const [projects, setProjects] = useState<Project[]>([])
  const [activeProject, setActiveProject] = useState<string | null>(null)
  const [tab, setTab] = useState<'general' | 'llm' | 'integrations' | 'environments' | 'tea'>('general')
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')

  // General form
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [engagementModel, setEngagementModel] = useState('tea_solo')
  const [stackType, setStackType] = useState('fullstack')
  const [projectType, setProjectType] = useState('web_app')
  const [isGreenfield, setIsGreenfield] = useState(true)

  // LLM form
  const [provider, setProvider] = useState('anthropic')
  const [model, setModel] = useState('claude-sonnet-4-6')
  const [temperature, setTemperature] = useState('0.3')
  const [maxTokens, setMaxTokens] = useState('4096')
  // R127.A — per-tool overrides + R127.C escalation knobs.
  // Keys: playwright | newman | k6 | axe | zap | selenium | cypress | appium
  // plus `<tool>_escalation` for R127.C (e.g. `playwright_escalation`).
  type ToolOverride = { provider: string; model: string; api_key: string }
  const [toolOverrides, setToolOverrides] = useState<Record<string, ToolOverride>>({})
  const [showOverridesPanel, setShowOverridesPanel] = useState(false)
  const [escalationThreshold, setEscalationThreshold] = useState('0.5')
  const [escalationCap, setEscalationCap] = useState('10')
  // R127.E.3 — per-req cap (separate from per-batch ceiling above).
  // Fresh budget per req so a dense 16-sub req can't single-handedly
  // exhaust the batch budget. Default 10 covers most reqs; operator
  // bumps to 16+ when projects have ≥10-scenario Gherkins.
  const [escalationCapPerReq, setEscalationCapPerReq] = useState('10')
  // R128.B — LLM output cache (Redis-backed) controls.
  // Opt-in via cacheEnabled; respects temperature ceiling for variance protection.
  const [cacheEnabled, setCacheEnabled] = useState(false)
  const [cacheTtlSeconds, setCacheTtlSeconds] = useState('604800')  // 7d
  const [cacheTemperatureMax, setCacheTemperatureMax] = useState('0.3')

  // Integration test states
  const [testingIntegration, setTestingIntegration] = useState<string | null>(null)
  const [integrationResults, setIntegrationResults] = useState<Record<string, { status: string; latency_ms?: number; message?: string }>>({})

  // Integration config form states
  const [openIntegration, setOpenIntegration] = useState<string | null>(null)
  const [integrationFormData, setIntegrationFormData] = useState<Record<string, string>>({})
  const [savingIntegration, setSavingIntegration] = useState(false)

  // Multi-repo state for GitHub integration
  const [settingsRepos, setSettingsRepos] = useState<{ name: string; repo: string; branch: string; provider?: string }[]>([])
  const [newSettingsRepo, setNewSettingsRepo] = useState({ name: '', repo: '', branch: 'main' })

  // New project modal
  const [showNewProject, setShowNewProject] = useState(false)
  const [newProjectName, setNewProjectName] = useState('')
  const [newProjectDesc, setNewProjectDesc] = useState('')
  const [newProjectColor, setNewProjectColor] = useState('#6366f1')
  const [newProjectIcon, setNewProjectIcon] = useState('')
  const [newProjectLLM, setNewProjectLLM] = useState('Anthropic')
  const [creatingProject, setCreatingProject] = useState(false)

  useEffect(() => {
    // F9-10: ignore the fetchProjects result if the component unmounted
    // mid-flight — otherwise React logs "Can't perform a React state update
    // on an unmounted component" and we wedge state.
    let cancelled = false
    fetchProjects()
      .then(d => {
        if (cancelled) return
        const list = d.projects || d
        setProjects(list as Project[])
        if (list.length > 0) {
          setActiveProject(list[0].id)
          populateForm(list[0])
        }
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  const populateForm = (p: Project) => {
    setName(p.name || '')
    setDescription(p.description || '')
    setEngagementModel((p as any).engagement_model || 'tea_solo')
    setStackType((p as any).stack_type || 'fullstack')
    setProjectType((p as any).project_type || 'web_app')
    setIsGreenfield((p as any).is_greenfield ?? true)
    const llm = p.llm_config || {}
    setProvider((llm as any).provider || 'anthropic')
    setModel((llm as any).model || 'claude-sonnet-4-6')
    setTemperature(String((llm as any).temperature ?? '0.3'))
    setMaxTokens(String((llm as any).max_tokens ?? '4096'))
    // R127.A — load tool_overrides + escalation knobs (backward compat: missing → defaults)
    const rawOverrides = (llm as any).tool_overrides || {}
    const normalized: Record<string, ToolOverride> = {}
    for (const [k, v] of Object.entries(rawOverrides)) {
      const o = (v || {}) as any
      normalized[k] = {
        provider: o.provider || '',
        model:    o.model    || '',
        api_key:  o.api_key  || '',
      }
    }
    setToolOverrides(normalized)
    setEscalationThreshold(String((llm as any).escalation_threshold ?? '0.5'))
    setEscalationCap(String((llm as any).escalation_cap ?? '10'))
    // R127.E.3 — backward compat: missing field → default 10
    setEscalationCapPerReq(String((llm as any).escalation_cap_per_req ?? '10'))
    // R128.B — backward compat: missing fields → defaults preserve behavior
    setCacheEnabled(Boolean((llm as any).cache_enabled ?? false))
    setCacheTtlSeconds(String((llm as any).cache_ttl_seconds ?? '604800'))
    setCacheTemperatureMax(String((llm as any).cache_temperature_max ?? '0.3'))
  }

  useEffect(() => {
    const p = projects.find(p => p.id === activeProject)
    if (p) populateForm(p)
  }, [activeProject])

  const proj = projects.find(p => p.id === activeProject)

  const showSaved = (msg: string) => {
    setSaveMsg(msg)
    setTimeout(() => setSaveMsg(''), 3000)
  }

  const handleSaveGeneral = async () => {
    if (!activeProject) return
    setSaving(true)
    const tid = `save-general-${Date.now()}`
    addTask({ id: tid, type: 'config_save', label: 'Saving general settings', result_url: '/settings' })
    try {
      await updateProject(activeProject, {
        name, description,
        engagement_model: engagementModel,
        stack_type: stackType,
        project_type: projectType,
        is_greenfield: isGreenfield,
      } as any)
      completeTask(tid, { detail: 'General settings saved' })
      showSaved('General settings saved')
    } catch {
      failTask(tid, 'Failed to save')
      showSaved('Failed to save')
    }
    setSaving(false)
  }

  const handleSaveLLM = async () => {
    if (!activeProject) return
    setSaving(true)
    const tid = `save-llm-${Date.now()}`
    addTask({ id: tid, type: 'config_save', label: 'Saving LLM configuration', result_url: '/settings' })
    try {
      // R127.A — strip empty overrides before save so the backend sees only
      // tools the operator actually configured. Empty api_key fields are
      // preserved (UI mask handles inheritance via projects.py preservation).
      const cleanedOverrides: Record<string, ToolOverride> = {}
      for (const [k, v] of Object.entries(toolOverrides)) {
        if (v.provider || v.model || v.api_key) {
          cleanedOverrides[k] = { provider: v.provider, model: v.model, api_key: v.api_key }
        }
      }
      await updateProject(activeProject, {
        llm_config: {
          provider,
          model,
          temperature: parseFloat(temperature),
          max_tokens: parseInt(maxTokens),
          tool_overrides: cleanedOverrides,
          escalation_threshold: parseFloat(escalationThreshold),
          escalation_cap: parseInt(escalationCap),
          escalation_cap_per_req: parseInt(escalationCapPerReq),
          cache_enabled: cacheEnabled,
          cache_ttl_seconds: parseInt(cacheTtlSeconds),
          cache_temperature_max: parseFloat(cacheTemperatureMax),
        },
      } as any)
      completeTask(tid, { detail: 'LLM config saved' })
      showSaved('LLM config saved')
    } catch {
      failTask(tid, 'Failed to save')
      showSaved('Failed to save')
    }
    setSaving(false)
  }

  const handleTestConnection = async (intKey: string) => {
    if (!activeProject) return
    setTestingIntegration(intKey)
    try {
      const result = await testIntegration(activeProject, intKey)
      setIntegrationResults(prev => ({ ...prev, [intKey]: result }))
    } catch (e: any) {
      setIntegrationResults(prev => ({ ...prev, [intKey]: { status: 'error', message: e.message } }))
    }
    setTestingIntegration(null)
  }

  const handleConfigureToggle = (intKey: string) => {
    if (openIntegration === intKey) {
      setOpenIntegration(null)
      setIntegrationFormData({})
    } else {
      setOpenIntegration(intKey)
      // Pre-populate from project integrations if available
      const existing = (proj?.integrations as Record<string, any>)?.[intKey] || {}
      const fields = INTEGRATION_FIELDS[intKey] || []
      const formData: Record<string, string> = {}
      fields.forEach(f => {
        formData[f.key] = existing[f.key] || ''
      })
      setIntegrationFormData(formData)
      // Load repositories for GitHub integration
      if (intKey === 'github') {
        const integ = (proj?.integrations as Record<string, any>) || {}
        setSettingsRepos(Array.isArray(integ.repositories) ? integ.repositories : [])
      }
    }
  }

  const handleSaveIntegration = async (intKey: string) => {
    if (!activeProject) return
    setSavingIntegration(true)
    try {
      const existingIntegrations = (proj?.integrations as Record<string, any>) || {}
      if (intKey === 'github') {
        // Special case: save PAT + repositories alongside the nested github config
        await updateProject(activeProject, {
          integrations: {
            ...existingIntegrations,
            [intKey]: integrationFormData,
            github_token: integrationFormData.personal_access_token || existingIntegrations.github_token || '',
            github_repo: settingsRepos[0]?.repo || existingIntegrations.github_repo || '',
            repositories: settingsRepos,
          },
        } as any)
      } else {
        await updateProject(activeProject, {
          integrations: {
            ...existingIntegrations,
            [intKey]: integrationFormData,
          },
        } as any)
      }
      showSaved(`${intKey} configuration saved`)
      setOpenIntegration(null)
      setIntegrationFormData({})
    } catch {
      showSaved('Failed to save integration')
    }
    setSavingIntegration(false)
  }

  const handleCancelIntegration = () => {
    setOpenIntegration(null)
    setIntegrationFormData({})
  }

  const handleCreateProject = async () => {
    if (!newProjectName.trim()) return
    setCreatingProject(true)
    try {
      const providerMap: Record<string, string> = {
        'Anthropic': 'anthropic',
        'Google Gemini': 'gemini',
        'Ollama': 'ollama',
        'OpenAI': 'openai',
        'Azure OpenAI': 'azure',
      }
      const newProj = await createProject({
        name: newProjectName.trim(),
        description: newProjectDesc.trim(),
        color: newProjectColor,
        icon: newProjectIcon || undefined,
        llm_config: { provider: providerMap[newProjectLLM] || 'anthropic' },
      } as any)
      setProjects(prev => [...prev, newProj])
      setActiveProject(newProj.id)
      populateForm(newProj)
      setShowNewProject(false)
      setNewProjectName('')
      setNewProjectDesc('')
      setNewProjectColor('#6366f1')
      setNewProjectIcon('')
      setNewProjectLLM('Anthropic')
      showSaved('Project created')
    } catch {
      showSaved('Failed to create project')
    }
    setCreatingProject(false)
  }

  const TABS = [
    { id: 'general' as const, label: 'General' },
    { id: 'llm' as const, label: 'LLM Configuration' },
    { id: 'integrations' as const, label: 'Integrations' },
    { id: 'environments' as const, label: 'Environments' },
    { id: 'tea' as const, label: 'TEA Pipeline' },
  ]

  return (
    <div className="p-8">
      <div className="flex items-center gap-3 mb-6">
        <div>
          <h1 className="text-xl font-bold mb-1">Settings</h1>
          <p className="text-sm" style={{ color: '#64748b' }}>
            Project configuration, LLM providers, and integrations
          </p>
        </div>
        {saveMsg && (
          <span className="ml-auto px-3 py-1 rounded text-xs"
                style={{ background: saveMsg.includes('Failed') ? 'rgba(251,113,133,0.15)' : 'rgba(52,211,153,0.15)',
                         color: saveMsg.includes('Failed') ? '#fb7185' : '#34d399' }}>
            {saveMsg}
          </span>
        )}
      </div>

      <div className="flex gap-6">
        {/* Project selector sidebar */}
        <div className="w-48 shrink-0">
          <div className="rounded-xl p-3" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="text-xs font-semibold mb-2" style={{ color: '#64748b' }}>Projects</div>
            <div className="space-y-1">
              {projects.map(p => (
                <button key={p.id}
                        onClick={() => setActiveProject(p.id)}
                        className="w-full text-left px-3 py-2 rounded-lg text-xs transition-all"
                        style={{
                          background: activeProject === p.id ? 'rgba(99,102,241,0.15)' : 'transparent',
                          color: activeProject === p.id ? '#c7d2fe' : '#94a3b8',
                        }}>
                  {p.name}
                </button>
              ))}
              {projects.length === 0 && (
                <div className="text-xs px-3 py-2" style={{ color: '#94a3b8' }}>No projects</div>
              )}
            </div>
            <button
              onClick={() => router.push('/onboarding')}
              className="w-full mt-2 px-3 py-2 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5"
              style={{
                background: 'transparent',
                color: '#6366f1',
                border: '1px dashed #6366f1',
              }}
            >
              <span className="text-sm leading-none">+</span> New Project
            </button>
          </div>
        </div>

        {/* Settings panel */}
        <div className="flex-1">
          {/* Tabs */}
          <div className="flex gap-1 mb-6">
            {TABS.map(t => (
              <button key={t.id}
                      onClick={() => setTab(t.id)}
                      className="px-4 py-2 rounded-lg text-xs font-medium transition-all"
                      style={{
                        background: tab === t.id ? 'rgba(99,102,241,0.15)' : 'transparent',
                        color: tab === t.id ? '#c7d2fe' : '#64748b',
                        border: tab === t.id ? '1px solid rgba(99,102,241,0.3)' : '1px solid transparent',
                      }}>
                {t.label}
              </button>
            ))}
          </div>

          <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            {tab === 'general' && (
              <div className="space-y-5">
                <h2 className="font-semibold" style={{ color: '#94a3b8' }}>General Settings</h2>
                <Field label="Project Name" value={name} onChange={setName} />
                <Field label="Project ID" value={proj?.id || ''} disabled />
                <Field label="Description" value={description} onChange={setDescription} />

                {/* Engagement Model */}
                <div>
                  <label className="text-xs mb-1 block" style={{ color: '#64748b' }}>Engagement Model</label>
                  <div className="flex gap-2">
                    {ENGAGEMENT_MODELS.map(em => (
                      <button key={em}
                              onClick={() => setEngagementModel(em)}
                              className="px-3 py-1.5 rounded-lg text-xs capitalize"
                              style={{
                                background: engagementModel === em ? '#6366f1' : '#0a0a14',
                                color: engagementModel === em ? '#fff' : '#64748b',
                                border: `1px solid ${engagementModel === em ? '#6366f1' : '#1e1e3a'}`,
                              }}>
                        {em.replace('_', ' ')}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Stack Type */}
                <div>
                  <label className="text-xs mb-1 block" style={{ color: '#64748b' }}>Stack Type</label>
                  <div className="flex gap-2">
                    {STACK_TYPES.map(st => (
                      <button key={st}
                              onClick={() => setStackType(st)}
                              className="px-3 py-1.5 rounded-lg text-xs capitalize"
                              style={{
                                background: stackType === st ? '#6366f1' : '#0a0a14',
                                color: stackType === st ? '#fff' : '#64748b',
                                border: `1px solid ${stackType === st ? '#6366f1' : '#1e1e3a'}`,
                              }}>
                        {st}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Project Type */}
                <div>
                  <label className="text-xs mb-1 block" style={{ color: '#64748b' }}>Project Type</label>
                  <div className="grid grid-cols-2 gap-2">
                    {PROJECT_TYPES.map(pt => (
                      <button key={pt.id}
                              onClick={() => setProjectType(pt.id)}
                              className="px-3 py-2 rounded-lg text-left"
                              style={{
                                background: projectType === pt.id ? '#6366f115' : '#0a0a14',
                                color: projectType === pt.id ? '#e2e8f0' : '#64748b',
                                border: `1px solid ${projectType === pt.id ? '#6366f1' : '#1e1e3a'}`,
                              }}>
                        <span className="text-xs font-medium block">{pt.label}</span>
                        <span className="text-[10px] block mt-0.5" style={{ color: '#94a3b8' }}>{pt.desc}</span>
                      </button>
                    ))}
                  </div>
                </div>

                {/* Greenfield Toggle */}
                <div className="flex items-center gap-3">
                  <label className="text-xs" style={{ color: '#64748b' }}>Greenfield Project</label>
                  <div className="w-8 h-4 rounded-full relative cursor-pointer"
                       onClick={() => setIsGreenfield(!isGreenfield)}
                       style={{ background: isGreenfield ? '#6366f1' : '#1e1e3a' }}>
                    <div className="absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all"
                         style={{ left: isGreenfield ? '1rem' : '0.125rem' }} />
                  </div>
                </div>

                <div className="pt-3">
                  <button onClick={handleSaveGeneral} disabled={saving}
                          className="px-4 py-2 rounded-lg text-xs font-medium"
                          style={{ background: saving ? '#4338ca' : '#6366f1', color: '#fff' }}>
                    {saving ? 'Saving...' : 'Save Changes'}
                  </button>
                </div>
              </div>
            )}

            {tab === 'llm' && (
              <div className="space-y-5">
                <h2 className="font-semibold" style={{ color: '#94a3b8' }}>LLM Provider Configuration</h2>
                <div>
                  <label className="text-xs mb-1 block" style={{ color: '#64748b' }}>Provider</label>
                  <div className="flex gap-2">
                    {LLM_PROVIDERS.map(p => (
                      <button key={p}
                              onClick={() => setProvider(p)}
                              className="px-3 py-1.5 rounded-lg text-xs"
                              style={{
                                background: provider === p ? '#6366f1' : '#0a0a14',
                                color: provider === p ? '#fff' : '#64748b',
                                border: `1px solid ${provider === p ? '#6366f1' : '#1e1e3a'}`,
                              }}>
                        {PROVIDER_LABELS[p] || p}
                      </button>
                    ))}
                  </div>
                  {provider === 'claude_code' && (
                    <p className="text-[11px] mt-1" style={{ color: '#8b5cf6' }}>
                      Use your Claude Code API token for direct integration with Claude Code CLI.
                    </p>
                  )}
                </div>
                <Field label="Model" value={model} onChange={setModel} />
                <div className="grid grid-cols-2 gap-4">
                  <Field label="Temperature" value={temperature} onChange={setTemperature} />
                  <Field label="Max Tokens" value={maxTokens} onChange={setMaxTokens} />
                </div>
                <Field label="API Key" value="••••••••••••••••" type="password" />

                {/* R127.A — collapsible per-tool overrides panel */}
                <div className="pt-4 mt-4" style={{ borderTop: '1px solid #1e1e3a' }}>
                  <button
                    onClick={() => setShowOverridesPanel(!showOverridesPanel)}
                    className="text-xs font-medium hover:underline"
                    style={{ color: '#94a3b8' }}>
                    {showOverridesPanel ? '▼' : '▶'} Per-tool overrides + escalation (advanced)
                  </button>
                  {showOverridesPanel && (
                    <div className="mt-3 space-y-3 p-3 rounded-lg" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
                      <p className="text-[11px]" style={{ color: '#64748b' }}>
                        Override the LLM provider/model for individual tools. Empty = inherit base config above.
                        Use <code style={{ color: '#8b5cf6' }}>&lt;tool&gt;_escalation</code> rows (e.g. <code style={{ color: '#8b5cf6' }}>playwright_escalation</code>)
                        to configure R127.C quality-gated re-routing.
                      </p>
                      {['playwright', 'newman', 'k6', 'axe', 'zap', 'selenium', 'cypress', 'appium', 'playwright_escalation'].map(toolKey => {
                        const o = toolOverrides[toolKey] || { provider: '', model: '', api_key: '' }
                        const updateField = (field: 'provider' | 'model' | 'api_key', val: string) => {
                          setToolOverrides({
                            ...toolOverrides,
                            [toolKey]: { ...o, [field]: val },
                          })
                        }
                        const isEscalation = toolKey.endsWith('_escalation')
                        return (
                          <div key={toolKey} className="grid grid-cols-12 gap-2 items-center">
                            <div className="col-span-3 text-[11px]" style={{ color: isEscalation ? '#8b5cf6' : '#94a3b8' }}>
                              {toolKey}
                            </div>
                            <select
                              value={o.provider}
                              onChange={(e) => updateField('provider', e.target.value)}
                              className="col-span-3 px-2 py-1 rounded text-[11px]"
                              style={{ background: '#06060c', color: '#94a3b8', border: '1px solid #1e1e3a' }}>
                              <option value="">(inherit base)</option>
                              {LLM_PROVIDERS.map(p => (
                                <option key={p} value={p}>{PROVIDER_LABELS[p] || p}</option>
                              ))}
                            </select>
                            <input
                              value={o.model}
                              onChange={(e) => updateField('model', e.target.value)}
                              placeholder="(inherit)"
                              className="col-span-4 px-2 py-1 rounded text-[11px]"
                              style={{ background: '#06060c', color: '#94a3b8', border: '1px solid #1e1e3a' }} />
                            <input
                              value={o.api_key ? '••••••••' : ''}
                              onChange={(e) => updateField('api_key', e.target.value)}
                              placeholder="(inherit)"
                              type="password"
                              className="col-span-2 px-2 py-1 rounded text-[11px]"
                              style={{ background: '#06060c', color: '#94a3b8', border: '1px solid #1e1e3a' }} />
                          </div>
                        )
                      })}
                      <div className="grid grid-cols-3 gap-3 pt-2" style={{ borderTop: '1px dashed #1e1e3a' }}>
                        <div>
                          <label className="text-[11px]" style={{ color: '#64748b' }}>Escalation threshold (R126.Q aggregate)</label>
                          <input
                            value={escalationThreshold}
                            onChange={(e) => setEscalationThreshold(e.target.value)}
                            placeholder="0.5"
                            className="w-full px-2 py-1 rounded text-[11px]"
                            style={{ background: '#06060c', color: '#94a3b8', border: '1px solid #1e1e3a' }} />
                        </div>
                        <div>
                          <label className="text-[11px]" style={{ color: '#64748b' }}>Escalation cap (per gen batch)</label>
                          <input
                            value={escalationCap}
                            onChange={(e) => setEscalationCap(e.target.value)}
                            placeholder="10"
                            className="w-full px-2 py-1 rounded text-[11px]"
                            style={{ background: '#06060c', color: '#94a3b8', border: '1px solid #1e1e3a' }} />
                        </div>
                        <div>
                          <label className="text-[11px]" style={{ color: '#64748b' }}>Escalation cap (per requirement)</label>
                          <input
                            value={escalationCapPerReq}
                            onChange={(e) => setEscalationCapPerReq(e.target.value)}
                            placeholder="10"
                            className="w-full px-2 py-1 rounded text-[11px]"
                            style={{ background: '#06060c', color: '#94a3b8', border: '1px solid #1e1e3a' }} />
                        </div>
                      </div>
                      {/* R128.B — LLM output cache (Redis-backed) controls */}
                      <div className="pt-3 mt-3" style={{ borderTop: '1px dashed #1e1e3a' }}>
                        <div className="flex items-center gap-2 mb-2">
                          <label className="text-[11px] flex items-center gap-2" style={{ color: '#94a3b8' }}>
                            <input
                              type="checkbox"
                              checked={cacheEnabled}
                              onChange={(e) => setCacheEnabled(e.target.checked)}
                              className="w-3 h-3" />
                            <span>LLM output cache (Redis): replay identical regens at zero token cost</span>
                          </label>
                        </div>
                        {cacheEnabled && (
                          <div className="grid grid-cols-2 gap-3">
                            <div>
                              <label className="text-[11px]" style={{ color: '#64748b' }}>Cache TTL (seconds; default 604800 = 7 days)</label>
                              <input
                                value={cacheTtlSeconds}
                                onChange={(e) => setCacheTtlSeconds(e.target.value)}
                                placeholder="604800"
                                className="w-full px-2 py-1 rounded text-[11px]"
                                style={{ background: '#06060c', color: '#94a3b8', border: '1px solid #1e1e3a' }} />
                            </div>
                            <div>
                              <label className="text-[11px]" style={{ color: '#64748b' }}>Bypass cache when temperature exceeds (variance protection)</label>
                              <input
                                value={cacheTemperatureMax}
                                onChange={(e) => setCacheTemperatureMax(e.target.value)}
                                placeholder="0.3"
                                className="w-full px-2 py-1 rounded text-[11px]"
                                style={{ background: '#06060c', color: '#94a3b8', border: '1px solid #1e1e3a' }} />
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>

                <div className="pt-3 flex gap-3">
                  <button onClick={handleSaveLLM} disabled={saving}
                          className="px-4 py-2 rounded-lg text-xs font-medium"
                          style={{ background: saving ? '#4338ca' : '#6366f1', color: '#fff' }}>
                    {saving ? 'Saving...' : 'Save Configuration'}
                  </button>
                  <button className="px-4 py-2 rounded-lg text-xs font-medium"
                          style={{ background: '#0a0a14', color: '#64748b', border: '1px solid #1e1e3a' }}>
                    Test Connection
                  </button>
                </div>
              </div>
            )}

            {tab === 'integrations' && (
              <div className="space-y-4">
                <h2 className="font-semibold" style={{ color: '#94a3b8' }}>Integrations</h2>
                {INTEGRATION_ITEMS.map(item => {
                  const result = integrationResults[item.key]
                  const isTesting = testingIntegration === item.key
                  const isOpen = openIntegration === item.key
                  const fields = INTEGRATION_FIELDS[item.key] || []
                  return (
                    <div key={item.key}>
                      <div className="flex items-center gap-4 px-4 py-3 rounded-lg"
                           style={{
                             background: '#0a0a14',
                             borderBottomLeftRadius: isOpen ? 0 : undefined,
                             borderBottomRightRadius: isOpen ? 0 : undefined,
                           }}>
                        <div className="flex-1">
                          <div className="text-sm font-medium">{item.label}</div>
                          <div className="text-xs" style={{ color: '#64748b' }}>{item.desc}</div>
                          {result && (
                            <div className="text-[10px] mt-1" style={{
                              color: result.status === 'connected' ? '#34d399' : '#fb7185'
                            }}>
                              {result.status === 'connected'
                                ? `Connected (${result.latency_ms}ms)`
                                : result.message || 'Connection failed'}
                            </div>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-[10px] px-2 py-0.5 rounded-full"
                                style={{
                                  background: result?.status === 'connected'
                                    ? 'rgba(52,211,153,0.15)'
                                    : 'rgba(245,158,11,0.15)',
                                  color: result?.status === 'connected' ? '#34d399' : '#f59e0b',
                                }}>
                            {result?.status === 'connected' ? 'Connected' : 'Not connected'}
                          </span>
                          <button onClick={() => handleTestConnection(item.key)}
                                  disabled={isTesting}
                                  className="px-3 py-1 rounded text-xs"
                                  style={{
                                    border: '1px solid #1e1e3a',
                                    color: isTesting ? '#94a3b8' : '#06b6d4',
                                    background: isTesting ? '#0a0a14' : 'transparent',
                                  }}>
                            {isTesting ? 'Testing...' : 'Test Connection'}
                          </button>
                          <button
                            onClick={() => handleConfigureToggle(item.key)}
                            className="px-3 py-1 rounded text-xs"
                            style={{
                              border: `1px solid ${isOpen ? '#6366f1' : '#1e1e3a'}`,
                              color: isOpen ? '#c7d2fe' : '#94a3b8',
                              background: isOpen ? 'rgba(99,102,241,0.15)' : 'transparent',
                            }}>
                            {isOpen ? 'Close' : 'Configure'}
                          </button>
                        </div>
                      </div>
                      {isOpen && (
                        <div className="px-4 py-4 rounded-b-lg space-y-3"
                             style={{ background: '#0d0d1a', borderTop: '1px solid #1e1e3a' }}>
                          {fields.map(f => (
                            <Field
                              key={f.key}
                              label={f.label}
                              type={f.type}
                              value={integrationFormData[f.key] || ''}
                              onChange={v => setIntegrationFormData(prev => ({ ...prev, [f.key]: v }))}
                            />
                          ))}

                          {/* Multi-repo table for GitHub */}
                          {item.key === 'github' && (
                            <div className="rounded-lg overflow-hidden mt-3" style={{ border: '1px solid #1e1e3a' }}>
                              <div className="px-3 py-2 flex items-center justify-between" style={{ background: '#0a0a14', borderBottom: '1px solid #1e1e3a' }}>
                                <span className="text-[10px] font-semibold" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
                                  REPOSITORIES ({settingsRepos.length})
                                </span>
                              </div>
                              {settingsRepos.length > 0 && (
                                <div style={{ maxHeight: 240, overflowY: 'auto' }}>
                                  <table className="w-full text-xs" style={{ color: '#94a3b8' }}>
                                    <thead>
                                      <tr style={{ background: '#08080f', borderBottom: '1px solid #1e1e3a' }}>
                                        <th className="text-left px-3 py-1.5 font-medium" style={{ color: '#94a3b8' }}>Name</th>
                                        <th className="text-left px-3 py-1.5 font-medium" style={{ color: '#94a3b8' }}>Repository</th>
                                        <th className="text-left px-3 py-1.5 font-medium" style={{ color: '#94a3b8' }}>Branch</th>
                                        <th className="px-3 py-1.5 w-8" />
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {settingsRepos.map((r, idx) => (
                                        <tr key={idx} style={{ borderBottom: '1px solid #1e1e3a' }}>
                                          <td className="px-3 py-1.5" style={{ color: '#c7d2fe' }}>{r.name}</td>
                                          <td className="px-3 py-1.5" style={{ fontFamily: 'JetBrains Mono, monospace', color: '#818cf8' }}>{r.repo}</td>
                                          <td className="px-3 py-1.5">
                                            <span className="px-1.5 py-0.5 rounded text-[10px]" style={{ background: 'rgba(99,102,241,0.12)', color: '#a5b4fc' }}>{r.branch}</span>
                                          </td>
                                          <td className="px-3 py-1.5 text-center">
                                            <button
                                              onClick={() => setSettingsRepos(prev => prev.filter((_, i) => i !== idx))}
                                              className="text-xs transition-colors"
                                              style={{ color: '#94a3b8', background: 'none', border: 'none', cursor: 'pointer' }}
                                              onMouseEnter={e => (e.currentTarget.style.color = '#ef4444')}
                                              onMouseLeave={e => (e.currentTarget.style.color = '#94a3b8')}
                                            >{'\u2715'}</button>
                                          </td>
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                </div>
                              )}
                              <div className="px-3 py-2 flex gap-2 items-end" style={{ background: '#08080f', borderTop: settingsRepos.length > 0 ? '1px solid #1e1e3a' : 'none' }}>
                                <div className="flex-1">
                                  <input
                                    type="text"
                                    value={newSettingsRepo.name}
                                    onChange={e => setNewSettingsRepo(r => ({ ...r, name: e.target.value }))}
                                    placeholder="Name"
                                    className="w-full px-2 py-1 rounded text-xs outline-none"
                                    style={{ background: '#0a0a14', color: '#e2e8f0', border: '1px solid #1e1e3a' }}
                                  />
                                </div>
                                <div className="flex-1">
                                  <input
                                    type="text"
                                    value={newSettingsRepo.repo}
                                    onChange={e => setNewSettingsRepo(r => ({ ...r, repo: e.target.value }))}
                                    placeholder="owner/repo"
                                    className="w-full px-2 py-1 rounded text-xs outline-none"
                                    style={{ background: '#0a0a14', color: '#e2e8f0', border: '1px solid #1e1e3a' }}
                                    onKeyDown={e => {
                                      if (e.key === 'Enter' && newSettingsRepo.name.trim() && newSettingsRepo.repo.trim()) {
                                        setSettingsRepos(prev => [...prev, { ...newSettingsRepo }])
                                        setNewSettingsRepo({ name: '', repo: '', branch: 'main' })
                                      }
                                    }}
                                  />
                                </div>
                                <div style={{ width: 80 }}>
                                  <input
                                    type="text"
                                    value={newSettingsRepo.branch}
                                    onChange={e => setNewSettingsRepo(r => ({ ...r, branch: e.target.value }))}
                                    placeholder="main"
                                    className="w-full px-2 py-1 rounded text-xs outline-none"
                                    style={{ background: '#0a0a14', color: '#e2e8f0', border: '1px solid #1e1e3a' }}
                                  />
                                </div>
                                <button
                                  onClick={() => {
                                    if (newSettingsRepo.name.trim() && newSettingsRepo.repo.trim()) {
                                      setSettingsRepos(prev => [...prev, { ...newSettingsRepo }])
                                      setNewSettingsRepo({ name: '', repo: '', branch: 'main' })
                                    }
                                  }}
                                  className="px-2 py-1 rounded text-xs font-medium"
                                  style={{ background: '#6366f1', color: '#fff' }}
                                >+</button>
                              </div>
                            </div>
                          )}

                          <div className="flex gap-2 pt-2">
                            <button
                              onClick={() => handleSaveIntegration(item.key)}
                              disabled={savingIntegration}
                              className="px-4 py-2 rounded-lg text-xs font-medium"
                              style={{ background: savingIntegration ? '#4338ca' : '#6366f1', color: '#fff' }}>
                              {savingIntegration ? 'Saving...' : 'Save'}
                            </button>
                            <button
                              onClick={handleCancelIntegration}
                              className="px-4 py-2 rounded-lg text-xs font-medium"
                              style={{ background: '#0a0a14', color: '#64748b', border: '1px solid #1e1e3a' }}>
                              Cancel
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            {tab === 'environments' && (
              <EnvironmentsTab projectId={activeProject} project={proj || null} onSave={showSaved} />
            )}

            {tab === 'tea' && (
              <TeaPipelineTab projectId={activeProject} onSave={showSaved} />
            )}
          </div>
        </div>
      </div>

      {/* New Project Modal */}
      {showNewProject && (
        <div className="fixed inset-0 z-50 flex items-center justify-center"
             style={{ background: 'rgba(0,0,0,0.6)' }}>
          <div className="rounded-xl p-6 w-full max-w-md space-y-4"
               style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <h2 className="font-semibold text-base" style={{ color: '#e2e8f0' }}>Create New Project</h2>

            <Field label="Project Name *" value={newProjectName} onChange={setNewProjectName} />
            <Field label="Description" value={newProjectDesc} onChange={setNewProjectDesc} />

            <div>
              <label className="text-xs mb-2 block" style={{ color: '#64748b' }}>Color</label>
              <div className="flex gap-3">
                {PROJECT_COLORS.map(c => (
                  <button
                    key={c.name}
                    onClick={() => setNewProjectColor(c.value)}
                    className="w-7 h-7 rounded-full transition-all flex items-center justify-center"
                    style={{
                      background: c.value,
                      boxShadow: newProjectColor === c.value ? `0 0 0 2px #12121f, 0 0 0 4px ${c.value}` : 'none',
                    }}
                    title={c.name}
                  />
                ))}
              </div>
            </div>

            <Field label="Icon (emoji)" value={newProjectIcon} onChange={setNewProjectIcon} />

            <div>
              <label className="text-xs mb-1 block" style={{ color: '#64748b' }}>LLM Provider</label>
              <select
                value={newProjectLLM}
                onChange={e => setNewProjectLLM(e.target.value)}
                className="w-full px-3 py-2 rounded-lg text-sm outline-none appearance-none"
                style={{
                  background: '#0a0a14',
                  border: '1px solid #1e1e3a',
                  color: '#e2e8f0',
                }}>
                {NEW_PROJECT_LLM_PROVIDERS.map(p => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </div>

            <div className="flex gap-2 pt-2">
              <button
                onClick={handleCreateProject}
                disabled={creatingProject || !newProjectName.trim()}
                className="px-4 py-2 rounded-lg text-xs font-medium"
                style={{
                  background: creatingProject || !newProjectName.trim() ? '#4338ca' : '#6366f1',
                  color: '#fff',
                  opacity: !newProjectName.trim() ? 0.5 : 1,
                }}>
                {creatingProject ? 'Creating...' : 'Create Project'}
              </button>
              <button
                onClick={() => setShowNewProject(false)}
                className="px-4 py-2 rounded-lg text-xs font-medium"
                style={{ background: '#0a0a14', color: '#64748b', border: '1px solid #1e1e3a' }}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Helper: create blank environment config ─────────────────────────────────
function blankEnvConfig(): EnvironmentConfig {
  return {
    base_url: '',
    api_base_url: '',
    auth_method: 'cookie',
    auth: { local_storage: [] },
    has_roles: false,
    roles: [],
    variables: [],
  }
}

const DEFAULT_ENVS = ['local', 'staging', 'production']
const AUTH_METHODS: { id: AuthMethod; label: string }[] = [
  { id: 'cookie', label: 'Cookie' },
  { id: 'bearer', label: 'Bearer Token' },
  { id: 'basic', label: 'Basic Auth' },
  { id: 'oauth2', label: 'OAuth2' },
  { id: 'none', label: 'None' },
]

function EnvironmentsTab({ projectId, project, onSave }: { projectId: string | null; project: Project | null; onSave: (msg: string) => void }) {
  const [envNames, setEnvNames] = useState<string[]>(DEFAULT_ENVS)
  const [selectedEnv, setSelectedEnv] = useState('local')
  const [envConfigs, setEnvConfigs] = useState<Record<string, EnvironmentConfig>>({})
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ status: string; message?: string } | null>(null)
  const [showAddEnv, setShowAddEnv] = useState(false)
  const [newEnvName, setNewEnvName] = useState('')
  const [showVariables, setShowVariables] = useState(false)
  const [revealFields, setRevealFields] = useState<Record<string, boolean>>({})

  // Load from project when project changes
  useEffect(() => {
    const envs = (project as any)?.environments as Record<string, any> | undefined
    if (envs && typeof envs === 'object') {
      const names = Array.from(new Set([...DEFAULT_ENVS, ...Object.keys(envs)]))
      setEnvNames(names)
      const configs: Record<string, EnvironmentConfig> = {}
      names.forEach(n => {
        if (envs[n]) {
          const env = envs[n]
          const apiAuth = env.auth || {}
          const creds = apiAuth.credentials || {}
          const lsEntries = creds.localStorage || creds.local_storage || {}
          const lsArray = Array.isArray(lsEntries) ? lsEntries : Object.entries(lsEntries).map(([k, v]: any) => ({ key: k, value: String(v) }))
          const rawRoles = env.roles || []
          const mappedRoles = rawRoles.map((r: any) => ({
            roleName: r.roleName || r.name || '',
            fieldName: r.fieldName || r.role_field || 'role',
            roleValue: r.roleValue || r.role_value || '',
          }))
          const rawVars = env.variables || {}
          const varArray = Array.isArray(rawVars) ? rawVars : Object.entries(rawVars).map(([k, v]: any) => ({ key: k, value: String(v) }))
          configs[n] = {
            base_url: env.base_url || '',
            api_base_url: env.api_base_url || '',
            auth_method: (apiAuth.method || env.auth_method || 'none') as any,
            auth: {
              cookie_name: creds.cookie_name || env.auth?.cookie_name || '',
              cookie_value: creds.cookie_value || env.auth?.cookie_value || '',
              local_storage: lsArray,
              bearer_token: creds.bearer_token || env.auth?.bearer_token || '',
              basic_username: creds.basic_username || creds.username || '',
              basic_password: creds.basic_password || creds.password || '',
              oauth2_client_id: creds.oauth2_client_id || '',
              oauth2_client_secret: creds.oauth2_client_secret || '',
              oauth2_token_url: creds.oauth2_token_url || '',
            },
            has_roles: mappedRoles.length > 0,
            roles: mappedRoles,
            variables: varArray,
          }
        } else {
          configs[n] = blankEnvConfig()
        }
      })
      setEnvConfigs(configs)
    } else {
      const configs: Record<string, EnvironmentConfig> = {}
      DEFAULT_ENVS.forEach(n => { configs[n] = blankEnvConfig() })
      setEnvConfigs(configs)
      setEnvNames(DEFAULT_ENVS)
    }
    setSelectedEnv('local')
    setTestResult(null)
  }, [project?.id])

  const config = envConfigs[selectedEnv] || blankEnvConfig()

  const updateConfig = useCallback((patch: Partial<EnvironmentConfig>) => {
    setEnvConfigs(prev => ({
      ...prev,
      [selectedEnv]: { ...(prev[selectedEnv] || blankEnvConfig()), ...patch },
    }))
  }, [selectedEnv])

  const updateAuth = useCallback((patch: Partial<EnvironmentConfig['auth']>) => {
    setEnvConfigs(prev => {
      const cur = prev[selectedEnv] || blankEnvConfig()
      return { ...prev, [selectedEnv]: { ...cur, auth: { ...cur.auth, ...patch } } }
    })
  }, [selectedEnv])

  const toggleReveal = (field: string) => setRevealFields(prev => ({ ...prev, [field]: !prev[field] }))

  const handleAddEnv = () => {
    const n = newEnvName.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '')
    if (!n || envNames.includes(n)) return
    setEnvNames(prev => [...prev, n])
    setEnvConfigs(prev => ({ ...prev, [n]: blankEnvConfig() }))
    setSelectedEnv(n)
    setNewEnvName('')
    setShowAddEnv(false)
  }

  const handleTestConnection = async () => {
    if (!projectId) return
    setTesting(true)
    setTestResult(null)
    try {
      const res = await testConnectivity(projectId, selectedEnv)
      setTestResult({ status: res.status || 'ok', message: res.message || 'Connection successful' })
    } catch (e: any) {
      setTestResult({ status: 'error', message: e.message || 'Connection failed' })
    }
    setTesting(false)
  }

  const handleSave = async () => {
    if (!projectId) return
    setSaving(true)
    try {
      // M1 (frontend) — never echo a masked GET back on save: the API masks secrets
      // to '***', and re-sending that would overwrite the real value. Drop any value
      // still equal to the mask sentinel so it is simply omitted from the PUT.
      const MASK = '***'
      const unmask = (v?: string) => (v && v !== MASK ? v : undefined)
      // Transform flat form structure back to nested API structure for execution engine
      const lsObject: Record<string, string> = {}
      for (const entry of (config.auth.local_storage || [])) {
        if (entry.key && unmask(entry.value) !== undefined) lsObject[entry.key] = entry.value
      }
      const varsObject: Record<string, string> = {}
      for (const v of (config.variables || [])) {
        if (v.key && unmask(v.value) !== undefined) varsObject[v.key] = v.value
      }
      const envPayload = {
        base_url: config.base_url,
        api_base_url: config.api_base_url,
        auth: {
          method: config.auth_method,
          credentials: {
            cookie_name: config.auth.cookie_name || undefined,
            cookie_value: unmask(config.auth.cookie_value),
            localStorage: Object.keys(lsObject).length > 0 ? lsObject : undefined,
            bearer_token: unmask(config.auth.bearer_token),
            basic_username: config.auth.basic_username || undefined,
            basic_password: unmask(config.auth.basic_password),
            oauth2_client_id: config.auth.oauth2_client_id || undefined,
            oauth2_client_secret: unmask(config.auth.oauth2_client_secret),
            oauth2_token_url: config.auth.oauth2_token_url || undefined,
          },
        },
        roles: config.roles.filter(r => r.roleName).map(r => ({
          name: r.roleName,
          role_field: r.fieldName || 'role',
          role_value: r.roleValue,
        })),
        variables: Object.keys(varsObject).length > 0 ? varsObject : {},
      }
      // M1 (frontend) — send ONLY the edited env, never the whole (masked) environments
      // map. The backend merge-preserves omitted envs/keys, so sibling envs the operator
      // didn't touch can no longer be clobbered by re-sent '***' values.
      await updateProject(projectId, {
        environments: { [selectedEnv]: envPayload },
      } as any)
      onSave(`Environment "${selectedEnv}" saved`)
    } catch {
      onSave('Failed to save environment')
    }
    setSaving(false)
  }

  // Role helpers
  const addRole = () => {
    updateConfig({ roles: [...config.roles, { roleName: '', fieldName: 'role', roleValue: '' }] })
  }
  const removeRole = (idx: number) => {
    updateConfig({ roles: config.roles.filter((_, i) => i !== idx) })
  }
  const updateRole = (idx: number, patch: Partial<TestRole>) => {
    updateConfig({ roles: config.roles.map((r, i) => i === idx ? { ...r, ...patch } : r) })
  }

  // Variable helpers
  const addVariable = () => {
    updateConfig({ variables: [...config.variables, { key: '', value: '' }] })
  }
  const removeVariable = (idx: number) => {
    updateConfig({ variables: config.variables.filter((_, i) => i !== idx) })
  }
  const updateVariable = (idx: number, patch: Partial<EnvVariable>) => {
    updateConfig({ variables: config.variables.map((v, i) => i === idx ? { ...v, ...patch } : v) })
  }

  // LocalStorage helpers
  const lsEntries = config.auth.local_storage || []
  const addLSEntry = () => {
    updateAuth({ local_storage: [...lsEntries, { key: '', value: '' }] })
  }
  const removeLSEntry = (idx: number) => {
    updateAuth({ local_storage: lsEntries.filter((_, i) => i !== idx) })
  }
  const updateLSEntry = (idx: number, patch: Partial<LocalStorageEntry>) => {
    updateAuth({ local_storage: lsEntries.map((e, i) => i === idx ? { ...e, ...patch } : e) })
  }

  return (
    <div className="flex gap-4" style={{ minHeight: '500px' }}>
      {/* Environment list sidebar */}
      <div className="w-44 shrink-0">
        <div className="rounded-xl p-3" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
          <div className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: '#64748b' }}>Environments</div>
          <div className="space-y-1">
            {envNames.map(envName => (
              <button key={envName}
                      onClick={() => { setSelectedEnv(envName); setTestResult(null) }}
                      className="w-full text-left px-3 py-2 rounded-lg text-xs transition-all"
                      style={{
                        background: selectedEnv === envName ? 'rgba(99,102,241,0.15)' : 'transparent',
                        color: selectedEnv === envName ? '#c7d2fe' : '#94a3b8',
                        border: selectedEnv === envName ? '1px solid rgba(99,102,241,0.3)' : '1px solid transparent',
                      }}>
                {envName}
              </button>
            ))}
          </div>
          {showAddEnv ? (
            <div className="mt-2 space-y-1">
              <input
                autoFocus
                value={newEnvName}
                onChange={e => setNewEnvName(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleAddEnv()}
                placeholder="env-name"
                className="w-full px-2 py-1.5 rounded text-xs outline-none"
                style={{ background: '#12121f', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
              <div className="flex gap-1">
                <button onClick={handleAddEnv}
                        className="flex-1 px-2 py-1 rounded text-[10px] font-medium"
                        style={{ background: '#6366f1', color: '#fff' }}>Add</button>
                <button onClick={() => { setShowAddEnv(false); setNewEnvName('') }}
                        className="flex-1 px-2 py-1 rounded text-[10px]"
                        style={{ color: '#64748b', border: '1px solid #1e1e3a' }}>Cancel</button>
              </div>
            </div>
          ) : (
            <button
              onClick={() => setShowAddEnv(true)}
              className="w-full mt-2 px-3 py-2 rounded-lg text-xs font-medium transition-all flex items-center gap-1.5"
              style={{ background: 'transparent', color: '#6366f1', border: '1px dashed #6366f1' }}>
              <span className="text-sm leading-none">+</span> Add Environment
            </button>
          )}
        </div>
      </div>

      {/* Environment editor */}
      <div className="flex-1 space-y-6">
        {/* Section A: Target Application */}
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wider mb-3" style={{ color: '#94a3b8' }}>Target Application</div>
          <div className="space-y-3">
            <Field label="Base URL *" value={config.base_url} onChange={v => updateConfig({ base_url: v })}
                   placeholder="http://localhost:3005" />
            <Field label="API Base URL" value={config.api_base_url} onChange={v => updateConfig({ api_base_url: v })}
                   placeholder="http://localhost:3005/api" />
          </div>
        </div>

        {/* Section B: Authentication */}
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wider mb-3" style={{ color: '#94a3b8' }}>Authentication</div>
          {/* M4 — read-only "locked/solved auth profile" indicator, in sync with the
              backend: when a durable refresh is configured (reusable grant /
              operator-corrected refresh / explicit lock) the profile is authoritative
              — ARTA self-refreshes it and discovery won't re-derive it. Steer the
              operator to the Refresh-Auth paste modal instead of editing it away. */}
          {(() => {
            const rawEnv = (project as any)?.environments?.[selectedEnv] || {}
            const rawAuth = rawEnv.auth || {}
            const reusable = String(rawEnv.variables?.arta_refresh_reusable ?? '').toLowerCase()
            const locked = !!(rawAuth.locked || rawAuth.refresh?._source_corrected
              || reusable === '1' || reusable === 'true')
            return locked ? (
              <div className="mb-3 text-[11px] p-2 rounded" style={{
                background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.3)', color: '#a7f3d0' }}>
                🔒 This SUT&apos;s auth is <b>solved &amp; locked</b>: a durable refresh is configured, so ARTA keeps
                the token fresh automatically and discovery won&apos;t re-derive it. Editing here is usually unnecessary;
                use the <b>Refresh Auth</b> paste modal to update the live token.
              </div>
            ) : null
          })()}
          {/* Auth method pills */}
          <div className="flex gap-2 mb-4">
            {AUTH_METHODS.map(m => (
              <button key={m.id}
                      onClick={() => updateConfig({ auth_method: m.id })}
                      className="px-3 py-1.5 rounded-lg text-xs"
                      style={{
                        background: config.auth_method === m.id ? '#6366f1' : '#0a0a14',
                        color: config.auth_method === m.id ? '#fff' : '#64748b',
                        border: `1px solid ${config.auth_method === m.id ? '#6366f1' : '#1e1e3a'}`,
                      }}>
                {m.label}
              </button>
            ))}
          </div>

          {/* Method-specific fields */}
          {config.auth_method === 'cookie' && (
            <div className="space-y-3">
              <Field label="Cookie Name" value={config.auth.cookie_name || ''} onChange={v => updateAuth({ cookie_name: v })}
                     placeholder="session_id" />
              <PasswordField label="Cookie Value" value={config.auth.cookie_value || ''} onChange={v => updateAuth({ cookie_value: v })}
                             revealed={revealFields['cookie_value']} onToggle={() => toggleReveal('cookie_value')} />
              {/* LocalStorage entries */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-xs" style={{ color: '#64748b' }}>LocalStorage Entries</label>
                  <button onClick={addLSEntry} className="text-[10px] px-2 py-0.5 rounded"
                          style={{ color: '#6366f1', border: '1px solid rgba(99,102,241,0.3)' }}>+ Add</button>
                </div>
                {lsEntries.map((entry, idx) => (
                  <div key={idx} className="flex gap-2 mb-2">
                    <input value={entry.key} onChange={e => updateLSEntry(idx, { key: e.target.value })}
                           placeholder="key" className="flex-1 px-3 py-2 rounded-lg text-xs outline-none"
                           style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }} />
                    <input value={entry.value} onChange={e => updateLSEntry(idx, { value: e.target.value })}
                           placeholder="value" className="flex-1 px-3 py-2 rounded-lg text-xs outline-none"
                           style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }} />
                    <button onClick={() => removeLSEntry(idx)} className="px-2 text-xs" style={{ color: '#fb7185' }}>x</button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {config.auth_method === 'bearer' && (
            <PasswordField label="Bearer Token" value={config.auth.bearer_token || ''} onChange={v => updateAuth({ bearer_token: v })}
                           revealed={revealFields['bearer_token']} onToggle={() => toggleReveal('bearer_token')} />
          )}

          {config.auth_method === 'basic' && (
            <div className="space-y-3">
              <Field label="Username" value={config.auth.basic_username || ''} onChange={v => updateAuth({ basic_username: v })} />
              <PasswordField label="Password" value={config.auth.basic_password || ''} onChange={v => updateAuth({ basic_password: v })}
                             revealed={revealFields['basic_password']} onToggle={() => toggleReveal('basic_password')} />
            </div>
          )}

          {config.auth_method === 'oauth2' && (
            <div className="space-y-3">
              <Field label="Client ID" value={config.auth.oauth2_client_id || ''} onChange={v => updateAuth({ oauth2_client_id: v })} />
              <PasswordField label="Client Secret" value={config.auth.oauth2_client_secret || ''} onChange={v => updateAuth({ oauth2_client_secret: v })}
                             revealed={revealFields['oauth2_client_secret']} onToggle={() => toggleReveal('oauth2_client_secret')} />
              <Field label="Token URL" value={config.auth.oauth2_token_url || ''} onChange={v => updateAuth({ oauth2_token_url: v })}
                     placeholder="https://auth.example.com/oauth/token" />
            </div>
          )}

          {config.auth_method === 'none' && (
            <p className="text-xs" style={{ color: '#94a3b8' }}>No authentication configured for this environment.</p>
          )}
        </div>

        {/* Section C: Test User Roles */}
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wider mb-3" style={{ color: '#94a3b8' }}>Test User Roles</div>
          <div className="flex items-center gap-3 mb-3">
            <label className="text-xs" style={{ color: '#64748b' }}>This app has role-based access</label>
            <div className="w-8 h-4 rounded-full relative cursor-pointer"
                 onClick={() => updateConfig({ has_roles: !config.has_roles })}
                 style={{ background: config.has_roles ? '#6366f1' : '#1e1e3a' }}>
              <div className="absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all"
                   style={{ left: config.has_roles ? '1rem' : '0.125rem' }} />
            </div>
          </div>
          {config.has_roles && (
            <div className="space-y-2">
              {config.roles.map((role, idx) => (
                <div key={idx} className="flex gap-2 items-center">
                  <input value={role.roleName} onChange={e => updateRole(idx, { roleName: e.target.value })}
                         placeholder="Role name (e.g. admin)" className="flex-1 px-3 py-2 rounded-lg text-xs outline-none"
                         style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }} />
                  <input value={role.fieldName} onChange={e => updateRole(idx, { fieldName: e.target.value })}
                         placeholder="Field name" className="w-28 px-3 py-2 rounded-lg text-xs outline-none"
                         style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }} />
                  <input value={role.roleValue} onChange={e => updateRole(idx, { roleValue: e.target.value })}
                         placeholder="Value" className="w-28 px-3 py-2 rounded-lg text-xs outline-none"
                         style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }} />
                  <button onClick={() => removeRole(idx)} className="px-2 text-xs" style={{ color: '#fb7185' }}>x</button>
                </div>
              ))}
              <button onClick={addRole} className="text-[10px] px-2 py-1 rounded"
                      style={{ color: '#6366f1', border: '1px solid rgba(99,102,241,0.3)' }}>+ Add Role</button>
            </div>
          )}
        </div>

        {/* Section D: Variables (collapsible) */}
        <div>
          <button onClick={() => setShowVariables(!showVariables)}
                  className="flex items-center gap-2 text-xs font-medium"
                  style={{ color: '#818cf8' }}>
            <span style={{ transform: showVariables ? 'rotate(90deg)' : 'rotate(0deg)', display: 'inline-block', transition: 'transform 0.2s' }}>
              ▶
            </span>
            <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: '#94a3b8' }}>Custom Variables</span>
          </button>
          {showVariables && (
            <div className="mt-3 space-y-2">
              {config.variables.map((v, idx) => (
                <div key={idx} className="flex gap-2">
                  <input value={v.key} onChange={e => updateVariable(idx, { key: e.target.value })}
                         placeholder="KEY" className="flex-1 px-3 py-2 rounded-lg text-xs outline-none font-mono"
                         style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }} />
                  <input value={v.value} onChange={e => updateVariable(idx, { value: e.target.value })}
                         placeholder="value" className="flex-1 px-3 py-2 rounded-lg text-xs outline-none"
                         style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }} />
                  <button onClick={() => removeVariable(idx)} className="px-2 text-xs" style={{ color: '#fb7185' }}>x</button>
                </div>
              ))}
              <button onClick={addVariable} className="text-[10px] px-2 py-1 rounded"
                      style={{ color: '#6366f1', border: '1px solid rgba(99,102,241,0.3)' }}>+ Add Variable</button>
            </div>
          )}
        </div>

        {/* R29.4 — Section E: Operator-fillable env-var values.
            Surfaces the persisted env block's variables so operators
            can fill the 22+ unfilled slots that previously required
            hand-editing .arta/projects.json. Distinct from "Custom
            Variables" above (which manages the SET of names) — this
            editor focuses on filling VALUES with placeholder/sensitive
            UI affordances. */}
        {projectId && selectedEnv && (
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider mb-3" style={{ color: '#94a3b8' }}>
              Variable Values
              <span className="ml-2 text-[9px] font-normal normal-case" style={{ color: '#64748b' }}>
                Fill placeholders to unblock dispatched tests (R29.3 BLOCKED rows)
              </span>
            </div>
            <EnvVariablesEditor projectId={projectId} envName={selectedEnv} />
          </div>
        )}

        {/* Toast for test result */}
        {testResult && (
          <div className="px-3 py-2 rounded-lg text-xs"
               style={{
                 background: testResult.status === 'error' ? 'rgba(251,113,133,0.15)' : 'rgba(52,211,153,0.15)',
                 color: testResult.status === 'error' ? '#fb7185' : '#34d399',
                 border: `1px solid ${testResult.status === 'error' ? 'rgba(251,113,133,0.2)' : 'rgba(52,211,153,0.2)'}`,
               }}>
            {testResult.message}
          </div>
        )}

        {/* Actions */}
        <div className="pt-2 flex gap-3">
          <button onClick={handleSave} disabled={saving}
                  className="px-4 py-2 rounded-lg text-xs font-medium"
                  style={{ background: saving ? '#4338ca' : '#6366f1', color: '#fff' }}>
            {saving ? 'Saving...' : 'Save Environment'}
          </button>
          <button onClick={handleTestConnection} disabled={testing || !config.base_url}
                  className="px-4 py-2 rounded-lg text-xs font-medium"
                  style={{
                    background: '#0a0a14',
                    color: testing || !config.base_url ? '#94a3b8' : '#06b6d4',
                    border: '1px solid #1e1e3a',
                    opacity: !config.base_url ? 0.5 : 1,
                  }}>
            {testing ? 'Testing...' : 'Test Connection'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Password field with show/hide ────────────────────────────────────────────
function PasswordField({
  label, value, onChange, revealed, onToggle, placeholder,
}: {
  label: string; value: string; onChange: (v: string) => void; revealed: boolean; onToggle: () => void; placeholder?: string
}) {
  return (
    <div>
      <label className="text-xs mb-1 block" style={{ color: '#64748b' }}>{label}</label>
      <div className="flex gap-1">
        <input
          type={revealed ? 'text' : 'password'}
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={placeholder}
          className="flex-1 px-3 py-2 rounded-lg text-sm outline-none"
          style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
        />
        <button onClick={onToggle}
                className="px-3 py-2 rounded-lg text-[10px] font-medium shrink-0"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#64748b' }}>
          {revealed ? 'Hide' : 'Show'}
        </button>
      </div>
    </div>
  )
}

const DEFAULT_LAYERS = [
  { layer: 1, name: 'Requirement Intelligence', desc: 'Parse and analyze requirements from multiple sources' },
  { layer: 2, name: 'Risk-Based Strategy', desc: 'Score requirements by Impact × Probability' },
  { layer: 3, name: 'ATDD Design', desc: 'Generate Gherkin scenarios with 7 scenario types' },
  { layer: 4, name: 'Automation Engineering', desc: 'Generate Playwright/Newman/k6 test scripts' },
  { layer: 5, name: 'Test Execution', desc: 'Run tests with retry and self-healing' },
  { layer: 6, name: 'Evidence Collection', desc: 'Capture screenshots, logs, and artifacts' },
  { layer: 7, name: 'Traceability & Coverage', desc: 'Update Neo4j requirement-to-test graph' },
  { layer: 8, name: 'Non-Functional Validation', desc: 'Performance, security, accessibility checks' },
  { layer: 9, name: 'Quality Gate', desc: 'PASS/CONCERNS/FAIL/WAIVED release decision' },
]

const THRESHOLD_PRESETS: Record<string, Record<string, number>> = {
  strict:   { p0_coverage: 100, p1_coverage: 95, p2_coverage: 80, overall_coverage: 85, p0_pass_rate: 100, overall_pass_rate: 95, max_p0_defects: 0, max_p1_defects: 1 },
  standard: { p0_coverage: 100, p1_coverage: 90, p2_coverage: 75, overall_coverage: 80, p0_pass_rate: 100, overall_pass_rate: 90, max_p0_defects: 0, max_p1_defects: 3 },
  lenient:  { p0_coverage: 95,  p1_coverage: 80, p2_coverage: 60, overall_coverage: 70, p0_pass_rate: 95,  overall_pass_rate: 85, max_p0_defects: 0, max_p1_defects: 5 },
}

function TeaPipelineTab({ projectId, onSave }: { projectId: string | null; onSave: (msg: string) => void }) {
  const [layers, setLayers] = useState(() => DEFAULT_LAYERS.map(l => ({ ...l, enabled: l.layer !== 8 })))
  const [thresholdPreset, setThresholdPreset] = useState('standard')
  const [thresholds, setThresholds] = useState(THRESHOLD_PRESETS.standard)
  const [showThresholds, setShowThresholds] = useState(false)
  const [saving, setSaving] = useState(false)

  const toggleLayer = (layer: number) => {
    setLayers(prev => prev.map(l => l.layer === layer ? { ...l, enabled: !l.enabled } : l))
  }

  const selectPreset = (preset: string) => {
    setThresholdPreset(preset)
    setThresholds(THRESHOLD_PRESETS[preset])
  }

  const handleSave = async () => {
    if (!projectId) return
    setSaving(true)
    try {
      await updateProject(projectId, {
        tea_pipeline: {
          layers: layers.reduce((acc, l) => ({ ...acc, [`layer_${l.layer}`]: l.enabled }), {}),
          threshold_preset: thresholdPreset,
          thresholds,
        },
      } as any)
      onSave('Pipeline configuration saved')
    } catch {
      onSave('Failed to save pipeline config')
    }
    setSaving(false)
  }

  return (
    <div className="space-y-5">
      <h2 className="font-semibold" style={{ color: '#94a3b8' }}>TEA Pipeline Configuration</h2>
      <p className="text-xs" style={{ color: '#94a3b8' }}>
        Enable or disable pipeline layers. Disabled layers will be skipped during execution.
      </p>
      <div className="space-y-2">
        {layers.map(l => (
          <div key={l.layer}
               className="flex items-center gap-4 px-4 py-3 rounded-lg transition-all"
               style={{ background: '#0a0a14', opacity: l.enabled ? 1 : 0.5 }}>
            <span className="text-xs font-mono w-6 text-center font-bold"
                  style={{ color: l.enabled ? '#6366f1' : '#94a3b8' }}>
              {l.layer}
            </span>
            <div className="flex-1">
              <span className="text-sm">{l.name}</span>
              <p className="text-[10px] mt-0.5" style={{ color: '#94a3b8' }}>{l.desc}</p>
            </div>
            <div className="w-9 h-5 rounded-full relative cursor-pointer transition-colors"
                 onClick={() => toggleLayer(l.layer)}
                 style={{ background: l.enabled ? '#6366f1' : '#1e1e3a' }}>
              <div className="absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all"
                   style={{ left: l.enabled ? '1.1rem' : '0.125rem' }} />
            </div>
          </div>
        ))}
      </div>

      {/* Quality Thresholds */}
      <div className="pt-4">
        <button onClick={() => setShowThresholds(!showThresholds)}
                className="flex items-center gap-2 text-sm font-medium"
                style={{ color: '#818cf8' }}>
          <span style={{ transform: showThresholds ? 'rotate(90deg)' : 'rotate(0deg)', display: 'inline-block', transition: 'transform 0.2s' }}>
            ▶
          </span>
          Quality Gate Thresholds
        </button>
        {showThresholds && (
          <div className="mt-3 space-y-4">
            <div className="flex gap-2">
              {(['strict', 'standard', 'lenient'] as const).map(p => (
                <button key={p} onClick={() => selectPreset(p)}
                        className="px-4 py-2 rounded-lg text-xs font-medium capitalize transition-all"
                        style={{
                          background: thresholdPreset === p ? '#6366f1' : '#0a0a14',
                          color: thresholdPreset === p ? '#fff' : '#64748b',
                          border: `1px solid ${thresholdPreset === p ? '#6366f1' : '#1e1e3a'}`,
                        }}>
                  {p}
                </button>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-3">
              {Object.entries(thresholds).map(([key, val]) => (
                <div key={key} className="flex items-center gap-3 px-3 py-2 rounded-lg" style={{ background: '#0a0a14' }}>
                  <span className="text-xs flex-1" style={{ color: '#94a3b8' }}>
                    {key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
                  </span>
                  <input
                    type="number"
                    value={val}
                    onChange={e => setThresholds(prev => ({ ...prev, [key]: Number(e.target.value) }))}
                    className="w-16 px-2 py-1 rounded text-xs text-right outline-none"
                    style={{ background: '#12121f', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                  />
                  <span className="text-[10px]" style={{ color: '#94a3b8' }}>
                    {key.includes('defect') ? 'max' : '%'}
                  </span>
                </div>
              ))}
            </div>
            <button onClick={() => selectPreset('standard')}
                    className="text-xs" style={{ color: '#6366f1' }}>
              Reset to Standard Defaults
            </button>
          </div>
        )}
      </div>

      <div className="pt-3 flex gap-3">
        <button onClick={handleSave} disabled={saving}
                className="px-4 py-2 rounded-lg text-xs font-medium"
                style={{ background: saving ? '#4338ca' : '#6366f1', color: '#fff' }}>
          {saving ? 'Saving...' : 'Save Pipeline Config'}
        </button>
      </div>
    </div>
  )
}

function Field({
  label, value, onChange, disabled, type = 'text', placeholder,
}: {
  label: string; value: string; onChange?: (v: string) => void; disabled?: boolean; type?: string; placeholder?: string
}) {
  return (
    <div>
      <label className="text-xs mb-1 block" style={{ color: '#64748b' }}>{label}</label>
      <input
        type={type}
        value={value}
        onChange={onChange ? e => onChange(e.target.value) : undefined}
        disabled={disabled}
        readOnly={!onChange}
        placeholder={placeholder}
        className="w-full px-3 py-2 rounded-lg text-sm outline-none"
        style={{
          background: '#0a0a14',
          border: '1px solid #1e1e3a',
          color: disabled ? '#94a3b8' : '#e2e8f0',
        }}
      />
    </div>
  )
}
