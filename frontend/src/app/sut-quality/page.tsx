'use client'

// R74.1 — SUT quality dashboard. Renders R72.4's per-endpoint health
// rollup as the operator-visible "Report the quality of the SUT"
// deliverable. Backend endpoint: GET /api/sut/endpoint-health
//
// What this page answers:
//   - Which SUT endpoints are healthy (pass_pct >= 95)?
//   - Which 10 endpoints need fixing most urgently (lowest pass_pct)?
//   - How does each SUT component (regex-bucketed) score?
//   - When did each endpoint last fire a test?
//
// Distinct from /quality-score (composite test-quality gauge) and
// /defects (per-defect drill-down). This page is the "SUT health"
// view the user explicitly asked for in the goal statement.

import { useState, useEffect, useCallback } from 'react'
import Link from 'next/link'
import {
  fetchEndpointHealth,
  fetchArchitectureSummary,
  type EndpointHealthPayload,
  type EndpointHealthRow,
  type ArchitectureSummary,
} from '@/lib/api-client'
import { useProject } from '@/lib/project-context'
import SutUnderstandingPanel from '@/components/SutUnderstandingPanel'
import ArchitectureGraphView from '@/components/ArchitectureGraphView'
import MissionOutcomeStrip from '@/components/MissionOutcomeStrip'
import SutRegressionPanel from '@/components/SutRegressionPanel'
import ProtocolCoverage from '@/components/ProtocolCoverage'

// ── Color helpers (match /quality-score conventions) ──────────────────────

function passColor(pct: number): string {
  if (pct >= 95) return '#34d399'   // green
  if (pct >= 80) return '#60a5fa'   // blue
  if (pct >= 60) return '#fbbf24'   // amber
  if (pct >= 40) return '#f97316'   // orange
  return '#fb7185'                  // red
}

function formatLastSeen(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const now = new Date()
  const deltaH = (now.getTime() - d.getTime()) / 3600000
  if (deltaH < 1) return `${Math.round(deltaH * 60)}m ago`
  if (deltaH < 24) return `${Math.round(deltaH)}h ago`
  if (deltaH < 24 * 7) return `${Math.round(deltaH / 24)}d ago`
  return d.toISOString().slice(0, 10)
}

// ── Components ────────────────────────────────────────────────────────────

function SummaryCard({
  label, value, sublabel, color,
}: { label: string; value: string | number; sublabel?: string; color?: string }) {
  return (
    <div className="rounded-xl p-4"
         style={{ background: 'rgba(15,23,42,0.5)', border: '1px solid rgba(99,102,241,0.2)' }}>
      <div className="text-[11px] font-semibold uppercase tracking-wider"
           style={{ color: '#94a3b8' }}>
        {label}
      </div>
      <div className="text-3xl font-bold mt-1"
           style={{ color: color || '#e2e8f0', fontFamily: "'Space Mono', monospace" }}>
        {value}
      </div>
      {sublabel && (
        <div className="text-xs mt-1" style={{ color: '#64748b' }}>{sublabel}</div>
      )}
    </div>
  )
}

function PassPctBadge({ pct, total }: { pct: number; total: number }) {
  const color = passColor(pct)
  return (
    <div className="inline-flex items-center gap-2 px-2 py-0.5 rounded-md text-xs font-semibold"
         style={{ background: `${color}22`, border: `1px solid ${color}55`, color }}>
      <span style={{ fontFamily: "'Space Mono', monospace" }}>{pct.toFixed(1)}%</span>
      <span style={{ color: '#94a3b8', fontWeight: 400 }}>({total} runs)</span>
    </div>
  )
}

