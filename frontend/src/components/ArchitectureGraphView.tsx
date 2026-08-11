'use client'

// R330 P4 — visualize the SUT-understanding: the 6 Architecture Discovery graphs
// (architecture / API / auth-chain / workflow / data-flow / dependency) finally get
// a UI, reusing the D3 TraceabilityGraph. API endpoints are colored by GROUNDING
// DEPTH (request+response shape captured → green, one → amber, none → red) so the
// operator SEES how well ARTA actually understands each part of the SUT — the honest
// picture P1/P2a made truthful, now made visible (frontend ↔ backend sync).

import { useEffect, useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import { ChunkLoadErrorBoundary } from '@/components/ChunkLoadErrorBoundary'
import {
  fetchArchitectureSummary,
  fetchArchitectureGraph,
  type RawArchitectureGraph,
} from '@/lib/api-client'

const TraceabilityGraph = dynamic(() => import('@/components/TraceabilityGraph'), { ssr: false })

const GRAPHS: { key: string; label: string }[] = [
  { key: 'architecture_map', label: 'Architecture' },
  { key: 'api_graph', label: 'API' },
  { key: 'auth_graph', label: 'Auth chain' },
  { key: 'workflow_graph', label: 'Workflow' },
  { key: 'data_flow_graph', label: 'Data flow' },
  { key: 'dependency_graph', label: 'Dependencies' },
]

const KIND_COLOR: Record<string, string> = {
  service: '#6366f1', endpoint: '#06b6d4', token: '#f59e0b', claim: '#8b5cf6',
  scenario: '#ec4899', step: '#34d399', var: '#94a3b8', env_var: '#64748b',
  datastore: '#a5b4fc',
}
const MAX_NODES = 120

type AdaptedNode = {
  id: string
  label: string
  type: string
  color: string
  coverage_level?: 'FULL' | 'PARTIAL' | 'NONE'
}

function adapt(g: RawArchitectureGraph) {
  const rawNodes = (g.nodes || []).slice(0, MAX_NODES)
  const keep = new Set(rawNodes.map((n) => n.id))
  const nodes: AdaptedNode[] = rawNodes.map((n) => {
    const kind = n.kind || 'node'
    let coverage_level: AdaptedNode['coverage_level']
    // R330 tie-in: color API endpoints by how well ARTA understands them.
    if (kind === 'endpoint' && ('has_request_shape' in n || 'has_response_shape' in n)) {
      const score = (n.has_request_shape ? 1 : 0) + (n.has_response_shape ? 1 : 0)
      coverage_level = score === 2 ? 'FULL' : score === 1 ? 'PARTIAL' : 'NONE'
    }
    return {
      id: n.id,
      label: n.name || n.path || n.id,
      type: kind,
      color: KIND_COLOR[kind] || '#64748b',
      coverage_level,
    }
  })
  const edges = (g.edges || [])
    .filter((e) => e.from && e.to && keep.has(e.from) && keep.has(e.to))
    .map((e) => ({ source: e.from as string, target: e.to as string, type: e.kind || 'link' }))
  return { nodes, edges, node_colors: {} }
}

export default function ArchitectureGraphView({ projectId }: { projectId: string }) {
  const [available, setAvailable] = useState<Record<string, unknown> | null>(null)
  const [active, setActive] = useState<string>('api_graph')
  const [raw, setRaw] = useState<RawArchitectureGraph | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!projectId) { setAvailable(null); return }
    fetchArchitectureSummary(projectId)
      .then((s) => setAvailable(s?.graphs || {}))
      .catch(() => setAvailable({}))
  }, [projectId])

  useEffect(() => {
    if (!projectId || !active) return
    setLoading(true)
    setErr(null)
    fetchArchitectureGraph(projectId, active)
      .then((g) => setRaw(g))
      .catch(() => { setRaw(null); setErr('not discovered yet') })
      .finally(() => setLoading(false))
  }, [projectId, active])

  const data = useMemo(() => (raw ? adapt(raw) : null), [raw])

  if (!available) return null
  const graphsWithData = GRAPHS.filter((x) => available[x.key])
  if (!graphsWithData.length) return null

  const nodeCount = raw?.nodes?.length || 0

  return (
    <div className="glass-card p-5">
      <div className="flex items-center gap-3 mb-3">
        <h3 className="text-sm font-semibold" style={{ color: '#e2e8f0' }}>SUT Architecture</h3>
        <span className="text-[10px]" style={{ color: '#64748b' }}>
          what ARTA discovered about this SUT — API endpoints colored by grounding depth
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5 mb-3">
        {graphsWithData.map((x) => (
          <button
            key={x.key}
            onClick={() => setActive(x.key)}
            className="px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors"
            style={{
              background: active === x.key ? 'rgba(99,102,241,0.25)' : 'rgba(255,255,255,0.04)',
              color: active === x.key ? '#c7d2fe' : '#94a3b8',
              border: `1px solid ${active === x.key ? 'rgba(99,102,241,0.5)' : 'rgba(255,255,255,0.08)'}`,
            }}
          >
            {x.label}
          </button>
        ))}
      </div>

      {active === 'api_graph' && (
        <div className="flex flex-wrap gap-3 mb-2 text-[10px]">
          <span style={{ color: '#34d399' }}>● grounded (req+resp shape)</span>
          <span style={{ color: '#fbbf24' }}>● partial</span>
          <span style={{ color: '#fb7185' }}>● ungrounded</span>
        </div>
      )}

      {loading ? (
        <div className="text-xs py-10 text-center" style={{ color: '#64748b' }}>loading…</div>
      ) : err ? (
        <div className="text-xs py-10 text-center" style={{ color: '#64748b' }}>{err}</div>
      ) : data && data.nodes.length ? (
        <>
          {nodeCount > MAX_NODES && (
            <div className="text-[10px] mb-1" style={{ color: '#f59e0b' }}>
              showing first {MAX_NODES} of {nodeCount} nodes
            </div>
          )}
          <ChunkLoadErrorBoundary>
            <TraceabilityGraph data={data} />
          </ChunkLoadErrorBoundary>
        </>
      ) : (
        <div className="text-xs py-10 text-center" style={{ color: '#64748b' }}>no nodes in this graph</div>
      )}
    </div>
  )
}
