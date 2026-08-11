'use client'

/**
 * R30.2 — Operator triage queue.
 *
 * Lists defects whose triage classifier (R30.1) landed in
 * `operator_review`: pattern not matched by deterministic rules OR
 * confidence < 0.7. Each row carries 3 quick-action buttons mapped
 * to the triage_category the operator picks. Decision flows into
 * the defect's metadata + status; subsequent runs use the verdict
 * to gate self-heal proposals (R30.3).
 */

import { useEffect, useState, useCallback } from 'react'
import { useSearchParams } from 'next/navigation'
import { useProject } from '@/lib/project-context'
import RunScopeSelector from '@/components/RunScopeSelector'

const API_BASE = process.env.NEXT_PUBLIC_ARTA_API_URL ?? ''

interface TriageItem {
  defect_id: string
  title: string
  test_id: string | null
  severity: string | null
  priority: string | null
  status: string | null
  triage_category: string | null
  triage_confidence: number | null
  triage_signals: string[]
  operator_triage: any
  error_message: string | null
  project_id: string | null
  run_id: string | null
  created_at: string | null
  // R69.4 — surface the subclass so we can group items
  operator_review_subclass: string | null
}

// R69.4 — group operator_review items by sub-bucket so 510 flat items
// become 4 actionable sections. Each bucket has a user-friendly label +
// recommended remediation hint.
interface TriageBucket {
  key: string
  label: string
  hint: string
  matcher: (i: TriageItem) => boolean
  items: TriageItem[]
}

function classifyBucket(items: TriageItem[]): TriageBucket[] {
  const buckets: TriageBucket[] = [
    {
      key: 'auth_scope',
      label: 'Auth scope mismatch',
      hint: 'The session cookie\'s role lacks scope for these endpoints. Update auth profile or paste a cookie with broader role.',
      matcher: i => i.operator_review_subclass === 'auth_scope_mismatch',
      items: [],
    },
    {
      key: 'spec_drift',
      label: 'Endpoint not found (spec drift)',
      hint: 'The SUT no longer serves these endpoints. Re-run discovery to refresh the captured-endpoints store, OR remove from the requirement\'s API surface.',
      matcher: i =>
        i.operator_review_subclass === 'spec_drift'
        || (i.error_message?.includes('404') ?? false),
      items: [],
    },
    {
      key: 'bad_request',
      label: 'Bad request payload',
      hint: 'The SUT rejected the request body. Likely missing required fields or wrong content type. Inspect the test\'s request body vs OpenAPI schema.',
      matcher: i => i.error_message?.includes('400') ?? false,
      items: [],
    },
    {
      key: 'other',
      label: 'Other (unclassified)',
      hint: 'Failures the deterministic classifier couldn\'t bucket. Inspect and pick the right triage action.',
      matcher: () => true,   // catches everything not matched above
      items: [],
    },
  ]
  for (const item of items) {
    for (const b of buckets) {
      if (b.matcher(item)) {
        b.items.push(item)
        break
      }
    }
  }
  return buckets.filter(b => b.items.length > 0)
}

function buildHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (typeof window !== 'undefined') {
    const tok = window.localStorage.getItem('arta_token')
    if (tok) headers['Authorization'] = `Bearer ${tok}`
  }
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY
  if (apiKey) headers['X-API-Key'] = apiKey
  return headers
}

async function fetchTriageQueue(
  projectId?: string,
  runId?: string,
): Promise<TriageItem[]> {
  // R68.3 — thread run_id into the existing endpoint param. Backend
  // `triage.list_triage_queue` (triage.py:75) already accepts run_id.
  const params = new URLSearchParams()
  if (projectId) params.set('project_id', projectId)
  if (runId) params.set('run_id', runId)
  const qs = params.toString() ? `?${params.toString()}` : ''
  const res = await fetch(`${API_BASE}/api/triage${qs}`, { headers: buildHeaders() })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  const j = await res.json()
  return j.items || []
}

