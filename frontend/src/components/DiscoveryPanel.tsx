/**
 * Phase H5 — Discovery Panel
 *
 * Surfaces Stage 2.5 (UI Discovery) state for a project:
 *   - Last discovery run timestamp + envvars harvested
 *   - Captured endpoint count + chain count
 *   - Discovery status: pending / api-only / disabled / fresh
 *   - Refresh button → POST /api/discovery/refresh
 *
 * Design: matches the IntegrationCard glass-morphism aesthetic; degrades
 * gracefully when the project has never run discovery (renders "Never run"
 * + "Trigger first discovery" CTA).
 */
'use client'

import { useEffect, useState } from 'react'
import {
  fetchDiscoverySummary,
  fetchArchitectureSummary,
  refreshDiscovery,
  type DiscoverySummary,
  type ArchitectureSummary,
} from '@/lib/api-client'
import ProtocolCoverage from './ProtocolCoverage'

interface Props {
  projectId: string
  onRefreshTriggered?: () => void
}

const formatRelative = (iso: string | null): string => {
  if (!iso) return 'Never'
  try {
    const t = new Date(iso).getTime()
    const ago = Date.now() - t
    if (ago < 60_000) return 'Just now'
    if (ago < 3_600_000) return `${Math.floor(ago / 60_000)}m ago`
    if (ago < 86_400_000) return `${Math.floor(ago / 3_600_000)}h ago`
    return `${Math.floor(ago / 86_400_000)}d ago`
  } catch {
    return iso
  }
}

const StatusBadge = ({ summary }: { summary: DiscoverySummary }) => {
  if (summary.is_api_only) {
    return (
      <span
        className="text-[10px] px-2 py-0.5 rounded-full font-medium"
        style={{ background: '#1e293b', color: '#94a3b8' }}
      >
        API-ONLY (skipped)
      </span>
    )
  }
  if (!summary.stage_2_5_enabled) {
    return (
      <span
        className="text-[10px] px-2 py-0.5 rounded-full font-medium"
        style={{ background: '#3f1d1d', color: '#fca5a5' }}
      >
        DISABLED
      </span>
    )
  }
  if (summary.discovery_pending) {
    return (
      <span
        className="text-[10px] px-2 py-0.5 rounded-full font-medium"
        style={{ background: '#3f3a1d', color: '#fde68a' }}
      >
        PENDING
      </span>
    )
  }
  if (!summary.last_discovery_at) {
    return (
      <span
        className="text-[10px] px-2 py-0.5 rounded-full font-medium"
        style={{ background: '#1e293b', color: '#94a3b8' }}
      >
        NEVER RUN
      </span>
    )
  }
  return (
    <span
      className="text-[10px] px-2 py-0.5 rounded-full font-medium"
      style={{ background: '#173d2a', color: '#86efac' }}
    >
      ACTIVE
    </span>
  )
}

