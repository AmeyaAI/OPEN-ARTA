'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useProject } from '@/lib/project-context'
import { useToast } from '@/components/ui/Toast'
import { fetchRuns, testIntegration, updateProject, type TestRun } from '@/lib/api-client'
import { useTasks } from '@/lib/task-context'

// ── Provider definitions ─────────────────────────────────────────────────────

const PROVIDERS = [
  { key: 'github_actions', icon: '\u25C9', name: 'GitHub Actions', category: 'CI/CD', desc: 'Automated CI/CD workflows triggered on push, PR, and release events' },
  { key: 'jenkins', icon: '\u25C6', name: 'Jenkins', category: 'CI/CD', desc: 'Enterprise CI/CD server with pipeline-as-code support' },
  { key: 'circleci', icon: '\u25CE', name: 'CircleCI', category: 'CI/CD', desc: 'Cloud-native CI/CD with parallel execution and caching' },
  { key: 'gitlab_ci', icon: '\u25B3', name: 'GitLab CI', category: 'CI/CD', desc: 'Integrated CI/CD pipelines within GitLab repositories' },
  { key: 'jira', icon: '\u25E7', name: 'Jira', category: 'Project Management', desc: 'Import stories, sync defects, and track sprint progress' },
  { key: 'slack', icon: '\u25C8', name: 'Slack', category: 'Communications', desc: 'Pipeline notifications, gate decisions, and alert channels' },
  { key: 'confluence', icon: '\u25A3', name: 'Confluence', category: 'Documentation', desc: 'Ingest PRDs, architecture docs, and test plans' },
]

const PROVIDER_FIELDS: Record<string, { key: string; label: string; type?: string; placeholder?: string }[]> = {
  github_actions: [
    { key: 'repository_url', label: 'Repository URL', placeholder: 'https://github.com/org/repo' },
    { key: 'personal_access_token', label: 'Personal Access Token', type: 'password', placeholder: 'ghp_...' },
    { key: 'workflow_file', label: 'Workflow File', placeholder: '.github/workflows/arta-quality.yml' },
  ],
  jenkins: [
    { key: 'server_url', label: 'Server URL', placeholder: 'https://jenkins.example.com' },
    { key: 'username', label: 'Username', placeholder: 'admin' },
    { key: 'api_token', label: 'API Token', type: 'password' },
    { key: 'job_name', label: 'Job Name', placeholder: 'arta-pipeline' },
  ],
  circleci: [
    { key: 'api_token', label: 'API Token', type: 'password', placeholder: 'CCIPAT_...' },
    { key: 'project_slug', label: 'Project Slug', placeholder: 'gh/org/repo' },
  ],
  gitlab_ci: [
    { key: 'server_url', label: 'GitLab URL', placeholder: 'https://gitlab.com' },
    { key: 'private_token', label: 'Private Token', type: 'password' },
    { key: 'project_id', label: 'Project ID', placeholder: '12345' },
  ],
  jira: [
    { key: 'server_url', label: 'Server URL', placeholder: 'https://org.atlassian.net' },
    { key: 'email', label: 'Email' },
    { key: 'api_token', label: 'API Token', type: 'password' },
    { key: 'project_key', label: 'Project Key', placeholder: 'ARTA' },
  ],
  slack: [
    { key: 'webhook_url', label: 'Webhook URL', placeholder: 'https://hooks.slack.com/services/...' },
    { key: 'channel', label: 'Channel', placeholder: '#arta-alerts' },
  ],
  confluence: [
    { key: 'server_url', label: 'Server URL', placeholder: 'https://org.atlassian.net/wiki' },
    { key: 'email', label: 'Email' },
    { key: 'api_token', label: 'API Token', type: 'password' },
    { key: 'space_key', label: 'Space Key', placeholder: 'ARTA' },
  ],
}

// ── Run status colors ────────────────────────────────────────────────────────

const RUN_STATUS_STYLES: Record<string, { bg: string; text: string }> = {
  PASS:    { bg: 'rgba(52,211,153,0.15)', text: '#34d399' },
  FAIL:    { bg: 'rgba(251,113,133,0.15)', text: '#fb7185' },
  RUNNING: { bg: 'rgba(99,102,241,0.15)', text: '#818cf8' },
}

