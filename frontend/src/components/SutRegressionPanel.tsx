'use client'

// R37.6 SUT-regression panel — extracted from the (now-consolidated)
// /quality-score page so the SUT-quality VERDICT trend (real backend
// regressions detected/ticketed/resolved, MTTR, open P0/P1) lives on the
// single canonical SUT Quality page. Reads /api/sut/quality (30d window).
import { useState, useEffect } from 'react'
import { fetchSutQualityScore, type SutQualityScore } from '@/lib/api-client'

function SutSparkline({ trend }: { trend: { date: string; new_regressions: number }[] }) {
  const W = 600, H = 80, pad = 12
  if (!trend || trend.length === 0) {
    return (
      <div className="flex items-center justify-center text-xs" style={{ height: H, color: '#64748b' }}>
        No regression data in the lookback window
      </div>
    )
  }
  const values = trend.map(t => t.new_regressions)
  const max = Math.max(1, ...values)
  const xs = trend.map((_, i) => pad + (i * (W - 2 * pad)) / Math.max(1, trend.length - 1))
  const ys = values.map(v => H - pad - ((v / max) * (H - 2 * pad)))
  const pts = xs.map((x, i) => `${x},${ys[i]}`).join(' ')
  const area = `${xs[0]},${H - pad} ${pts} ${xs[xs.length - 1]},${H - pad}`
  const tail = values.slice(-7)
  const trendUp = tail.length >= 2 && tail[tail.length - 1] > tail[0]
  const lineColor = trendUp ? '#fb7185' : '#34d399'
  const fillColor = trendUp ? 'rgba(251,113,133,0.25)' : 'rgba(52,211,153,0.20)'
  return (
    <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      <polygon points={area} fill={fillColor} />
      <polyline points={pts} fill="none" stroke={lineColor} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {xs.map((x, i) => (
        <circle key={i} cx={x} cy={ys[i]} r={2.5} fill={lineColor} stroke="#0d0d1a" strokeWidth={1} />
      ))}
    </svg>
  )
}

function SutMetricTile({ label, value, unit, color, hint }: {
  label: string; value: string | number | null; unit?: string; color: string; hint?: string
}) {
  const display = value === null || value === undefined ? '—' : value
  return (
    <div className="rounded-xl p-4" style={{ background: '#0d0d1a', border: '1px solid #1e1e3a' }}>
      <p className="text-[10px] uppercase tracking-widest mb-2" style={{ color: '#64748b' }}>{label}</p>
      <p className="text-2xl font-bold" style={{ color, fontFamily: "'Space Mono', monospace" }}>
        {display}{unit && value !== null && value !== undefined ? (
          <span className="text-sm ml-1" style={{ color: '#94a3b8' }}>{unit}</span>
        ) : null}
      </p>
      {hint && <p className="text-[10px] mt-1.5" style={{ color: '#64748b' }}>{hint}</p>}
    </div>
  )
}

export default function SutRegressionPanel({ projectId }: { projectId: string | null }) {
  const [data, setData] = useState<SutQualityScore | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!projectId) { setData(null); setLoading(false); return }
    setLoading(true); setError(null)
    fetchSutQualityScore(projectId, 30)
      .then(d => { if (!cancelled) { setData(d); setLoading(false) } })
      .catch(e => { if (!cancelled) { setError(typeof e?.message === 'string' ? e.message : 'failed'); setLoading(false) } })
    return () => { cancelled = true }
  }, [projectId])

  if (!projectId) return null

  const healthColor = data?.sut_health_pct == null ? '#94a3b8'
    : data.sut_health_pct >= 90 ? '#34d399'
    : data.sut_health_pct >= 70 ? '#fbbf24' : '#fb7185'

  return (
    <div className="rounded-xl p-6 mb-8" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      <div className="flex items-center justify-between mb-5">
        <div>
          <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: '#64748b' }}>SUT Regression Trend (R37.6)</p>
          <p className="text-xs mt-1" style={{ color: '#94a3b8' }}>
            Real backend regressions ARTA detected. Is the SUT getting better over time? (MTTR, open P0/P1, resolved).
          </p>
        </div>
        <span className="text-[10px] px-2 py-1 rounded" style={{ background: '#0a0a14', color: '#94a3b8', border: '1px solid #1e1e3a' }}>
          {data?.window_days ?? 30}d window
        </span>
      </div>
      {loading && <div className="flex items-center justify-center py-8 text-xs" style={{ color: '#94a3b8' }}>Loading SUT quality metrics…</div>}
      {error && !loading && <div className="text-xs py-4" style={{ color: '#fb7185' }}>Failed to load SUT quality: {error}</div>}
      {data && !loading && !error && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
            <SutMetricTile label="SUT Health" value={data.sut_health_pct ?? null} unit="%" color={healthColor} hint="passing / (passing + sut_regression fails)" />
            <SutMetricTile label="Open P0 Regressions" value={data.open_p0_count} color={data.open_p0_count > 0 ? '#fb7185' : '#34d399'} hint={data.open_p0_count > 0 ? 'Critical SUT bugs awaiting fix' : 'No critical bugs open'} />
            <SutMetricTile label="MTTR (p50)" value={data.mttr_hours_p50 ?? null} unit="h" color="#a5b4fc" hint="median time-to-resolve" />
            <SutMetricTile label="Fixed (window)" value={data.fixed_count} color="#34d399" hint="resolved sut_regression count" />
          </div>
          <div className="rounded-xl p-4" style={{ background: '#0d0d1a', border: '1px solid #1e1e3a' }}>
            <div className="flex items-center justify-between mb-2">
              <p className="text-[10px] uppercase tracking-widest" style={{ color: '#64748b' }}>New regressions per day</p>
              <p className="text-[10px]" style={{ color: '#94a3b8' }}>
                {data.new_regressions_count} new in last 24h · P1 open: {data.open_p1_count} · P95 MTTR: {data.mttr_hours_p95 != null ? `${data.mttr_hours_p95}h` : '—'}
              </p>
            </div>
            <SutSparkline trend={data.trend} />
          </div>
        </>
      )}
    </div>
  )
}