export default function DiscoveryPanel({ projectId, onRefreshTriggered }: Props) {
  const [summary, setSummary] = useState<DiscoverySummary | null>(null)
  const [arch, setArch] = useState<ArchitectureSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      // FE-sync P2/P4 — also pull the architecture summary for the protocol strip;
      // best-effort (null when discovery hasn't produced graphs) so it never blocks
      // the primary discovery summary.
      const [data, archData] = await Promise.all([
        fetchDiscoverySummary(projectId),
        fetchArchitectureSummary(projectId).catch(() => null),
      ])
      setSummary(data)
      setArch(archData)
    } catch (e: any) {
      setError(e.message || 'Failed to load discovery summary')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (projectId) load()
  }, [projectId])

  const handleRefresh = async () => {
    if (refreshing || !summary) return
    setRefreshing(true)
    try {
      await refreshDiscovery(projectId)
      await load()
      onRefreshTriggered?.()
    } catch (e: any) {
      setError(e.message || 'Failed to trigger discovery refresh')
    } finally {
      setRefreshing(false)
    }
  }

  if (loading) {
    return (
      <div
        className="rounded-xl p-5"
        style={{ background: '#0f0f23', border: '1px solid #1e1e3a' }}
      >
        <div className="text-sm" style={{ color: '#64748b' }}>
          Loading discovery state…
        </div>
      </div>
    )
  }

  if (error || !summary) {
    return (
      <div
        className="rounded-xl p-5"
        style={{ background: '#0f0f23', border: '1px solid #3f1d1d' }}
      >
        <div className="text-sm" style={{ color: '#fca5a5' }}>
          Discovery panel unavailable: {error}
        </div>
      </div>
    )
  }

  const ctaLabel = summary.last_discovery_at
    ? 'Re-run discovery'
    : 'Trigger first discovery'

  return (
    <div
      className="rounded-xl overflow-hidden transition-colors"
      style={{ background: '#0f0f23', border: '1px solid #1e1e3a' }}
    >
      <div
        className="px-5 py-4 flex items-center gap-4"
        style={{ borderBottom: '1px solid #1e1e3a' }}
      >
        <span className="text-2xl">🔍</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm text-white">
              UI Discovery (Stage 2.5)
            </span>
            <StatusBadge summary={summary} />
          </div>
          <p className="text-xs mt-1 leading-relaxed" style={{ color: '#64748b' }}>
            Captures network traffic during Playwright runs to auto-populate
            path-param env vars and build the call-chain graph.
          </p>
        </div>
      </div>

      <div className="px-5 py-4 grid grid-cols-3 gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
            Last run
          </div>
          <div className="text-sm font-medium text-white mt-1">
            {formatRelative(summary.last_discovery_at)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
            Env vars filled
          </div>
          <div className="text-sm font-medium text-white mt-1">
            {summary.envvars_harvested_count}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
            Chains captured
          </div>
          <div className="text-sm font-medium text-white mt-1">
            {summary.captured_chain_count}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
            Endpoints captured
          </div>
          <div className="text-sm font-medium text-white mt-1">
            {summary.captured_endpoint_count}
          </div>
        </div>
        <div className="col-span-2 flex items-end justify-end">
          <button
            disabled={refreshing || !summary.stage_2_5_enabled || summary.is_api_only}
            onClick={handleRefresh}
            className="text-xs px-3 py-1.5 rounded-md font-medium transition-colors"
            style={{
              background:
                refreshing || !summary.stage_2_5_enabled || summary.is_api_only
                  ? '#1e1e3a'
                  : '#6366f1',
              color: '#fff',
              cursor:
                refreshing || !summary.stage_2_5_enabled || summary.is_api_only
                  ? 'not-allowed'
                  : 'pointer',
              opacity:
                refreshing || !summary.stage_2_5_enabled || summary.is_api_only
                  ? 0.5
                  : 1,
            }}
          >
            {refreshing ? 'Triggering…' : ctaLabel}
          </button>
        </div>
      </div>

      {/* FE-sync P2/P4 — protocol coverage (counts + non-REST + warnings). Self-hides
          until architecture discovery has produced protocol_counts. */}
      {arch?.protocol_counts && (
        <div className="px-5 py-3" style={{ borderTop: '1px solid #1e1e3a' }}>
          <ProtocolCoverage summary={arch} />
        </div>
      )}

      {summary.is_api_only && (
        <div
          className="px-5 py-3 text-xs"
          style={{ borderTop: '1px solid #1e1e3a', color: '#94a3b8' }}
        >
          This project is API-only — Stage 2.5 is intentionally skipped. Env
          vars come from OpenAPI + operator entry.
        </div>
      )}
      {!summary.is_api_only && !summary.stage_2_5_enabled && (
        <div
          className="px-5 py-3 text-xs"
          style={{ borderTop: '1px solid #1e1e3a', color: '#fde68a' }}
        >
          UI Discovery is opt-in for existing projects. Enable in Settings →
          Discovery to activate.
        </div>
      )}
      {summary.discovery_pending && (
        <div
          className="px-5 py-3 text-xs"
          style={{ borderTop: '1px solid #1e1e3a', color: '#fde68a' }}
        >
          Pending — Stage 2.5 will fire on the next pipeline run.
        </div>
      )}
    </div>
  )
}
