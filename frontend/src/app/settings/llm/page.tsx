'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { authHeaders } from '../../../lib/auth-state'

// G2.2 (E4): Global LLM Provider Settings page.
// Backend endpoints already exist: GET/PUT /api/settings/llm, POST /api/settings/llm/test.
// Per-project overrides live in /settings (Integrations tab).

type ProviderKey = 'ollama' | 'anthropic' | 'claude_code'

interface SettingsResp {
  current: { provider: string; model: string; base_url?: string }
  available: {
    ollama: { reachable: boolean; base_url: string; models: string[] }
    anthropic: { reachable: boolean; models: string[] }
    claude_code: { reachable: boolean; cli_path: string; version: string | null }
  }
}

interface TestResp {
  status: 'ok' | 'error'
  provider: string
  latency_ms: number
  sample_response?: string
  error?: string
}

// Phase-1 A1 (FE↔BE sync) — use the canonical authHeaders() (Bearer JWT +
// X-API-Key from env) instead of the old X-API-Key-only 'arta-dev-key'. The
// prior code never sent the logged-in JWT, so LLM-settings 401'd in any env
// where ARTA_API_KEY != 'arta-dev-key'. authHeaders() already sets
// Content-Type: application/json (harmless on GET/POST).
async function apiGet<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: authHeaders() })
  if (!r.ok) throw new Error(`GET ${path}: ${r.status}`)
  return r.json()
}

async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'PUT',
    headers: authHeaders(),
    body: JSON.stringify(body),
  })
  if (!r.ok) {
    const errTxt = await r.text()
    throw new Error(`PUT ${path}: ${r.status} — ${errTxt}`)
  }
  return r.json()
}

async function apiPost<T>(path: string): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    headers: authHeaders(),
  })
  if (!r.ok) throw new Error(`POST ${path}: ${r.status}`)
  return r.json()
}

