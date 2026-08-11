'use client'

// F1-4: Per-trace cohort view. Shows every test produced in a single generation
// request — useful for debugging model/prompt regressions by comparing traces.

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { useParams } from 'next/navigation'

interface CohortTest {
  id: string
  title: string
  priority: string
  test_type: string
  automation_tool: string
  trace_id?: string
  model_version?: string
  prompt_version?: string
  red_phase_status?: string
  judge_score?: number | null
  dataset_version?: string
  analytics_layer?: string
  tier?: number
  adversarial_input?: string
  error_message?: string
  generation_source?: string
  last_status?: string
  avg_duration_ms?: number
  created_at?: string
  requirement_id?: string
}

interface Cohort {
  trace_id: string
  requirement_id?: string
  model_version?: string
  prompt_version?: string
  total_tests: number
  by_tool: Record<string, number>
  by_analytics_layer: Record<string, number>
  judge_score_avg?: number | null
  judge_score_count?: number
  red_phase_summary: Record<string, number>
  tests: CohortTest[]
}

const TOOL_COLORS: Record<string, string> = {
  playwright: '#6366f1',
  newman: '#34d399',
  k6: '#f59e0b',
  zap: '#f43f5e',
  pytest: '#ec4899',
  selenium: '#8b5cf6',
  cypress: '#06b6d4',
}

const STATUS_COLORS: Record<string, string> = {
  PASS: '#34d399', passed: '#34d399',
  FAIL: '#fb7185', failed: '#fb7185',
  PENDING: '#94a3b8', pending: '#94a3b8',
  SKIP: '#fbbf24', skipped: '#fbbf24',
}

function getApiKey() {
  if (typeof window === 'undefined') return 'arta-dev-key'
  return localStorage.getItem('arta_api_key') || 'arta-dev-key'
}

