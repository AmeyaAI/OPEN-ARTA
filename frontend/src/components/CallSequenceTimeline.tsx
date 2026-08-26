/**
 * Phase H2 — Call-sequence Timeline
 *
 * Renders a Gantt-style timeline of API calls within a run. For each test:
 *   - One horizontal row of step bars (width ∝ duration_ms)
 *   - Bar color: green (2xx), red (4xx/5xx), gray (cascade_skip)
 *   - Cascade-skip steps render with a dashed left border + "↳ skipped"
 *     annotation citing the missing var
 *   - Provider-contract violations get a yellow warning badge
 *   - Per-endpoint p95/p99 panel below the timeline (Phase E3 data)
 *
 * Performance: handles 200+ steps without virtualization — rows are
 * lightweight and the time axis is computed once on mount.
 */
'use client'

import { useEffect, useMemo, useState } from 'react'
import { fetchRunSteps, type RunStep } from '@/lib/api-client'

interface Props {
  runId: string
}

const STATUS_COLOR = {
  pass: '#34d399',     // emerald-400
  fail: '#f87171',     // red-400
  skip: '#64748b',     // slate-500
  pcv: '#fbbf24',      // amber-400
}

const statusBucket = (s: RunStep): keyof typeof STATUS_COLOR => {
  if (s.cascade_skip) return 'skip'
  if (s.provider_contract_violation) return 'pcv'
  if (s.status >= 400 || (s.status === 0 && s.error)) return 'fail'
  return 'pass'
}