function EndpointRow({ row }: { row: EndpointHealthRow }) {
  return (
    <tr className="border-b" style={{ borderColor: 'rgba(99,102,241,0.1)' }}>
      <td className="py-2 pr-4 text-xs font-mono" style={{ color: '#e2e8f0' }}>
        {row.endpoint_key}
      </td>
      <td className="py-2 pr-4">
        <PassPctBadge pct={row.pass_pct} total={row.judge_total} />
      </td>
      <td className="py-2 pr-4 text-xs" style={{ color: '#34d399', fontFamily: "'Space Mono', monospace" }}>
        {row.pass_count}
      </td>
      <td className="py-2 pr-4 text-xs" style={{ color: '#fb7185', fontFamily: "'Space Mono', monospace" }}>
        {row.fail_count}
      </td>
      <td className="py-2 pr-4 text-xs" style={{ color: '#fbbf24', fontFamily: "'Space Mono', monospace" }}>
        {row.blocked_count}
      </td>
      <td className="py-2 pr-4 text-xs" style={{ color: '#94a3b8' }}>
        {row.coverage_test_count}
      </td>
      <td className="py-2 pr-4 text-xs" style={{ color: '#64748b' }}>
        {formatLastSeen(row.last_seen)}
      </td>
    </tr>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────

export default function SutQualityPage() {
  const { currentProjectId, currentProject } = useProject()
  const [data, setData] = useState<EndpointHealthPayload | null>(null)
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [days, setDays] = useState<number>(7)
  const [showAll, setShowAll] = useState<boolean>(false)
  const [arch, setArch] = useState<ArchitectureSummary | null>(null)  // FE-sync P2/P4

  const load = useCallback(async () => {
    if (!currentProjectId) {
      setData(null)
      setArch(null)
      return
    }
    setLoading(true)
    setError(null)
    // FE-sync P2/P4 — protocol coverage (best-effort; independent of endpoint-health).
    fetchArchitectureSummary(currentProjectId).then(setArch).catch(() => setArch(null))
    try {
      const result = await fetchEndpointHealth(currentProjectId, days)
      setData(result)
    } catch (err) {
      const e = err as { message?: string }
      setError(e.message || 'Failed to load endpoint health')
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [currentProjectId, days])

  useEffect(() => {
    load()
  }, [load])

  if (!currentProjectId) {
    return (
      <div className="p-8 text-sm" style={{ color: '#94a3b8' }}>
        Select a project from the top-left to view SUT quality.
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: '#e2e8f0' }}>
            SUT Quality
          </h1>
          <p className="text-sm mt-1" style={{ color: '#94a3b8' }}>
            The SUT-quality verdict + its credibility for{' '}
            <span style={{ color: '#c4b5fd', fontFamily: "'Space Mono', monospace" }}>
              {currentProject?.name || currentProjectId}
            </span>
            : latest-run mission outcome (verdict + trust), regression trend, and
            per-endpoint health ({data?.window_days ?? days}d).
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-xs font-semibold uppercase tracking-wider"
                 style={{ color: '#94a3b8' }}>
            Window
          </label>
          <select
            value={days}
            onChange={e => setDays(parseInt(e.target.value, 10))}
            className="px-2 py-1 text-xs rounded-md"
            style={{
              background: 'rgba(15,23,42,0.5)',
              border: '1px solid rgba(99,102,241,0.3)',
              color: '#e2e8f0',
            }}
          >
            <option value={1}>1 day</option>
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
          </select>
          <button
            onClick={load}
            disabled={loading}
            className="px-3 py-1 text-xs rounded-md font-semibold"
            style={{
              background: loading ? 'rgba(99,102,241,0.2)' : 'rgba(99,102,241,0.4)',
              border: '1px solid rgba(99,102,241,0.6)',
              color: '#e2e8f0',
              cursor: loading ? 'not-allowed' : 'pointer',
            }}
          >
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      {/* R330 — SUT Understanding: how well-grounded ARTA's knowledge of this SUT
          is (source vs guessed), + flagged tests to fix (close the loop). */}
      <SutUnderstandingPanel projectId={currentProjectId} />

      {/* R330 P4 — visualize what ARTA discovered: the 6 Architecture Discovery
          graphs (API endpoints colored by grounding depth). Self-hides until
          discovery has run for this project. */}
      <ArchitectureGraphView projectId={currentProjectId} />

      {/* VERDICT + CREDIBILITY + ACTION — latest-run mission outcome (the
          4-pillar SUT-quality answer + whether to trust it + next steps).
          Moved here from the consolidated /quality-score so the credibility
          chain is preserved on the single canonical SUT Quality page. */}
      <MissionOutcomeStrip projectId={currentProjectId} />

      {/* SUT regression trend (R37.6) — MTTR, open P0/P1, resolved over 30d. */}
      <SutRegressionPanel projectId={currentProjectId} />

      {/* FE-sync P2/P4 — protocol coverage from Architecture Discovery: REST vs
          non-REST (sse/soap/signalr/graphql) distribution + any inspection warnings
          (e.g. graphql_introspection_failed). Self-hides until discovery has run. */}
      {arch?.protocol_counts && (
        <div className="rounded-xl p-4" style={{ background: '#12122a', border: '1px solid #1e1e3a' }}>
          <ProtocolCoverage summary={arch} />
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="rounded-xl p-4"
             style={{ background: 'rgba(251,113,133,0.10)', border: '1px solid rgba(251,113,133,0.4)' }}>
          <div className="text-sm font-semibold" style={{ color: '#fb7185' }}>
            ⛔ {error}
          </div>
          <div className="text-xs mt-1" style={{ color: '#94a3b8' }}>
            Likely cause: backend not yet redeployed with R72.4, OR no Newman runs
            have stamped endpoint_keys (R55.7) in the window. Trigger a run with
            Newman dispatch to populate the data.
          </div>
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && data && data.endpoints.length === 0 && (
        <div className="rounded-xl p-6 text-center"
             style={{ background: 'rgba(15,23,42,0.5)', border: '1px solid rgba(99,102,241,0.2)' }}>
          <div className="text-sm font-semibold" style={{ color: '#e2e8f0' }}>
            No endpoint coverage in the last {days} days.
          </div>
          <div className="text-xs mt-2" style={{ color: '#94a3b8' }}>
            Either no runs dispatched, or the runs didn&apos;t exercise endpoints via
            Newman. R55.7 stamps endpoint_keys on Newman result rows; other tools
            don&apos;t yet contribute (R74.6 will extend coverage to Playwright + k6).
          </div>
          <Link href="/test-explorer"
                className="inline-block mt-3 px-3 py-1.5 text-xs rounded-md font-semibold"
                style={{ background: 'rgba(99,102,241,0.4)', color: '#e2e8f0', border: '1px solid rgba(99,102,241,0.6)' }}>
            Run Suite →
          </Link>
        </div>
      )}

      {/* Summary cards */}
      {data && data.endpoints.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <SummaryCard
            label="Endpoints covered"
            value={data.totals.endpoints_seen}
            sublabel={`From ${data.window_days}-day window`}
          />
          <SummaryCard
            label="Healthy (≥95%)"
            value={data.totals.endpoints_above_95}
            sublabel="Operator-visible pass-rate threshold"
            color="#34d399"
          />
          <SummaryCard
            label="Critical (<50%)"
            value={data.totals.endpoints_below_50}
            sublabel="Priority fix targets"
            color="#fb7185"
          />
        </div>
      )}

      {/* Worst 10 */}
      {data && data.worst_10.length > 0 && (
        <section className="rounded-xl p-4"
                 style={{ background: 'rgba(15,23,42,0.4)', border: '1px solid rgba(251,113,133,0.3)' }}>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-bold uppercase tracking-wider" style={{ color: '#fb7185' }}>
              Worst 10: Priority Fix Targets
            </h2>
            <span className="text-[11px]" style={{ color: '#64748b' }}>
              Sorted by pass_pct ascending
            </span>
          </div>
          <table className="w-full">
            <thead>
              <tr className="border-b" style={{ borderColor: 'rgba(99,102,241,0.2)' }}>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Endpoint</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Pass Rate</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Pass</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Fail</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Blocked</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Tests</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Last Seen</th>
              </tr>
            </thead>
            <tbody>
              {data.worst_10.map(row => <EndpointRow key={row.endpoint_key} row={row} />)}
            </tbody>
          </table>
        </section>
      )}

      {/* Component rollup */}
      {data && data.components.length > 0 && (
        <section className="rounded-xl p-4"
                 style={{ background: 'rgba(15,23,42,0.4)', border: '1px solid rgba(99,102,241,0.2)' }}>
          <h2 className="text-sm font-bold uppercase tracking-wider mb-3"
              style={{ color: '#c4b5fd' }}>
            Component Health Rollup
          </h2>
          <p className="text-xs mb-3" style={{ color: '#64748b' }}>
            Endpoints bucketed by URL prefix (first 2 segments).
            Component pass-rate weighted by test count per endpoint.
          </p>
          <table className="w-full">
            <thead>
              <tr className="border-b" style={{ borderColor: 'rgba(99,102,241,0.2)' }}>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Component</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Pass Rate</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Pass</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Fail</th>
                <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                    style={{ color: '#94a3b8' }}>Endpoints</th>
              </tr>
            </thead>
            <tbody>
              {data.components.map(c => (
                <tr key={c.component} className="border-b" style={{ borderColor: 'rgba(99,102,241,0.1)' }}>
                  <td className="py-2 pr-4 text-xs font-mono" style={{ color: '#e2e8f0' }}>
                    {c.component}
                  </td>
                  <td className="py-2 pr-4">
                    <PassPctBadge pct={c.pass_pct} total={c.pass_count + c.fail_count} />
                  </td>
                  <td className="py-2 pr-4 text-xs"
                      style={{ color: '#34d399', fontFamily: "'Space Mono', monospace" }}>
                    {c.pass_count}
                  </td>
                  <td className="py-2 pr-4 text-xs"
                      style={{ color: '#fb7185', fontFamily: "'Space Mono', monospace" }}>
                    {c.fail_count}
                  </td>
                  <td className="py-2 pr-4 text-xs"
                      style={{ color: '#94a3b8', fontFamily: "'Space Mono', monospace" }}>
                    {c.endpoint_count}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {/* All endpoints (collapsible) */}
      {data && data.endpoints.length > data.worst_10.length && (
        <section className="rounded-xl p-4"
                 style={{ background: 'rgba(15,23,42,0.4)', border: '1px solid rgba(99,102,241,0.2)' }}>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-bold uppercase tracking-wider" style={{ color: '#c4b5fd' }}>
              All Endpoints ({data.endpoints.length})
            </h2>
            <button
              onClick={() => setShowAll(s => !s)}
              className="text-xs px-2 py-1 rounded-md font-semibold"
              style={{ background: 'rgba(99,102,241,0.2)', color: '#c4b5fd', border: '1px solid rgba(99,102,241,0.4)' }}
            >
              {showAll ? 'Hide' : `Show all ${data.endpoints.length}`}
            </button>
          </div>
          {showAll && (
            <table className="w-full">
              <thead>
                <tr className="border-b" style={{ borderColor: 'rgba(99,102,241,0.2)' }}>
                  <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                      style={{ color: '#94a3b8' }}>Endpoint</th>
                  <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                      style={{ color: '#94a3b8' }}>Pass Rate</th>
                  <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                      style={{ color: '#94a3b8' }}>Pass</th>
                  <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                      style={{ color: '#94a3b8' }}>Fail</th>
                  <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                      style={{ color: '#94a3b8' }}>Blocked</th>
                  <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                      style={{ color: '#94a3b8' }}>Tests</th>
                  <th className="text-left py-2 pr-4 text-[11px] font-semibold uppercase tracking-wider"
                      style={{ color: '#94a3b8' }}>Last Seen</th>
                </tr>
              </thead>
              <tbody>
                {data.endpoints.map(row => <EndpointRow key={row.endpoint_key} row={row} />)}
              </tbody>
            </table>
          )}
        </section>
      )}
    </div>
  )
}