export default function TraceCohortPage() {
  const params = useParams<{ trace_id: string }>()
  const traceId = params?.trace_id || ''
  const [cohort, setCohort] = useState<Cohort | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | 'analytics' | 'adversarial' | 'functional'>('all')

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      try {
        const r = await fetch(`/api/traceability/trace/${encodeURIComponent(traceId)}`, {
          headers: { 'X-API-Key': getApiKey() },
        })
        if (!r.ok) {
          const body = await r.text()
          throw new Error(`HTTP ${r.status}: ${body}`)
        }
        const data = (await r.json()) as Cohort
        if (!cancelled) {
          setCohort(data)
          setError(null)
        }
      } catch (e: any) {
        if (!cancelled) setError(e.message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    if (traceId) load()
    return () => { cancelled = true }
  }, [traceId])

  const filteredTests = (() => {
    if (!cohort) return []
    if (filter === 'all') return cohort.tests
    if (filter === 'analytics') return cohort.tests.filter(t => t.analytics_layer)
    if (filter === 'adversarial') return cohort.tests.filter(t => t.test_type === 'Adversarial' || !!t.adversarial_input)
    if (filter === 'functional') return cohort.tests.filter(t => !t.analytics_layer && t.test_type !== 'Adversarial')
    return cohort.tests
  })()

  const exportJson = () => {
    if (!cohort) return
    const blob = new Blob([JSON.stringify(cohort, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `trace-${traceId.slice(0, 8)}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="min-h-screen" style={{ background: '#05050a', color: '#e2e8f0' }}>
      <header style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}
              className="px-8 py-4 flex items-center gap-4">
        <Link href="/" className="flex items-center gap-3 hover:opacity-80 transition-opacity">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center text-lg"
               style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)' }}>A</div>
          <span className="font-bold text-lg">ARTA</span>
        </Link>
        <span style={{ color: '#1e1e3a' }}>›</span>
        <Link href="/traceability" style={{ color: '#94a3b8' }} className="text-sm hover:text-white">
          Traceability
        </Link>
        <span style={{ color: '#1e1e3a' }}>›</span>
        <span className="text-sm font-mono" style={{ color: '#06b6d4' }}>
          trace: {traceId.slice(0, 8)}
        </span>
        <div className="flex-1" />
        <button onClick={() => { navigator.clipboard.writeText(traceId) }}
                title="Copy full trace_id"
                className="px-3 py-1 rounded text-[11px] transition"
                style={{ background: '#1e293b', color: '#7dd3fc', border: '1px solid #334155' }}>
          📋 Copy trace_id
        </button>
        {cohort && (
          <button onClick={exportJson}
                  className="px-3 py-1 rounded text-[11px] transition"
                  style={{ background: '#1e293b', color: '#a5b4fc', border: '1px solid #334155' }}>
            ⭳ Export JSON
          </button>
        )}
      </header>

      <main className="px-8 py-8 max-w-7xl mx-auto">
        {loading && <div className="py-16 text-center" style={{ color: '#94a3b8' }}>Loading cohort…</div>}
        {error && (
          <div className="py-8 px-6 rounded-xl" style={{ background: '#1a0a14', border: '1px solid #7f1d1d' }}>
            <h2 className="text-base font-semibold mb-2" style={{ color: '#fb7185' }}>Failed to load trace</h2>
            <p className="text-sm" style={{ color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>{error}</p>
            <Link href="/traceability" className="inline-block mt-4 text-xs" style={{ color: '#a5b4fc' }}>
              ← Back to Traceability
            </Link>
          </div>
        )}

        {cohort && !loading && (
          <>
            {/* Cohort metadata */}
            <div className="grid md:grid-cols-4 gap-4 mb-6">
              <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div className="text-xs mb-1" style={{ color: '#64748b' }}>Requirement</div>
                <div className="text-sm font-mono" style={{ color: '#06b6d4' }}>
                  {cohort.requirement_id || '—'}
                </div>
              </div>
              <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div className="text-xs mb-1" style={{ color: '#64748b' }}>Model Version</div>
                <div className="text-sm" style={{ color: '#c4b5fd', fontFamily: 'JetBrains Mono, monospace' }}>
                  {cohort.model_version || '—'}
                </div>
              </div>
              <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div className="text-xs mb-1" style={{ color: '#64748b' }}>Prompt Version</div>
                <div className="text-sm font-mono" style={{ color: '#94a3b8' }}>
                  {cohort.prompt_version ? cohort.prompt_version.slice(0, 12) : '—'}
                </div>
              </div>
              <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div className="text-xs mb-1" style={{ color: '#64748b' }}>Total Tests</div>
                <div className="text-2xl font-bold" style={{ color: '#e2e8f0' }}>
                  {cohort.total_tests}
                </div>
              </div>
            </div>

            {/* Breakdowns */}
            <div className="grid md:grid-cols-3 gap-4 mb-6">
              <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div className="text-xs mb-2" style={{ color: '#64748b' }}>By Tool</div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(cohort.by_tool).map(([tool, count]) => (
                    <span key={tool}
                          className="text-[10px] px-2 py-0.5 rounded-full font-semibold"
                          style={{ background: `${TOOL_COLORS[tool] || '#1e293b'}22`, color: TOOL_COLORS[tool] || '#94a3b8' }}>
                      {tool}: {count}
                    </span>
                  ))}
                </div>
              </div>

              <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div className="text-xs mb-2" style={{ color: '#64748b' }}>Analytics Layers</div>
                <div className="flex flex-wrap gap-2">
                  {Object.keys(cohort.by_analytics_layer).length === 0 ? (
                    <span className="text-[11px]" style={{ color: '#94a3b8' }}>—</span>
                  ) : (
                    Object.entries(cohort.by_analytics_layer).map(([layer, count]) => (
                      <span key={layer}
                            className="text-[10px] px-2 py-0.5 rounded-full"
                            style={{ background: '#1e1b3a', color: '#a5b4fc' }}>
                        {layer.replace(/_/g, '→').replace('to→', '→')}: {count}
                      </span>
                    ))
                  )}
                </div>
              </div>

              <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div className="text-xs mb-2" style={{ color: '#64748b' }}>Judge / Red-Phase</div>
                <div className="space-y-1 text-[11px]">
                  {cohort.judge_score_avg != null && (
                    <div style={{ color: '#a5b4fc' }}>
                      Judge avg: <span style={{ color: cohort.judge_score_avg >= 0.8 ? '#34d399' : cohort.judge_score_avg >= 0.5 ? '#fbbf24' : '#fb7185' }}>
                        {cohort.judge_score_avg.toFixed(2)}
                      </span>
                      <span style={{ color: '#64748b' }}> ({cohort.judge_score_count} rated)</span>
                    </div>
                  )}
                  {Object.entries(cohort.red_phase_summary).map(([status, count]) =>
                    count > 0 ? (
                      <div key={status} style={{
                        color: status === 'RED' ? '#34d399' :
                               status === 'GREEN_UNEXPECTED' ? '#fbbf24' :
                               status === 'ERROR' ? '#fb7185' : '#94a3b8'
                      }}>
                        {status}: {count}
                      </div>
                    ) : null
                  )}
                </div>
              </div>
            </div>

            {/* Filter tabs */}
            <div className="mb-3 flex gap-2 text-[11px]">
              {(['all', 'analytics', 'adversarial', 'functional'] as const).map((f) => (
                <button key={f} onClick={() => setFilter(f)}
                        className="px-3 py-1 rounded-full capitalize"
                        style={{
                          background: filter === f ? '#6366f1' : '#1e1e3a',
                          color: filter === f ? 'white' : '#94a3b8',
                          border: '1px solid ' + (filter === f ? '#6366f1' : '#1e1e3a'),
                        }}>
                  {f}
                </button>
              ))}
              <span className="ml-auto text-[11px]" style={{ color: '#64748b' }}>
                Showing {filteredTests.length} of {cohort.total_tests}
              </span>
            </div>

            {/* Tests table */}
            <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <table className="w-full text-[11px]" style={{ fontFamily: 'JetBrains Mono, monospace' }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid #1e1e3a', background: '#0a0a14' }}>
                    <th className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>Test ID</th>
                    <th className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>Title</th>
                    <th className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>Tool</th>
                    <th className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>Layer / Type</th>
                    <th className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>Tier</th>
                    <th className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>Red-Phase</th>
                    <th className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>Judge</th>
                    <th className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredTests.map((t) => (
                    <tr key={t.id} style={{ borderBottom: '1px solid #1e1e3a' }}>
                      <td className="px-3 py-2" style={{ color: '#06b6d4' }}>
                        <Link href={`/test-explorer?test_id=${encodeURIComponent(t.id)}`}
                              className="hover:underline">
                          {t.id}
                        </Link>
                      </td>
                      <td className="px-3 py-2 truncate max-w-xs" style={{ color: '#e2e8f0' }} title={t.title}>
                        {t.title}
                      </td>
                      <td className="px-3 py-2">
                        <span style={{ color: TOOL_COLORS[t.automation_tool] || '#94a3b8' }}>
                          {t.automation_tool}
                        </span>
                      </td>
                      <td className="px-3 py-2" style={{ color: '#a5b4fc' }}>
                        {t.analytics_layer ? t.analytics_layer.replace(/_/g, '→').replace('to→', '→')
                          : t.test_type === 'Adversarial' ? <span style={{ color: '#fb7185' }}>⚠ Adversarial</span>
                          : t.test_type}
                      </td>
                      <td className="px-3 py-2" style={{ color: '#7dd3fc' }}>
                        {t.tier ? `T${t.tier}` : '—'}
                      </td>
                      <td className="px-3 py-2">
                        <span style={{
                          color: t.red_phase_status === 'RED' ? '#34d399' :
                                 t.red_phase_status === 'GREEN_UNEXPECTED' ? '#fbbf24' :
                                 t.red_phase_status === 'ERROR' ? '#fb7185' : '#64748b'
                        }}>
                          {t.red_phase_status || '—'}
                        </span>
                      </td>
                      <td className="px-3 py-2">
                        {typeof t.judge_score === 'number' ? (
                          <span style={{
                            color: t.judge_score >= 0.8 ? '#34d399' :
                                   t.judge_score >= 0.5 ? '#fbbf24' : '#fb7185',
                          }}>
                            {t.judge_score.toFixed(2)}
                          </span>
                        ) : <span style={{ color: '#94a3b8' }}>—</span>}
                      </td>
                      <td className="px-3 py-2">
                        <span style={{ color: STATUS_COLORS[t.last_status || ''] || '#64748b' }}>
                          {t.last_status || 'PENDING'}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
