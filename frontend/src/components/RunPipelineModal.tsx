'use client'

import { useEffect, useState } from 'react'
import {
  triggerRun,
  fetchDiscoveryStatus,
  type DiscoveryStatus,
} from '@/lib/api-client'
import { fetchAuthState, type AuthState } from '@/lib/auth-state'
import { useToast } from '@/components/ui/Toast'
import { useProject } from '@/lib/project-context'
import { usePermissions } from '@/lib/permissions'
import { fetchEnvVariables } from '@/lib/env-variables'
import RefreshAuthModal from '@/components/RefreshAuthModal'

const SUITE_TYPES = [
  { id: 'smoke', label: 'Smoke', desc: 'Fast critical-path validation' },
  { id: 'regression', label: 'Regression', desc: 'Full regression suite' },
  { id: 'full', label: 'Full', desc: 'All tests + non-functional' },
]

const ENVIRONMENTS = ['staging', 'production']

interface RunPipelineModalProps {
  open: boolean
  onClose: () => void
}

export default function RunPipelineModal({ open, onClose }: RunPipelineModalProps) {
  const toast = useToast()
  const { currentProjectId } = useProject()
  // RBAC (UX mirror of the backend gate): viewers can't dispatch runs. The backend
  // still enforces this — disabling here just avoids a dead-end 403 click.
  const { canWrite } = usePermissions()
  const [suiteType, setSuiteType] = useState('smoke')
  const [environment, setEnvironment] = useState('staging')
  const [branch, setBranch] = useState('main')
  const [loading, setLoading] = useState(false)
  // R33.5 — pre-run unfilled-vars banner. When operator opens the
  // modal, fetch the project's env-var status; show a non-blocking
  // warning with link to the R29.4 editor when N variables still
  // hold placeholder values. Operator can dismiss + run anyway, but
  // surfaces the gap BEFORE the run consumes 18 minutes.
  const [unfilledCount, setUnfilledCount] = useState<number | null>(null)
  const [unfilledNames, setUnfilledNames] = useState<string[]>([])
  // R39.2 — discovery + auth-state diagnostic. When auth state is
  // missing the 22 unfilled vars are a CONSEQUENCE, not the actual
  // problem. Show a distinct red banner above the unfilled-vars
  // warning + a "Paste Auth →" CTA that opens RefreshAuthModal inline.
  const [discoveryStatus, setDiscoveryStatus] = useState<DiscoveryStatus | null>(null)
  const [authState, setAuthState] = useState<AuthState | null>(null)
  const [showRefreshAuth, setShowRefreshAuth] = useState(false)

  useEffect(() => {
    if (!open || !currentProjectId) {
      setUnfilledCount(null)
      setDiscoveryStatus(null)
      return
    }
    let cancelled = false
    // Env-var fetch (existing R33.5 path)
    fetchEnvVariables(currentProjectId, environment)
      .then(r => {
        if (cancelled) return
        setUnfilledCount(r.needs_attention?.length || 0)
        setUnfilledNames(r.needs_attention || [])
      })
      .catch(() => {
        if (!cancelled) {
          setUnfilledCount(null)
          setUnfilledNames([])
        }
      })
    // R39.3 — discovery-status (auth-state aggregator). Independent
    // network call so a slow env-var endpoint doesn't block the auth
    // banner from rendering.
    fetchDiscoveryStatus(currentProjectId, environment)
      .then(s => { if (!cancelled) setDiscoveryStatus(s) })
      .catch(() => { if (!cancelled) setDiscoveryStatus(null) })
    // Pre-load auth state so RefreshAuthModal can render correctly when
    // the operator clicks "Paste Auth →".
    fetchAuthState(currentProjectId, environment)
      .then(s => { if (!cancelled) setAuthState(s) })
      .catch(() => { if (!cancelled) setAuthState(null) })
    return () => { cancelled = true }
  }, [open, currentProjectId, environment])

  // F9-5: Escape closes the modal — keyboard parity with the backdrop click.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  async function handleRun(force: boolean = false) {
    setLoading(true)
    try {
      // R36.2 — pass `force` so the backend gate accepts a partial-
      // coverage run when the operator clicks "Run Anyway".
      const result = await triggerRun({
        suite_type: suiteType,
        environment,
        tools: ['playwright', 'newman', 'k6'],
        ...(force ? { force: true } : {}),
      } as any)
      toast.success(`Pipeline started (run_id: ${result.run_id})`)
      onClose()
    } catch (err: any) {
      // R36.2 — surface the 409 config_incomplete payload distinctly
      // so the operator sees "fill vars first" instead of generic err.
      const detail = (err as any).detail
      if (detail && detail.error === 'config_incomplete') {
        toast.error(
          `Refusing to run: ${detail.unfilled_count} env vars need values. ` +
          `Fill via Settings → Environments → Variables, or click 'Run Anyway'.`
        )
      } else if (detail && detail.error === 'auth_state_missing') {
        // R39.4 — server-side rejected force=true because no auth
        // state. Open the RefreshAuthModal inline instead of a toast
        // dead-end so the operator can fix the actual problem.
        toast.error(
          'Run Anyway disabled: paste a fresh cookie via Refresh Auth first.',
        )
        setShowRefreshAuth(true)
      } else {
        toast.error(`Failed to trigger pipeline: ${err.message}`)
      }
    } finally {
      setLoading(false)
    }
  }

  // R39.2 — when RefreshAuthModal completes (operator pasted a fresh
  // cookie), re-fetch discovery-status so the banner clears + the run
  // can dispatch normally. Also fire a discovery refresh so the next
  // harvest auto-fills the env vars.
  async function handleRefreshAuthSuccess() {
    setShowRefreshAuth(false)
    if (!currentProjectId) return
    try {
      const fresh = await fetchDiscoveryStatus(currentProjectId, environment)
      setDiscoveryStatus(fresh)
      const fresh2 = await fetchEnvVariables(currentProjectId, environment)
      setUnfilledCount(fresh2.needs_attention?.length || 0)
      setUnfilledNames(fresh2.needs_attention || [])
      toast.success('Auth refreshed. Re-run discovery to auto-fill vars.')
    } catch {
      toast.success('Auth refreshed.')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center"
         role="dialog" aria-modal="true" aria-labelledby="run-pipeline-modal-title">
      {/* F9-5: Backdrop is a button so it's in the tab order with a screen-reader
           label, and Escape (handled at window level above) provides parity. */}
      <button
        type="button"
        aria-label="Close dialog"
        onClick={onClose}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-default"
      />

      {/* Modal */}
      <div
        className="relative w-full max-w-lg mx-4 rounded-2xl p-6 shadow-2xl"
        style={{
          background: '#12121f',
          border: '1px solid #1e1e3a',
          backdropFilter: 'blur(20px)',
        }}
      >
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <h2 id="run-pipeline-modal-title" className="text-lg font-bold"
              style={{ color: '#e2e8f0', fontFamily: 'DM Sans, sans-serif' }}>
            Run Pipeline
          </h2>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-sm hover:opacity-80 transition-opacity"
            style={{ background: '#1e1e3a', color: '#94a3b8' }}
          >
            &#x2715;
          </button>
        </div>

        {/* R39.2 — auth-state banner. When discovery-status reports
            no usable auth credential, the unfilled vars below are the
            CONSEQUENCE — they would auto-fill if the probe could
            authenticate. Render this ABOVE the unfilled-vars warning
            so the operator's mental model is correct. */}
        {discoveryStatus && !discoveryStatus.auth_state_present && (
          <div className="mb-4 p-3 rounded-lg flex items-start gap-3"
               style={{ background: 'rgba(251,113,133,0.10)',
                        border: '1px solid rgba(251,113,133,0.4)' }}>
            <span style={{ color: '#fb7185', fontSize: 20 }}>⛔</span>
            <div className="flex-1">
              <div className="text-sm font-semibold" style={{ color: '#fb7185' }}>
                No fresh auth state: discovery harvested 0 vars
              </div>
              <div className="text-[11px] mt-1" style={{ color: '#94a3b8' }}>
                {discoveryStatus.unfilled_count > 0
                  ? `The ${discoveryStatus.unfilled_count} unfilled vars below are the consequence: they would auto-fill if the probe could reach authenticated endpoints. `
                  : 'Discovery cannot authenticate against the SUT. '}
                {discoveryStatus.cookie_status === 'redacted_placeholder'
                  ? 'Cookie in projects.json is a redacted placeholder; '
                  : discoveryStatus.cookie_status === 'missing'
                    ? 'No cookie configured; '
                    : ''}
                {discoveryStatus.storage_state_file_present
                  ? 'storage state file exists.'
                  : 'no storage state file at .arta/environments/' + environment + '-storage.json.'}
              </div>
              <button
                type="button"
                onClick={() => setShowRefreshAuth(true)}
                className="mt-2 px-3 py-1 rounded text-xs font-semibold text-white"
                style={{ background: 'linear-gradient(135deg,#f59e0b,#fb7185)' }}
              >
                Paste Auth →
              </button>
            </div>
          </div>
        )}

        {/* R33.5 — pre-run unfilled-vars banner. Operator-actionable
            warning displayed BEFORE the run consumes 18 minutes if env
            vars are still placeholders. Non-blocking — operator can
            dismiss and run anyway, but the gap surfaces clearly. */}
        {unfilledCount !== null && unfilledCount > 0 && (
          <div className="mb-4 p-3 rounded-lg flex items-start gap-3"
               style={{ background: 'rgba(251,146,60,0.10)',
                        border: '1px solid rgba(251,146,60,0.3)' }}>
            <span style={{ color: '#fb923c', fontSize: 20 }}>⚠</span>
            <div className="flex-1">
              <div className="text-sm font-semibold" style={{ color: '#fb923c' }}>
                {unfilledCount} env variable{unfilledCount === 1 ? '' : 's'} need a value
              </div>
              <div className="text-[11px] mt-1" style={{ color: '#94a3b8' }}>
                Tests using these will be marked <strong>BLOCKED</strong>.
                Fill them via{' '}
                <a href="/settings#environments"
                   target="_blank" rel="noopener noreferrer"
                   className="underline" style={{ color: '#06b6d4' }}>
                  Settings → Environments → Variables
                </a>
                {' '}for full coverage. Sample:{' '}
                <span className="font-mono">
                  {unfilledNames.slice(0, 3).join(', ')}
                  {unfilledCount > 3 ? ` + ${unfilledCount - 3} more` : ''}
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Suite type selector */}
        <label className="block text-xs font-semibold mb-2" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
          SUITE TYPE
        </label>
        <div className="grid grid-cols-3 gap-3 mb-5">
          {SUITE_TYPES.map(s => (
            <button
              key={s.id}
              onClick={() => setSuiteType(s.id)}
              className="rounded-xl p-3 text-left transition-all"
              style={{
                background: suiteType === s.id ? 'rgba(99,102,241,0.15)' : '#0a0a14',
                border: `1px solid ${suiteType === s.id ? '#6366f1' : '#1e1e3a'}`,
              }}
            >
              <p className="text-sm font-semibold" style={{ color: suiteType === s.id ? '#818cf8' : '#e2e8f0' }}>
                {s.label}
              </p>
              <p className="text-[10px] mt-1" style={{ color: '#64748b' }}>{s.desc}</p>
            </button>
          ))}
        </div>

        {/* Environment */}
        <label className="block text-xs font-semibold mb-2" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
          ENVIRONMENT
        </label>
        <select
          value={environment}
          onChange={e => setEnvironment(e.target.value)}
          className="w-full rounded-lg px-3 py-2 text-sm mb-5 outline-none"
          style={{
            background: '#0a0a14',
            border: '1px solid #1e1e3a',
            color: '#e2e8f0',
          }}
        >
          {ENVIRONMENTS.map(env => (
            <option key={env} value={env}>{env}</option>
          ))}
        </select>

        {/* Branch */}
        <label className="block text-xs font-semibold mb-2" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
          BRANCH
        </label>
        <input
          type="text"
          value={branch}
          onChange={e => setBranch(e.target.value)}
          placeholder="main"
          className="w-full rounded-lg px-3 py-2 text-sm mb-6 outline-none focus:ring-1"
          style={{
            background: '#0a0a14',
            border: '1px solid #1e1e3a',
            color: '#e2e8f0',
          }}
        />

        {/* Actions */}
        {/* R36.2 — when > 5 unfilled vars, replace primary "Run Now"
            with "Fill Variables First →" linking to the R29.4 editor.
            Operator can still use "Run Anyway" for explicit override
            (force=true on the API call). */}
        <div className="flex items-center justify-end gap-3">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-sm transition-opacity hover:opacity-80"
            style={{ color: '#94a3b8', border: '1px solid #1e1e3a' }}
          >
            Cancel
          </button>
          {(unfilledCount !== null && unfilledCount > 5) ? (
            <>
              <button
                onClick={() => { handleRun(true) }}
                disabled={loading || !canWrite}
                className="px-3 py-2 rounded-lg text-xs disabled:opacity-50 disabled:cursor-not-allowed"
                style={{ color: '#94a3b8', border: '1px solid #1e1e3a' }}
                title={!canWrite
                  ? 'You have read-only (viewer) access. Ask a project admin for tester access to run.'
                  : 'Run despite unfilled vars; most items will be BLOCKED'}
              >
                {loading ? 'Running…' : 'Run Anyway'}
              </button>
              <a
                href="/settings#environments"
                className="px-5 py-2 rounded-lg text-sm font-semibold text-white"
                style={{
                  background: 'linear-gradient(135deg, #f59e0b, #fb923c)',
                  textDecoration: 'none',
                }}
              >
                Fill Variables First →
              </a>
            </>
          ) : (
            <button
              onClick={() => { handleRun(false) }}
              disabled={loading || !canWrite}
              className="px-5 py-2 rounded-lg text-sm font-semibold text-white transition-all hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
              title={!canWrite
                ? 'You have read-only (viewer) access. Ask a project admin for tester access to run.'
                : undefined}
              style={{
                background: 'linear-gradient(135deg, #6366f1, #8b5cf6)',
              }}
            >
              {loading ? (
                <span className="flex items-center gap-2">
                  <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Running...
                </span>
              ) : (
                'Run Now'
              )}
            </button>
          )}
        </div>
      </div>

      {/* R39.2 — RefreshAuthModal mounted inline. Opens when the operator
          clicks "Paste Auth →" on the no-auth-state banner OR when the
          server-side R39.4 412 response is received. Re-fetches discovery
          status on success so the banner clears + the run can proceed. */}
      {showRefreshAuth && (
        <RefreshAuthModal
          open={showRefreshAuth}
          projectId={currentProjectId || null}
          environment={environment}
          initialState={authState}
          onClose={() => setShowRefreshAuth(false)}
          onSuccess={handleRefreshAuthSuccess}
        />
      )}
    </div>
  )
}
