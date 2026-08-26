'use client'

import { useEffect, useState, useCallback } from 'react'
import {
  fetchFixtureData,
  fixtureDownloadUrl,
  materialiseFixture,
  reviewFixtureData,
  type FixtureDataResponse,
  type FixtureDataset,
  type FixtureReviewResponse,
} from '@/lib/api-client'
import { useToast } from '@/components/ui/Toast'

interface Props {
  testId: string | null
  /** When true, the panel actively fetches; the parent gates this on the
   *  Test Data tab being visible to avoid wasted requests on tab switches. */
  active: boolean
}

function formatBytes(n?: number): string {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

function formatMTime(epoch?: number): string {
  if (!epoch) return ''
  const d = new Date(epoch * 1000)
  const now = Date.now()
  const ageS = Math.floor((now - d.getTime()) / 1000)
  if (ageS < 60) return `${ageS}s ago`
  if (ageS < 3600) return `${Math.floor(ageS / 60)}m ago`
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h ago`
  return d.toISOString().slice(0, 10)
}

function FormatBadge({ format }: { format: string }) {
  const palette: Record<string, { bg: string; fg: string }> = {
    parquet: { bg: 'rgba(99,102,241,0.18)', fg: '#a5b4fc' },
    csv: { bg: 'rgba(16,185,129,0.18)', fg: '#34d399' },
    tsv: { bg: 'rgba(16,185,129,0.18)', fg: '#34d399' },
    json: { bg: 'rgba(245,158,11,0.18)', fg: '#fbbf24' },
    jsonl: { bg: 'rgba(245,158,11,0.18)', fg: '#fbbf24' },
  }
  const c = palette[format] || { bg: 'rgba(148,163,184,0.18)', fg: '#94a3b8' }
  return (
    <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider"
          style={{ background: c.bg, color: c.fg }}>
      {format || 'file'}
    </span>
  )
}

function ColumnSummaryTable({ ds }: { ds: FixtureDataset }) {
  if (!ds.column_summary || ds.column_summary.length === 0) return null
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider mb-1" style={{ color: '#64748b' }}>
        Schema · {ds.column_summary.length} column{ds.column_summary.length === 1 ? '' : 's'}
      </div>
      <div className="rounded-lg overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
        <table className="w-full text-[11px]" style={{ fontFamily: 'Space Mono, monospace' }}>
          <thead>
            <tr style={{ background: '#0a0a14', color: '#94a3b8' }}>
              <th className="text-left px-3 py-1.5 font-medium">Column</th>
              <th className="text-left px-3 py-1.5 font-medium">Type</th>
              <th className="text-right px-3 py-1.5 font-medium">Null %</th>
              <th className="text-right px-3 py-1.5 font-medium">Distinct</th>
              <th className="text-right px-3 py-1.5 font-medium">Min</th>
              <th className="text-right px-3 py-1.5 font-medium">Max</th>
            </tr>
          </thead>
          <tbody>
            {ds.column_summary.map((c) => (
              <tr key={c.name} style={{ borderTop: '1px solid #1e1e3a', background: '#12121f' }}>
                <td className="px-3 py-1 text-left" style={{ color: '#e2e8f0' }}>{c.name}</td>
                <td className="px-3 py-1" style={{ color: c.dtype === 'number' ? '#a5b4fc' : '#34d399' }}>
                  {c.dtype}
                </td>
                <td className="px-3 py-1 text-right" style={{ color: '#cbd5e1' }}>{c.null_pct}%</td>
                <td className="px-3 py-1 text-right" style={{ color: '#cbd5e1' }}>{c.distinct}</td>
                <td className="px-3 py-1 text-right" style={{ color: '#94a3b8' }}>
                  {c.min != null ? String(c.min) : '—'}
                </td>
                <td className="px-3 py-1 text-right" style={{ color: '#94a3b8' }}>
                  {c.max != null ? String(c.max) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SampleRowsTable({ ds, sampleLimit }: { ds: FixtureDataset; sampleLimit: number }) {
  if (!ds.sample_rows || ds.sample_rows.length === 0 || !ds.columns) return null
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider mb-1" style={{ color: '#64748b' }}>
        Sample · first {ds.sample_rows.length} of {ds.row_count ?? '?'} row{ds.row_count === 1 ? '' : 's'}
        {ds.row_count != null && ds.row_count > sampleLimit && ` (truncated to ${sampleLimit})`}
      </div>
      <div className="rounded-lg overflow-auto" style={{ border: '1px solid #1e1e3a', maxHeight: 320 }}>
        <table className="w-full text-[11px]" style={{ fontFamily: 'Space Mono, monospace' }}>
          <thead>
            <tr style={{ background: '#0a0a14', color: '#94a3b8' }}>
              {ds.columns.map((c) => (
                <th key={c} className="text-left px-3 py-1.5 font-medium whitespace-nowrap">{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ds.sample_rows.map((row, i) => (
              <tr key={i} style={{ borderTop: '1px solid #1e1e3a', background: '#12121f' }}>
                {ds.columns!.map((c) => (
                  <td key={c} className="px-3 py-1 whitespace-nowrap"
                      style={{ color: '#e2e8f0', maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {row[c] == null ? <span style={{ color: '#475569' }}>null</span> : String(row[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function DatasetCard({
  ds,
  onMaterialise,
  materialising,
  sampleLimit,
  testId,
}: {
  ds: FixtureDataset
  onMaterialise: () => void
  materialising: boolean
  sampleLimit: number
  testId: string
}) {
  return (
    <div className="rounded-xl p-3 space-y-3"
         style={{
           background: '#0a0a14',
           border: ds.purpose === 'alternative' ? '1px dashed #1e1e3a' : '1px solid #1e1e3a',
         }}>
      {ds.purpose === 'alternative' && ds.alternative_for && (
        <div className="text-[10px]" style={{ color: '#94a3b8' }}>
          Alternative for <code style={{ color: '#cbd5e1' }}>{ds.alternative_for}</code> (actual data from the materialised sibling)
        </div>
      )}
      <div className="flex items-center gap-2 flex-wrap">
        <FormatBadge format={ds.format} />
        {ds.purpose === 'alternative' && (
          <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider"
                style={{ background: 'rgba(6,182,212,0.18)', color: '#22d3ee' }}>
            Alternate
          </span>
        )}
        <code className="text-[11px] truncate" style={{ color: '#cbd5e1', fontFamily: 'JetBrains Mono, monospace' }}>
          {ds.path}
        </code>
        {ds.exists ? (
          <>
            <span className="text-[10px]" style={{ color: '#64748b' }}>·</span>
            <span className="text-[10px]" style={{ color: '#94a3b8' }}>{formatBytes(ds.size_bytes)}</span>
            {ds.row_count != null && (
              <>
                <span className="text-[10px]" style={{ color: '#64748b' }}>·</span>
                <span className="text-[10px]" style={{ color: '#94a3b8' }}>{ds.row_count.toLocaleString()} rows</span>
              </>
            )}
            {ds.modified_at && (
              <>
                <span className="text-[10px]" style={{ color: '#64748b' }}>·</span>
                <span className="text-[10px]" style={{ color: '#64748b' }}>{formatMTime(ds.modified_at)}</span>
              </>
            )}
            <a
              href={fixtureDownloadUrl(testId, ds.path)}
              download
              className="ml-auto text-[10px] uppercase tracking-wider px-2 py-1 rounded"
              style={{
                background: 'rgba(99,102,241,0.15)',
                color: '#a5b4fc',
                border: '1px solid rgba(99,102,241,0.35)',
              }}
            >
              ↓ Download
            </a>
          </>
        ) : (
          <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded"
                style={{ background: 'rgba(245,158,11,0.18)', color: '#fbbf24' }}>
            Not yet generated
          </span>
        )}
      </div>

      {!ds.exists && (
        <div className="space-y-2">
          <div className="text-[11px]" style={{ color: '#94a3b8' }}>
            The Gherkin references this fixture but no file is on disk. Click Materialise to
            generate a deterministic dataset from the linked requirement.
          </div>
          {ds.alternatives && ds.alternatives.length > 0 && (
            <div className="text-[11px]" style={{ color: '#64748b' }}>
              <div className="mb-1" style={{ color: '#94a3b8' }}>Existing fixtures for this project:</div>
              <ul className="list-disc ml-4 space-y-0.5">
                {ds.alternatives.slice(0, 5).map((alt) => (
                  <li key={alt}><code style={{ color: '#cbd5e1' }}>{alt}</code></li>
                ))}
              </ul>
            </div>
          )}
          <button
            type="button"
            onClick={onMaterialise}
            disabled={materialising}
            className="px-3 py-1.5 rounded-lg text-[11px] font-medium"
            style={{ background: '#6366f1', color: '#fff', opacity: materialising ? 0.6 : 1 }}
          >
            {materialising ? 'Materialising…' : 'Materialise fixture'}
          </button>
        </div>
      )}

      {ds.preview_unavailable && (
        <div className="text-[11px] px-2 py-1.5 rounded"
             style={{ background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.3)', color: '#fbbf24' }}>
          {ds.message || 'Preview unavailable for this format.'}
        </div>
      )}
      {ds.error && (
        <div className="text-[11px] px-2 py-1.5 rounded"
             style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.3)', color: '#f87171' }}>
          {ds.error}
        </div>
      )}

      {ds.exists && !ds.preview_unavailable && !ds.error && (
        <>
          <ColumnSummaryTable ds={ds} />
          <SampleRowsTable ds={ds} sampleLimit={sampleLimit} />
        </>
      )}
    </div>
  )
}

export default function FrozenDatasetPanel({ testId, active }: Props) {
  const toast = useToast()
  const [data, setData] = useState<FixtureDataResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [materialisingPath, setMaterialisingPath] = useState<string | null>(null)
  // Review state — fetched on demand when the user clicks "Review match".
  const [review, setReview] = useState<FixtureReviewResponse | null>(null)
  const [reviewLoading, setReviewLoading] = useState(false)

  const load = useCallback(async () => {
    if (!testId) return
    setLoading(true)
    setError(null)
    try {
      const res = await fetchFixtureData(testId)
      setData(res)
    } catch (err: any) {
      setError(err?.message || 'Failed to load fixture data')
    } finally {
      setLoading(false)
    }
  }, [testId])

  // Load when the tab activates and whenever the selected test changes.
  // Keeping this guarded by `active` avoids fetching for tests that the
  // tester never opens the Test Data tab on.
  useEffect(() => {
    if (active && testId) load()
    if (!testId) setData(null)
  }, [active, testId, load])

  async function handleReview() {
    if (!testId) return
    setReviewLoading(true)
    try {
      const res = await reviewFixtureData(testId)
      setReview(res)
    } catch (err: any) {
      toast.error(err?.message || 'Review failed')
    } finally {
      setReviewLoading(false)
    }
  }

  async function handleMaterialise(path: string) {
    if (!testId) return
    setMaterialisingPath(path)
    try {
      await materialiseFixture(testId)
      toast.success('Fixture materialised')
      await load()
    } catch (err: any) {
      toast.error(err?.message || 'Materialise failed')
    } finally {
      setMaterialisingPath(null)
    }
  }

  if (!active) return null

  if (loading && !data) {
    return (
      <div className="text-[11px] py-3 text-center" style={{ color: '#64748b' }}>
        Loading frozen datasets…
      </div>
    )
  }
  if (error) {
    return (
      <div className="text-[11px] py-3 px-2 rounded"
           style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.3)', color: '#f87171' }}>
        {error}
      </div>
    )
  }
  if (!data || data.datasets.length === 0) {
    return null
  }

  // Verdict-pill palette — info/match/partial/mismatch.
  const verdictStyles: Record<string, { bg: string; fg: string; label: string }> = {
    match: { bg: 'rgba(16,185,129,0.18)', fg: '#34d399', label: 'MATCH' },
    partial: { bg: 'rgba(245,158,11,0.18)', fg: '#fbbf24', label: 'PARTIAL' },
    mismatch: { bg: 'rgba(239,68,68,0.18)', fg: '#f87171', label: 'MISMATCH' },
    info: { bg: 'rgba(148,163,184,0.18)', fg: '#94a3b8', label: 'INFO' },
  }
  const findingPalette: Record<string, { bg: string; fg: string }> = {
    error: { bg: 'rgba(239,68,68,0.08)', fg: '#f87171' },
    warn: { bg: 'rgba(245,158,11,0.08)', fg: '#fbbf24' },
    info: { bg: 'rgba(148,163,184,0.08)', fg: '#94a3b8' },
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-[11px] uppercase tracking-wider" style={{ color: '#94a3b8' }}>
          Frozen datasets · {data.datasets.length}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleReview}
            disabled={reviewLoading}
            className="text-[10px] px-2 py-1 rounded"
            style={{ background: 'rgba(99,102,241,0.18)', color: '#a5b4fc', border: '1px solid rgba(99,102,241,0.35)' }}
          >
            {reviewLoading ? 'Reviewing…' : 'Review match'}
          </button>
          <button
            type="button"
            onClick={() => load()}
            disabled={loading}
            className="text-[10px] px-2 py-1 rounded"
            style={{ background: '#12121f', color: '#94a3b8', border: '1px solid #1e1e3a' }}
          >
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {review && (() => {
        const v = verdictStyles[review.verdict] || verdictStyles.info
        return (
          <div className="rounded-xl p-3 space-y-2"
               style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded"
                    style={{ background: v.bg, color: v.fg }}>
                {v.label}
              </span>
              <span className="text-[10px] uppercase tracking-wider" style={{ color: '#94a3b8' }}>
                Gherkin ↔ Data
              </span>
              {review.dataset_path && (
                <code className="text-[10px] truncate"
                      style={{ color: '#64748b', fontFamily: 'JetBrains Mono, monospace' }}>
                  {review.dataset_path}
                </code>
              )}
              <button
                type="button"
                onClick={() => setReview(null)}
                className="ml-auto text-[10px]"
                style={{ color: '#64748b', background: 'none', border: 'none' }}
              >
                Dismiss
              </button>
            </div>
            <div className="text-[11px]" style={{ color: '#cbd5e1' }}>{review.summary}</div>
            {review.findings.length > 0 && (
              <ul className="space-y-1.5">
                {review.findings.map((f, i) => {
                  const fp = findingPalette[f.level] || findingPalette.info
                  return (
                    <li key={i} className="text-[11px] px-2 py-1.5 rounded"
                        style={{ background: fp.bg, color: fp.fg }}>
                      <div>{f.message}</div>
                      {f.step && (
                        <code className="text-[10px] mt-0.5 block"
                              style={{ color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>
                          {f.step}
                        </code>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        )
      })()}

      {data.datasets.map((ds) => (
        <DatasetCard
          key={ds.path}
          ds={ds}
          onMaterialise={() => handleMaterialise(ds.path)}
          materialising={materialisingPath === ds.path}
          sampleLimit={data.sample_row_limit}
          testId={testId || ''}
        />
      ))}
    </div>
  )
}
