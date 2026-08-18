'use client'

// R330 (SUT-Understanding P1) — makes ARTA's understanding of the SUT HONEST +
// VISIBLE: how much of the API surface it actually KNOWS (from the SUT's OpenAPI/
// source) vs merely observed vs guessed, whether source grounding is even
// available, and how many generated tests are grounded vs GUESS / potentially
// incorrect — with a CTA to fix the flagged ones (close the loop, not a dashboard).

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { fetchGroundingCoverage, type GroundingCoverage } from '@/lib/api-client'

const PROV_COLOR: Record<string, string> = {
  source_grounded: '#10b981',       // ARTA read the SUT's contract/source
  human_corrected: '#a5b4fc',       // a human verified it (R320)
  requirement_declared: '#f59e0b',  // declared, may be idealized
  observed: '#06b6d4',              // seen once at runtime
  unlabeled: '#64748b',
  guess: '#fb7185',                 // hits an endpoint ARTA never captured
  ui: '#64748b',
  unknown: '#64748b',
}
const PROV_LABEL: Record<string, string> = {
  source_grounded: 'source-grounded', human_corrected: 'human-corrected',
  requirement_declared: 'requirement-declared', observed: 'observed',
  unlabeled: 'unlabeled', guess: 'GUESS', ui: 'ui-only', unknown: 'unknown',
}

function Bar({ dist }: { dist: Record<string, number> }) {
  const total = Object.values(dist).reduce((a, b) => a + b, 0)
  if (!total) return <div className="text-xs" style={{ color: '#64748b' }}>no data</div>
  const order = ['source_grounded', 'human_corrected', 'requirement_declared', 'observed', 'unlabeled', 'guess', 'ui', 'unknown']
  const keys = order.filter(k => dist[k]).concat(Object.keys(dist).filter(k => !order.includes(k)))
  return (
    <>
      <div className="flex h-2 rounded overflow-hidden mb-2" style={{ background: '#0a0a14' }}>
        {keys.map(k => (
          <div key={k} title={`${PROV_LABEL[k] || k}: ${dist[k]}`}
               style={{ width: `${(dist[k] / total) * 100}%`, background: PROV_COLOR[k] || '#64748b' }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1">
        {keys.map(k => (
          <span key={k} className="text-[10px]" style={{ color: PROV_COLOR[k] || '#64748b' }}>
            ● {PROV_LABEL[k] || k} {dist[k]}
          </span>
        ))}
      </div>
    </>
  )
}

export default function SutUnderstandingPanel({ projectId }: { projectId: string }) {
  const [cov, setCov] = useState<GroundingCoverage | null>(null)

  useEffect(() => {
    if (!projectId) { setCov(null); return }
    fetchGroundingCoverage(projectId).then(setCov).catch(() => setCov(null))
  }, [projectId])

  if (!cov || cov.disabled) return null

  const t = cov.tests || {}
  const guessed = (t.grounded_by?.guess || 0)
  const flagged = t.potentially_incorrect_count || 0
  // P1d — distinct union from the backend (guess tests are usually also
  // flagged; the old guess+flagged sum double-counted the CTA).
  const needsFixCount = t.needs_attention_count ?? (guessed + flagged)
  const needsFix = needsFixCount > 0
  // P1d — gen-time fail-loud statuses ("unavailable:no_github_token" etc.)
  const srcStatuses = Object.entries(t.source_grounding || {}).filter(([k]) => k.startsWith('unavailable'))

  return (
    <div className="glass-card p-5">
      <div className="flex items-center gap-3 mb-3">
        <h3 className="text-sm font-semibold" style={{ color: '#e2e8f0' }}>SUT Understanding</h3>
        <span className="text-[10px]" style={{ color: '#64748b' }}>
          how well-grounded ARTA's knowledge of this SUT is
        </span>
      </div>

      {/* Source grounding availability — fail-loud, honest */}
      <div className="mb-4 text-xs p-2 rounded" style={{
        background: cov.source_grounding_available ? 'rgba(16,185,129,0.08)' : 'rgba(245,158,11,0.08)',
        border: `1px solid ${cov.source_grounding_available ? 'rgba(16,185,129,0.3)' : 'rgba(245,158,11,0.3)'}`,
        color: '#cbd5e1',
      }}>
        {cov.source_grounding_available ? '✓ ' : '⚠ '}{cov.note}
        {srcStatuses.length > 0 && (
          <div className="mt-1" style={{ color: '#fb7185' }}>
            {srcStatuses.map(([k, n]) => `${n} test${n === 1 ? '' : 's'} generated with source grounding ${k.replace('unavailable:', 'unavailable (') + ')'}`).join(' · ')}
          </div>
        )}
      </div>

      {/* Endpoint grounding — known-real vs the aspirational source-grounded target */}
      <div className="mb-4">
        <div className="flex items-baseline gap-4 mb-1">
          <span>
            <span className="text-2xl font-bold" style={{ color: cov.grounded_pct >= 60 ? '#10b981' : '#f59e0b' }}>
              {cov.grounded_pct}%
            </span>
            <span className="text-[11px] ml-1" style={{ color: '#94a3b8' }}>known-real</span>
          </span>
          <span title="Understood from the SUT's OWN source/contract — the aspirational target">
            <span className="text-lg font-bold" style={{ color: (cov.source_grounded_pct ?? 0) >= 60 ? '#10b981' : '#fb7185' }}>
              {cov.source_grounded_pct ?? 0}%
            </span>
            <span className="text-[11px] ml-1" style={{ color: '#94a3b8' }}>source-grounded</span>
          </span>
          <span className="text-[11px]" style={{ color: '#64748b' }}>of {cov.total_endpoints} endpoints</span>
        </div>
        <Bar dist={cov.by_provenance || {}} />
      </div>

      {/* Per-test grounding */}
      {t.test_count ? (
        <div className="mb-3">
          <div className="text-[11px] mb-1" style={{ color: '#94a3b8' }}>
            {t.test_count} generated tests · {flagged} potentially incorrect · traceability {t.traceability_pct ?? '—'}%
          </div>
          <Bar dist={t.grounded_by || {}} />
        </div>
      ) : (
        <div className="text-[11px] mb-3" style={{ color: '#64748b' }}>No generated tests analyzed yet.</div>
      )}

      {/* Close the loop — deep-link the EXACT flagged/guessed tests (P1d) */}
      {needsFix && (
        <Link href={`/test-explorer?flag=needs_attention&project_id=${encodeURIComponent(projectId)}`}
              className="inline-block px-3 py-1.5 rounded-lg text-xs font-medium"
              style={{ background: 'linear-gradient(135deg,#fb7185,#f97316)', color: '#fff' }}>
          {needsFixCount} tests need grounding — Refine with AI →
        </Link>
      )}
    </div>
  )
}
