'use client'

// Consolidated Suite Report — ONE self-contained page, no hopping:
//   top   : aggregate (summed across the batch runs) + blocked/skip reasons
//   left  : the batch-run list
//   right : the clicked run's detail, rendered INLINE via the shared
//           RunDetailContent (no navigation away, no new tabs; Back works).
// Deep-links: ?run_id=<id> pre-selects a run; selection is mirrored into the
// URL with replaceState so refresh/share keeps context.
// All calls go through apiRequest() so an expired JWT (60-min TTL, no refresh)
// redirects to /login instead of the panel rendering the 401 body as a run.
import { useCallback, useEffect, useState } from 'react'
import RunDetailContent from '@/app/run-history/RunDetailContent'
import { apiRequest } from '@/lib/api-client'

type Rollup = {
  keyed_by: string; run_count: number
  passed: number; failed: number; blocked: number; skipped: number; total: number
  pass_of_executed: number; started_at: string | null; completed_at: string | null
  reason_breakdown: { blocked: Record<string, number>; skip: Record<string, number> }
}
type SuiteRun = {
  id: string; run_id: string; started_at: string | null
  passed: number; failed: number; skipped: number; gate_decision: string | null; status: string | null
}

const BAR = [
  ['PASS', 'passed', '#2c8a5b'], ['FAIL', 'failed', '#c24a3e'],
  ['BLOCKED', 'blocked', '#b27a16'], ['SKIP', 'skipped', '#7c8a90'],
] as const
const GATE_COLOR: Record<string, string> = { PASS: '#34d399', CONCERNS: '#fbbf24', FAIL: '#fb7185', WAIVED: '#a78bfa' }
const GATE_BG: Record<string, string> = { PASS: 'rgba(52,211,153,0.12)', CONCERNS: 'rgba(251,191,36,0.12)', FAIL: 'rgba(251,113,133,0.12)', WAIVED: 'rgba(167,139,250,0.12)' }

function fmt(iso: string | null): string {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString() } catch { return iso }
}
function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) } catch { return iso }
}

