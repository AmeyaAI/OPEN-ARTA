'use client'

import { useState } from 'react'
import type { GenerateAllJob } from '@/lib/api-client'

// F19-6: `axe` is the accessibility tool. Without it in the label/color
// maps, the badge silently disappears from the modal even though
// generation succeeded — masking the F19-4 wiring fix.
const TOOL_LABELS: Record<string, string> = {
  playwright: 'E2E UI', newman: 'API', k6: 'Performance', zap: 'Security',
  pytest: 'Analytics', axe: 'Accessibility',
}
const TOOL_COLORS: Record<string, string> = {
  playwright: '#6366f1', newman: '#06b6d4', k6: '#f59e0b', zap: '#fb7185',
  pytest: '#8b5cf6', axe: '#10b981',
}

interface Props {
  open: boolean
  onClose: () => void
  job: GenerateAllJob | null
  onRetry: (jobId: string, fromRequirement?: string) => void
  onRegenerate: (requirementId: string, feedback?: string) => void
}

export default function GenerateAllResultsModal({ open, onClose, job, onRetry, onRegenerate }: Props) {
  const [expandedReq, setExpandedReq] = useState<string | null>(null)
  const [feedbackText, setFeedbackText] = useState<Record<string, string>>({})

  if (!open || !job) return null

  // P3 (truthful reporting) — an empty tools map ({}) made `[].every()`
  // vacuously TRUE, so zero-test reqs were miscounted as "Complete" (green) and
  // the header read "COMPLETE / 0 failures" while 10 reqs actually generated 0
  // tests. Require ≥1 tool AND all generated for Complete; surface coverage gaps.
  const isGap = (r: any) =>
    r?.coverage_gap || r?.gen_failed ||
    r?.status === 'completed_no_tests' || r?.status === 'gen_failed' ||
    Object.keys(r?.tools || {}).length === 0
  const successCount = job.results.filter(r => {
    const vals = Object.values(r.tools || {})
    return vals.length > 0 && vals.every(t => t.status === 'generated')
  }).length
  const partialCount = job.results.filter(r => {
    const vals = Object.values(r.tools || {})
    return vals.length > 0 &&
      vals.some(t => t.status === 'generated') &&
      (vals.some(t => t.status === 'failed') || vals.length < 5)
  }).length
  const coverageGapCount = job.results.filter(r => r.status !== 'skipped' && isGap(r)).length
  const failedCount = job.failed
  const skippedCount = job.results.filter(r => r.status === 'skipped').length

  const rateLimitActive = job.rate_limit_reset && job.rate_limit_reset * 1000 > Date.now()

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
         onClick={onClose}>
      <div className="w-[720px] max-h-[80vh] rounded-2xl overflow-hidden flex flex-col"
           style={{ background: '#12121f', border: '1px solid #1e1e3a' }}
           onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div className="px-6 py-4 flex items-center justify-between"
             style={{ borderBottom: '1px solid #1e1e3a' }}>
          <div>
            <h2 className="text-lg font-semibold text-white">Generation Results</h2>
            <p className="text-xs" style={{ color: '#64748b' }}>
              Job {job.job_id} — {job.started_at ? new Date(job.started_at).toLocaleString() : ''}
              {job.retry_of && <span className="ml-2 text-amber-400">(retry of {job.retry_of})</span>}
            </p>
          </div>
          {(() => {
            // F12-5: distinguish "interrupted but kept N tests" from "interrupted with zero progress"
            // P3 (truthful reporting) — don't show green COMPLETE when reqs failed
            // to generate or have coverage gaps; surface them in the badge.
            const isComplete = job.status === 'completed' && failedCount === 0 && coverageGapCount === 0
            const completeWithGaps = job.status === 'completed' && (failedCount > 0 || coverageGapCount > 0)
            const interruptedWithProgress = job.status === 'interrupted' && (job.total_tests_generated ?? 0) > 0
            const interruptedZero = job.status === 'interrupted' && (job.total_tests_generated ?? 0) === 0
            let label: string
            let colorBase: string
            if (isComplete) {
              label = 'COMPLETE'; colorBase = '#10b981'
            } else if (completeWithGaps) {
              label = `COMPLETE · ${failedCount} FAILED · ${coverageGapCount} GAP`; colorBase = '#f59e0b'
            } else if (interruptedZero) {
              label = 'INTERRUPTED'; colorBase = '#fb7185'  // genuine zero-progress red
            } else if (interruptedWithProgress) {
              label = 'PARTIAL (RESTARTED)'; colorBase = '#f59e0b'  // amber — real work retained
            } else {
              label = 'PARTIAL'; colorBase = '#f59e0b'
            }
            const tooltip = interruptedWithProgress
              ? `Server restarted mid-run. ${job.total_tests_generated} tests retained from before the restart. Click Retry Failed to continue.`
              : undefined
            return (
              <span className="text-xs px-2 py-1 rounded-full font-medium"
                    title={tooltip}
                    style={{
                      background: `${colorBase}20`,
                      color: colorBase,
                      border: `1px solid ${colorBase}40`,
                    }}>
                {label}
              </span>
            )
          })()}
        </div>

        {/* Rate limit banner */}
        {rateLimitActive && (
          <div className="px-6 py-2 text-xs font-medium"
               style={{ background: '#f59e0b15', color: '#f59e0b', borderBottom: '1px solid #f59e0b30' }}>
            Rate limited. Resets at {new Date(job.rate_limit_reset! * 1000).toLocaleTimeString()}
          </div>
        )}

        {/* Summary strip */}
        <div className="px-6 py-3 flex items-center gap-4" style={{ borderBottom: '1px solid #1e1e3a' }}>
          <div className="flex items-center gap-1.5">
            <span className="text-lg font-bold" style={{ color: '#10b981' }}>{successCount}</span>
            <span className="text-xs" style={{ color: '#64748b' }}>Complete</span>
          </div>
          {partialCount > 0 && (
            <div className="flex items-center gap-1.5">
              <span className="text-lg font-bold" style={{ color: '#f59e0b' }}>{partialCount}</span>
              <span className="text-xs" style={{ color: '#64748b' }}>Partial</span>
            </div>
          )}
          {failedCount > 0 && (
            <div className="flex items-center gap-1.5">
              <span className="text-lg font-bold" style={{ color: '#fb7185' }}>{failedCount}</span>
              <span className="text-xs" style={{ color: '#64748b' }}>Failed</span>
            </div>
          )}
          {skippedCount > 0 && (
            <div className="flex items-center gap-1.5">
              <span className="text-lg font-bold" style={{ color: '#94a3b8' }}>{skippedCount}</span>
              <span className="text-xs" style={{ color: '#64748b' }}>Skipped</span>
            </div>
          )}
          <span className="text-xs ml-auto" style={{ color: '#94a3b8' }}>
            {job.total_tests_generated} tests generated
          </span>
        </div>

        {/* Requirement list */}
        <div className="flex-1 overflow-y-auto px-6 py-3 space-y-2">
          {/* Successful + Partial results */}
          {job.results.map(r => {
            const tools = r.tools || {}
            const isExpanded = expandedReq === r.requirement_id
            const hasFailed = Object.values(tools).some(t => t.status === 'failed')

            return (
              <div key={r.requirement_id} className="rounded-lg overflow-hidden"
                   style={{ border: '1px solid #1e1e3a' }}>
                {/* Row header */}
                <button className="w-full px-4 py-2.5 flex items-center gap-3 text-left hover:bg-white/[0.02] transition-colors"
                        onClick={() => setExpandedReq(isExpanded ? null : r.requirement_id)}>
                  <span className="text-xs font-mono font-bold" style={{ color: '#6366f1' }}>{r.requirement_id}</span>
                  <div className="flex items-center gap-1.5 flex-1">
                    {Object.entries(tools).map(([tool, detail]) => (
                      <span key={tool} className="text-[10px] px-1.5 py-0.5 rounded"
                            style={{
                              background: detail.status === 'generated' ? `${TOOL_COLORS[tool] || '#6366f1'}20` : '#fb718515',
                              color: detail.status === 'generated' ? (TOOL_COLORS[tool] || '#6366f1') : '#fb7185',
                              border: `1px solid ${detail.status === 'generated' ? `${TOOL_COLORS[tool] || '#6366f1'}40` : '#fb718530'}`,
                            }}>
                        {detail.status === 'generated' ? '✓' : '✗'} {TOOL_LABELS[tool] || tool} {detail.test_count > 0 ? `(${detail.test_count})` : ''}
                      </span>
                    ))}
                    {/* P3 — zero-test reqs rendered blank pre-fix; show the gap + its cause */}
                    {Object.keys(tools).length === 0 && r.status !== 'skipped' && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded"
                            style={{ background: '#fb718515', color: '#fb7185', border: '1px solid #fb718530' }}>
                        ⚠ 0 tests{(r as any).coverage_gap_cause ? `: ${(r as any).coverage_gap_cause}` : ''}
                      </span>
                    )}
                  </div>
                  {r.status === 'skipped' && <span className="text-[10px]" style={{ color: '#94a3b8' }}>up to date</span>}
                  <span className="text-[10px]" style={{ color: '#94a3b8' }}>{isExpanded ? '▲' : '▼'}</span>
                </button>

                {/* Expanded detail */}
                {isExpanded && (
                  <div className="px-4 py-3 space-y-2" style={{ background: '#0d0d1a', borderTop: '1px solid #1e1e3a' }}>
                    {Object.entries(tools).map(([tool, detail]) => (
                      <div key={tool} className="flex items-center gap-3 text-xs">
                        <span className="w-24 font-medium" style={{ color: TOOL_COLORS[tool] || '#94a3b8' }}>
                          {TOOL_LABELS[tool] || tool}
                        </span>
                        <span style={{ color: detail.status === 'generated' ? '#10b981' : '#fb7185' }}>
                          {detail.status === 'generated' ? `${detail.test_count} tests` : detail.error || 'Failed'}
                        </span>
                        {detail.ac_covered && detail.ac_covered.length > 0 && (
                          <span style={{ color: '#94a3b8' }}>
                            ({detail.ac_covered.length} ACs covered)
                          </span>
                        )}
                      </div>
                    ))}

                    {/* Actions */}
                    <div className="flex items-center gap-2 pt-2" style={{ borderTop: '1px solid #1e1e3a' }}>
                      {hasFailed && (
                        <button onClick={() => onRetry(job.job_id, r.requirement_id)}
                                className="text-[10px] px-2 py-1 rounded transition-colors"
                                style={{ background: '#f59e0b20', color: '#f59e0b', border: '1px solid #f59e0b30' }}>
                          Retry Failed Tools
                        </button>
                      )}
                      <button onClick={() => {
                                const fb = feedbackText[r.requirement_id]
                                onRegenerate(r.requirement_id, fb)
                              }}
                              className="text-[10px] px-2 py-1 rounded transition-colors"
                              style={{ background: '#6366f115', color: '#6366f1', border: '1px solid #6366f130' }}>
                        Regenerate
                      </button>
                      <input type="text" placeholder="Feedback (optional)..."
                             value={feedbackText[r.requirement_id] || ''}
                             onChange={e => setFeedbackText(prev => ({ ...prev, [r.requirement_id]: e.target.value }))}
                             className="flex-1 text-[10px] px-2 py-1 rounded bg-transparent outline-none"
                             style={{ border: '1px solid #1e1e3a', color: '#94a3b8' }} />
                    </div>
                  </div>
                )}
              </div>
            )
          })}

          {/* Error rows */}
          {job.errors.map(e => (
            <div key={e.requirement_id} className="rounded-lg px-4 py-2.5 flex items-center gap-3"
                 style={{ background: '#fb718508', border: '1px solid #fb718520' }}>
              <span className="text-xs font-mono font-bold" style={{ color: '#fb7185' }}>{e.requirement_id}</span>
              <span className="text-[10px] flex-1" style={{ color: '#fb7185' }}>
                {e.is_rate_limited ? '⏳ Rate limited' : e.error?.substring(0, 80)}
              </span>
              <button onClick={() => onRetry(job.job_id, e.requirement_id)}
                      className="text-[10px] px-2 py-1 rounded"
                      style={{ background: '#f59e0b20', color: '#f59e0b', border: '1px solid #f59e0b30' }}>
                Retry
              </button>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 flex items-center justify-between"
             style={{ borderTop: '1px solid #1e1e3a' }}>
          <div className="flex items-center gap-2">
            {(failedCount > 0 || partialCount > 0) && (
              <button onClick={() => onRetry(job.job_id)}
                      className="text-xs px-3 py-1.5 rounded-lg font-medium transition-colors"
                      style={{ background: 'linear-gradient(135deg, #f59e0b, #d97706)', color: '#fff' }}>
                Retry All Failed
              </button>
            )}
            {job.errors.length > 0 && (
              <button onClick={() => onRetry(job.job_id, job.errors[0]?.requirement_id)}
                      className="text-xs px-3 py-1.5 rounded-lg font-medium transition-colors"
                      style={{ background: '#1e1e3a', color: '#94a3b8', border: '1px solid #2d2d4a' }}>
                Continue From First Failed
              </button>
            )}
          </div>
          <button onClick={onClose}
                  className="text-xs px-3 py-1.5 rounded-lg"
                  style={{ background: '#1e1e3a', color: '#94a3b8', border: '1px solid #2d2d4a' }}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
