'use client'

// FE-sync P2/P4 — surfaces the Architecture-Discovery protocol signals the backend now
// produces (architecture_discovery.py) but the dashboard had no consumer for:
//   - protocol_counts  : REST vs sse/soap/signalr/graphql distribution (= P4)
//   - non_rest_endpoints: the non-REST surface inventory
//   - protocol_warnings : "we couldn't fully inspect this" signals (e.g.
//                         graphql_introspection_failed) — a truthful SUT-coverage gap.
// Presentational + defensive: renders nothing until discovery has run.

import type { ArchitectureSummary } from '../lib/api-client'

const REST_COLOR = '#334155'
const NON_REST_COLOR = '#3f2d5c' // violet-ish for non-REST protocols

export default function ProtocolCoverage({ summary }: { summary: ArchitectureSummary | null }) {
  const counts = summary?.protocol_counts
  if (!counts || Object.keys(counts).length === 0) return null

  const nonRest = summary?.coverage_gaps?.non_rest_endpoints ?? []
  const warnings = summary?.protocol_warnings ?? []
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1])

  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide mb-1.5" style={{ color: '#64748b' }}>
        Protocol coverage
      </div>
      <div className="flex items-center gap-1.5 flex-wrap">
        {entries.map(([proto, n]) => (
          <span
            key={proto}
            className="text-[10px] px-2 py-0.5 rounded-full font-medium"
            style={{
              background: proto === 'rest' ? REST_COLOR : NON_REST_COLOR,
              color: proto === 'rest' ? '#cbd5e1' : '#ddd6fe',
            }}
            title={proto === 'rest' ? 'REST endpoints' : `${proto} (non-REST) endpoints`}
          >
            {proto}: {n}
          </span>
        ))}
      </div>

      {nonRest.length > 0 && (
        <div className="mt-1.5 text-[10px]" style={{ color: '#94a3b8' }}>
          <span style={{ color: '#ddd6fe' }}>{nonRest.length} non-REST</span>
          {': '}
          {nonRest.slice(0, 3).join(', ')}
          {nonRest.length > 3 ? ` +${nonRest.length - 3} more` : ''}
        </div>
      )}

      {warnings.length > 0 && (
        <div className="mt-1.5 space-y-1">
          {warnings.map((w, i) => (
            <div key={i} className="flex items-start gap-1.5">
              <span
                className="text-[10px] px-2 py-0.5 rounded-full font-medium shrink-0"
                style={{ background: '#3f3a1d', color: '#fde68a' }}
                title={w.signal}
              >
                {w.protocol.toUpperCase()} WARN
              </span>
              <span className="text-[10px] leading-relaxed" style={{ color: '#94a3b8' }}>
                {w.detail || w.signal}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
