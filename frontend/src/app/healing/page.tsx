'use client'

import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import {
  fetchHealingStats,
  fetchHealingQueue,
  fetchHealingProposal,
  approveHealingProposal,
  rejectHealingProposal,
  runHealingFix,
  getHealingRunResult,
  type HealingStats,
  type HealingProposal,
} from '@/lib/api-client'
import { useProject } from '@/lib/project-context'
import { useTasks } from '@/lib/task-context'
import RunScopeSelector from '@/components/RunScopeSelector'

export default function HealingPage() {
  const { currentProjectId, isLoading: projectLoading } = useProject()
  const sp = useSearchParams()
  // R68.4 — run_id is the source of truth for scope. Auto-selected to
  // latest completed run by <RunScopeSelector> on mount (URL syncs).
  const runIdFromUrl = sp?.get('run_id') || ''
  const [runId, setRunId] = useState<string>(runIdFromUrl)
  useEffect(() => { setRunId(runIdFromUrl) }, [runIdFromUrl])
  const [stats, setStats] = useState<HealingStats | null>(null)
  const [queue, setQueue] = useState<HealingProposal[]>([])
  const [pending, setPending] = useState(0)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<HealingProposal | null>(null)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [runStatus, setRunStatus] = useState<Record<string, 'idle' | 'running' | 'pass' | 'fail'>>({})
  const [analyzingIds, setAnalyzingIds] = useState<Set<string>>(new Set())
  const [actionResult, setActionResult] = useState<{ id: string; type: 'approved' | 'rejected' | 'error'; message: string } | null>(null)

  // F15-5: shared mount-state ref so async pollers know when to stop.
  // Flipped to false in the unmount effect at the bottom.
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // F15-5: poll with abort + max-duration. Returns "pass"|"fail"|"timeout".
  // - "timeout" means the run is still going after MAX_ATTEMPTS — we toast a
  //   warning and let the user check Run History instead of falsely marking
  //   the test as failed.
  // - Honors mountedRef so a navigation-away doesn't write to unmounted state.
  const _pollHealRun = async (id: string): Promise<'pass' | 'fail' | 'timeout'> => {
    const MAX_ATTEMPTS = 40   // ~2 minutes at 3s intervals (was 20 / ~60s)
    for (let i = 0; i < MAX_ATTEMPTS; i++) {
      if (!mountedRef.current) return 'timeout'
      await new Promise(r => setTimeout(r, 3000))
      if (!mountedRef.current) return 'timeout'
      try {
        const result = await getHealingRunResult(id)
        if (result && result.status !== 'running') {
          return result.status as 'pass' | 'fail'
        }
      } catch { /* keep polling */ }
    }
    return 'timeout'
  }

  const handleRunWithFix = async (id: string) => {
    setRunStatus(prev => ({ ...prev, [id]: 'running' }))
    sessionStorage.setItem('arta_healing_running', id)
    try {
      await runHealingFix(id)
    } catch {
      if (mountedRef.current) {
        setRunStatus(prev => ({ ...prev, [id]: 'fail' }))
      }
      sessionStorage.removeItem('arta_healing_running')
      return
    }
    const outcome = await _pollHealRun(id)
    if (!mountedRef.current) return
    sessionStorage.removeItem('arta_healing_running')
    if (outcome === 'timeout') {
      // F15-5: Don't lie — show a warning and leave status as 'running'.
      // The next page mount will resume polling via the effect below.
      setActionResult({
        id, type: 'error',
        message: 'Healing run still in progress after 2 minutes. Check Run History for status.',
      })
    } else {
      setRunStatus(prev => ({ ...prev, [id]: outcome }))
    }
  }

  // Resume healing fix polling on page mount
  useEffect(() => {
    const runningId = sessionStorage.getItem('arta_healing_running')
    if (!runningId) return
    setRunStatus(prev => ({ ...prev, [runningId]: 'running' }))
    ;(async () => {
      const outcome = await _pollHealRun(runningId)
      if (!mountedRef.current) return
      sessionStorage.removeItem('arta_healing_running')
      if (outcome !== 'timeout') {
        setRunStatus(prev => ({ ...prev, [runningId]: outcome }))
      }
    })()
  }, [])

  const pollUntilReady = async (id: string) => {
    setAnalyzingIds(prev => new Set(prev).add(id))
    for (let i = 0; i < 15; i++) {
      await new Promise(r => setTimeout(r, 2000))
      try {
        const updated = await fetchHealingProposal(id)
        if (!(updated as any)._analyzing) {
          setAnalyzingIds(prev => { const s = new Set(prev); s.delete(id); return s })
          // Refresh queue and update selected if it's the same proposal
          const [s, q] = await Promise.all([
            fetchHealingStats(currentProjectId || undefined),
            fetchHealingQueue(undefined, currentProjectId || undefined),
          ])
          setStats(s)
          setQueue(q.proposals)
          setPending(q.pending)
          setSelected(prev => prev?.id === id ? (updated as HealingProposal) : prev)
          return
        }
      } catch { /* keep polling */ }
    }
    setAnalyzingIds(prev => { const s = new Set(prev); s.delete(id); return s })
  }

  const load = () => {
    setLoading(true)
    // R68.4 — thread runId so both endpoints filter to the same scope.
    Promise.all([
      fetchHealingStats(currentProjectId || undefined, runId || undefined),
      fetchHealingQueue(undefined, currentProjectId || undefined, runId || undefined),
    ])
      .then(([s, q]) => {
        setStats(s)
        setQueue(q.proposals)
        setPending(q.pending)
        const analyzing = q.proposals.filter((p: any) => p._analyzing)
        analyzing.forEach((p: any) => pollUntilReady(p.id))
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => { if (!projectLoading) load() }, [currentProjectId, projectLoading, runId])

  void useTasks() // retained for task context availability (subscribes to context)

  const handleApprove = async (id: string) => {
    setActionLoading(id)
    setActionResult(null)
    try {
      const res = await approveHealingProposal(id)
      const fileUpdated = (res as any)?.file_updated
      setActionResult({
        id, type: 'approved',
        message: fileUpdated
          ? 'Fix approved and applied to the test file.'
          : 'Fix approved. Test file could not be updated automatically.',
      })
      load()
    } catch {
      setActionResult({ id, type: 'error', message: 'Approval failed. Please try again.' })
    }
    setActionLoading(null)
  }

  const handleReject = async (id: string) => {
    setActionLoading(id)
    setActionResult(null)
    try {
      await rejectHealingProposal(id)
      setActionResult({ id, type: 'rejected', message: 'Proposal rejected and removed from queue.' })
      load()
    } catch {
      setActionResult({ id, type: 'error', message: 'Rejection failed. Please try again.' })
    }
    setActionLoading(null)
  }

  const confidenceColor = (c: number) =>
    c >= 0.85 ? '#10b981' : c >= 0.6 ? '#f59e0b' : '#ef4444'

  const strategyLabel: Record<string, string> = {
    selector_update: 'Selector Update',
    wait_selector: 'Wait Strategy',
    threshold_adjustment: 'Threshold Adjust',
  }

  return (
    <div className="min-h-screen" style={{ background: '#05050a', color: '#e2e8f0' }}>
      <header style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}
              className="px-8 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <a href="/" className="w-8 h-8 rounded-lg flex items-center justify-center text-lg"
             style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)' }}>A</a>
          <div>
            <h1 className="font-bold text-lg leading-none">Self-Healing</h1>
            <p className="text-xs" style={{ color: '#6366f1' }}>AI-Powered Test Maintenance</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs px-2 py-1 rounded-full" style={{ background: '#1e1e3a', color: '#f59e0b' }}>
            {pending} pending
          </span>
          <a href="/" className="text-xs px-3 py-1.5 rounded-lg" style={{ background: '#1e1e3a', color: '#94a3b8' }}>
            Dashboard
          </a>
        </div>
      </header>

      <main className="px-8 py-8 max-w-6xl mx-auto">
        {/* R68.4 — run-scope selector. Defaults to LATEST completed run. */}
        <RunScopeSelector
          runId={runId}
          onRunChange={setRunId}
          projectId={currentProjectId || undefined}
          allowAllRuns={true}
          autoSelectLatest={true}
        />

        {/* Stats cards */}
        {stats && (
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4 mb-8">
            {[
              { label: 'Total Healed', value: stats.total_healed, color: '#10b981' },
              { label: 'Approval Rate', value: `${stats.approval_rate}%`, color: '#6366f1' },
              { label: 'Avg Confidence', value: `${Math.round(stats.avg_confidence * 100)}%`, color: '#06b6d4' },
              { label: 'Hours Saved', value: `${stats.hours_saved}h`, color: '#8b5cf6' },
              { label: 'Maint. Reduction', value: `${stats.maintenance_reduction_pct}%`, color: '#f59e0b' },
              { label: 'Pending', value: stats.total_pending, color: '#ef4444' },
            ].map(m => (
              <div key={m.label} className="rounded-xl p-4"
                   style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <p className="text-xs mb-1" style={{ color: '#64748b' }}>{m.label}</p>
                <p className="text-2xl font-bold" style={{ color: m.color }}>{m.value}</p>
              </div>
            ))}
          </div>
        )}

        {/* Charts */}
        {stats && (
          <div className="grid md:grid-cols-2 gap-6 mb-8">
            {/* Healing trend */}
            <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <h2 className="font-semibold mb-4" style={{ color: '#94a3b8' }}>Healing Trends (7 days)</h2>
              <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={stats.weekly_trends}>
                  <defs>
                    <linearGradient id="gHealed" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#10b981" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="gRejected" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#ef4444" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="date" tick={{ fill: '#94a3b8', fontSize: 10 }} axisLine={false} tickLine={false}
                         tickFormatter={(v: string) => v.slice(5)} />
                  <YAxis tick={{ fill: '#94a3b8', fontSize: 11 }} axisLine={false} tickLine={false} width={25} />
                  <Tooltip
                    contentStyle={{ background: '#1e1e3a', border: '1px solid #2d2d4a', borderRadius: 8, color: '#e2e8f0', fontSize: 12 }}
                    labelStyle={{ color: '#94a3b8' }}
                  />
                  <Area type="monotone" dataKey="healed" stroke="#10b981" fill="url(#gHealed)" strokeWidth={2} name="Healed" />
                  <Area type="monotone" dataKey="rejected" stroke="#ef4444" fill="url(#gRejected)" strokeWidth={2} name="Rejected" />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Confidence trend + strategy breakdown */}
            <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <h2 className="font-semibold mb-4" style={{ color: '#94a3b8' }}>Strategy Breakdown</h2>
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={Object.entries(stats.strategy_breakdown).map(([k, v]) => ({
                  name: strategyLabel[k] || k, count: v,
                }))}>
                  <XAxis dataKey="name" tick={{ fill: '#94a3b8', fontSize: 10 }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fill: '#94a3b8', fontSize: 11 }} axisLine={false} tickLine={false} width={25} />
                  <Tooltip
                    contentStyle={{ background: '#1e1e3a', border: '1px solid #2d2d4a', borderRadius: 8, color: '#e2e8f0', fontSize: 12 }}
                    labelStyle={{ color: '#94a3b8' }}
                  />
                  <Bar dataKey="count" fill="#8b5cf6" radius={[4, 4, 0, 0]} name="Healed" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* Approval Queue */}
        <div className="grid md:grid-cols-3 gap-6">
          <div className={`${selected ? 'md:col-span-2' : 'md:col-span-3'}`}>
            <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <h2 className="font-semibold mb-4" style={{ color: '#94a3b8' }}>
                Approval Queue ({queue.length})
              </h2>
              {loading && <p className="text-sm" style={{ color: '#94a3b8' }}>Loading...</p>}
              <div className="space-y-2">
                {queue.map(p => (
                  <button
                    key={p.id}
                    onClick={() => setSelected(p)}
                    className={`w-full text-left px-4 py-3 rounded-lg transition-colors ${selected?.id === p.id ? 'ring-1 ring-indigo-500' : ''}`}
                    style={{ background: '#0a0a14' }}
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-mono" style={{ color: '#c7d2fe' }}>{p.test_id}</span>
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] px-1.5 py-0.5 rounded"
                              style={{
                                background: p.status === 'pending' ? '#1e1e3a' : p.status === 'approved' ? '#064e3b' : '#4c0519',
                                color: p.status === 'pending' ? '#f59e0b' : p.status === 'approved' ? '#34d399' : '#fb7185',
                              }}>
                          {p.status}
                        </span>
                        {analyzingIds.has(p.id)
                          ? <span className="text-[10px] animate-pulse" style={{ color: '#6366f1' }}>Analyzing…</span>
                          : <span className="text-xs font-mono" style={{ color: confidenceColor(p.confidence) }}>{Math.round(p.confidence * 100)}%</span>
                        }
                      </div>
                    </div>
                    <div className="text-xs truncate" style={{ color: '#64748b' }}>
                      {strategyLabel[p.strategy] || p.strategy} — {p.ai_reasoning.slice(0, 80)}...
                    </div>
                  </button>
                ))}
                {queue.length === 0 && !loading && (
                  <p className="text-sm text-center py-4" style={{ color: '#94a3b8' }}>No proposals in queue</p>
                )}
              </div>
            </div>
          </div>

          {/* Detail panel */}
          {selected && (
            <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold text-sm" style={{ color: '#c7d2fe' }}>{selected.id}</h3>
                <button onClick={() => setSelected(null)} className="text-xs px-2 py-1 rounded"
                        style={{ background: '#0a0a14', color: '#64748b' }}>Close</button>
              </div>

              <div className="space-y-3">
                <div>
                  <p className="text-[10px] mb-1" style={{ color: '#64748b' }}>Test ID</p>
                  <p className="text-sm font-mono" style={{ color: '#06b6d4' }}>{selected.test_id}</p>
                </div>
                <div>
                  <p className="text-[10px] mb-1" style={{ color: '#64748b' }}>Strategy</p>
                  <p className="text-sm">{strategyLabel[selected.strategy] || selected.strategy}</p>
                </div>
                <div>
                  <p className="text-[10px] mb-1" style={{ color: '#64748b' }}>Confidence</p>
                  {analyzingIds.has(selected.id)
                    ? <p className="text-sm animate-pulse" style={{ color: '#6366f1' }}>Analyzing with AI…</p>
                    : <p className="text-sm font-mono" style={{ color: confidenceColor(selected.confidence) }}>{Math.round(selected.confidence * 100)}%</p>
                  }
                </div>
                <div>
                  <p className="text-[10px] mb-1" style={{ color: '#64748b' }}>AI Reasoning</p>
                  {selected.ai_reasoning?.includes('AI-verified') && (
                    <span className="inline-block text-[9px] px-1.5 py-0.5 rounded mb-1 font-semibold"
                          style={{ background: 'rgba(16,185,129,0.15)', color: '#34d399', border: '1px solid #10b98130' }}>
                      ✓ AI-verified: test passed with this fix
                    </span>
                  )}
                  <p className="text-xs" style={{ color: '#94a3b8' }}>{selected.ai_reasoning}</p>
                </div>
                <div>
                  <p className="text-[10px] mb-1" style={{ color: '#64748b' }}>Diff Preview</p>
                  <pre className="text-[11px] p-2 rounded overflow-x-auto"
                       style={{ background: '#0a0a14', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>
                    {selected.diff_preview}
                  </pre>
                </div>
              </div>

              {selected.status === 'pending' && (
                <div className="mt-4 space-y-2">
                  {/* Run with Fix */}
                  <button
                    onClick={() => handleRunWithFix(selected.id)}
                    disabled={runStatus[selected.id] === 'running'}
                    className="w-full text-xs py-2 rounded-lg font-medium transition-colors"
                    style={{ background: '#0c1a2e', color: '#06b6d4', border: '1px solid #0e3a5c',
                             opacity: runStatus[selected.id] === 'running' ? 0.6 : 1 }}>
                    {runStatus[selected.id] === 'running' ? 'Running test...' : 'Run with Fix'}
                  </button>
                  {runStatus[selected.id] === 'pass' && (
                    <p className="text-xs text-center" style={{ color: '#34d399' }}>
                      ✓ Test passed with fix. Safe to approve
                    </p>
                  )}
                  {runStatus[selected.id] === 'fail' && (
                    <p className="text-xs text-center" style={{ color: '#fb7185' }}>
                      ✗ Test still failing. Review the diff
                    </p>
                  )}
                  {/* Action result confirmation */}
                  {actionResult && actionResult.id === selected.id && (
                    <div className="rounded-lg px-3 py-2 text-xs text-center font-medium" style={{
                      background: actionResult.type === 'approved' ? '#064e3b' : actionResult.type === 'rejected' ? '#4c0519' : '#78350f',
                      color: actionResult.type === 'approved' ? '#34d399' : actionResult.type === 'rejected' ? '#fb7185' : '#fbbf24',
                      border: `1px solid ${actionResult.type === 'approved' ? '#34d39933' : actionResult.type === 'rejected' ? '#fb718533' : '#fbbf2433'}`,
                    }}>
                      {actionResult.type === 'approved' && '✓ '}{actionResult.type === 'rejected' && '✗ '}{actionResult.message}
                    </div>
                  )}
                  {/* Approve / Reject */}
                  {selected.status === 'pending' && (
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleApprove(selected.id)}
                      disabled={actionLoading === selected.id}
                      className="flex-1 text-xs py-2 rounded-lg font-medium transition-colors"
                      style={{ background: '#064e3b', color: '#34d399' }}>
                      {actionLoading === selected.id ? '...' : 'Approve'}
                    </button>
                    <button
                      onClick={() => handleReject(selected.id)}
                      disabled={actionLoading === selected.id}
                      className="flex-1 text-xs py-2 rounded-lg font-medium transition-colors"
                      style={{ background: '#4c0519', color: '#fb7185' }}>
                      {actionLoading === selected.id ? '...' : 'Reject'}
                    </button>
                  </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
