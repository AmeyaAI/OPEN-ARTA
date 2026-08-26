'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import Link from 'next/link'
import LiveExecutionFeed from '@/components/LiveExecutionFeed'
import { useProject } from '@/lib/project-context'
import RunDetailContent from './RunDetailContent'

const API = ''
const API_KEY = process.env.NEXT_PUBLIC_ARTA_API_KEY ?? ''

type Run = {
  id: string
  build_id: string
  trigger: string
  branch: string
  environment: string
  status: string
  gate_decision: string
  started_at: string
  finished_at?: string
  total: number
  passed: number
  failed: number
  skipped: number
  blocked?: number
  pass_rate?: number
  coverage_pct: number
  duration_s: number
}

type RunDetail = Run & {
  results: { test_id: string; title: string; status: string; duration_ms: number; tool: string; browser?: string }[]
}

type Summary = {
  total_runs: number
  avg_pass_rate: number
  avg_duration_s: number
  gate_counts?: { PASS?: number; CONCERNS?: number; FAIL?: number; WAIVED?: number }
  trend: { run_id: string; started_at: string; pass_rate: number; coverage_pct: number; gate_decision: string }[]
}

// BMAD TEA 4-outcome gate colors: PASS (emerald), CONCERNS (amber), FAIL (rose), WAIVED (violet)
const GATE_COLOR: Record<string, string> = { PASS: '#34d399', CONCERNS: '#fbbf24', FAIL: '#fb7185', WAIVED: '#a78bfa' }
const GATE_BG:    Record<string, string> = { PASS: 'rgba(52,211,153,0.12)', CONCERNS: 'rgba(251,191,36,0.12)', FAIL: 'rgba(251,113,133,0.12)', WAIVED: 'rgba(167,139,250,0.12)' }

