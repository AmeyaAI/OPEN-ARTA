'use client'

import { useEffect, useState, useMemo } from 'react'
import dynamic from 'next/dynamic'
import Link from 'next/link'
import { useSearchParams } from 'next/navigation'
import { fetchRequirements, fetchDashboardTrends, exportCoverageReport, generateTests, fetchTraceabilityGraph, type Requirement } from '@/lib/api-client'
import { useProject } from '@/lib/project-context'
import { useExecutionAndHealing } from '@/lib/use-execution-healing'
import { ChunkLoadErrorBoundary } from '@/components/ChunkLoadErrorBoundary'

// R28.2 — `loading` fallback fires DURING chunk fetch. ChunkLoadErrorBoundary
// (wrapped at the call site) catches the error if the chunk fetch FAILS
// (asset cache miss after redeploy, transient network blip, etc.).
const TraceabilityGraph = dynamic(() => import('@/components/TraceabilityGraph'), {
  ssr: false,
  loading: () => (
    <div className="text-xs px-3 py-8 text-center" style={{ color: '#64748b' }}>
      Loading traceability graph…
    </div>
  ),
})

type Node = { id: string; label: string; type: 'req' | 'ac' | 'test' | 'result' }
type Edge = { from: string; to: string }

type GraphData = {
  nodes: { id: string; label: string; type: string; color: string; [k: string]: any }[]
  edges: { source: string; target: string; type: string }[]
  node_colors: Record<string, string>
  ac_tests?: Record<string, AcTestItem[]>
  ac_defects?: Record<string, AcDefectItem[]>
  // WS1 — coverage gaps (where the gen→execute chain is broken).
  gaps?: {
    unplanned_requirements?: string[]
    requirements_without_spec?: string[]
    unrun_specs?: string[]
    open_defect_count?: number
  }
}

const PRIORITY_COLORS: Record<string, string> = {
  P0: '#fb7185', P1: '#f59e0b', P2: '#60a5fa', P3: '#94a3b8',
}

const SOURCE_BADGES: { icon: string; label: string; bg: string }[] = [
  { icon: '🔗', label: 'Jira', bg: 'rgba(99,102,241,0.15)' },
  { icon: '📄', label: 'Doc', bg: 'rgba(139,92,246,0.15)' },
  { icon: '✏️', label: 'Manual', bg: 'rgba(6,182,212,0.15)' },
]

type AcTestItem = { id: string; title: string; status: string; tool: string }
type AcDefectItem = { id: string; title: string; severity: string }

const TOOL_COLORS: Record<string, string> = {
  Playwright: '#06b6d4',
  k6: '#8b5cf6',
  Newman: '#10b981',
}

const STATUS_COLORS: Record<string, string> = {
  PASS: '#10b981',
  FAIL: '#ef4444',
  SKIP: '#f59e0b',
}

function getSourceBadge(reqId: string) {
  // Derive source from req ID pattern (mock logic)
  if (reqId.startsWith('JIRA-') || reqId.includes('-')) return SOURCE_BADGES[0]
  if (reqId.startsWith('DOC-')) return SOURCE_BADGES[1]
  return SOURCE_BADGES[2]
}

function CoverageTrendSparkline({ points }: { points: number[] }) {
  if (!points || points.length < 2) return null
  const maxVal = 100
  const w = 80
  const h = 24
  const coords = points.map((v, i) => `${(i / (points.length - 1)) * w},${h - (v / maxVal) * h}`)
  const last = points[points.length - 1]
  const endX = w
  const endY = h - (last / maxVal) * h
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="inline-block ml-2">
      <polyline
        points={coords.join(' ')}
        fill="none"
        stroke="#34d399"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx={endX} cy={endY} r="2.5" fill="#34d399" />
    </svg>
  )
}