export default function LLMSettingsPage() {
  const [settings, setSettings] = useState<SettingsResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [provider, setProvider] = useState<ProviderKey>('ollama')
  const [model, setModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('http://host.docker.internal:11434')
  const [apiKey, setApiKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestResp | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 3500)
  }

  const refresh = async () => {
    setLoading(true)
    try {
      const data = await apiGet<SettingsResp>('/api/settings/llm')
      setSettings(data)
      setProvider((data.current.provider as ProviderKey) || 'ollama')
      setModel(data.current.model || '')
      if (data.current.base_url) setBaseUrl(data.current.base_url)
    } catch (e: any) {
      showToast(`Failed to load settings: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  const availableModels = (() => {
    if (!settings) return []
    if (provider === 'ollama') return settings.available.ollama.models
    if (provider === 'anthropic') return settings.available.anthropic.models
    return []
  })()

  const providerReachable = (() => {
    if (!settings) return false
    if (provider === 'ollama') return settings.available.ollama.reachable
    if (provider === 'anthropic') return settings.available.anthropic.reachable
    return settings.available.claude_code.reachable
  })()

  const onTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const body: any = { provider, model }
      if (provider === 'ollama') body.base_url = baseUrl
      if (provider === 'anthropic' && apiKey) body.api_key = apiKey
      // Apply first (test uses the active client) — then test
      await apiPut('/api/settings/llm', body)
      const r = await apiPost<TestResp>('/api/settings/llm/test')
      setTestResult(r)
    } catch (e: any) {
      setTestResult({ status: 'error', provider, latency_ms: 0, error: e.message })
    } finally {
      setTesting(false)
    }
  }

  const onSave = async () => {
    setSaving(true)
    try {
      const body: any = { provider, model }
      if (provider === 'ollama') body.base_url = baseUrl
      if (provider === 'anthropic' && apiKey) body.api_key = apiKey
      await apiPut('/api/settings/llm', body)
      showToast('Settings applied. New requests will use the updated provider.')
      setApiKey('') // clear after save
      await refresh()
    } catch (e: any) {
      showToast(`Save failed: ${e.message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="min-h-screen" style={{ background: '#05050a', color: '#e2e8f0' }}>
      {toast && (
        <div className="fixed top-4 right-4 z-50 px-4 py-3 rounded-xl text-sm font-medium shadow-lg"
             style={{ background: '#1e1e3a', border: '1px solid #6366f1', color: '#c7d2fe' }}>
          {toast}
        </div>
      )}

      <header style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}
              className="px-8 py-4 flex items-center gap-4">
        <Link href="/" className="flex items-center gap-3 hover:opacity-80 transition-opacity">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center text-lg"
               style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)' }}>A</div>
          <span className="font-bold text-lg">ARTA</span>
        </Link>
        <span style={{ color: '#1e1e3a' }}>›</span>
        <Link href="/settings" style={{ color: '#94a3b8' }} className="text-sm hover:text-white">Settings</Link>
        <span style={{ color: '#1e1e3a' }}>›</span>
        <span style={{ color: '#e2e8f0' }} className="text-sm">LLM Provider</span>
      </header>

      <main className="px-8 py-8 max-w-4xl mx-auto">
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-white mb-1">Global LLM Provider</h1>
          <p className="text-sm" style={{ color: '#64748b' }}>
            Controls which LLM ARTA uses for generation, evaluation, and chat. Individual projects
            can override this in their <Link href="/integrations" style={{ color: '#a5b4fc' }}>Integrations</Link> tab.
          </p>
        </div>

        {loading ? (
          <div className="py-16 text-center" style={{ color: '#94a3b8' }}>Loading current settings…</div>
        ) : !settings ? (
          <div className="py-16 text-center" style={{ color: '#f43f5e' }}>Failed to load settings.</div>
        ) : (
          <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            {/* Current state */}
            <div className="mb-6 p-3 rounded-lg" style={{ background: 'rgba(99,102,241,0.08)', border: '1px solid rgba(99,102,241,0.25)' }}>
              <div className="text-xs mb-1" style={{ color: '#94a3b8' }}>Current (live)</div>
              <div className="flex items-center gap-3" style={{ fontFamily: 'Space Mono, monospace' }}>
                <span className="text-sm" style={{ color: '#c7d2fe' }}>
                  {settings.current.provider} / {settings.current.model}
                </span>
                {settings.current.base_url && (
                  <span className="text-xs" style={{ color: '#64748b' }}>{settings.current.base_url}</span>
                )}
              </div>
            </div>

            {/* Provider selection */}
            <div className="mb-6">
              <label className="block text-sm font-semibold mb-3" style={{ color: '#e2e8f0' }}>Provider</label>
              <div className="space-y-2">
                {(['ollama', 'anthropic', 'claude_code'] as ProviderKey[]).map((p) => {
                  const reachable = p === 'ollama' ? settings.available.ollama.reachable
                                  : p === 'anthropic' ? settings.available.anthropic.reachable
                                  : settings.available.claude_code.reachable
                  const label = p === 'ollama' ? 'Ollama (local, no cost)'
                              : p === 'anthropic' ? 'Anthropic API (cloud, requires API key)'
                              : 'Claude Code CLI (uses host binary)'
                  return (
                    <label key={p}
                           className="flex items-center gap-3 px-3 py-2 rounded-lg cursor-pointer transition"
                           style={{
                             background: provider === p ? 'rgba(99,102,241,0.12)' : 'transparent',
                             border: provider === p ? '1px solid #6366f1' : '1px solid #1e1e3a',
                           }}>
                      <input type="radio" name="provider" value={p} checked={provider === p}
                             onChange={() => setProvider(p)}
                             className="accent-[#6366f1]" />
                      <span className="flex-1 text-sm">{label}</span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full"
                            style={{
                              background: reachable ? 'rgba(52,211,153,0.15)' : 'rgba(251,113,133,0.15)',
                              color: reachable ? '#34d399' : '#fb7185',
                            }}>
                        {reachable ? 'reachable' : 'unavailable'}
                      </span>
                    </label>
                  )
                })}
              </div>
            </div>

            {/* Model + extra fields — conditional by provider */}
            {(provider === 'ollama' || provider === 'anthropic') && (
              <div className="mb-4">
                <label className="block text-sm font-semibold mb-2" style={{ color: '#e2e8f0' }}>Model</label>
                <select value={model} onChange={(e) => setModel(e.target.value)}
                        className="w-full px-3 py-2 rounded-lg text-sm"
                        style={{ background: '#0d0d1a', color: '#e2e8f0', border: '1px solid #1e1e3a' }}>
                  <option value="">Select a model</option>
                  {availableModels.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
              </div>
            )}

            {provider === 'ollama' && (
              <div className="mb-4">
                <label className="block text-sm font-semibold mb-2" style={{ color: '#e2e8f0' }}>Base URL</label>
                <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
                       placeholder="http://host.docker.internal:11434"
                       className="w-full px-3 py-2 rounded-lg text-sm"
                       style={{ background: '#0d0d1a', color: '#e2e8f0', border: '1px solid #1e1e3a' }} />
              </div>
            )}

            {provider === 'anthropic' && (
              <div className="mb-4">
                <label className="block text-sm font-semibold mb-2" style={{ color: '#e2e8f0' }}>
                  API Key <span className="text-[10px]" style={{ color: '#64748b' }}>(only sent at save time; never displayed)</span>
                </label>
                <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)}
                       autoComplete="new-password"
                       placeholder="sk-ant-… (leave empty to keep existing)"
                       className="w-full px-3 py-2 rounded-lg text-sm"
                       style={{ background: '#0d0d1a', color: '#e2e8f0', border: '1px solid #1e1e3a' }} />
              </div>
            )}

            {provider === 'claude_code' && settings.available.claude_code && (
              <div className="mb-4 p-3 rounded-lg" style={{ background: '#0d0d1a', border: '1px solid #1e1e3a' }}>
                <div className="text-xs mb-1" style={{ color: '#94a3b8' }}>CLI Path</div>
                <div className="text-sm mb-2" style={{ fontFamily: 'Space Mono, monospace', color: '#c7d2fe' }}>
                  {settings.available.claude_code.cli_path}
                </div>
                <div className="text-xs" style={{ color: '#64748b' }}>
                  Status: {settings.available.claude_code.reachable
                    ? `✓ Available (${settings.available.claude_code.version})`
                    : '✗ CLI not found. Mount the host binary or pick a different provider'}
                </div>
              </div>
            )}

            {/* Test connection + Save */}
            <div className="flex items-center gap-3 mt-6">
              <button onClick={onTest} disabled={testing || !providerReachable}
                      className="px-4 py-2 rounded-lg text-sm font-medium transition disabled:opacity-50"
                      style={{ background: '#1e1e3a', color: '#c7d2fe', border: '1px solid #2d2d4a' }}>
                {testing ? 'Testing…' : 'Test Connection'}
              </button>
              <button onClick={onSave} disabled={saving || !providerReachable}
                      className="px-5 py-2 rounded-lg text-sm font-medium transition disabled:opacity-50"
                      style={{ background: '#6366f1', color: 'white' }}>
                {saving ? 'Saving…' : 'Save & Apply'}
              </button>
              <div className="flex-1" />
              {testResult && (
                <span className="text-xs" style={{
                  color: testResult.status === 'ok' ? '#34d399' : '#fb7185',
                  fontFamily: 'Space Mono, monospace',
                }}>
                  {testResult.status === 'ok'
                    ? `✓ ${testResult.latency_ms}ms`
                    : `✗ ${testResult.error?.slice(0, 100) || 'failed'}`}
                </span>
              )}
            </div>

            {testResult && testResult.status === 'ok' && testResult.sample_response && (
              <div className="mt-3 p-3 rounded-lg text-xs"
                   style={{ background: '#0d0d1a', color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
                Sample: {testResult.sample_response}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  )
}
