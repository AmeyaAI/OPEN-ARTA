'use client'

// R74.2 — global auth-staleness badge. Reads R72.5's
// /api/discovery/projects/{pid}/auth-staleness endpoint and surfaces
// stale_soon / expired states BEFORE the operator's pipeline silently
// breaks. Mounts in AppShell so every page sees the signal.
//
// Behavior:
//   - state=fresh: no render
//   - state=stale_soon: amber chip "Auth refreshes in ~Xh — refresh now"
//   - state=expired: red banner "Auth expired — pipelines will fail"
//   - state=unknown: no render (silent; covers cookie-less / opaque-cookie projects)
//   - Click → opens RefreshAuthModal (existing component)
//
// Poll cadence: every 5 minutes + on tab focus. Operators get the
// signal within minutes of state transition, not after a failed run.

import { useState, useEffect, useCallback } from 'react'
import { fetchAuthStaleness, type AuthStaleness } from '@/lib/api-client'
import { useProject } from '@/lib/project-context'
import RefreshAuthModal from '@/components/RefreshAuthModal'

const POLL_INTERVAL_MS = 5 * 60 * 1000   // 5 minutes
const DEFAULT_ENVIRONMENT = 'staging'

export default function AuthStalenessBadge() {
  const { currentProjectId } = useProject()
  const [staleness, setStaleness] = useState<AuthStaleness | null>(null)
  const [showModal, setShowModal] = useState<boolean>(false)
  const [dismissed, setDismissed] = useState<boolean>(false)

  const load = useCallback(async () => {
    if (!currentProjectId) {
      setStaleness(null)
      return
    }
    try {
      const result = await fetchAuthStaleness(currentProjectId, DEFAULT_ENVIRONMENT)
      setStaleness(result)
      // Reset dismissal when state worsens (expired requires re-acknowledgement)
      if (result.state === 'expired') setDismissed(false)
    } catch {
      // Silent failure — endpoint may not be deployed yet OR project may
      // not have an env block. Better to render nothing than show a
      // confusing error banner.
      setStaleness(null)
    }
  }, [currentProjectId])

  useEffect(() => {
    load()
    if (!currentProjectId) return
    // Poll periodically + on tab focus
    const interval = setInterval(load, POLL_INTERVAL_MS)
    const onFocus = () => load()
    window.addEventListener('focus', onFocus)
    return () => {
      clearInterval(interval)
      window.removeEventListener('focus', onFocus)
    }
  }, [load, currentProjectId])

  // No render for fresh / unknown / dismissed (stale_soon only)
  if (!staleness) return null
  // R306.C — defense-in-depth: never raise a blocking banner for a SUT whose
  // runtime auth does not depend on the pasted cookie (bearer/api_key —
  // those to state='unknown', but guard here too so a stale client/build can't
  // resurrect the false "pipelines will BLOCK" alarm.
  if (staleness.pipeline_blocking === false) return null
  if (staleness.state === 'fresh' || staleness.state === 'unknown') return null
  if (staleness.state === 'stale_soon' && dismissed) return null

  const isExpired = staleness.state === 'expired'
  const bg = isExpired ? 'rgba(251,113,133,0.12)' : 'rgba(245,158,11,0.10)'
  const border = isExpired ? 'rgba(251,113,133,0.4)' : 'rgba(245,158,11,0.4)'
  const textColor = isExpired ? '#fb7185' : '#fbbf24'
  const icon = isExpired ? '⛔' : '⚠'
  const hours = staleness.ttl_remaining_hours ?? null
  // R306.C — only cookie-dependent SUTs reach this point (the pipeline_blocking
  // guard above suppresses bearer/api_key SUTs), so the cookie wording is now
  // always accurate.
  const headline = isExpired
    ? 'Auth cookie EXPIRED. Pipelines that depend on auth will BLOCK'
    : `Auth cookie expires ${hours !== null ? `in ~${hours}h` : 'soon'}. Refresh before the autonomous loop breaks`

  return (
    <>
      <div className="sticky top-0 z-30 px-4 py-2 flex items-center justify-between gap-3"
           style={{ background: bg, borderBottom: `1px solid ${border}` }}>
        <div className="flex items-center gap-3 min-w-0">
          <span style={{ fontSize: 16, color: textColor }}>{icon}</span>
          <div className="min-w-0">
            <div className="text-xs font-semibold truncate" style={{ color: textColor }}>
              {headline}
            </div>
            {staleness.hint && (
              <div className="text-[11px] truncate" style={{ color: '#94a3b8' }}>
                {staleness.hint}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => setShowModal(true)}
            className="px-3 py-1 rounded-md text-xs font-semibold text-white shrink-0"
            style={{
              background: isExpired
                ? 'linear-gradient(135deg, #fb7185, #f97316)'
                : 'linear-gradient(135deg, #f59e0b, #fbbf24)',
              border: 'none',
              cursor: 'pointer',
            }}
          >
            Refresh Auth →
          </button>
          {!isExpired && (
            <button
              onClick={() => setDismissed(true)}
              className="px-2 py-1 rounded-md text-[11px] shrink-0"
              style={{ background: 'transparent', color: '#94a3b8', cursor: 'pointer' }}
              title="Dismiss until next state change (will re-appear on refresh)"
            >
              Dismiss
            </button>
          )}
        </div>
      </div>
      {showModal && currentProjectId && (
        <RefreshAuthModal
          open={showModal}
          onClose={() => setShowModal(false)}
          projectId={currentProjectId}
          environment={staleness.environment || DEFAULT_ENVIRONMENT}
          initialState={null}
          onSuccess={() => {
            setShowModal(false)
            setDismissed(false)
            // Re-fetch so the badge clears immediately on successful paste
            load()
          }}
        />
      )}
    </>
  )
}