async function postTriageDecision(
  defectId: string,
  triage_category: string,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/triage/${encodeURIComponent(defectId)}`, {
    method: 'POST',
    headers: buildHeaders(),
    body: JSON.stringify({ triage_category }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
}

const ACTIONS: Array<{
  category: string
  label: string
  description: string
  color: string
}> = [
  {
    category: 'test_gen_bug',
    label: 'Test-gen bug',
    description: 'Auto-heal queued; no operator ticket.',
    color: '#06b6d4',
  },
  {
    category: 'sut_regression',
    label: 'SUT regression',
    description: 'Open Defect ticket; investigate SUT.',
    color: '#fb7185',
  },
  {
    category: 'sut_contract_change',
    label: 'Contract change',
    description: 'Heal + notify operator of API drift.',
    color: '#fb923c',
  },
]

export default function TriagePage() {
  const { currentProjectId } = useProject()
  const sp = useSearchParams()
  // R68.3 — run_id is the source of truth for scope. RunScopeSelector
  // syncs URL on operator switches; on first mount (no URL param), the
  // selector auto-selects the latest completed run and the URL updates.
  const runIdFromUrl = sp?.get('run_id') || ''
  const [runId, setRunId] = useState<string>(runIdFromUrl)
  const [items, setItems] = useState<TriageItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [pendingDecisions, setPendingDecisions] = useState<Set<string>>(new Set())

  // Sync runId state with URL (browser back/forward, deep links).
  useEffect(() => { setRunId(runIdFromUrl) }, [runIdFromUrl])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const rows = await fetchTriageQueue(currentProjectId || undefined, runId || undefined)
      setItems(rows)
    } catch (e: any) {
      setError(e?.message || 'Failed to load triage queue')
    } finally {
      setLoading(false)
    }
  }, [currentProjectId, runId])

  useEffect(() => { refresh() }, [refresh])

  const handleDecision = async (defectId: string, category: string) => {
    setPendingDecisions(prev => new Set(prev).add(defectId))
    try {
      await postTriageDecision(defectId, category)
      // Optimistically remove from queue.
      setItems(prev => prev.filter(i => i.defect_id !== defectId))
    } catch (e: any) {
      setError(e?.message || 'Decision failed')
    } finally {
      setPendingDecisions(prev => {
        const next = new Set(prev)
        next.delete(defectId)
        return next
      })
    }
  }

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold mb-2" style={{ color: '#e2e8f0' }}>
          Triage Queue
        </h1>
        <p className="text-sm" style={{ color: '#94a3b8' }}>
          Failures the deterministic classifier couldn't categorize. Pick the
          right action so future runs route similar failures correctly.
        </p>
      </div>

      {/* R68.3 — run-scope. Defaults to LATEST completed run on mount. */}
      <RunScopeSelector
        runId={runId}
        onRunChange={setRunId}
        projectId={currentProjectId || undefined}
        allowAllRuns={true}
        autoSelectLatest={true}
      />

      {error && (
        <div className="mb-4 px-3 py-2 rounded-lg text-xs"
             style={{ background: 'rgba(251,113,133,0.15)', color: '#fb7185',
                      border: '1px solid rgba(251,113,133,0.2)' }}>
          {error}
        </div>
      )}

      {loading && (
        <div className="text-sm" style={{ color: '#64748b' }}>Loading triage queue…</div>
      )}

      {!loading && items.length === 0 && (
        <div className="rounded-xl p-8 text-center"
             style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
          <div className="text-base font-medium mb-2" style={{ color: '#34d399' }}>
            No failures awaiting triage
          </div>
          <div className="text-xs" style={{ color: '#94a3b8' }}>
            Either no recent runs produced ambiguous failures, or every failure
            was classified deterministically (test_gen_bug / sut_regression /
            sut_contract_change). Operators see this queue grow only when the
            triage classifier hits patterns it hasn't seen before.
          </div>
        </div>
      )}

      {/* R69.4 — group items by operator_review_subclass so the 510-noise
          flat list becomes 4 actionable buckets with per-bucket remediation
          hints. Each bucket renders a section header + the underlying items. */}
      {classifyBucket(items).map(bucket => (
        <div key={bucket.key} className="mb-8">
          <div
            className="rounded-xl px-4 py-3 mb-3 flex items-start gap-3"
            style={{
              background: 'rgba(245,158,11,0.08)',
              border: '1px solid rgba(245,158,11,0.30)',
            }}
          >
            <div className="flex-1">
              <p className="text-sm font-semibold" style={{ color: '#f59e0b' }}>
                {bucket.label} <span className="opacity-70">({bucket.items.length})</span>
              </p>
              <p className="text-[11px] mt-1" style={{ color: '#94a3b8' }}>
                {bucket.hint}
              </p>
            </div>
          </div>
          <div className="space-y-3">
            {bucket.items.map(item => {
              const pending = pendingDecisions.has(item.defect_id)
              return (
                <div key={item.defect_id}
                     className="rounded-xl p-4"
                     style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1.5">
                        <code className="text-[10px] font-mono px-1.5 py-0.5 rounded"
                              style={{ background: 'rgba(99,102,241,0.15)', color: '#818cf8' }}>
                          {item.defect_id}
                        </code>
                    {item.test_id && (
                      <code className="text-[10px] font-mono" style={{ color: '#06b6d4' }}>
                        {item.test_id}
                      </code>
                    )}
                    {item.severity && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded font-semibold uppercase"
                            style={{
                              background: 'rgba(251,191,36,0.15)',
                              color: '#fbbf24',
                            }}>
                        {item.severity}
                      </span>
                    )}
                  </div>
                  <div className="text-sm font-medium mb-2" style={{ color: '#e2e8f0' }}>
                    {item.title || '(untitled)'}
                  </div>
                  {item.error_message && (
                    <pre className="text-[11px] font-mono px-2 py-1.5 rounded mb-2 whitespace-pre-wrap break-words"
                         style={{
                           background: '#12121f',
                           color: '#94a3b8',
                           maxHeight: 120,
                           overflowY: 'auto',
                         }}>
                      {item.error_message.slice(0, 600)}
                      {item.error_message.length > 600 ? '…' : ''}
                    </pre>
                  )}
                  {item.triage_signals && item.triage_signals.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1">
                      {item.triage_signals.map(s => (
                        <span key={s} className="text-[9px] font-mono px-1.5 py-0.5 rounded"
                              style={{ background: 'rgba(100,116,139,0.15)', color: '#94a3b8' }}>
                          {s}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                <div className="flex flex-col gap-2 shrink-0" style={{ minWidth: 180 }}>
                  {ACTIONS.map(a => (
                    <button
                      key={a.category}
                      onClick={() => handleDecision(item.defect_id, a.category)}
                      disabled={pending}
                      className="text-left px-3 py-2 rounded-lg text-xs"
                      style={{
                        background: pending ? '#12121f' : `${a.color}15`,
                        color: pending ? '#64748b' : a.color,
                        border: `1px solid ${a.color}40`,
                        cursor: pending ? 'wait' : 'pointer',
                      }}
                      title={a.description}>
                      <div className="font-semibold">{a.label}</div>
                      <div className="text-[10px] opacity-80 mt-0.5">{a.description}</div>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}
