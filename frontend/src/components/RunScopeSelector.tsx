'use client'

// R68.1 — Shared run-scope selector. Pre-R68 the Defects page (R28.5) had
// inline run-picker logic; Triage / Healing / NFR pages had no scope at
// all. This component unifies the pattern: auto-select latest completed
// run on mount when no `?run_id=` URL param present; sticky banner shows
// the selected run; dropdown lets operator switch; URL is the source of
// truth for the selection (deep-linkable, browser back/forward works).

import { useEffect, useState, useCallback } from 'react'
import { useRouter, usePathname, useSearchParams } from 'next/navigation'
import { authHeaders } from '../lib/auth-state'

interface RecentRun {
  run_id: string
  started_at?: string
  environment?: string
  status?: string
}

interface RunScopeSelectorProps {
  /** Current run_id (from URL via useSearchParams()). Empty string = "all runs". */
  runId: string
  /** Callback when operator picks a different run. Component will sync URL. */
  onRunChange: (newRunId: string) => void
  /** Optional project filter for the recent-runs dropdown. */
  projectId?: string
  /** Optional className for the wrapper. */
  className?: string
  /** When true (default), include an "All runs" option in the dropdown
   * and let operator clear the selection. Set false for pages where
   * aggregating doesn't make sense (e.g., NFR Assessment). */
  allowAllRuns?: boolean
  /** When true (default), auto-select the latest completed run on mount
   * if no `?run_id=` param is present. Set false for pages that want
   * explicit operator action (e.g., a "Compare Runs" view). */
  autoSelectLatest?: boolean
}

/** Auth helper. Phase-1 A2 (FE↔BE sync) — delegate to the canonical
 * authHeaders() (Bearer JWT + X-API-Key from NEXT_PUBLIC_ARTA_API_KEY). The
 * old inline version read `localStorage.arta_api_key`, which nothing in the app
 * ever writes → it shipped NO key when the JWT was absent. */
function buildHeaders(): Record<string, string> {
  return authHeaders()
}

export default function RunScopeSelector({
  runId,
  onRunChange,
  projectId,
  className,
  allowAllRuns = true,
  autoSelectLatest = true,
}: RunScopeSelectorProps) {
  const router = useRouter()
  const pathname = usePathname()
  const sp = useSearchParams()
  const [recentRuns, setRecentRuns] = useState<RecentRun[]>([])
  const [loading, setLoading] = useState(true)

  // Sync URL when operator picks a different run. Empty string clears the
  // param (reverts to all-runs view, where allowed).
  const handleChange = useCallback(
    (newRunId: string) => {
      onRunChange(newRunId)
      const qs = new URLSearchParams(sp?.toString() ?? '')
      if (newRunId) qs.set('run_id', newRunId)
      else qs.delete('run_id')
      const next = qs.toString() ? `${pathname}?${qs.toString()}` : pathname
      router.replace(next)
    },
    [onRunChange, sp, pathname, router],
  )

  // Fetch recent runs (newest-first, scoped to project when supplied)
  useEffect(() => {
    let cancelled = false
    const qs = new URLSearchParams()
    if (projectId) qs.set('project_id', projectId)
    qs.set('limit', '20')
    fetch(`/api/execution/runs?${qs.toString()}`, { headers: buildHeaders() })
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (cancelled) return
        const arr = Array.isArray(d?.runs) ? d.runs : Array.isArray(d) ? d : []
        const mapped = (arr as Array<Record<string, unknown>>)
          .map(r => ({
            run_id: String(r.run_id || r.id || ''),
            started_at: r.started_at as string | undefined,
            environment: r.environment as string | undefined,
            status: r.status as string | undefined,
          }))
          .filter(r => r.run_id)
        setRecentRuns(mapped)
        setLoading(false)
        // Auto-select latest completed run when no run_id in URL AND the
        // page wants that default. Picks the most recent COMPLETED run
        // (not running/failed-to-start) so the operator sees stable data.
        if (autoSelectLatest && !runId && mapped.length > 0) {
          const latestCompleted =
            mapped.find(r => r.status === 'completed') ?? mapped[0]
          if (latestCompleted?.run_id) handleChange(latestCompleted.run_id)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setRecentRuns([])
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  // Cosmetic short id for the banner
  const shortId = (rid: string) =>
    rid.length > 16 ? `${rid.slice(0, 8)}…${rid.slice(-4)}` : rid

  const selectedRun = recentRuns.find(r => r.run_id === runId)

  return (
    <div className={className}>
      {/* Sticky banner — visible when a specific run is selected */}
      {runId && (
        <div
          className="mb-4 px-4 py-3 rounded-lg flex items-center gap-3"
          style={{
            background: 'rgba(99,102,241,0.12)',
            border: '1px solid rgba(99,102,241,0.3)',
          }}
        >
          <span
            className="text-xs font-semibold"
            style={{ color: '#818cf8' }}
          >
            Showing data from run
          </span>
          <span
            className="font-mono text-xs px-2 py-0.5 rounded"
            style={{ background: '#0a0a14', color: '#a5b4fc' }}
          >
            {shortId(runId)}
          </span>
          {selectedRun?.environment && (
            <span className="text-[10px]" style={{ color: '#94a3b8' }}>
              · {selectedRun.environment}
            </span>
          )}
          {selectedRun?.started_at && (
            <span className="text-[10px]" style={{ color: '#94a3b8' }}>
              · {new Date(selectedRun.started_at).toLocaleString()}
            </span>
          )}
          <div className="flex-1" />
          {allowAllRuns && (
            <button
              onClick={() => handleChange('')}
              className="px-2 py-1 rounded text-[10px] font-medium"
              style={{
                background: 'rgba(148,163,184,0.15)',
                color: '#cbd5e1',
                border: '1px solid #1e1e3a',
              }}
            >
              Show all runs
            </button>
          )}
        </div>
      )}

      {/* Run picker dropdown */}
      {recentRuns.length > 0 && (
        <div className="flex gap-2 items-center mb-4">
          <label className="text-[11px]" style={{ color: '#94a3b8' }}>
            Run scope:
          </label>
          <select
            value={runId}
            onChange={e => handleChange(e.target.value)}
            disabled={loading}
            className="px-3 py-1.5 rounded-lg text-xs"
            style={{
              background: '#12121f',
              border: '1px solid #1e1e3a',
              color: '#e2e8f0',
              minWidth: 280,
            }}
          >
            {allowAllRuns && <option value="">All runs (lifetime)</option>}
            {recentRuns.map(r => (
              <option key={r.run_id} value={r.run_id}>
                {shortId(r.run_id)}
                {r.environment ? ` · ${r.environment}` : ''}
                {r.started_at
                  ? ` · ${new Date(r.started_at).toLocaleDateString()} ${new Date(r.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
                  : ''}
                {r.status ? ` · ${r.status}` : ''}
              </option>
            ))}
          </select>
        </div>
      )}
    </div>
  )
}