export default function SuiteReportPage() {
  const [rollup, setRollup] = useState<Rollup | null>(null)
  const [runs, setRuns] = useState<SuiteRun[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<any | null>(null)
  const [detailErr, setDetailErr] = useState<string | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [filter, setFilter] = useState<'all' | 'fail' | 'pass'>('all')

  // Inline drill-down: fetch the run's detail into the right panel — no navigation.
  const selectRun = useCallback((id: string) => {
    setSelectedId(id)
    setLoadingDetail(true)
    setDetailErr(null)
    apiRequest<any>(`/api/execution/runs/${encodeURIComponent(id)}`)
      .then(d => {
        // Guard: only accept a run-shaped payload; anything else is surfaced, not rendered.
        if (d && typeof d === 'object' && (d.id || d.run_id)) { setDetail(d) }
        else { setDetail(null); setDetailErr('Run detail came back in an unexpected shape.') }
      })
      .catch(e => { setDetail(null); setDetailErr(e?.message || String(e)) })
      .finally(() => setLoadingDetail(false))
    // mirror into the URL so refresh / share keeps the selected run (no reload)
    if (typeof window !== 'undefined') {
      const u = new URL(window.location.href)
      u.searchParams.set('run_id', id)
      window.history.replaceState(null, '', u.toString())
    }
  }, [])

  // Load the suite (aggregate + batch runs). Query params pass straight through.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const sp = new URLSearchParams(window.location.search)
    const preselect = sp.get('run_id')
    sp.delete('run_id')
    const q = sp.toString()
    Promise.all([
      apiRequest<Rollup>(`/api/execution/runs/suite-rollup?${q}`),
      apiRequest<{ runs?: SuiteRun[] }>(`/api/execution/runs/suite-runs?${q}`),
    ])
      .then(([ro, ru]) => {
        setRollup(ro); setRuns(ru.runs ?? [])
        if (preselect) selectRun(preselect)
      })
      .catch(e => setErr(e?.message || String(e)))
  }, [selectRun])

  const gate = rollup && rollup.failed > 0 ? 'FAIL' : 'PASS'
  const visibleRuns = runs.filter(r =>
    filter === 'all' ? true : filter === 'fail' ? r.failed > 0 : (r.passed > 0 && r.failed === 0))
  const panelOpen = Boolean(detail || loadingDetail || detailErr)

  return (
    <div style={{ minHeight: '100vh', background: '#0a0a14', color: '#e2e8f0',
                  fontFamily: 'DM Sans, system-ui, sans-serif' }}>
      <div style={{ maxWidth: 1400, margin: '0 auto', padding: '1.5rem 1.5rem 3rem' }}>

        {/* Breadcrumb */}
        <nav style={{ fontSize: 13, color: '#64748b', marginBottom: 10 }}>
          <a href="/run-history" style={{ color: '#a5b4fc', textDecoration: 'none' }}>Run History</a>
          <span style={{ margin: '0 8px' }}>›</span>
          <span style={{ color: '#e2e8f0' }}>Suite Report</span>
        </nav>

        {err && <p style={{ color: '#fca5a5' }}>Couldn’t load the suite report: {err}</p>}

        {/* Header + aggregate */}
        <div style={{ background: '#12121f', border: '1px solid #1c6d66', borderRadius: 12, padding: 18, marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginBottom: 12 }}>
            <div>
              <h1 style={{ fontSize: 22, fontWeight: 700, margin: 0 }}>Suite Report — consolidated</h1>
              <p style={{ color: '#64748b', fontSize: 12, margin: '4px 0 0' }}>
                {rollup ? `${rollup.run_count} batch runs · ${rollup.keyed_by} · ${fmt(rollup.started_at)} → ${fmt(rollup.completed_at)}` : 'Loading…'}
              </p>
            </div>
            {rollup && (
              <div style={{ textAlign: 'right' }}>
                <span style={{ fontSize: 30, fontWeight: 700, color: '#34d399', fontFamily: "'Space Mono',monospace" }}>{rollup.pass_of_executed}%</span>
                <span style={{ fontSize: 12, color: '#64748b', marginLeft: 6 }}>pass of executed</span>
                <span style={{ marginLeft: 12, fontSize: 12, fontWeight: 700, padding: '2px 8px', borderRadius: 6,
                               background: GATE_BG[gate], color: GATE_COLOR[gate], fontFamily: "'Space Mono',monospace" }}>{gate}</span>
              </div>
            )}
          </div>

          {rollup && (
            <>
              <div style={{ display: 'flex', height: 34, borderRadius: 8, overflow: 'hidden', border: '1px solid #1e1e3a', marginBottom: 6 }}>
                {BAR.map(([label, key, color]) => {
                  const n = rollup[key]
                  return n > 0 ? (
                    <div key={label} title={`${label}: ${n}`}
                         style={{ flex: n, background: color, color: '#fff', display: 'flex', alignItems: 'center',
                                  justifyContent: 'center', fontSize: 12, fontWeight: 600,
                                  fontFamily: "'Space Mono',monospace", minWidth: 0 }}>
                      {n > rollup.total * 0.06 ? `${label} ${n}` : n}
                    </div>
                  ) : null
                })}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 12 }}>
                {(['blocked', 'skip'] as const).map(kind => (
                  <div key={kind}>
                    <p style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.06em', color: '#64748b', margin: '0 0 6px' }}>
                      {kind === 'blocked' ? `Why blocked (${rollup.blocked})` : `Why skipped (${rollup.skipped})`}
                    </p>
                    {Object.entries(rollup.reason_breakdown?.[kind] || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
                      <div key={k} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0', fontSize: 12, color: '#cbd5e1' }}>
                        <span>{k}</span><span style={{ fontFamily: "'Space Mono',monospace", color: '#94a3b8' }}>{v}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        {/* Batch runs (left) + inline run detail (right) */}
        <div style={{ display: 'grid', gridTemplateColumns: panelOpen ? '400px 1fr' : '1fr', gap: 16, alignItems: 'start' }}>
          <div style={{ background: '#12121f', border: '1px solid #1e1e3a', borderRadius: 12, overflow: 'hidden' }}>
            <div style={{ padding: '10px 14px', borderBottom: '1px solid #1e1e3a', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 600 }}>Batch runs <span style={{ color: '#64748b', fontWeight: 400 }}>· {visibleRuns.length}</span></span>
              <div style={{ display: 'flex', gap: 4 }}>
                {(['all', 'fail', 'pass'] as const).map(f => (
                  <button key={f} onClick={() => setFilter(f)}
                          style={{ fontSize: 11, padding: '2px 8px', borderRadius: 6, cursor: 'pointer',
                                   border: '1px solid #1e1e3a', background: filter === f ? '#1c6d66' : '#0d0d1a',
                                   color: filter === f ? '#fff' : '#94a3b8' }}>
                    {f === 'all' ? 'All' : f === 'fail' ? 'With failures' : 'All-pass'}
                  </button>
                ))}
              </div>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 80px 78px 52px', padding: '6px 14px', fontSize: 10,
                          textTransform: 'uppercase', letterSpacing: '.06em', color: '#64748b', background: '#0d0d1a',
                          borderBottom: '1px solid #1e1e3a' }}>
              <span>Run</span><span>Started</span><span>P/F/S</span><span>Gate</span>
            </div>
            <div style={{ maxHeight: 'calc(100vh - 380px)', overflowY: 'auto' }}>
              {visibleRuns.map(r => {
                const active = r.id === selectedId
                return (
                  <div key={r.id} onClick={() => selectRun(r.id)} role="button" tabIndex={0}
                       onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') selectRun(r.id) }}
                       style={{ display: 'grid', gridTemplateColumns: '1fr 80px 78px 52px', padding: '8px 14px', fontSize: 12,
                                borderBottom: '1px solid #1e1e3a', cursor: 'pointer',
                                background: active ? 'rgba(99,102,241,0.14)' : 'transparent',
                                borderLeft: active ? '3px solid #6366f1' : '3px solid transparent' }}>
                    <span style={{ color: '#a5b4fc', fontFamily: 'monospace', fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.run_id || r.id}</span>
                    <span style={{ color: '#94a3b8', fontSize: 11 }}>{fmtTime(r.started_at)}</span>
                    <span style={{ fontFamily: "'Space Mono',monospace", color: '#94a3b8', fontSize: 11 }}>{r.passed}/{r.failed}/{r.skipped}</span>
                    <span style={{ fontFamily: "'Space Mono',monospace", fontSize: 11,
                                   color: r.gate_decision === 'FAIL' ? '#fb7185' : '#34d399' }}>{r.gate_decision || '—'}</span>
                  </div>
                )
              })}
              {visibleRuns.length === 0 && (
                <div style={{ padding: 20, textAlign: 'center', color: '#64748b', fontSize: 12 }}>
                  {runs.length === 0 && !err ? 'Loading batch runs…' : 'No runs match this filter.'}
                </div>
              )}
            </div>
          </div>

          {/* Inline detail — stays on this page */}
          {panelOpen && (
            <div style={{ background: '#12121f', border: '1px solid #1e1e3a', borderRadius: 12, overflow: 'hidden' }}>
              <div style={{ padding: '10px 16px', borderBottom: '1px solid #1e1e3a', display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: '#0d0d1a' }}>
                <span style={{ color: '#a5b4fc', fontFamily: 'monospace', fontSize: 12 }}>
                  {loadingDetail ? 'Loading run…' : (detail?.run_id || detail?.id || selectedId)}
                </span>
                {detail?.gate_decision && (
                  <span style={{ fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 6, fontFamily: "'Space Mono',monospace",
                                 background: GATE_BG[detail.gate_decision] || '#1e1e3a', color: GATE_COLOR[detail.gate_decision] || '#94a3b8' }}>
                    {detail.gate_decision}
                  </span>
                )}
              </div>
              {detailErr && !loadingDetail && (
                <div style={{ padding: 20, color: '#fca5a5', fontSize: 13 }}>
                  Couldn’t load this run: {detailErr}
                  <div style={{ marginTop: 8 }}>
                    <button onClick={() => selectedId && selectRun(selectedId)}
                            style={{ fontSize: 12, padding: '4px 10px', borderRadius: 6, cursor: 'pointer',
                                     border: '1px solid #1e1e3a', background: '#0d0d1a', color: '#a5b4fc' }}>Retry</button>
                  </div>
                </div>
              )}
              {detail && <RunDetailContent selected={detail} />}
            </div>
          )}
        </div>

        {!panelOpen && runs.length > 0 && (
          <p style={{ color: '#64748b', fontSize: 12, marginTop: 12 }}>
            Click a batch run to see its detailed results here — no page change.
          </p>
        )}
      </div>
    </div>
  )
}
