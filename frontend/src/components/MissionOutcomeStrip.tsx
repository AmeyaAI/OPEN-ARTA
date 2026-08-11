'use client'

// Mission Outcome strip for the canonical SUT Quality page — the latest run's
// 4-pillar report rendered as VERDICT (Pillar 4 = the SUT-quality answer) +
// CREDIBILITY (Pillars 1/1b/2 = "can I trust this verdict? were the cases
// grounded, scripts sound, the run attributable?") + ACTION (actionable_ctas).
// This is the test-quality/credibility content moved off the redirected
// /quality-score page, so consolidating loses nothing in the mission chain.
import { useState, useEffect } from 'react'
import { fetchRuns } from '@/lib/api-client'
import { authHeaders } from '@/lib/auth-state'
import { sutVerdict, attributablePassRate, a11yVerdict, actionableCtas, reportingFidelity, type MissionReport, type Tone } from '@/lib/missionReport'

const SCORE_COLOR: Record<string, { fg: string; bg: string; bd: string }> = {
  CLEAN:       { fg: '#10b981', bg: 'rgba(16,185,129,0.10)', bd: '#10b98140' },
  OPTIMISTIC:  { fg: '#06b6d4', bg: 'rgba(6,182,212,0.10)',  bd: '#06b6d440' },
  MIXED:       { fg: '#f59e0b', bg: 'rgba(245,158,11,0.10)', bd: '#f59e0b40' },
  PESSIMISTIC: { fg: '#fb7185', bg: 'rgba(251,113,133,0.10)', bd: '#fb718540' },
  UNKNOWN:     { fg: '#94a3b8', bg: 'rgba(148,163,184,0.06)', bd: '#1e1e3a' },
}
const PILLARS = [
  { key: 'pillar_1_test_case_quality',    label: 'P1 · Test Cases',   kind: 'credibility' },
  { key: 'pillar_1b_test_script_quality', label: 'P1b · Test Scripts', kind: 'credibility' },
  { key: 'pillar_2_execute_flawlessly',   label: 'P2 · Execution',    kind: 'credibility' },
  { key: 'pillar_4_report_sut_quality',   label: 'P4 · SUT Report',   kind: 'verdict' },
]
const TONE: Record<Tone, string> = { good: '#34d399', warn: '#fbbf24', bad: '#fb7185', muted: '#94a3b8' }

export default function MissionOutcomeStrip({ projectId }: { projectId: string | null }) {
  const [mr, setMr] = useState<MissionReport | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [msg, setMsg] = useState<string>('Loading latest-run mission outcome…')

  useEffect(() => {
    let cancelled = false
    if (!projectId) { setMr(null); return }
    setMr(null); setMsg('Loading latest-run mission outcome…')
    fetchRuns({ project_id: projectId })
      .then(d => {
        const runs = (d as any)?.runs || []
        if (!runs.length) { if (!cancelled) setMsg('No runs yet — dispatch a run to assess SUT quality.'); return }
        const sorted = [...runs].sort((a, b) => ((b.started_at ?? '').localeCompare(a.started_at ?? '')))
        const rid = sorted[0].run_id || sorted[0].id
        if (cancelled) return
        setRunId(rid)
        return fetch(`/api/execution/runs/${rid}/mission-report`, { headers: authHeaders() })
          .then(r => (r.ok ? r.json() : null))
          .then(j => { if (!cancelled) { if (j) setMr(j); else setMsg('Mission report unavailable for the latest run.') } })
      })
      .catch(e => { if (!cancelled) setMsg(`Mission outcome: ${String(e)}`) })
    return () => { cancelled = true }
  }, [projectId])

  if (!projectId) return null
  if (!mr) {
    return (
      <div className="rounded-xl p-6 mb-8 text-xs" style={{ background: '#12121f', border: '1px solid #1e1e3a', color: '#64748b' }}>{msg}</div>
    )
  }

  const v = sutVerdict(mr)
  const attr = attributablePassRate(mr)
  const a11y = a11yVerdict(mr)
  const rf = reportingFidelity(mr)
  const ctas = actionableCtas(mr)
  const band = (mr.outcome_band || 'MIXED').toUpperCase()
  const bandColor = SCORE_COLOR[band] || SCORE_COLOR.MIXED

  return (
    <div className="rounded-xl p-6 mb-8" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      <div className="flex items-center justify-between mb-1">
        <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: '#64748b' }}>Mission Outcome · latest run</p>
        <div className="flex items-center gap-2">
          <span className="text-[10px] px-2 py-0.5 rounded-full font-bold" style={{ color: bandColor.fg, background: bandColor.bg, border: `1px solid ${bandColor.bd}` }}>{band}</span>
          {runId && <a href={`/run-history?run_id=${runId}`} className="text-[10px]" style={{ color: '#a5b4fc' }}>full report →</a>}
        </div>
      </div>
      <p className="text-xs mb-4" style={{ color: '#94a3b8' }}>The SUT-quality VERDICT (P4) + whether to TRUST it — CREDIBILITY from the test cases, scripts &amp; execution (P1/P1b/P2).</p>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
        {PILLARS.map(p => {
          const pdata = (mr as any)[p.key] || {}
          const score = (pdata.score || 'UNKNOWN').toUpperCase()
          const c = SCORE_COLOR[score] || SCORE_COLOR.UNKNOWN
          return (
            <div key={p.key} className="rounded-lg p-3" style={{ background: '#0d0d1a', border: `1px solid ${c.bd}` }}>
              <p className="text-[10px]" style={{ color: '#64748b' }}>{p.label} · {p.kind}</p>
              <p className="text-sm font-bold mt-1" style={{ color: c.fg, fontFamily: "'Space Mono', monospace" }}>{score}</p>
            </div>
          )
        })}
      </div>
      <div className="flex flex-wrap gap-2 mb-3">
        <span className="text-[11px] px-2 py-1 rounded" style={{ background: '#0d0d1a', border: '1px solid #1e1e3a', color: attr !== null ? (attr >= 90 ? '#34d399' : '#fbbf24') : '#94a3b8' }}>
          Credibility: {attr !== null ? `${Math.round(attr)}% attributable` : 'n/a'}
        </span>
        <span className="text-[11px] px-2 py-1 rounded" style={{ background: '#0d0d1a', border: '1px solid #1e1e3a', color: TONE[a11y.tone] }}>{a11y.label}</span>
        {v.sutCritical > 0 && (
          <span className="text-[11px] px-2 py-1 rounded" style={{ background: 'rgba(251,113,133,0.1)', border: '1px solid #fb718540', color: '#fb7185' }}>{v.sutCritical} SUT-critical</span>
        )}
        {rf && (
          <span className="text-[11px] px-2 py-1 rounded" title={rf.scoreReason || undefined}
                style={{ background: '#0d0d1a', border: '1px solid #1e1e3a', color: TONE[rf.tone] }}>
            Fidelity: {rf.confidence.toUpperCase()} · {rf.adjudicationPending} adjudication-pending · {Math.round(rf.notAssessedPct * 100)}% not-assessed
          </span>
        )}
      </div>
      {ctas.length > 0 && (
        <div className="rounded-lg p-3" style={{ background: '#0d0d1a', border: '1px solid #1e1e3a' }}>
          <p className="text-[10px] uppercase tracking-widest mb-2" style={{ color: '#64748b' }}>Next steps to improve SUT quality</p>
          <ul className="space-y-1">
            {ctas.slice(0, 6).map((c, i) => (<li key={i} className="text-xs" style={{ color: '#cbd5e1' }}>› {c}</li>))}
          </ul>
        </div>
      )}
    </div>
  )
}