export default function TraceabilityPage() {
  const { currentProjectId } = useProject()
  const sp = useSearchParams()
  // R28.6 — read run_id from URL. When set, graph endpoint scopes
  // ExecutionResult + Defect nodes to THAT run only. Operator lands
  // here from RunDetailContent's "Traceability for this run" link
  // (R28.6.1) or via a manually-constructed URL.
  const runIdFromUrl = sp.get('run_id') || ''
  const { resultsByTestId, handleRequestHeal, healingTestId } = useExecutionAndHealing()
  const [reqs, setReqs] = useState<Requirement[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [selectedAcId, setSelectedAcId] = useState<string | null>(null)
  const [view, setView] = useState<'graph' | 'list'>('graph')
  const [graphData, setGraphData] = useState<GraphData | null>(null)
  const [, setExpanded] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [coverageTrend, setCoverageTrend] = useState<number[]>([])

  // Filters
  const [priorityFilter, setPriorityFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<string>('all')

  useEffect(() => {
    fetchRequirements({ project_id: currentProjectId || undefined }).then(d => setReqs(d.requirements)).catch(() => {})
    // R28.6 — append run_id when present so the graph reflects THAT run's
    // PASS/FAIL/Defect attribution (per-run scope) instead of the project view.
    // FE-sync P3 — via the typed client (sends JWT + X-API-Key; was a hand-rolled
    // header fetch reading the dead `arta_api_key`).
    fetchTraceabilityGraph<GraphData>({
      project_id: currentProjectId || undefined,
      run_id: runIdFromUrl || undefined,
    })
      .then(d => setGraphData(d))
      .catch(() => {})
    fetchDashboardTrends(14, currentProjectId || undefined)
      .then(d => {
        const trends = d?.quality_trends
        if (Array.isArray(trends) && trends.length > 0) {
          setCoverageTrend(trends.map(t => t.coverage))
        }
      })
      .catch(() => {})
  }, [currentProjectId, runIdFromUrl])

  // Apply filters to requirements
  const filteredReqs = reqs.filter(r => {
    if (priorityFilter !== 'all' && (r as any).priority !== priorityFilter) return false
    if (statusFilter !== 'all') {
      const allAcs = r.acceptance_criteria || []
      const coveredCount = allAcs.filter(ac => ac.covered).length
      if (statusFilter === 'covered' && coveredCount !== allAcs.length) return false
      if (statusFilter === 'partial' && (coveredCount === 0 || coveredCount === allAcs.length)) return false
      if (statusFilter === 'uncovered' && coveredCount > 0) return false
    }
    return true
  })

  // Build graph from requirements -> ACs -> tests (for list view)
  const nodes: Node[] = []
  const edges: Edge[] = []

  for (const r of filteredReqs) {
    nodes.push({ id: r.req_id || r.id!, label: r.title, type: 'req' })
    for (const ac of r.acceptance_criteria || []) {
      const acId = ac.id || ac.statement?.slice(0, 8)
      nodes.push({ id: acId, label: ac.statement, type: 'ac' })
      edges.push({ from: r.req_id || r.id!, to: acId })
    }
  }

  // Coverage stats — use req.coverage_pct when available, fall back to AC coverage check
  const totalAc = reqs.reduce((n, r) => n + (r.acceptance_criteria?.length || 0), 0)
  const coveredAc = reqs.reduce(
    (n, r) => n + (r.acceptance_criteria?.filter((ac: any) => ac.covered).length || 0), 0,
  )
  const avgReqCoverage = reqs.length > 0
    ? Math.round(reqs.reduce((n, r) => n + ((r as any).coverage_pct || 0), 0) / reqs.length)
    : 0
  const coveragePct = coveredAc > 0
    ? Math.round((coveredAc / totalAc) * 100)
    : avgReqCoverage

  // P0/P1 coverage
  const p0Reqs = reqs.filter(r => (r as any).priority === 'P0')
  const p1Reqs = reqs.filter(r => (r as any).priority === 'P1')
  const p0Coverage = p0Reqs.length > 0
    ? Math.round(p0Reqs.reduce((n, r) => n + (r.coverage_pct || 0), 0) / p0Reqs.length) : 0
  const p1Coverage = p1Reqs.length > 0
    ? Math.round(p1Reqs.reduce((n, r) => n + (r.coverage_pct || 0), 0) / p1Reqs.length) : 0

  // Get ACs for the selected requirement
  const selectedReq = selected ? filteredReqs.find(r => (r.req_id || r.id) === selected) : null
  const selectedAcs = selectedReq?.acceptance_criteria || []

  // Get tests and defects for the selected AC from graph data
  const acTestMap = graphData?.ac_tests || {}
  const acDefectMap = graphData?.ac_defects || {}
  const acTests = selectedAcId ? (acTestMap[selectedAcId] || []) : []
  const acDefects = selectedAcId ? (acDefectMap[selectedAcId] || []) : []
  const hasGap = selectedAcId ? acTests.length === 0 : false

  // Export handler
  const handleExport = async () => {
    setExporting(true)
    try {
      await exportCoverageReport('pdf')
    } catch {
      // silently fail
    } finally {
      setExporting(false)
    }
  }

  // When selecting a requirement, clear AC selection
  const handleSelectReq = (id: string) => {
    if (selected === id) {
      setSelected(null)
      setSelectedAcId(null)
      setExpanded(null)
    } else {
      setSelected(id)
      setSelectedAcId(null)
      setExpanded(id)
    }
  }

  // Filter graph data for a single requirement when selected (per-requirement DAG)
  const filteredGraphData = useMemo(() => {
    if (!selected || !graphData || !graphData.nodes.length) return null
    const getId = (ref: any): string => typeof ref === 'string' ? ref : ref?.id || ''
    const reqNodeId = selected
    // AC nodes connected via has_ac
    const acIds = new Set(graphData.edges
      .filter(e => e.type === 'has_ac' && getId(e.source) === reqNodeId)
      .map(e => getId(e.target)))
    // Test nodes connected to req or ACs via covers OR (FE-sync P3) tested_by.
    const testIds = new Set(graphData.edges
      .filter(e => (e.type === 'covers' || e.type === 'tested_by')
        && (getId(e.source) === reqNodeId || acIds.has(getId(e.source))))
      .map(e => getId(e.target)))
    // FE-sync (R311) — the run-scoped execution lane: ExecutionResult nodes a
    // test PRODUCED, and the Defects those results TRIGGERED. The backend adds
    // these on /traceability?run_id=X (traceability._full_chain_graph run-scoped
    // block); without traversing them here the per-requirement DAG silently drops
    // the whole pass/fail + run-defect lane the requirement's tests produced.
    const resultIds = new Set(graphData.edges
      .filter(e => e.type === 'produced' && testIds.has(getId(e.source)))
      .map(e => getId(e.target)))
    // Defects linked to req, tests (found_by/impacts) OR results (R311 triggered)
    const defectIds = new Set(graphData.edges
      .filter(e => (
        ((e.type === 'found_by' || e.type === 'impacts') && (
          testIds.has(getId(e.source)) || getId(e.source) === reqNodeId))
        || (e.type === 'triggered' && resultIds.has(getId(e.source)))
      ))
      .map(e => getId(e.target)))
    // FE-sync P3 — the charter-conformance spine: endpoints that IMPLEMENT the
    // requirement (implemented_by) or are EXERCISED by its tests (exercises), plus
    // the frontend routes that INVOKE those endpoints. These edges are written by the
    // backend but were previously dropped by this filter.
    const endpointIds = new Set(graphData.edges
      .filter(e => (e.type === 'implemented_by' && getId(e.source) === reqNodeId)
        || (e.type === 'exercises' && testIds.has(getId(e.source))))
      .map(e => getId(e.target)))
    const feRouteIds = new Set(graphData.edges
      .filter(e => e.type === 'invokes' && endpointIds.has(getId(e.target)))
      .map(e => getId(e.source)))
    const allIds = new Set([reqNodeId, ...acIds, ...testIds, ...resultIds, ...defectIds,
      ...endpointIds, ...feRouteIds])
    return {
      nodes: graphData.nodes.filter(n => allIds.has(n.id)),
      edges: graphData.edges.filter(e => allIds.has(getId(e.source)) && allIds.has(getId(e.target))),
      node_colors: graphData.node_colors,
    }
  }, [selected, graphData])

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-xl font-bold">Traceability Matrix</h1>
        <div className="flex rounded-lg overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
          {(['graph', 'list'] as const).map(v => (
            <button key={v} onClick={() => setView(v)}
                    className="px-4 py-1.5 text-xs font-medium transition-colors"
                    style={{
                      background: view === v ? 'rgba(99,102,241,0.2)' : '#0a0a14',
                      color: view === v ? '#818cf8' : '#64748b',
                    }}>
              {v === 'graph' ? 'Force Graph' : 'List View'}
            </button>
          ))}
        </div>
      </div>
      <p className="text-sm mb-6" style={{ color: '#64748b' }}>
        Requirement → Acceptance Criteria → Test Case → Result chain
      </p>

      {/* Coverage summary banner */}
      <div className="rounded-xl p-4 mb-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-3">
            <div className="text-3xl font-bold" style={{ color: coveragePct >= 80 ? '#34d399' : coveragePct >= 50 ? '#fbbf24' : '#fb7185', fontFamily: "'Space Mono',monospace" }}>
              {coveragePct}%
            </div>
            <div>
              <div className="text-xs font-semibold flex items-center" style={{ color: '#e2e8f0' }}>
                Overall Coverage
                <CoverageTrendSparkline points={coverageTrend} />
              </div>
              <div className="text-[10px]" style={{ color: '#64748b' }}>{coveredAc}/{totalAc} acceptance criteria</div>
            </div>
          </div>
          <div className="h-8 w-px" style={{ background: '#1e1e3a' }} />
          <div className="flex gap-6">
            <div>
              <div className="text-lg font-bold" style={{ color: '#fb7185', fontFamily: "'Space Mono',monospace" }}>{p0Coverage}%</div>
              <div className="text-[10px]" style={{ color: '#64748b' }}>P0 Coverage</div>
            </div>
            <div>
              <div className="text-lg font-bold" style={{ color: '#f59e0b', fontFamily: "'Space Mono',monospace" }}>{p1Coverage}%</div>
              <div className="text-[10px]" style={{ color: '#64748b' }}>P1 Coverage</div>
            </div>
            <div>
              <div className="text-lg font-bold" style={{ color: '#6366f1', fontFamily: "'Space Mono',monospace" }}>
                {graphData?.nodes?.filter(n => n.type === 'requirement').length ?? reqs.length}
              </div>
              <div className="text-[10px]" style={{ color: '#64748b' }}>Requirements</div>
            </div>
            <div>
              <div className="text-lg font-bold" style={{ color: '#06b6d4', fontFamily: "'Space Mono',monospace" }}>
                {graphData?.nodes?.filter(n => n.type === 'test_case').length ?? 0}
              </div>
              <div className="text-[10px]" style={{ color: '#64748b' }}>Test Cases</div>
            </div>
            <div>
              <Link href="/defects" className="hover:brightness-125 transition-all">
                <div className="text-lg font-bold" style={{ color: '#ef4444', fontFamily: "'Space Mono',monospace" }}>
                  {graphData?.nodes?.filter(n => n.type === 'defect').length ?? 0}
                </div>
                <div className="text-[10px] underline" style={{ color: '#64748b' }}>Defects</div>
              </Link>
            </div>
          </div>
          {/* Coverage bar */}
          <div className="flex-1">
            <div className="h-3 rounded-full overflow-hidden" style={{ background: '#1e1e3a' }}>
              <div className="h-full rounded-full transition-all" style={{
                width: `${coveragePct}%`,
                background: coveragePct >= 80 ? '#34d399' : coveragePct >= 50 ? '#fbbf24' : '#fb7185',
              }} />
            </div>
          </div>
          {/* Export button */}
          <button
            onClick={handleExport}
            disabled={exporting}
            className="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors shrink-0"
            style={{
              background: 'rgba(99,102,241,0.15)',
              color: '#818cf8',
              border: '1px solid rgba(99,102,241,0.3)',
              opacity: exporting ? 0.5 : 1,
            }}
          >
            {exporting ? 'Exporting...' : 'Export PDF'}
          </button>
        </div>
      </div>

      {/* WS1 — Chain coverage gaps: the MISSION OUTPUT (where gen→execute→report
          is incomplete). Each gap deep-links to the page that fixes it. */}
      {graphData?.gaps && (() => {
        const g = graphData.gaps
        const unplanned = g.unplanned_requirements?.length ?? 0
        const noSpec = g.requirements_without_spec?.length ?? 0
        const unrun = g.unrun_specs?.length ?? 0
        const openDefects = g.open_defect_count ?? 0
        if (!unplanned && !noSpec && !unrun && !openDefects) return null
        const Cell = ({ n, label, href, color }: { n: number; label: string; href: string; color: string }) => (
          <Link href={href} className="flex-1 rounded-lg px-3 py-2 hover:brightness-125 transition-all"
                style={{ background: '#0a0a14', border: `1px solid ${color}40` }}>
            <div className="text-lg font-bold" style={{ color, fontFamily: "'Space Mono',monospace" }}>{n}</div>
            <div className="text-[10px]" style={{ color: '#94a3b8' }}>{label}</div>
          </Link>
        )
        return (
          <div className="mb-6 rounded-xl p-3" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="text-[10px] uppercase tracking-widest mb-2" style={{ color: '#64748b' }}>
              Chain coverage gaps: where gen → execute → report is incomplete
            </div>
            <div className="flex items-stretch gap-2">
              <Cell n={unplanned} label="reqs not risk-scored" href="/architecture" color="#f59e0b" />
              <Cell n={noSpec} label="reqs with no test script" href="/architecture" color="#fb7185" />
              <Cell n={unrun} label="specs never executed" href="/run-history" color="#fbbf24" />
              <Cell n={openDefects} label="open defects" href="/defects" color="#ef4444" />
            </div>
          </div>
        )
      })()}

      {/* Filters toolbar */}
      <div className="flex items-center gap-4 mb-6">
        <div className="flex items-center gap-2">
          <span className="text-xs" style={{ color: '#64748b' }}>Priority:</span>
          <div className="flex rounded-lg overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
            {['all', 'P0', 'P1', 'P2', 'P3'].map(p => (
              <button key={p} onClick={() => setPriorityFilter(p)}
                      className="px-3 py-1 text-xs font-medium transition-colors"
                      style={{
                        background: priorityFilter === p ? 'rgba(99,102,241,0.2)' : '#0a0a14',
                        color: priorityFilter === p ? (p === 'all' ? '#818cf8' : PRIORITY_COLORS[p]) : '#94a3b8',
                      }}>
                {p === 'all' ? 'All' : p}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs" style={{ color: '#64748b' }}>Status:</span>
          <div className="flex rounded-lg overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
            {[
              { id: 'all', label: 'All', color: '#818cf8' },
              { id: 'covered', label: 'Covered', color: '#34d399' },
              { id: 'partial', label: 'Partial', color: '#fbbf24' },
              { id: 'uncovered', label: 'Uncovered', color: '#fb7185' },
            ].map(s => (
              <button key={s.id} onClick={() => setStatusFilter(s.id)}
                      className="px-3 py-1 text-xs font-medium transition-colors"
                      style={{
                        background: statusFilter === s.id ? 'rgba(99,102,241,0.2)' : '#0a0a14',
                        color: statusFilter === s.id ? s.color : '#94a3b8',
                      }}>
                {s.label}
              </button>
            ))}
          </div>
        </div>
        <span className="text-xs ml-auto" style={{ color: '#94a3b8' }}>
          Showing {filteredReqs.length} of {reqs.length} requirements
        </span>
      </div>

      {/* Graph or List view */}
      {view === 'graph' ? (
        selected && filteredGraphData && filteredGraphData.nodes.length > 0 ? (
          /* Per-requirement DAG — shows one requirement expanded */
          <div>
            <button
              onClick={() => { setSelected(null); setSelectedAcId(null) }}
              className="mb-4 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors"
              style={{ background: 'rgba(99,102,241,0.15)', color: '#818cf8', border: '1px solid rgba(99,102,241,0.3)' }}>
              ← Back to all requirements
            </button>
            <div className="mb-3 text-sm" style={{ color: '#94a3b8' }}>
              <span className="font-mono font-bold" style={{ color: '#6366f1' }}>{selected}</span>
              {' — '}
              {selectedReq?.title || ''}
            </div>
            <ChunkLoadErrorBoundary>
              <TraceabilityGraph data={filteredGraphData} executionResults={resultsByTestId} />
            </ChunkLoadErrorBoundary>
          </div>
        ) : (
          /* Overview: clickable requirement list to drill into */
          <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <h2 className="font-semibold text-sm mb-3" style={{ color: '#94a3b8' }}>
              Click a requirement to view its traceability graph
            </h2>
            <div className="space-y-2">
              {filteredReqs.map(r => {
                const id = r.req_id || r.id!
                const priority = (r as any).priority || 'P2'
                const acCount = r.acceptance_criteria?.length || 0
                const covCount = r.acceptance_criteria?.filter(ac => ac.covered).length || 0
                const covPct = r.coverage_pct || (acCount > 0 ? Math.round((covCount / acCount) * 100) : 0)
                return (
                  <button key={id}
                          onClick={() => handleSelectReq(id)}
                          className="w-full text-left px-4 py-3 rounded-lg transition-all hover:ring-1 hover:ring-indigo-500 flex items-center gap-4"
                          style={{ background: '#0a0a14' }}>
                    <span className="font-mono font-bold text-xs" style={{ color: '#818cf8' }}>{id}</span>
                    <span className="px-1.5 py-0.5 rounded text-[9px] font-bold"
                          style={{ background: `${PRIORITY_COLORS[priority]}22`, color: PRIORITY_COLORS[priority] }}>
                      {priority}
                    </span>
                    <span className="text-sm flex-1 truncate" style={{ color: '#e2e8f0' }}>{r.title}</span>
                    <span className="text-[10px] font-mono" style={{ color: '#64748b' }}>{covCount}/{acCount} AC</span>
                    <div className="w-16 h-1.5 rounded-full overflow-hidden" style={{ background: '#1e1e3a' }}>
                      <div className="h-full rounded-full" style={{
                        width: `${covPct}%`,
                        background: covPct >= 80 ? '#10b981' : covPct >= 50 ? '#f59e0b' : '#ef4444',
                      }} />
                    </div>
                    <span className="text-[10px] font-mono w-8 text-right" style={{ color: covPct >= 80 ? '#10b981' : covPct >= 50 ? '#f59e0b' : '#ef4444' }}>
                      {covPct}%
                    </span>
                  </button>
                )
              })}
              {filteredReqs.length === 0 && (
                <div className="text-xs px-3 py-8 text-center" style={{ color: '#94a3b8' }}>
                  No requirements match the current filters
                </div>
              )}
            </div>
          </div>
        )
      ) : (
        <div className="grid grid-cols-3 gap-6">
          {/* Requirements column */}
          <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <h2 className="font-semibold text-sm mb-3" style={{ color: '#6366f1' }}>Requirements</h2>
            <div className="space-y-2">
              {filteredReqs.map(r => {
                const id = r.req_id || r.id!
                const isSelected = selected === id
                const priority = (r as any).priority || 'P2'
                const acCount = r.acceptance_criteria?.length || 0
                const covCount = r.acceptance_criteria?.filter(ac => ac.covered).length || 0
                const covPct = r.coverage_pct || (acCount > 0 ? Math.round((covCount / acCount) * 100) : 0)
                const source = getSourceBadge(id)
                return (
                  <div key={id}>
                    <button
                          onClick={() => handleSelectReq(id)}
                          className="w-full text-left px-3 py-2 rounded-lg text-xs transition-all"
                          style={{
                            background: isSelected ? 'rgba(99,102,241,0.2)' : '#0a0a14',
                            border: isSelected ? '1px solid #6366f1' : '1px solid transparent',
                          }}>
                      <div className="flex items-center gap-2">
                        <span className="font-mono font-bold" style={{ color: '#818cf8' }}>{id}</span>
                        <span className="px-1 py-0.5 rounded text-[9px]" style={{ background: source.bg }}>
                          {source.icon}
                        </span>
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-bold"
                              style={{ background: `${PRIORITY_COLORS[priority]}22`, color: PRIORITY_COLORS[priority] }}>
                          {priority}
                        </span>
                        <span className="ml-auto text-[10px]" style={{ color: '#94a3b8' }}>
                          {covCount}/{acCount} AC
                        </span>
                      </div>
                      <span className="mt-1 block truncate" style={{ color: '#94a3b8' }}>{r.title}</span>
                      {/* Coverage progress bar */}
                      <div className="mt-1.5 h-1.5 rounded-full overflow-hidden" style={{ background: '#1e1e3a' }}>
                        <div className="h-full rounded-full transition-all" style={{
                          width: `${covPct}%`,
                          background: covPct >= 80 ? '#10b981' : covPct >= 50 ? '#f59e0b' : '#ef4444',
                        }} />
                      </div>
                    </button>
                  </div>
                )
              })}
              {filteredReqs.length === 0 && (
                <div className="text-xs px-3 py-4 text-center" style={{ color: '#94a3b8' }}>
                  No requirements match the current filters
                </div>
              )}
            </div>
          </div>

          {/* Acceptance Criteria column — filtered to selected requirement */}
          <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <h2 className="font-semibold text-sm mb-3" style={{ color: '#8b5cf6' }}>
              Acceptance Criteria
              {selectedReq && (
                <span className="text-[10px] font-normal ml-2" style={{ color: '#94a3b8' }}>
                  for {selected}
                </span>
              )}
            </h2>
            <div className="space-y-2">
              {selected ? (
                selectedAcs.length > 0 ? (
                  selectedAcs.map((ac, i) => {
                    const acId = ac.id || `${selected}-AC-${i + 1}`
                    const isAcSelected = selectedAcId === acId
                    const testCount = (acTestMap[acId] || []).length
                    const coverageStatus = testCount > 1 ? 'FULL'
                      : testCount === 1 ? 'PARTIAL'
                      : 'NONE'
                    const coverageColor = coverageStatus === 'FULL' ? '#10b981'
                      : coverageStatus === 'PARTIAL' ? '#f59e0b' : '#ef4444'
                    const coverageBg = coverageStatus === 'FULL' ? 'rgba(16,185,129,0.15)'
                      : coverageStatus === 'PARTIAL' ? 'rgba(245,158,11,0.15)' : 'rgba(239,68,68,0.15)'
                    return (
                      <button key={acId}
                              onClick={() => setSelectedAcId(selectedAcId === acId ? null : acId)}
                              className="w-full text-left px-3 py-2 rounded-lg text-xs transition-all"
                              style={{
                                background: isAcSelected ? 'rgba(139,92,246,0.2)' : '#0a0a14',
                                border: isAcSelected ? '1px solid #8b5cf6' : '1px solid transparent',
                              }}>
                        <div className="flex items-center gap-2">
                          <span className="font-mono" style={{ color: '#a78bfa' }}>{acId}</span>
                          <span className="px-1.5 py-0.5 rounded text-[9px] font-bold"
                                style={{ background: coverageBg, color: coverageColor }}>
                            {coverageStatus}
                          </span>
                          <span className="ml-auto px-1.5 py-0.5 rounded text-[9px] font-medium"
                                style={{
                                  background: testCount > 0 ? 'rgba(6,182,212,0.15)' : 'rgba(239,68,68,0.15)',
                                  color: testCount > 0 ? '#06b6d4' : '#ef4444',
                                }}>
                            {testCount > 0 ? `${testCount} test${testCount > 1 ? 's' : ''}` : '\u26A0 0 tests'}
                          </span>
                        </div>
                        <div className="mt-1 truncate" style={{ color: '#94a3b8' }}>{ac.statement}</div>
                      </button>
                    )
                  })
                ) : (
                  <div className="text-xs px-3 py-4 text-center" style={{ color: '#94a3b8' }}>
                    No acceptance criteria found for this requirement
                  </div>
                )
              ) : (
                <div className="text-xs px-3 py-4 text-center" style={{ color: '#94a3b8' }}>
                  Select a requirement to view its acceptance criteria
                </div>
              )}
            </div>
          </div>

          {/* Test Cases & Defects column */}
          <div className="rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <h2 className="font-semibold text-sm mb-3" style={{ color: '#06b6d4' }}>
              Test Cases & Defects
            </h2>
            {selectedAcId ? (
              <div className="space-y-3">
                {/* Test Cases */}
                {acTests.length > 0 && (
                  <div>
                    <h3 className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: '#64748b' }}>
                      Test Cases ({acTests.length})
                    </h3>
                    <div className="space-y-1.5">
                      {acTests.map(tc => {
                        // Use real execution results if available, fall back to graph data
                        const execResult = resultsByTestId[tc.id]
                        const realStatus = execResult?.status || tc.status || 'PENDING'
                        const statusColor = STATUS_COLORS[realStatus] || '#94a3b8'
                        return (
                        <div key={tc.id} className="px-3 py-2 rounded-lg text-xs transition-all hover:brightness-125"
                              style={{ background: '#0a0a14' }}>
                          <Link href="/test-explorer" className="block">
                          <div className="flex items-center gap-2">
                            <span className="font-mono font-bold" style={{ color: '#818cf8' }}>{tc.id}</span>
                            <span className="px-1.5 py-0.5 rounded text-[9px] font-bold"
                                  style={{ background: `${statusColor}22`, color: statusColor }}>
                              {realStatus}
                            </span>
                            <span className="ml-auto px-1.5 py-0.5 rounded text-[9px] font-medium"
                                  style={{
                                    background: `${TOOL_COLORS[tc.tool] || '#94a3b8'}22`,
                                    color: TOOL_COLORS[tc.tool] || '#94a3b8',
                                  }}>
                              {tc.tool}
                            </span>
                          </div>
                          <div className="mt-1 truncate" style={{ color: '#94a3b8' }}>{tc.title}</div>
                          </Link>
                          {realStatus === 'FAIL' && (
                            <button
                              onClick={() => handleRequestHeal(tc.id, '', undefined, tc.title)}
                              className="mt-1 text-[9px] px-2 py-0.5 rounded font-medium"
                              style={{ background: '#f59e0b18', color: '#fbbf24', border: '1px solid #f59e0b30' }}
                            >
                              {healingTestId === tc.id ? 'Requesting...' : 'Request Heal'}
                            </button>
                          )}
                        </div>
                        )
                      })}
                    </div>
                  </div>
                )}

                {/* Defects */}
                {acDefects.length > 0 && (
                  <div>
                    <h3 className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: '#64748b' }}>
                      Defects ({acDefects.length})
                    </h3>
                    <div className="space-y-1.5">
                      {acDefects.map(def => (
                        <div key={def.id} className="px-3 py-2 rounded-lg text-xs"
                             style={{ background: '#0a0a14' }}>
                          <div className="flex items-center gap-2">
                            <span className="font-mono font-bold" style={{ color: '#fb7185' }}>{def.id}</span>
                            <span className="px-1.5 py-0.5 rounded text-[9px] font-bold"
                                  style={{
                                    background: def.severity === 'P0' ? 'rgba(239,68,68,0.15)' : 'rgba(245,158,11,0.15)',
                                    color: def.severity === 'P0' ? '#ef4444' : '#f59e0b',
                                  }}>
                              {def.severity}
                            </span>
                          </div>
                          <div className="mt-1 truncate" style={{ color: '#94a3b8' }}>{def.title}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Coverage Gap */}
                {hasGap && (
                  <div className="px-3 py-3 rounded-lg text-xs"
                       style={{
                         background: 'rgba(239,68,68,0.05)',
                         border: '2px dashed rgba(239,68,68,0.3)',
                       }}>
                    <div className="font-semibold mb-1" style={{ color: '#fb7185' }}>
                      No automation coverage
                    </div>
                    <div className="text-[10px] mb-2" style={{ color: '#64748b' }}>
                      This acceptance criterion has no linked test cases.
                    </div>
                    <button className="px-3 py-1 rounded text-[10px] font-medium transition-colors"
                            style={{
                              background: 'rgba(99,102,241,0.2)',
                              color: '#818cf8',
                              border: '1px solid rgba(99,102,241,0.3)',
                            }}
                            onClick={() => {
                              if (selected) {
                                generateTests(selected)
                                  .then(() => {
                                    // Refresh graph after generation. FE-sync P3 — via
                                    // the typed client (was a headerless fetch → 401 in
                                    // keyed envs → the graph silently went stale).
                                    fetchTraceabilityGraph<GraphData>({
                                      project_id: currentProjectId || undefined,
                                    })
                                      .then(d => { if (d) setGraphData(d) })
                                      .catch(() => {})
                                  })
                                  .catch(() => {})
                              }
                            }}>
                      Generate Tests
                    </button>
                  </div>
                )}
              </div>
            ) : (
              <div className="text-xs px-3 py-4 text-center" style={{ color: '#94a3b8' }}>
                {selected
                  ? 'Select an acceptance criterion to see linked test cases'
                  : 'Select a requirement, then an acceptance criterion to see linked test cases'}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
