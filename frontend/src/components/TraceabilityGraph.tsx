'use client'

import { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'

type GraphNode = {
  id: string
  label: string
  type: string
  color: string
  priority?: string
  severity?: string
  tool?: string
  status?: string
  coverage_pct?: number
  coverage_level?: 'FULL' | 'PARTIAL' | 'NONE'
  // layout positions (assigned by layered algorithm, not d3 force)
  x?: number
  y?: number
}

type GraphEdge = {
  source: string | GraphNode
  target: string | GraphNode
  type: string
}

type GraphData = {
  nodes: GraphNode[]
  edges: GraphEdge[]
  node_colors: Record<string, string>
}

// BMAD TEA coverage level colors
const COVERAGE_COLORS: Record<string, string> = {
  FULL: '#34d399',
  PARTIAL: '#fbbf24',
  NONE: '#fb7185',
}

const NODE_TYPE_COLORS: Record<string, string> = {
  requirement: '#6366f1',
  acceptance_criteria: '#8b5cf6',
  test_case: '#06b6d4',
  defect: '#ef4444',
  result: '#10b981',
  execution_result: '#10b981',  // FE-sync (R311) — run-scoped exec result (green status node)
  // WS1 — full-chain stages
  risk_profile: '#f59e0b',
  dataset_recipe: '#14b8a6',
  fixture: '#0ea5e9',
  recipe_verifier: '#a78bfa',
  scenario: '#ec4899',
  spec: '#22d3ee',
  execution: '#10b981',
  // FE-sync P3 — charter conformance spine: requirement→endpoint←test / frontend_route
  endpoint: '#84cc16',        // lime = API endpoint
  frontend_route: '#f472b6',  // pink = UI route
  // R330 P4 — Architecture Discovery graph kinds (service/auth/workflow/data-flow)
  service: '#6366f1',
  token: '#f59e0b',
  claim: '#8b5cf6',
  step: '#34d399',
  var: '#94a3b8',
  env_var: '#64748b',
  datastore: '#a5b4fc',
}

const EDGE_COLORS: Record<string, string> = {
  has_ac: '#6366f180',
  covers: '#06b6d480',
  found_by: '#ef444480',
  impacts: '#f59e0b80',
  // WS1 — full-chain edges
  has_profile: '#f59e0b80',
  has_recipe: '#14b8a680',
  materialises_to: '#0ea5e980',
  verified_by_recipe: '#a78bfa80',
  has_scenario: '#ec489980',
  realised_by: '#ec489980',
  has_spec: '#22d3ee80',
  used_by: '#0ea5e980',
  generates: '#22d3ee80',
  verified_by: '#10b98180',
  executed_as: '#10b98180',
  detected_by: '#ef444480',
  // FE-sync P3 — spine edges (already written by the backend, now traversed)
  implemented_by: '#84cc1680',  // requirement → endpoint
  exercises: '#84cc1680',       // test_case → endpoint
  invokes: '#f472b680',         // frontend_route → endpoint
  tested_by: '#06b6d480',       // acceptance_criteria → test_case
  // FE-sync (R311) — run-scoped execution lane (also used by the PG-fallback path)
  produced: '#10b98180',        // test_case → execution_result
  triggered: '#ef444480',       // execution_result → defect
  // R330 P4 — Architecture Discovery edge kinds
  derives_from: '#f59e0b80',    // token → token (auth chain)
  flows_from: '#94a3b880',      // var → endpoint (data flow)
  depends_on: '#6366f180',      // endpoint → endpoint (dependency)
  calls: '#84cc1680',
}

const STATUS_COLORS: Record<string, string> = {
  PASS: '#10b981',
  FAIL: '#ef4444',
  SKIP: '#f59e0b',
}

// Column x-positions (fraction of width) for hierarchical layout — the deck's
// complete chain left→right.
const LAYER_X: Record<string, number> = {
  requirement: 0.04,
  risk_profile: 0.13,
  dataset_recipe: 0.22,
  fixture: 0.31,
  recipe_verifier: 0.40,
  acceptance_criteria: 0.48,
  scenario: 0.57,
  frontend_route: 0.61,   // FE-sync P3 — UI route, left of test/endpoint
  test_case: 0.66,
  endpoint: 0.70,         // FE-sync P3 — API endpoint, right of test_case (own column, no 0.5 collision)
  spec: 0.75,
  execution: 0.85,
  result: 0.85,
  execution_result: 0.85,  // FE-sync (R311)
  defect: 0.94,
  // R330 P4 — Architecture Discovery columns (service → token/claim → endpoint → var)
  service: 0.10,
  token: 0.30,
  claim: 0.45,
  step: 0.62,
  var: 0.82,
  env_var: 0.82,
  datastore: 0.90,
}

// Node dimensions by type (width × height)
const NODE_SIZE: Record<string, [number, number]> = {
  requirement: [150, 44],
  acceptance_criteria: [120, 34],
  test_case: [120, 34],
  result: [26, 26],        // small status circle
  execution_result: [26, 26],  // FE-sync (R311) — small status circle
  defect: [100, 30],
  // WS1 — full-chain stages
  risk_profile: [110, 32],
  dataset_recipe: [120, 32],
  fixture: [100, 30],
  recipe_verifier: [110, 30],
  scenario: [100, 30],
  spec: [120, 32],
  execution: [110, 32],
  // FE-sync P3
  endpoint: [140, 32],
  frontend_route: [130, 30],
  // R330 P4 — Architecture Discovery kinds
  service: [140, 36],
  token: [120, 32],
  claim: [100, 28],
  step: [100, 28],
  var: [90, 26],
  env_var: [90, 26],
  datastore: [120, 32],
}

export default function TraceabilityGraph({ data, executionResults }: { data: GraphData | null; executionResults?: Record<string, { status: string }> }) {
  const svgRef = useRef<SVGSVGElement>(null)
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null)
  const [dimensions, setDimensions] = useState({ width: 900, height: 600 })

  // Responsive sizing
  useEffect(() => {
    const updateSize = () => {
      const container = svgRef.current?.parentElement
      if (container) {
        setDimensions({
          width: container.clientWidth,
          height: Math.max(500, window.innerHeight - 340),
        })
      }
    }
    updateSize()
    window.addEventListener('resize', updateSize)
    return () => window.removeEventListener('resize', updateSize)
  }, [])

  // Hierarchical DAG layout + D3 rendering
  useEffect(() => {
    if (!svgRef.current) return
    const svg = d3.select(svgRef.current)
    svg.selectAll('*').remove()

    // Check for empty/no data
    if (!data || !data.nodes || data.nodes.length === 0) return

    const { width, height } = dimensions
    const nodes = data.nodes.filter(d => d.id && d.id !== 'None').map(d => ({ ...d }))
    const nodeIds = new Set(nodes.map(n => n.id))

    // Build edges — filter out invalid references
    const links = data.edges
      .filter(d => d.source && d.target)
      .filter(d => {
        const srcId = typeof d.source === 'string' ? d.source : (d.source as any)?.id
        const tgtId = typeof d.target === 'string' ? d.target : (d.target as any)?.id
        return srcId && srcId !== 'None' && tgtId && tgtId !== 'None' && nodeIds.has(srcId) && nodeIds.has(tgtId)
      })
      .map(d => ({
        source: typeof d.source === 'string' ? d.source : (d.source as any).id,
        target: typeof d.target === 'string' ? d.target : (d.target as any).id,
        type: d.type,
      }))

    // Build adjacency for detecting uncovered ACs
    const acWithTests = new Set<string>()
    for (const e of links) {
      if (e.type === 'covers') {
        // The source of a "covers" edge is the AC or requirement
        const srcNode = nodes.find(n => n.id === e.source)
        if (srcNode?.type === 'acceptance_criteria') acWithTests.add(e.source)
        // If source is a requirement, find ACs in that req
        if (srcNode?.type === 'requirement') {
          for (const e2 of links) {
            if (e2.type === 'has_ac' && e2.source === srcNode.id) acWithTests.add(e2.target)
          }
        }
      }
    }

    // ── Layered layout: assign (x, y) per node ──────────────────────────
    // Group nodes by type (column)
    const byType: Record<string, GraphNode[]> = {}
    for (const n of nodes) {
      const t = n.type || 'test_case'
      if (!byType[t]) byType[t] = []
      byType[t].push(n)
    }

    // Fixed minimum spacing — never squeeze nodes. Graph grows to fit content.
    const MIN_SPACING = 60
    const pad = { top: 50, bottom: 50, left: 40, right: 40 }
    const maxGroupSize = Math.max(...Object.values(byType).map(g => g.length), 1)
    const contentHeight = Math.max(maxGroupSize * MIN_SPACING + pad.top + pad.bottom, height)
    const contentWidth = width

    for (const [type, group] of Object.entries(byType)) {
      const xFrac = LAYER_X[type] ?? 0.5
      const x = pad.left + xFrac * (contentWidth - pad.left - pad.right)
      const totalH = (group.length - 1) * MIN_SPACING
      const startY = (contentHeight - totalH) / 2
      group.forEach((n, i) => {
        n.x = x
        n.y = startY + i * MIN_SPACING
      })
    }

    // Zoom container
    const g = svg.append('g')
    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.15, 4])
      .on('zoom', (event) => g.attr('transform', event.transform))
    svg.call(zoom)

    // Auto-zoom-to-fit: scale the graph so all nodes are visible in the viewport
    const fitScale = Math.min(width / contentWidth, height / contentHeight, 1) * 0.9
    const fitTx = (width - contentWidth * fitScale) / 2
    const fitTy = (height - contentHeight * fitScale) / 2
    svg.call(zoom.transform as any, d3.zoomIdentity.translate(fitTx, fitTy).scale(fitScale))

    // ── Draw edges (bezier curves) ──────────────────────────────────────
    const nodeMap = new Map(nodes.map(n => [n.id, n]))

    g.selectAll('.edge')
      .data(links)
      .join('path')
      .attr('class', 'edge')
      .attr('d', (d: any) => {
        const src = nodeMap.get(d.source)
        const tgt = nodeMap.get(d.target)
        if (!src || !tgt) return ''
        const srcSize = NODE_SIZE[src.type] || [100, 36]
        const tgtSize = NODE_SIZE[tgt.type] || [100, 36]
        const x0 = (src.x || 0) + srcSize[0] / 2
        const y0 = src.y || 0
        const x1 = (tgt.x || 0) - tgtSize[0] / 2
        const y1 = tgt.y || 0
        const mx = (x0 + x1) / 2
        return `M${x0},${y0} C${mx},${y0} ${mx},${y1} ${x1},${y1}`
      })
      .attr('fill', 'none')
      .attr('stroke', (d: any) => EDGE_COLORS[d.type] || '#94a3b840')
      .attr('stroke-width', 1.5)
      .attr('stroke-dasharray', (d: any) => {
        // Dashed for links to uncovered ACs
        if (d.type === 'has_ac') {
          const tgtNode = nodeMap.get(d.target)
          if (tgtNode?.type === 'acceptance_criteria' && !acWithTests.has(d.target)) return '6,4'
        }
        return null
      })
      .attr('marker-end', (d: any) => `url(#arrow-${d.type})`)

    // Arrow markers. FE-sync P3 — build a marker for EVERY edge type in
    // EDGE_COLORS (not the old 4-item literal), so the spine edges
    // (implemented_by/exercises/invokes/tested_by) AND the pre-existing WS1
    // edges all get an arrowhead instead of falling back to arrow-less gray.
    const defs = svg.append('defs')
    for (const t of Object.keys(EDGE_COLORS)) {
      defs.append('marker')
        .attr('id', `arrow-${t}`)
        .attr('viewBox', '0 -5 10 10')
        .attr('refX', 10).attr('refY', 0)
        .attr('markerWidth', 6).attr('markerHeight', 6)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,-4L10,0L0,4')
        .attr('fill', EDGE_COLORS[t] || '#94a3b8')
    }

    // ── Draw nodes ──────────────────────────────────────────────────────
    const nodeColor = (d: GraphNode) => {
      if (d.type === 'acceptance_criteria') {
        const isUncovered = !acWithTests.has(d.id)
        if (isUncovered) return COVERAGE_COLORS.NONE
        if (d.coverage_level) return COVERAGE_COLORS[d.coverage_level] || NODE_TYPE_COLORS[d.type]
      }
      if (d.type === 'test_case' && executionResults) {
        const result = executionResults[d.id] || executionResults[d.label]
        if (result) return STATUS_COLORS[result.status] || NODE_TYPE_COLORS[d.type]
      }
      if (d.type === 'test_case' && d.status) {
        return STATUS_COLORS[d.status] || NODE_TYPE_COLORS[d.type]
      }
      return NODE_TYPE_COLORS[d.type] || d.color || '#6366f1'
    }

    const nodeGroups = g.selectAll('.node')
      .data(nodes)
      .join('g')
      .attr('class', 'node')
      .attr('transform', (d: any) => `translate(${d.x},${d.y})`)
      .style('cursor', 'pointer')

    // Rounded rectangles for most node types
    nodeGroups.filter((d: any) => d.type !== 'result' && d.type !== 'execution_result')
      .append('rect')
      .attr('x', (d: any) => -(NODE_SIZE[d.type]?.[0] || 100) / 2)
      .attr('y', (d: any) => -(NODE_SIZE[d.type]?.[1] || 36) / 2)
      .attr('width', (d: any) => NODE_SIZE[d.type]?.[0] || 100)
      .attr('height', (d: any) => NODE_SIZE[d.type]?.[1] || 36)
      .attr('rx', 8)
      .attr('fill', (d: any) => {
        const c = nodeColor(d)
        return `${c}18`  // 10% opacity fill
      })
      .attr('stroke', (d: any) => nodeColor(d))
      .attr('stroke-width', 2)
      .attr('stroke-dasharray', (d: any) => {
        // Dashed border for uncovered ACs
        if (d.type === 'acceptance_criteria' && !acWithTests.has(d.id)) return '6,4'
        return null
      })

    // Small circles for result/status nodes
    nodeGroups.filter((d: any) => d.type === 'result' || d.type === 'execution_result')
      .append('circle')
      .attr('r', 13)
      .attr('fill', (d: any) => nodeColor(d))
      .attr('fill-opacity', 0.3)
      .attr('stroke', (d: any) => nodeColor(d))
      .attr('stroke-width', 2)

    // Node label text (ID)
    nodeGroups.filter((d: any) => d.type !== 'result' && d.type !== 'execution_result')
      .append('text')
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'central')
      .attr('dy', -2)
      .attr('font-size', (d: any) => d.type === 'requirement' ? '11px' : '10px')
      .attr('font-weight', 'bold')
      .attr('font-family', "'Space Mono', monospace")
      .attr('fill', (d: any) => nodeColor(d))
      .text((d: any) => {
        // Short label: ID for short IDs, truncated label otherwise
        const id = d.id || ''
        if (id.length <= 16) return id
        return id.slice(0, 14) + '…'
      })

    // Subtitle text (description) below ID
    nodeGroups.filter((d: any) => d.type !== 'result' && d.type !== 'execution_result' && d.label && d.label !== d.id)
      .append('text')
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'central')
      .attr('dy', 12)
      .attr('font-size', '8px')
      .attr('fill', '#64748b')
      .text((d: any) => {
        const label = d.label || ''
        return label.length > 20 ? label.slice(0, 18) + '…' : label
      })

    // Status text on result nodes
    nodeGroups.filter((d: any) => d.type === 'result' || d.type === 'execution_result')
      .append('text')
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'central')
      .attr('font-size', '9px')
      .attr('font-weight', 'bold')
      .attr('fill', (d: any) => nodeColor(d))
      .text((d: any) => d.status || d.label || '')

    // UNCOVERED label for ACs without tests
    nodeGroups.filter((d: any) => d.type === 'acceptance_criteria' && !acWithTests.has(d.id))
      .append('text')
      .attr('text-anchor', 'middle')
      .attr('dy', (d: any) => (NODE_SIZE[d.type]?.[1] || 36) / 2 + 14)
      .attr('font-size', '9px')
      .attr('font-weight', 'bold')
      .attr('fill', COVERAGE_COLORS.NONE)
      .text('UNCOVERED')

    // Priority badge on requirement nodes
    nodeGroups.filter((d: any) => d.type === 'requirement' && d.priority)
      .append('text')
      .attr('text-anchor', 'end')
      .attr('x', (d: any) => (NODE_SIZE[d.type]?.[0] || 150) / 2 - 6)
      .attr('y', (d: any) => -(NODE_SIZE[d.type]?.[1] || 44) / 2 + 12)
      .attr('font-size', '9px')
      .attr('font-weight', 'bold')
      .attr('fill', (d: any) => d.priority === 'P0' ? '#fb7185' : d.priority === 'P1' ? '#f59e0b' : '#64748b')
      .text((d: any) => d.priority)

    // Severity badge on defect nodes
    nodeGroups.filter((d: any) => d.type === 'defect' && d.severity)
      .append('text')
      .attr('text-anchor', 'start')
      .attr('x', (d: any) => -(NODE_SIZE[d.type]?.[0] || 110) / 2 + 6)
      .attr('y', (d: any) => -(NODE_SIZE[d.type]?.[1] || 34) / 2 + 11)
      .attr('font-size', '8px')
      .attr('font-weight', 'bold')
      .attr('fill', (d: any) => d.severity === 'P0' ? '#ef4444' : '#f59e0b')
      .text((d: any) => d.severity)

    // Hover effect
    nodeGroups.on('mouseover', function () {
      d3.select(this).select('rect, circle')
        .transition().duration(200)
        .attr('stroke-width', 3)
        .attr('fill-opacity', 0.3)
    })
    nodeGroups.on('mouseout', function () {
      d3.select(this).select('rect')
        .transition().duration(200)
        .attr('stroke-width', 2)
      d3.select(this).select('circle')
        .transition().duration(200)
        .attr('stroke-width', 2)
        .attr('fill-opacity', 0.3)
    })

    // Click handler
    nodeGroups.on('click', (_event: any, d: any) => {
      setSelectedNode((prev: any) => prev?.id === d.id ? null : d)
    })

    // Cleanup
    return () => {
      svg.on('.zoom', null)
    }
  }, [data, dimensions, executionResults])

  // Find connections for selected node
  const connections = selectedNode && data
    ? data.edges.filter(e => {
        const sourceId = typeof e.source === 'string' ? e.source : (e.source as any).id
        const targetId = typeof e.target === 'string' ? e.target : (e.target as any).id
        return sourceId === selectedNode.id || targetId === selectedNode.id
      })
    : []

  // Check for empty data
  const isEmpty = !data || !data.nodes || data.nodes.length === 0

  return (
    <div className="flex gap-4">
      {/* Graph canvas */}
      <div className="flex-1 rounded-xl overflow-hidden" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
        {isEmpty ? (
          <div className="flex flex-col items-center justify-center h-96 gap-3">
            <div className="text-sm" style={{ color: '#64748b' }}>
              No traceability data yet
            </div>
            <p className="text-xs text-center max-w-md" style={{ color: '#94a3b8' }}>
              Generate tests from the Test Architecture page to build the requirement → acceptance criteria → test case → execution result coverage graph.
            </p>
            <a href="/architecture" className="text-xs px-3 py-1.5 rounded-lg transition-colors"
               style={{ background: 'rgba(99,102,241,0.15)', color: '#818cf8', border: '1px solid rgba(99,102,241,0.3)' }}>
              Go to Test Architecture
            </a>
          </div>
        ) : (
          <svg
            ref={svgRef}
            width={dimensions.width - (selectedNode ? 320 : 0)}
            height={dimensions.height}
            style={{ background: '#0a0a14' }}
          />
        )}
        {/* Legend */}
        {!isEmpty && (
          <div className="px-4 py-3" style={{ borderTop: '1px solid #1e1e3a' }}>
            <div className="flex items-center gap-4 flex-wrap">
              {[
                { label: 'Requirement', color: '#6366f1' },
                { label: 'Acceptance Criteria', color: '#8b5cf6' },
                { label: 'Test Case', color: '#06b6d4' },
                { label: 'Endpoint', color: '#84cc16' },
                { label: 'Defect', color: '#ef4444' },
              ].map(item => (
                <div key={item.label} className="flex items-center gap-1.5 text-[10px]" style={{ color: '#94a3b8' }}>
                  <span className="w-2.5 h-2.5 rounded inline-block" style={{ background: item.color, borderRadius: '3px' }} />
                  {item.label}
                </div>
              ))}
              <div className="w-px h-3" style={{ background: '#1e1e3a' }} />
              {[
                { label: 'PASS', color: '#10b981' },
                { label: 'FAIL', color: '#ef4444' },
                { label: 'Uncovered', color: '#fb7185' },
              ].map(item => (
                <div key={item.label} className="flex items-center gap-1.5 text-[10px]" style={{ color: '#94a3b8' }}>
                  <span className="w-2 h-2 rounded-full inline-block" style={{ background: item.color }} />
                  {item.label}
                </div>
              ))}
              <div className="ml-auto text-[10px]" style={{ color: '#94a3b8' }}>
                Scroll to zoom · Click for details
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Detail panel */}
      {selectedNode && (
        <div className="w-72 shrink-0 rounded-xl p-4" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-semibold text-sm" style={{ color: NODE_TYPE_COLORS[selectedNode.type] || selectedNode.color }}>
              {selectedNode.type.replace('_', ' ').toUpperCase()}
            </h3>
            <button onClick={() => setSelectedNode(null)} className="text-xs px-2 py-1 rounded"
                    style={{ background: '#0a0a14', color: '#64748b' }}>
              Close
            </button>
          </div>
          <div className="px-3 py-2 rounded-lg mb-4" style={{ background: '#0a0a14' }}>
            <div className="text-xs font-mono mb-1" style={{ color: NODE_TYPE_COLORS[selectedNode.type] || selectedNode.color }}>
              {selectedNode.id}
            </div>
            <div className="text-sm">{selectedNode.label}</div>
            {selectedNode.priority && (
              <div className="text-[10px] mt-1" style={{ color: '#f59e0b' }}>Priority: {selectedNode.priority}</div>
            )}
            {selectedNode.severity && (
              <div className="text-[10px] mt-1" style={{ color: '#ef4444' }}>Severity: {selectedNode.severity}</div>
            )}
            {selectedNode.tool && (
              <div className="text-[10px] mt-1" style={{ color: '#06b6d4' }}>Tool: {selectedNode.tool}</div>
            )}
            {selectedNode.status && (
              <div className="flex items-center gap-1 text-[10px] mt-1">
                <span className="w-2 h-2 rounded-full inline-block" style={{ background: STATUS_COLORS[selectedNode.status] || '#94a3b8' }} />
                <span style={{ color: STATUS_COLORS[selectedNode.status] || '#94a3b8' }}>{selectedNode.status}</span>
              </div>
            )}
            {selectedNode.coverage_level && (
              <div className="text-[10px] mt-1" style={{ color: COVERAGE_COLORS[selectedNode.coverage_level] || '#94a3b8' }}>
                Coverage: {selectedNode.coverage_level}
              </div>
            )}
          </div>

          <h4 className="text-xs font-semibold mb-2" style={{ color: '#64748b' }}>
            Connections ({connections.length})
          </h4>
          <div className="space-y-1 max-h-80 overflow-y-auto">
            {connections.map((e, i) => {
              const sourceId = typeof e.source === 'string' ? e.source : (e.source as GraphNode).id
              const targetId = typeof e.target === 'string' ? e.target : (e.target as GraphNode).id
              const otherId = sourceId === selectedNode.id ? targetId : sourceId
              const otherNode = data?.nodes.find(n => n.id === otherId)
              const direction = sourceId === selectedNode.id ? '\u2192' : '\u2190'
              return (
                <button key={i}
                        onClick={() => otherNode && setSelectedNode(otherNode)}
                        className="w-full text-left text-xs px-3 py-2 rounded transition-colors hover:brightness-125"
                        style={{ background: '#0a0a14' }}>
                  <span style={{ color: '#94a3b8' }}>{direction}</span>{' '}
                  <span className="font-mono" style={{ color: NODE_TYPE_COLORS[otherNode?.type || ''] || otherNode?.color || '#a78bfa' }}>{otherId}</span>
                  <div className="text-[10px] mt-0.5 truncate" style={{ color: '#64748b' }}>
                    {e.type.replace('_', ' ')} · {otherNode?.label || ''}
                  </div>
                </button>
              )
            })}
            {connections.length === 0 && (
              <div className="text-xs" style={{ color: '#94a3b8' }}>No connections</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