export default function CallSequenceTimeline({ runId }: Props) {
  const [steps, setSteps] = useState<RunStep[]>([])
  const [p95, setP95] = useState<Record<string, any>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!runId) return
    let cancelled = false
    setLoading(true)
    fetchRunSteps(runId)
      .then((data) => {
        if (cancelled) return
        setSteps(data.steps || [])
        setP95(data.per_endpoint_p95 || {})
        setLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e.message || 'Failed to load run steps')
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [runId])

  // Group by test_id, preserving step order within each test.
  const grouped = useMemo(() => {
    const out: Record<string, RunStep[]> = {}
    for (const s of steps) {
      const tid = s.test_id || '(no-test)'
      if (!out[tid]) out[tid] = []
      out[tid].push(s)
    }
    for (const k of Object.keys(out)) {
      out[k].sort((a, b) => a.seq - b.seq)
    }
    return out
  }, [steps])

  // Max duration for bar-width normalization. Cap min at 50ms so 1-2ms bars
  // are still visible.
  const maxDuration = useMemo(() => {
    const m = Math.max(50, ...steps.map((s) => s.duration_ms || 0))
    return m
  }, [steps])

  if (loading) {
    return (
      <div className="text-sm" style={{ color: '#64748b' }}>
        Loading call-sequence timeline…
      </div>
    )
  }
  if (error) {
    return (
      <div className="text-sm" style={{ color: '#f87171' }}>
        {error}
      </div>
    )
  }
  if (steps.length === 0) {
    return (
      <div
        className="rounded-lg p-4 text-sm"
        style={{ background: '#0f0f23', border: '1px solid #1e1e3a', color: '#64748b' }}
      >
        No step data captured for this run. Step-level recording requires
        Phase E (chain-aware execution). Older runs don't carry it.
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div
        className="rounded-xl overflow-hidden"
        style={{ background: '#0f0f23', border: '1px solid #1e1e3a' }}
      >
        <div
          className="px-5 py-3 flex items-center justify-between"
          style={{ borderBottom: '1px solid #1e1e3a' }}
        >
          <div>
            <div className="font-semibold text-sm text-white">
              Call-sequence Timeline
            </div>
            <div className="text-xs mt-0.5" style={{ color: '#64748b' }}>
              {steps.length} steps across {Object.keys(grouped).length} tests
            </div>
          </div>
          <div className="flex items-center gap-3 text-[10px]">
            {(['pass', 'fail', 'skip', 'pcv'] as const).map((k) => (
              <div key={k} className="flex items-center gap-1.5">
                <span
                  className="w-3 h-3 rounded-sm"
                  style={{ background: STATUS_COLOR[k] }}
                />
                <span style={{ color: '#94a3b8' }}>
                  {k === 'pass' && '2xx'}
                  {k === 'fail' && '4xx/5xx'}
                  {k === 'skip' && 'cascade'}
                  {k === 'pcv' && 'contract violation'}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="px-5 py-4 space-y-3">
          {Object.entries(grouped).map(([testId, testSteps]) => (
            <div key={testId}>
              <div className="flex items-center justify-between mb-1.5">
                <div
                  className="text-xs font-medium"
                  style={{ color: '#cbd5e1', fontFamily: 'monospace' }}
                >
                  {testId}
                </div>
                <div className="text-[10px]" style={{ color: '#64748b' }}>
                  {testSteps.length} step{testSteps.length === 1 ? '' : 's'}
                </div>
              </div>
              <div className="space-y-1">
                {testSteps.map((s) => {
                  const bucket = statusBucket(s)
                  const widthPct = Math.max(
                    2,
                    Math.min(100, ((s.duration_ms || 0) / maxDuration) * 100),
                  )
                  return (
                    <div
                      key={`${testId}-${s.seq}`}
                      className="flex items-center gap-2 text-xs"
                      title={
                        s.cascade_skip
                          ? `Cascade skip: ${s.cascade_reason}`
                          : s.error
                          ? `Error: ${s.error}`
                          : `${s.status} · ${s.duration_ms}ms`
                      }
                    >
                      <div
                        className="w-12 text-right"
                        style={{ color: '#64748b', fontFamily: 'monospace' }}
                      >
                        {s.method}
                      </div>
                      <div
                        className="flex-1 min-w-0"
                        style={{
                          background: '#0a0a1c',
                          border: '1px solid #1e1e3a',
                          borderLeft: s.cascade_skip
                            ? '2px dashed ' + STATUS_COLOR.skip
                            : '1px solid #1e1e3a',
                          borderRadius: 4,
                          height: 22,
                          position: 'relative',
                        }}
                      >
                        <div
                          style={{
                            position: 'absolute',
                            top: 0,
                            left: 0,
                            height: '100%',
                            width: `${widthPct}%`,
                            background: STATUS_COLOR[bucket],
                            opacity: s.cascade_skip ? 0.3 : 0.7,
                            borderRadius: 3,
                            transition: 'width 0.2s ease',
                          }}
                        />
                        <div
                          className="absolute inset-0 flex items-center px-2 truncate"
                          style={{ color: '#e2e8f0', fontFamily: 'monospace' }}
                        >
                          {s.path}
                          {s.cascade_skip && (
                            <span
                              className="ml-2 text-[10px]"
                              style={{ color: STATUS_COLOR.skip }}
                            >
                              ↳ skipped
                            </span>
                          )}
                          {s.provider_contract_violation && (
                            <span
                              className="ml-2 text-[10px]"
                              style={{ color: STATUS_COLOR.pcv }}
                            >
                              ⚠ contract
                            </span>
                          )}
                        </div>
                      </div>
                      <div
                        className="w-16 text-right"
                        style={{ color: '#64748b', fontFamily: 'monospace' }}
                      >
                        {s.cascade_skip ? '—' : `${s.duration_ms}ms`}
                      </div>
                      <div
                        className="w-10 text-right"
                        style={{
                          color: s.status >= 400 ? STATUS_COLOR.fail : '#94a3b8',
                          fontFamily: 'monospace',
                        }}
                      >
                        {s.status || '—'}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Phase E3 — per-endpoint p95 / p99 surface */}
      {Object.keys(p95).length > 0 && (
        <div
          className="rounded-xl overflow-hidden"
          style={{ background: '#0f0f23', border: '1px solid #1e1e3a' }}
        >
          <div
            className="px-5 py-3"
            style={{ borderBottom: '1px solid #1e1e3a' }}
          >
            <div className="font-semibold text-sm text-white">
              Per-endpoint Latency
            </div>
            <div className="text-xs mt-0.5" style={{ color: '#64748b' }}>
              p50 / p95 / p99 across all observations of each endpoint.
              Surfaces "Nth call in chain breaches SLA" cases that aggregate
              metrics mask.
            </div>
          </div>
          <table className="w-full text-xs" style={{ fontFamily: 'monospace' }}>
            <thead>
              <tr style={{ color: '#64748b' }}>
                <th className="text-left px-5 py-2">Endpoint</th>
                <th className="text-right px-3 py-2">N</th>
                <th className="text-right px-3 py-2">p50</th>
                <th className="text-right px-3 py-2">p95</th>
                <th className="text-right px-3 py-2">p99</th>
                <th className="text-right px-5 py-2">Err%</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(p95)
                .sort(([, a], [, b]) => (b as any).p95 - (a as any).p95)
                .map(([key, m]) => (
                  <tr key={key} style={{ borderTop: '1px solid #1e1e3a' }}>
                    <td className="px-5 py-1.5 truncate" style={{ color: '#e2e8f0' }}>
                      {key}
                    </td>
                    <td className="text-right px-3 py-1.5" style={{ color: '#94a3b8' }}>
                      {(m as any).count}
                    </td>
                    <td className="text-right px-3 py-1.5" style={{ color: '#94a3b8' }}>
                      {(m as any).p50}ms
                    </td>
                    <td className="text-right px-3 py-1.5" style={{ color: '#cbd5e1' }}>
                      {(m as any).p95}ms
                    </td>
                    <td className="text-right px-3 py-1.5" style={{ color: '#cbd5e1' }}>
                      {(m as any).p99}ms
                    </td>
                    <td
                      className="text-right px-5 py-1.5"
                      style={{
                        color:
                          (m as any).error_rate > 0.05
                            ? STATUS_COLOR.fail
                            : '#94a3b8',
                      }}
                    >
                      {((m as any).error_rate * 100).toFixed(1)}%
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
