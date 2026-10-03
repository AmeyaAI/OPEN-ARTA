'use client'

import { useEffect, useState, useCallback } from 'react'
import { getGenerationCoverage, type GenerationCoverage } from '@/lib/api-client'

// Persistent, project-level generation-DISPOSITION summary. Unlike the transient
// GenerateAllResultsModal (which only appears after a UI-launched job), this reads
// /api/tests/generation-coverage — an aggregation over EVERY generate-all band —
// so the breakdown is visible any time, including for API/orchestrator-driven runs.

// Shared disposition vocabulary — label + color per bucket. Exported so the
// per-requirement chips on the architecture cards render identical to this panel
// (one source of truth for the visual language).
export const DISPOSITION_META: Record<string, { label: string; color: string; help: string }> = {
  automated:       { label: 'Automated',       color: '#10b981', help: 'Has ≥1 generated Playwright/Newman spec' },
  gen_failed:      { label: 'Gen failed',      color: '#fb7185', help: '0 tests from a hard failure (e.g. timeout) — a real coverage gap' },
  not_automatable: { label: 'Not automatable', color: '#64748b', help: 'CVE-triage / dependency bump / doc-spike — verified by inspection, not a running test' },
  needs_attention: { label: 'Needs attention', color: '#f59e0b', help: 'Spec quarantined or flagged potentially-incorrect' },
  pending:         { label: 'Pending',         color: '#6366f1', help: 'Automatable, not yet generated' },
}
export const DISPOSITION_ORDER: (keyof GenerationCoverage['buckets'])[] =
  ['automated', 'gen_failed', 'not_automatable', 'needs_attention', 'pending']

export interface ActiveJobProgress { band?: string; completed: number; total: number; tests: number }

export default function GenerationCoveragePanel(
  { projectId, coverage, onSelectDisposition, activeJob }: {
    projectId: string
    coverage?: GenerationCoverage | null
    onSelectDisposition?: (key: string) => void
    activeJob?: ActiveJobProgress | null
  }
) {
  const [selfData, setSelfData] = useState<GenerationCoverage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  // Prefer the parent-provided coverage (shared with the cards); otherwise
  // self-fetch so the panel still works standalone.
  const data = coverage !== undefined ? coverage : selfData

  const load = useCallback(async () => {
    try {
      setSelfData(await getGenerationCoverage(projectId))
      setError(null)
    } catch (e: any) {
      setError(e?.message || 'failed to load')
    }
  }, [projectId])

  useEffect(() => {
    if (!projectId || coverage !== undefined) return  // parent owns the data
    load()
    // light auto-refresh so a long bulk run's numbers climb live
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [projectId, coverage, load])

  if (!projectId) return null

  const total = data?.total ?? 0
  const pct = (n: number) => (total > 0 ? Math.round((n / total) * 100) : 0)

  return (
    <div className="rounded-xl p-3.5 mb-3" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      <div className="flex items-center justify-between mb-2.5">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-white">Generation Coverage</span>
          {data && (
            <span className="text-[10px] px-2 py-0.5 rounded-full font-medium"
                  title="All requirements accounted for across the disposition buckets"
                  style={{
                    background: data.complete ? '#10b98118' : '#f59e0b18',
                    color: data.complete ? '#10b981' : '#f59e0b',
                  }}>
              {data.complete ? 'COMPLETE' : 'IN PROGRESS'} · {total} reqs
            </span>
          )}
          {activeJob && (
            <span className="text-[10px] px-2 py-0.5 rounded-full font-mono"
                  title="Live progress of the running generation band"
                  style={{ background: '#6366f118', color: '#818cf8' }}>
              {activeJob.band ? `${activeJob.band} · ` : ''}{activeJob.completed}/{activeJob.total} · {activeJob.tests} tests
            </span>
          )}
        </div>
        <button onClick={load} className="text-[10px] px-2 py-1 rounded"
                style={{ background: '#1e1e3a', color: '#94a3b8', border: '1px solid #2d2d4a' }}>
          Refresh
        </button>
      </div>

      {error && <p className="text-[11px]" style={{ color: '#fb7185' }}>Couldn’t load coverage: {error}</p>}

      {data && (
        <>
          {/* Proportion bar */}
          <div className="flex h-2 rounded-full overflow-hidden mb-3" style={{ background: '#0d0d1a' }}>
            {DISPOSITION_ORDER.map(key => {
              const m = DISPOSITION_META[key]; const n = data.buckets[key] || 0
              return n > 0 ? <div key={key} title={`${m.label}: ${n}`} style={{ width: `${pct(n)}%`, background: m.color }} /> : null
            })}
          </div>

          {/* Disposition chips */}
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
            {DISPOSITION_ORDER.map(key => {
              const m = DISPOSITION_META[key]
              const n = data.buckets[key] || 0
              const active = open === key
              return (
                <button key={key}
                        onClick={() => { onSelectDisposition?.(key); setOpen(active ? null : key) }}
                        title={`${m.help}${onSelectDisposition ? ' — click to filter the list' : ''}`}
                        className="text-left rounded-lg px-2.5 py-2 transition-colors"
                        style={{ background: active ? `${m.color}12` : '#0d0d1a',
                                 border: `1px solid ${active ? `${m.color}55` : '#1e1e3a'}` }}>
                  <div className="text-lg font-bold" style={{ color: m.color }}>{n}</div>
                  <div className="text-[10px]" style={{ color: '#94a3b8' }}>{m.label}</div>
                  <div className="text-[9px]" style={{ color: '#64748b' }}>{pct(n)}%</div>
                </button>
              )
            })}
          </div>

          {/* Drill-down samples */}
          {open && (data.samples[open]?.length ?? 0) > 0 && (
            <div className="mt-2 rounded-lg px-3 py-2 space-y-1 max-h-40 overflow-y-auto"
                 style={{ background: '#0d0d1a', border: '1px solid #1e1e3a' }}>
              <div className="text-[10px] mb-1" style={{ color: '#64748b' }}>
                Sample {DISPOSITION_META[open]?.label} requirements
                {data.buckets[open as keyof GenerationCoverage['buckets']] > (data.samples[open]?.length ?? 0)
                  ? ` (first ${data.samples[open].length})` : ''}:
              </div>
              {data.samples[open].map(s => (
                <div key={s.req_id} className="flex items-center gap-2 text-[10px]">
                  <span className="font-mono font-bold" style={{ color: '#06b6d4' }}>{s.req_id}</span>
                  {s.reason && <span style={{ color: '#64748b' }}>— {s.reason}</span>}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