// ── Helper ───────────────────────────────────────────────────────────────────

function formatDuration(ms?: number): string {
  if (!ms) return '--'
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
}

// ── Page Component ───────────────────────────────────────────────────────────

export default function IntegrationsPage() {
  const router = useRouter()
  const { currentProject, currentProjectId } = useProject()
  const toast = useToast()

  const [expandedProvider, setExpandedProvider] = useState<string | null>(null)
  const [formData, setFormData] = useState<Record<string, string>>({})
  const [testingProvider, setTestingProvider] = useState<string | null>(null)
  const [savingProvider, setSavingProvider] = useState<string | null>(null)

  const [runs, setRuns] = useState<TestRun[]>([])
  const [loadingRuns, setLoadingRuns] = useState(true)

  // Fetch recent runs — re-fetch when project changes
  useEffect(() => {
    setLoadingRuns(true)
    fetchRuns({ project_id: currentProjectId || undefined })
      .then(d => {
        const sorted = [...(d.runs || [])].sort((a, b) =>
          (b.started_at ?? '').localeCompare(a.started_at ?? ''))
        setRuns(sorted.slice(0, 10))
      })
      .catch(() => {})
      .finally(() => setLoadingRuns(false))
  }, [currentProjectId])

  const refreshRuns = () => {
    setLoadingRuns(true)
    fetchRuns({ project_id: currentProjectId || undefined })
      .then(d => {
        const sorted = [...(d.runs || [])].sort((a, b) =>
          (b.started_at ?? '').localeCompare(a.started_at ?? ''))
        setRuns(sorted.slice(0, 10))
        toast.success('Runs refreshed')
      })
      .catch(() => toast.error('Failed to refresh runs'))
      .finally(() => setLoadingRuns(false))
  }

  // Determine provider connection status from project integrations
  const getProviderStatus = (providerKey: string): boolean => {
    if (!currentProject?.integrations) return false
    const config = (currentProject.integrations as Record<string, any>)[providerKey]
    if (!config || typeof config !== 'object') return false
    return Object.values(config).some(v => v && String(v).length > 0)
  }

  const handleConfigure = (providerKey: string) => {
    if (expandedProvider === providerKey) {
      setExpandedProvider(null)
      setFormData({})
      return
    }
    setExpandedProvider(providerKey)
    // Pre-populate form from project integrations
    const existing = (currentProject?.integrations as Record<string, any>)?.[providerKey] || {}
    const fields = PROVIDER_FIELDS[providerKey] || []
    const data: Record<string, string> = {}
    fields.forEach(f => { data[f.key] = existing[f.key] || '' })
    setFormData(data)
  }

  const { addTask, completeTask, failTask } = useTasks()

  const handleTestConnection = async (providerKey: string) => {
    if (!currentProject) {
      toast.warning('No project selected')
      return
    }
    setTestingProvider(providerKey)
    const tid = `test-conn-${providerKey}-${Date.now()}`
    addTask({ id: tid, type: 'integration_test', label: `Testing ${providerKey} connection`, result_url: '/integrations' })
    try {
      const result = await testIntegration(currentProject.id, providerKey)
      if (result.status === 'connected') {
        completeTask(tid, { detail: `Connected (${result.latency_ms}ms)` })
        toast.success(`${providerKey} connected (${result.latency_ms}ms)`)
      } else {
        failTask(tid, result.message || 'Connection failed')
        toast.error(result.message || 'Connection failed')
      }
    } catch (e: any) {
      failTask(tid, e.message || 'Connection test failed')
      toast.error(e.message || 'Connection test failed')
    }
    setTestingProvider(null)
  }

  const handleSaveIntegration = async (providerKey: string) => {
    if (!currentProject) return
    setSavingProvider(providerKey)
    try {
      const existingIntegrations = (currentProject.integrations as Record<string, any>) || {}
      await updateProject(currentProject.id, {
        integrations: {
          ...existingIntegrations,
          [providerKey]: formData,
        },
      } as any)
      toast.success(`${providerKey} configuration saved`)
      setExpandedProvider(null)
      setFormData({})
    } catch {
      toast.error('Failed to save integration')
    }
    setSavingProvider(null)
  }

  return (
    <div className="p-8">
      {/* Header */}
      <div className="mb-8">
        <div className="flex items-center gap-2 text-xs mb-2" style={{ color: '#94a3b8' }}>
          <span>Dashboard</span>
          <span>/</span>
          <span style={{ color: '#94a3b8' }}>Integrations</span>
        </div>
        <h1 className="text-xl font-bold mb-1">Integrations</h1>
        <p className="text-sm" style={{ color: '#64748b' }}>
          Connect CI/CD providers, project management tools, and communication channels
        </p>
      </div>

      <div className="max-w-7xl mx-auto space-y-8">
        {/* Section 1 — Provider Cards */}
        <section>
          <h2 className="text-sm font-semibold mb-4" style={{ color: '#94a3b8' }}>CI/CD & Integration Providers</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
            {PROVIDERS.map(provider => {
              const isConnected = getProviderStatus(provider.key)
              const isExpanded = expandedProvider === provider.key
              const isTesting = testingProvider === provider.key
              const isSaving = savingProvider === provider.key
              const fields = PROVIDER_FIELDS[provider.key] || []

              return (
                <div key={provider.key} className="rounded-xl overflow-hidden"
                     style={{ background: '#12121f', border: `1px solid ${isExpanded ? '#6366f140' : '#1e1e3a'}` }}>
                  {/* Card header */}
                  <div className="p-4">
                    <div className="flex items-start gap-3 mb-3">
                      <div className="w-10 h-10 rounded-lg flex items-center justify-center text-lg flex-shrink-0"
                           style={{ background: isConnected ? 'rgba(16,185,129,0.1)' : '#0a0a14', border: `1px solid ${isConnected ? '#10b98130' : '#1e1e3a'}` }}>
                        {provider.icon}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium">{provider.name}</span>
                        </div>
                        <span className="text-[10px] px-1.5 py-0.5 rounded"
                              style={{ background: '#0a0a14', color: '#64748b', border: '1px solid #1e1e3a' }}>
                          {provider.category}
                        </span>
                      </div>
                    </div>

                    <p className="text-xs mb-3" style={{ color: '#64748b' }}>{provider.desc}</p>

                    {/* Status badge */}
                    <div className="flex items-center gap-2 mb-3">
                      <span className={`w-2 h-2 rounded-full flex-shrink-0 ${isConnected ? 'bg-emerald-400' : 'bg-slate-600'}`} />
                      <span className="text-[11px]" style={{ color: isConnected ? '#34d399' : '#64748b' }}>
                        {isConnected ? 'Connected' : 'Not Configured'}
                      </span>
                    </div>

                    {/* Action buttons */}
                    <div className="flex gap-2">
                      <button
                        onClick={() => handleConfigure(provider.key)}
                        className="flex-1 px-3 py-1.5 rounded-lg text-xs font-medium transition-all"
                        style={{
                          background: isExpanded ? 'rgba(99,102,241,0.15)' : '#0a0a14',
                          color: isExpanded ? '#c7d2fe' : '#94a3b8',
                          border: `1px solid ${isExpanded ? '#6366f140' : '#1e1e3a'}`,
                        }}>
                        {isExpanded ? 'Close' : 'Configure'}
                      </button>
                      <button
                        onClick={() => handleTestConnection(provider.key)}
                        disabled={isTesting}
                        className="px-3 py-1.5 rounded-lg text-xs font-medium transition-all"
                        style={{
                          background: 'transparent',
                          color: isTesting ? '#94a3b8' : '#06b6d4',
                          border: '1px solid #1e1e3a',
                        }}>
                        {isTesting ? 'Testing...' : 'Test'}
                      </button>
                    </div>
                  </div>

                  {/* Expanded config form */}
                  {isExpanded && (
                    <div className="px-4 pb-4 pt-2 space-y-3" style={{ borderTop: '1px solid #1e1e3a' }}>
                      {fields.map(f => (
                        <div key={f.key}>
                          <label className="text-[10px] mb-1 block" style={{ color: '#64748b' }}>{f.label}</label>
                          <input
                            type={f.type || 'text'}
                            value={formData[f.key] || ''}
                            onChange={e => setFormData(prev => ({ ...prev, [f.key]: e.target.value }))}
                            placeholder={f.placeholder || ''}
                            className="w-full px-3 py-1.5 rounded-lg text-xs outline-none"
                            style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                          />
                        </div>
                      ))}
                      <div className="flex gap-2 pt-1">
                        <button
                          onClick={() => handleSaveIntegration(provider.key)}
                          disabled={isSaving}
                          className="px-3 py-1.5 rounded-lg text-xs font-medium"
                          style={{ background: isSaving ? '#4338ca' : '#6366f1', color: '#fff' }}>
                          {isSaving ? 'Saving...' : 'Save'}
                        </button>
                        <button
                          onClick={() => { setExpandedProvider(null); setFormData({}) }}
                          className="px-3 py-1.5 rounded-lg text-xs font-medium"
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
        </section>

        {/* Section 2 — Recent CI/CD Runs */}
        <section>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold" style={{ color: '#94a3b8' }}>Recent CI/CD Runs</h2>
            <button onClick={refreshRuns}
                    className="px-3 py-1.5 rounded-lg text-xs font-medium transition-all"
                    style={{ background: '#0a0a14', color: '#06b6d4', border: '1px solid #1e1e3a' }}>
              {loadingRuns ? 'Loading...' : 'Refresh'}
            </button>
          </div>
          <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            {/* Table header */}
            <div className="grid grid-cols-6 gap-4 px-4 py-2 text-[10px] font-semibold"
                 style={{ background: '#0a0a14', color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
              <span>PIPELINE</span>
              <span>BRANCH</span>
              <span>PROVIDER</span>
              <span>STATUS</span>
              <span>DURATION</span>
              <span>TRIGGER</span>
            </div>
            {/* Table body */}
            {runs.length === 0 && !loadingRuns && (
              <div className="px-4 py-8 text-center text-xs" style={{ color: '#94a3b8' }}>
                No runs found. Trigger a pipeline to see results here.
              </div>
            )}
            {loadingRuns && runs.length === 0 && (
              <div className="px-4 py-8 text-center text-xs" style={{ color: '#94a3b8' }}>
                Loading runs...
              </div>
            )}
            {runs.map((run, _i) => {
              const statusKey = run.status?.toUpperCase() ?? 'RUNNING'
              const statusStyle = RUN_STATUS_STYLES[statusKey] || RUN_STATUS_STYLES.RUNNING
              const trigger = run.trigger || run.triggered_by || '\u2014'
              return (
                <div
                  key={run.run_id}
                  className="grid grid-cols-6 gap-4 px-4 py-3 text-xs cursor-pointer transition-all hover:opacity-80"
                  style={{ borderTop: '1px solid #1e1e3a' }}
                  onClick={() => router.push('/run-history')}
                >
                  <span className="font-mono truncate" style={{ color: '#818cf8' }}>
                    {run.build_id || run.run_id.slice(0, 8)}
                  </span>
                  <span style={{ color: '#94a3b8' }}>{run.branch || '\u2014'}</span>
                  <span className="px-1.5 py-0.5 rounded text-[10px] inline-block w-fit"
                        style={{ background: 'rgba(99,102,241,0.1)', color: '#818cf8', border: '1px solid #6366f130' }}>
                    {run.triggered_by || '\u2014'}
                  </span>
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-bold inline-block w-fit"
                        style={{ background: statusStyle.bg, color: statusStyle.text, fontFamily: 'Space Mono, monospace' }}>
                    {statusKey}
                  </span>
                  <span style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
                    {formatDuration(run.duration_ms)}
                  </span>
                  <span className="capitalize" style={{ color: '#64748b' }}>{trigger}</span>
                </div>
              )
            })}
          </div>
        </section>

        {/* Section 3 — Webhook Events Log */}
        <section>
          <div className="flex items-center gap-3 mb-4">
            <h2 className="text-sm font-semibold" style={{ color: '#94a3b8' }}>Webhook Events</h2>
            <span className="px-2 py-0.5 rounded-full text-[10px] font-bold"
                  style={{ background: 'rgba(99,102,241,0.15)', color: '#818cf8', fontFamily: 'Space Mono, monospace' }}>
              0
            </span>
          </div>
          <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="px-4 py-8 text-center">
              <p className="text-xs mb-1" style={{ color: '#94a3b8' }}>No webhook events received.</p>
              <p className="text-[10px]" style={{ color: '#334155' }}>
                Events from connected CI/CD providers will appear here once configured.
              </p>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
