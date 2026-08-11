'use client'

import { useEffect, useState, useMemo } from 'react'
import { fetchRequirements, type Requirement } from '@/lib/api-client'
import { useProject } from '@/lib/project-context'

const PRIORITY_COLORS: Record<string, string> = {
  P0: '#ef4444', P1: '#f59e0b', P2: '#6366f1', P3: '#10b981',
}

// Risk score color for the 3×3 grid (score range 1-9)
function cellColor(score: number): string {
  if (score >= 8) return 'rgba(239,68,68,0.35)'   // red — P0 Critical
  if (score >= 6) return 'rgba(245,158,11,0.25)'   // amber — P1 High
  if (score >= 3) return 'rgba(99,102,241,0.18)'    // indigo — P2 Medium
  return 'rgba(16,185,129,0.12)'                    // teal — P3 Low
}

export default function RiskMatrixPage() {
  const { currentProjectId } = useProject()
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedCell, setSelectedCell] = useState<{ impact: number; prob: number } | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetchRequirements({ project_id: currentProjectId || undefined })
      .then(d => { setReqs(d.requirements); setLoading(false) })
      .catch(() => { setError('Failed to load requirements. Check the API connection.'); setLoading(false) })
  }, [currentProjectId])

  // Group by priority for summary cards
  const byPriority = reqs.reduce<Record<string, Requirement[]>>((acc, r) => {
    const p = r.priority || 'P3'
    ;(acc[p] = acc[p] || []).push(r)
    return acc
  }, {})

  // Memoized filtered + sorted requirement list (uses direct impact/probability, no remapping)
  const filteredReqs = useMemo(() => {
    let result = [...reqs]
    if (selectedCell) {
      result = result.filter(r => {
        const impact = r.impact || Math.ceil(Math.sqrt(r.risk_score || 5))
        const prob = r.probability || Math.ceil(Math.sqrt(r.risk_score || 5))
        return impact === selectedCell.impact && prob === selectedCell.prob
      })
    }
    return result.sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0))
  }, [reqs, selectedCell])

  return (
    <div className="p-8">
      <h1 className="text-xl font-bold mb-1">Risk Matrix</h1>
      <p className="text-sm mb-2" style={{ color: '#64748b' }}>
        Impact × Probability = Risk Score &nbsp;·&nbsp; Scale: 1–3 × 1–3 = Score 1–9
      </p>
      <p className="text-xs mb-6" style={{ color: '#94a3b8' }}>
        P0 ≥ 8 (auto-block release) · P1 6–7 · P2 4–5 · P3 ≤ 3
      </p>

      {/* Loading state */}
      {loading && (
        <div className="rounded-xl p-8 text-center mb-8" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
          <p className="text-sm animate-pulse" style={{ color: '#64748b' }}>Loading risk data…</p>
        </div>
      )}

      {/* Error state */}
      {error && !loading && (
        <div className="rounded-xl p-4 mb-8 flex items-center gap-3" style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.25)' }}>
          <span style={{ color: '#ef4444' }}>⚠</span>
          <p className="text-sm" style={{ color: '#ef4444' }}>{error}</p>
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && reqs.length === 0 && (
        <div className="rounded-xl p-8 text-center mb-8" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
          <p className="text-lg mb-2" style={{ color: '#64748b' }}>No requirements found</p>
          <p className="text-sm" style={{ color: '#94a3b8' }}>
            Add requirements in <a href="/architecture" style={{ color: '#6366f1' }}>Test Architecture</a> to see the risk matrix.
          </p>
        </div>
      )}

      {/* Priority distribution */}
      <div className="grid grid-cols-4 gap-4 mb-8">
        {['P0', 'P1', 'P2', 'P3'].map(p => (
          <div key={p} className="rounded-xl p-4 text-center"
               style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="text-3xl font-bold" style={{ color: PRIORITY_COLORS[p] }}>
              {(byPriority[p] || []).length}
            </div>
            <div className="text-xs mt-1" style={{ color: '#64748b' }}>
              {p} {p === 'P0' ? 'Critical' : p === 'P1' ? 'High' : p === 'P2' ? 'Medium' : 'Low'}
            </div>
          </div>
        ))}
      </div>

      {/* 3×3 Risk Heatmap */}
      <div className="rounded-xl p-6 mb-8" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
        <h2 className="font-semibold mb-4" style={{ color: '#94a3b8' }}>Risk Heatmap</h2>
        <div className="flex gap-6">
          {/* Y-axis label + grid */}
          <div className="flex items-stretch gap-2">
            <div className="flex flex-col justify-between py-2 text-right pr-1" style={{ width: '60px' }}>
              {[3, 2, 1].map(v => (
                <div key={v} className="text-[10px] font-mono h-20 flex items-center justify-end" style={{ color: '#64748b' }}>
                  Impact {v}
                </div>
              ))}
            </div>
            <div>
              <div className="grid grid-cols-3 gap-1.5">
                {/* 3×3 grid: row 0 = impact 3 (top), row 2 = impact 1 (bottom) */}
                {[...Array(9)].map((_, i) => {
                  const row = Math.floor(i / 3)        // 0=top, 2=bottom
                  const col = i % 3                     // 0=left, 2=right
                  const cellImpact = 3 - row            // 3 (top) → 1 (bottom)
                  const cellProb = col + 1              // 1 (left) → 3 (right)
                  const score = cellImpact * cellProb
                  const isP0 = score >= 8
                  const inCell = reqs.filter(r => {
                    const impact = r.impact || Math.ceil(Math.sqrt(r.risk_score || 5))
                    const prob = r.probability || Math.ceil(Math.sqrt(r.risk_score || 5))
                    return impact === cellImpact && prob === cellProb
                  })
                  const isSelected = selectedCell?.impact === cellImpact && selectedCell?.prob === cellProb

                  return (
                    <div key={i}
                         className="w-20 h-20 rounded-lg flex flex-col items-center justify-center cursor-pointer transition-all hover:ring-2 hover:ring-indigo-500 relative"
                         style={{
                           background: cellColor(score),
                           outline: isSelected ? '2px solid #6366f1' : 'none',
                         }}
                         title={`Impact: ${cellImpact}, Prob: ${cellProb}, Score: ${score}, ${inCell.length} requirement(s)`}
                         onClick={() => setSelectedCell(isSelected ? null : { impact: cellImpact, prob: cellProb })}>
                      <span className="text-lg font-bold" style={{ color: '#e2e8f0' }}>{score}</span>
                      {isP0 && (
                        <span className="absolute top-1 right-1 text-[9px] font-bold px-1 rounded"
                              style={{ background: 'rgba(239,68,68,0.3)', color: '#fb7185' }}>
                          P0
                        </span>
                      )}
                      {inCell.length > 0 && (
                        <span className="text-[10px] mt-0.5" style={{ color: '#94a3b8' }}>
                          {inCell.length} req{inCell.length > 1 ? 's' : ''}
                        </span>
                      )}
                    </div>
                  )
                })}
              </div>
              {/* X-axis labels */}
              <div className="grid grid-cols-3 gap-1.5 mt-1">
                {[1, 2, 3].map(v => (
                  <div key={v} className="text-[10px] font-mono text-center" style={{ color: '#64748b' }}>
                    Prob {v}
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Legend */}
          <div className="flex flex-col gap-2 ml-4 pt-2">
            {[
              { label: 'P0 ≥8  Critical', color: 'rgba(239,68,68,0.35)', text: '#ef4444', note: '100% coverage required' },
              { label: 'P1 6–7  High', color: 'rgba(245,158,11,0.25)', text: '#f59e0b', note: '≥90% coverage' },
              { label: 'P2 4–5  Medium', color: 'rgba(99,102,241,0.18)', text: '#6366f1', note: '≥75% coverage' },
              { label: 'P3 ≤3  Low', color: 'rgba(16,185,129,0.12)', text: '#10b981', note: '≥50% coverage' },
            ].map(item => (
              <div key={item.label} className="flex items-center gap-2 text-[11px]">
                <span className="w-4 h-4 rounded" style={{ background: item.color, border: `1px solid ${item.text}30` }} />
                <span style={{ color: item.text }}>{item.label}</span>
                <span className="text-[10px] ml-1" style={{ color: '#94a3b8' }}>{item.note}</span>
              </div>
            ))}
            <div className="mt-2 px-3 py-2 rounded-lg text-[10px]" style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)', color: '#fb7185' }}>
              P0 requirements auto-block releases if uncovered
            </div>
          </div>
        </div>
      </div>

      {/* Requirement risk table */}
      <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold" style={{ color: '#94a3b8' }}>
            {selectedCell ? `Requirements at Impact ${selectedCell.impact} × Prob ${selectedCell.prob} (score ${selectedCell.impact * selectedCell.prob})` : 'All Requirements by Risk'}
          </h2>
          {selectedCell && (
            <button onClick={() => setSelectedCell(null)} className="text-xs px-2 py-1 rounded" style={{ color: '#6366f1', border: '1px solid #6366f130' }}>
              Clear filter
            </button>
          )}
        </div>
        <div className="space-y-2">
          {filteredReqs.map(r => (
            <div key={r.req_id || r.id} className="flex items-center gap-4 px-4 py-3 rounded-lg"
                 style={{ background: '#0a0a14' }}>
              <span className="text-xs font-mono px-2 py-0.5 rounded"
                    style={{ background: `${PRIORITY_COLORS[r.priority]}22`, color: PRIORITY_COLORS[r.priority] }}>
                {r.priority}
              </span>
              <span className="text-sm flex-1">{r.title}</span>
              <span className="text-xs font-mono" style={{ color: '#64748b' }}>
                {r.impact || '?'}×{r.probability || '?'}
              </span>
              <span className="text-sm font-mono font-bold" style={{ color: PRIORITY_COLORS[r.priority] }}>
                {r.risk_score}
              </span>
            </div>
          ))}
          {filteredReqs.length === 0 && !loading && (
            <div className="text-sm text-center py-4" style={{ color: '#94a3b8' }}>
              {selectedCell ? 'No requirements in this cell' : 'No requirements loaded'}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
