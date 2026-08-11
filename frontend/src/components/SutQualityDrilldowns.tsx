'use client'

// FE-sync P3 — run-scoped SUT-quality drill-downs the backend computes but the dashboard
// never surfaced: top 5xx endpoints, per-requirement verdict, 401 auth-scope breakdown,
// and the downloadable evidence package. Self-contained + defensive (each sub-panel hides
// when empty) so it can drop onto the run-detail view without touching its existing code.

import { useEffect, useState } from 'react'
import {
  fetchRunTopEndpoints5xx,
  fetchRunByRequirement,
  fetchRunAuthScopeSummary,
  downloadEvidencePackage,
  type RunTopEndpoints5xx,
  type RunByRequirement,
  type RunAuthScopeSummary,
} from '@/lib/api-client'

const LABEL = { color: '#64748b' }
const VAL = { color: '#e2e8f0' }

export default function SutQualityDrilldowns({ runId }: { runId: string | null | undefined }) {
  const [top5xx, setTop5xx] = useState<RunTopEndpoints5xx | null>(null)
  const [byReq, setByReq] = useState<RunByRequirement | null>(null)
  const [authScope, setAuthScope] = useState<RunAuthScopeSummary | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [dlError, setDlError] = useState<string | null>(null)

  useEffect(() => {
    if (!runId) return
    fetchRunTopEndpoints5xx(runId).then(setTop5xx).catch(() => setTop5xx(null))
    fetchRunByRequirement(runId).then(setByReq).catch(() => setByReq(null))
    fetchRunAuthScopeSummary(runId).then(setAuthScope).catch(() => setAuthScope(null))
  }, [runId])

  if (!runId) return null

  const endpoints = top5xx?.endpoints ?? []
  const reqs = (byReq?.requirements ?? []).slice(0, 8)
  const prefixes = authScope?.prefixes ?? []

  const hasAny = endpoints.length > 0 || reqs.length > 0 || prefixes.length > 0
  if (!hasAny) return null

  const handleDownload = async () => {
    if (!runId || downloading) return
    setDownloading(true)
    setDlError(null)
    try {
      await downloadEvidencePackage(runId)
    } catch {
      setDlError('Evidence package unavailable')
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="rounded-xl p-4 space-y-4" style={{ background: '#12122a', border: '1px solid #1e1e3a' }}>
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">SUT-quality drill-downs</h3>
        <button
          onClick={handleDownload}
          disabled={downloading}
          className="text-xs px-3 py-1.5 rounded-md font-medium transition-colors"
          style={{
            background: downloading ? '#1e1e3a' : 'rgba(99,102,241,0.2)',
            color: '#818cf8',
            border: '1px solid rgba(99,102,241,0.3)',
            cursor: downloading ? 'wait' : 'pointer',
          }}>
          {downloading ? 'Preparing…' : '⬇ Evidence package'}
        </button>
      </div>
      {dlError && <div className="text-[11px]" style={{ color: '#fb7185' }}>{dlError}</div>}

      {/* Top 5xx endpoints — the most actionable "fix these first" list. */}
      {endpoints.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide mb-1.5" style={LABEL}>
            Top 5xx endpoints{typeof top5xx?.arta_attributed_excluded === 'number' && top5xx.arta_attributed_excluded > 0
              ? ` · ${top5xx.arta_attributed_excluded} ARTA-attributed excluded` : ''}
          </div>
          <div className="space-y-1">
            {endpoints.map((e, i) => (
              <div key={i} className="flex items-center justify-between text-xs">
                <span className="font-mono truncate mr-3" style={VAL}>{e.endpoint_template}</span>
                <span className="shrink-0" style={{ color: '#fb7185' }}>
                  {e.count_5xx} 5xx <span style={LABEL}>/ {e.total_requests} ({e.pct_5xx}%)</span>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Per-requirement verdict. */}
      {reqs.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide mb-1.5" style={LABEL}>
            Per-requirement verdict{typeof byReq?.attributed_requirements === 'number'
              ? ` · ${byReq.attributed_requirements} attributed` : ''}
          </div>
          <div className="space-y-1">
            {reqs.map((r, i) => (
              <div key={i} className="flex items-center justify-between text-xs">
                <span className="font-mono mr-3" style={VAL}>{r.requirement_id}</span>
                <span className="shrink-0">
                  <span style={{ color: '#34d399' }}>{r.passed}P</span>{' '}
                  <span style={{ color: '#fb7185' }}>{r.failed}F</span>{' '}
                  <span style={{ color: '#fbbf24' }}>{r.blocked}B</span>{' '}
                  <span style={LABEL}>
                    {r.executed_pass_pct != null ? `${r.executed_pass_pct}% exec-pass` : '—'}
                  </span>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 401 auth-scope breakdown. */}
      {prefixes.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide mb-1.5" style={LABEL}>
            Auth scope · {authScope?.total_401s ?? 0} × 401
          </div>
          <div className="space-y-1">
            {prefixes.slice(0, 6).map((p, i) => (
              <div key={i} className="flex items-center justify-between text-xs">
                <span className="font-mono mr-3" style={VAL}>{p.prefix}</span>
                <span className="shrink-0" style={{ color: '#fbbf24' }}>{p.count} × 401</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