function fmtDuration(s: number) {
  if (!s || isNaN(s)) return '—'
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.round(s % 60)
  if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`
  return `${m}:${String(sec).padStart(2,'0')}`
}

function fmtDate(iso: string) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '—'
  return d.toLocaleDateString('en-GB', { day:'2-digit', month:'short' }) + ' ' +
         d.toLocaleTimeString('en-GB', { hour:'2-digit', minute:'2-digit', second:'2-digit' })
}

// R306.A — pass rate OVER EXECUTED tests (passed + failed). BLOCKED/SKIP rows
// did not run, so they are excluded from the denominator — matching the backend
// (_normalize_run.pass_rate) + the summary report. Prefer the backend-computed
// pass_rate when present so the whole surface reads ONE number.
function passRate(r: Run) {
  if (typeof r.pass_rate === 'number') return r.pass_rate.toFixed(1)
  const executed = (r.passed || 0) + (r.failed || 0)
  return executed > 0 ? ((r.passed / executed) * 100).toFixed(1) : '0.0'
}

// R306.B — BLOCKED count for the P/F/S/B display. The backend list-run object
// does not always serialize `blocked`, but `total` counts every result row
// (PASS+FAIL+SKIP+BLOCKED), so derive it when absent. Guarantees the four
// segments sum to `total` (run-26aa5f: 87+70+1+14 = 172).
function blockedOf(r: Run) {
  if (typeof r.blocked === 'number') return r.blocked
  return Math.max(0, (r.total || 0) - (r.passed || 0) - (r.failed || 0) - (r.skipped || 0))
}

function Sparkline({ values, color, height = 60 }: { values: number[]; color: string; height?: number }) {
  if (values.length < 2) return null
  const W = 400, H = height, pad = 6
  const min = Math.min(...values) - 2
  const max = Math.max(...values) + 2
  const range = max - min || 1
  const xs = values.map((_, i) => pad + (i / (values.length - 1)) * (W - pad * 2))
  const ys = values.map(v => H - pad - ((v - min) / range) * (H - pad * 2))
  const pts = xs.map((x, i) => `${x},${ys[i]}`).join(' ')
  const area = `${xs[0]},${H-pad} ${pts} ${xs[xs.length-1]},${H-pad}`
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full" style={{ height }}>
      <polygon points={area} fill={color} opacity={0.12} />
      <polyline points={pts} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {xs.map((x, i) => (
        <circle key={i} cx={x} cy={ys[i]} r={3} fill={color} />
      ))}
    </svg>
  )
}

export default function RunHistoryPage() {
  const { currentProjectId, isLoading: projectLoading } = useProject()
  const [summary,  setSummary]  = useState<Summary | null>(null)
  const [runs,     setRuns]     = useState<Run[]>([])
  const [selected, setSelected] = useState<RunDetail | null>(null)
  const [loading,  setLoading]  = useState(true)
  const [error,    setError]    = useState<string | null>(null)
  const [watchingRunId, setWatchingRunId] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval>>(undefined)
  const initialSelectDone = useRef(false)

  // Build auth headers fresh each time (avoids stale closure)
  function getHeaders(): Record<string, string> {
    const h: Record<string, string> = {}
    const tok = typeof window !== 'undefined' ? localStorage.getItem('arta_token') || '' : ''
    if (tok) h['Authorization'] = `Bearer ${tok}`
    else if (API_KEY) h['X-API-Key'] = API_KEY
    return h
  }

  const loadRuns = useCallback(() => {
    const pidQs = currentProjectId ? `&project_id=${currentProjectId}` : ''
    const h = getHeaders()
    return Promise.all([
      fetch(`${API}/api/execution/runs/summary?limit=10${pidQs}`, { headers: h }).then(r => r.json()),
      fetch(`${API}/api/execution/runs?limit=10${pidQs}`,         { headers: h }).then(r => r.json()),
    ])
      .then(([s, r]) => { setSummary(s); setRuns(r.runs ?? []); setLoading(false); return r.runs ?? [] })
      .catch(() => {
        return new Promise<Run[]>(resolve => {
          setTimeout(() => {
            Promise.all([
              fetch(`${API}/api/execution/runs/summary?limit=10${pidQs}`, { headers: h }).then(r => r.json()),
              fetch(`${API}/api/execution/runs?limit=10${pidQs}`,         { headers: h }).then(r => r.json()),
            ])
              .then(([s, r]) => { setSummary(s); setRuns(r.runs ?? []); resolve(r.runs ?? []) })
              .catch(e => { setError(String(e)); resolve([]) })
              .finally(() => setLoading(false))
          }, 2000)
        })
      })
  }, [currentProjectId])

  useEffect(() => {
    if (projectLoading) return
    initialSelectDone.current = false
    loadRuns()
  }, [currentProjectId, projectLoading, loadRuns])

  // Auto-select run from URL param (e.g., ?run_id=run-xxx) — no useSearchParams needed
  useEffect(() => {
    if (initialSelectDone.current || runs.length === 0) return
    if (typeof window === 'undefined') return
    const params = new URLSearchParams(window.location.search)
    const runIdParam = params.get('run_id')
    if (runIdParam) {
      const match = runs.find(r => r.id === runIdParam)
      if (match) {
        initialSelectDone.current = true
        selectRun(match)
      }
    }
  }, [runs])

  const fetchRunDetail = useCallback((runId: string) => {
    return fetch(`${API}/api/execution/runs/${runId}`, { headers: getHeaders() })
      .then(r => r.json())
  }, [])

  const selectRun = useCallback((run: Run) => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = undefined }

    fetchRunDetail(run.id)
      .then((detail: RunDetail) => {
        setSelected(detail)
        if (detail.status === 'running' || detail.status === 'queued') {
          setWatchingRunId(detail.id)
          pollRef.current = setInterval(() => {
            fetchRunDetail(run.id).then((updated: RunDetail) => {
              setSelected(updated)
              if (updated.status !== 'running' && updated.status !== 'queued') {
                if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = undefined }
                loadRuns()
              }
            }).catch(() => {})
          }, 4000)
        } else {
          // Run already completed — refresh list so P/F/S counts are up-to-date
          const listRun = runs.find(r => r.id === run.id)
          if (!listRun || listRun.total === 0) loadRuns()
        }
      })
      .catch(() => setSelected({ ...run, results: [] } as RunDetail))
  }, [fetchRunDetail, loadRuns, runs])

  useEffect(() => {
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  // Auto-select and poll any running run on page mount
  useEffect(() => {
    if (!runs.length) return
    const runningRun = runs.find(r => r.status === 'running' || r.status === 'queued')
    if (runningRun && (!selected || selected.id !== runningRun.id)) {
      selectRun(runningRun)
    }
  }, [runs])

  return (
    <div className="min-h-screen" style={{ background: '#05050a', color: '#e2e8f0' }}>
      {/* Header */}
      <header style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}
              className="px-8 py-4 flex items-center gap-4">
        <Link href="/" className="flex items-center gap-3 hover:opacity-80 transition-opacity">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center text-lg"
               style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)' }}>A</div>
          <span className="font-bold text-lg">ARTA</span>
        </Link>
        <span style={{ color: '#1e1e3a' }}>›</span>
        <span style={{ color: '#94a3b8' }} className="text-sm">Run History</span>
      </header>

      <main className="px-8 py-8 max-w-7xl mx-auto">
        {error && (
          <div className="mb-6 px-4 py-3 rounded-lg text-sm"
               style={{ background: '#1a0a0a', border: '1px solid #7f1d1d', color: '#fca5a5' }}>
            {error}. Ensure <code>arta-api</code> is running and <code>NEXT_PUBLIC_ARTA_API_KEY</code> is set
          </div>
        )}

        {/* Summary stats */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          {[
            { label: 'Total Runs',    value: summary ? String(summary.total_runs)       : '—', unit: 'last 10',   color: '#a5b4fc' },
            { label: 'Avg Pass Rate', value: summary ? `${summary.avg_pass_rate}`        : '—', unit: '%',         color: '#34d399' },
            { label: 'Avg Duration',  value: summary ? fmtDuration(summary.avg_duration_s) : '—', unit: '',       color: '#22d3ee' },
            { label: 'Gate: PASS',    value: summary ? String(summary?.gate_counts?.PASS ?? 0) : '—', unit: 'runs', color: '#34d399' },
          ].map(m => (
            <div key={m.label} className="rounded-xl p-5"
                 style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <p className="text-xs mb-1" style={{ color: '#64748b' }}>{m.label}</p>
              <p className="text-2xl font-bold" style={{ color: m.color, fontFamily: "'Space Mono',monospace" }}>
                {m.value}<span className="text-sm ml-1" style={{ color: '#64748b' }}>{m.unit}</span>
              </p>
            </div>
          ))}
        </div>

        {/* Gate distribution */}
        {summary && (
          <div className="grid grid-cols-4 gap-4 mb-6">
            {(['PASS','CONCERNS','FAIL','WAIVED'] as const).map(g => (
              <div key={g} className="rounded-xl p-4 flex items-center gap-4"
                   style={{ background: '#12121f', border: `1px solid ${GATE_COLOR[g]}33` }}>
                <span className="text-3xl font-bold" style={{ color: GATE_COLOR[g], fontFamily: "'Space Mono',monospace" }}>
                  {summary.gate_counts?.[g] ?? 0}
                </span>
                <div>
                  <p className="font-semibold text-sm" style={{ color: GATE_COLOR[g] }}>{g}</p>
                  <p className="text-xs" style={{ color: '#94a3b8' }}>
                    {summary.total_runs ? Math.round(((summary.gate_counts?.[g] ?? 0) / summary.total_runs) * 100) : 0}% of runs
                  </p>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Trend charts */}
        {summary && summary.trend?.length >= 2 && (
          <div className="grid md:grid-cols-2 gap-4 mb-6">
            <div className="rounded-xl p-5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <div className="flex justify-between items-center mb-3">
                <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: '#64748b' }}>Pass Rate Trend</p>
                <p className="text-xs" style={{ color: '#94a3b8' }}>oldest → newest</p>
              </div>
              <Sparkline values={summary.trend.map(t => t.pass_rate)} color="#6366f1" />
            </div>
            <div className="rounded-xl p-5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <div className="flex justify-between items-center mb-3">
                <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: '#64748b' }}>Coverage % Trend</p>
                <p className="text-xs" style={{ color: '#94a3b8' }}>oldest → newest</p>
              </div>
              <Sparkline values={summary.trend.map(t => t.coverage_pct)} color="#06b6d4" />
            </div>
          </div>
        )}

        {/* Empty state */}
        {!loading && runs.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 mb-6 rounded-2xl" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="text-4xl mb-4">🏃</div>
            <h3 className="text-xl font-semibold text-white mb-2">No Test Runs Yet</h3>
            <p className="text-sm mb-6 text-center max-w-md" style={{ color: '#64748b' }}>Run your test suite to see execution history, pass/fail trends, and quality gate decisions here.</p>
            <Link href="/test-explorer" className="px-5 py-2.5 rounded-lg text-sm font-semibold transition" style={{ background: '#6366f1', color: '#fff' }}>Run Tests</Link>
          </div>
        )}

        {/* Run list + detail */}
        <div className="grid gap-4" style={{ gridTemplateColumns: selected ? '1fr 420px' : '1fr' }}>
          {/* Run table */}
          <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="grid text-xs uppercase tracking-wider px-5 py-3"
                 style={{ gridTemplateColumns: '110px 150px 1fr 90px 100px 80px 70px',
                          background: '#0d0d1a', color: '#94a3b8', borderBottom: '1px solid #1e1e3a' }}>
              <span>Run</span><span>Started</span><span>Branch · Trigger</span>
              <span>Env</span><span title="passed / failed / skipped / blocked">P / F / S / B</span><span>Duration</span><span>Gate</span>
            </div>
            {loading && (
              <div className="px-5 py-8 text-sm text-center" style={{ color: '#94a3b8' }}>Loading runs…</div>
            )}
            {!loading && runs.map(r => (
              <div key={r.id}
                   onClick={() => selectRun(r)}
                   className="grid cursor-pointer px-5 py-3 text-sm transition-colors hover:bg-opacity-50"
                   style={{
                     gridTemplateColumns: '110px 150px 1fr 90px 100px 80px 70px',
                     borderBottom: '1px solid #1e1e3a',
                     background: selected?.id === r.id ? 'rgba(99,102,241,0.1)' : undefined,
                   }}>
                <span style={{ color: '#a5b4fc', fontFamily: 'monospace', fontSize: 12 }}>{r.id}</span>
                <span style={{ color: '#94a3b8', fontSize: 11 }}>{fmtDate(r.started_at)}</span>
                <div className="min-w-0">
                  <p className="truncate text-xs" style={{ color: '#e2e8f0' }}>{r.branch}</p>
                  <p className="text-xs" style={{ color: '#94a3b8' }}>{r.trigger}</p>
                </div>
                <span className="self-center text-xs px-2 py-0.5 rounded"
                      style={{ background: r.environment === 'production' ? 'rgba(6,182,212,0.12)' : 'rgba(99,102,241,0.1)',
                               color: r.environment === 'production' ? '#22d3ee' : '#a5b4fc' }}>
                  {r.environment}
                </span>
                <div className="self-center">
                  <p style={{ fontFamily: 'monospace', fontSize: 11 }}
                     title="passed / failed / skipped / blocked">
                    <span style={{ color: '#34d399' }}>{r.passed}</span>
                    <span style={{ color: '#94a3b8' }}>/</span>
                    <span style={{ color: '#fb7185' }}>{r.failed}</span>
                    <span style={{ color: '#94a3b8' }}>/</span>
                    <span style={{ color: '#fbbf24' }}>{r.skipped}</span>
                    {/* R306.B — BLOCKED segment so P/F/S/B sums to total (was
                        hidden: run-26aa5f showed 87/70/1 for a 172-row run). */}
                    {blockedOf(r) > 0 && <>
                      <span style={{ color: '#94a3b8' }}>/</span>
                      <span style={{ color: '#c084fc' }}>{blockedOf(r)}</span>
                    </>}
                  </p>
                  <p style={{ color: '#94a3b8', fontSize: 10 }}>{passRate(r)}% pass</p>
                </div>
                <span className="self-center text-xs" style={{ color: '#94a3b8', fontFamily: 'monospace' }}>
                  {fmtDuration(r.duration_s)}
                </span>
                <span className="self-center text-xs font-semibold px-2 py-0.5 rounded"
                      style={{ background: GATE_BG[r.gate_decision] || '#1e1e3a',
                               color: GATE_COLOR[r.gate_decision] || '#94a3b8',
                               fontFamily: "'Space Mono',monospace" }}>
                  {r.gate_decision}
                </span>
              </div>
            ))}
          </div>

          {/* Detail panel */}
          {selected && (
            <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <div className="px-5 py-3 flex items-center justify-between"
                   style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}>
                <span style={{ color: '#a5b4fc', fontFamily: 'monospace', fontSize: 12 }}>{selected.id}</span>
                <div className="flex items-center gap-2">
                  {(selected.status === 'running' || selected.status === 'queued') && !watchingRunId && (
                    <button
                      onClick={() => setWatchingRunId(selected.id)}
                      className="flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-lg transition hover:opacity-90"
                      style={{ background: '#6366f1', color: '#fff' }}
                    >
                      <span className="w-1.5 h-1.5 rounded-full animate-pulse" style={{ background: '#34d399' }} />
                      Watch Live
                    </button>
                  )}
                  <span className="text-xs font-semibold px-2 py-0.5 rounded"
                        style={{ background: GATE_BG[selected.gate_decision], color: GATE_COLOR[selected.gate_decision],
                                 fontFamily: "'Space Mono',monospace" }}>
                    {selected.gate_decision}
                  </span>
                </div>
              </div>

              {/* Live execution feed */}
              {watchingRunId === selected.id && (
                <LiveExecutionFeed
                  runId={selected.id}
                  onClose={() => setWatchingRunId(null)}
                  onComplete={() => {
                    // Refresh run list and detail when execution completes
                    loadRuns()
                    fetchRunDetail(selected.id).then(setSelected).catch(() => {})
                  }}
                />
              )}
              <RunDetailContent selected={selected} isLive={watchingRunId === selected.id} />
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
