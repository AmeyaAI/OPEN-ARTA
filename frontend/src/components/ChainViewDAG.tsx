/**
 * Phase H1 — Chain View DAG
 *
 * Renders captured CallChains as a left-to-right directed graph:
 *   - Each chain is a horizontal sequence of endpoint nodes.
 *   - Provides/consumes edges shown as dotted arcs above the row when a
 *     later node consumes a value the earlier node provides.
 *   - Multiple chains stack vertically, sorted by occurrence_count desc.
 *   - Click a node → side panel with full request/response shape.
 *
 * Why SVG instead of D3/dagre: this is a simple layered-LR layout and the
 * existing TraceabilityGraph already pulls in d3-force for a different
 * use case. Pure SVG keeps the bundle small and avoids the layout
 * complexity dagre would solve for free graphs (we already have order).
 *
 * Performance: caps at 50 chains, 20 nodes per chain. Larger projects
 * use the side panel + filter UI to drill in.
 */
'use client'

import { useEffect, useMemo, useState } from 'react'
import {
  fetchCapturedChains,
  type CapturedChain,
} from '@/lib/api-client'

interface Props {
  projectId: string
}

const NODE_W = 120
const NODE_H = 40
const NODE_GAP_X = 32
const NODE_GAP_Y = 28
const ROW_HEIGHT = NODE_H + NODE_GAP_Y + 24 // node + gap + arc lane
const PADDING_X = 24

const METHOD_COLOR: Record<string, string> = {
  GET: '#06b6d4',     // cyan-500
  POST: '#8b5cf6',    // violet-500
  PUT: '#f59e0b',     // amber-500
  PATCH: '#f59e0b',
  DELETE: '#f87171',  // red-400
}

interface Selection {
  chainIdx: number
  nodeIdx: number
}

const truncate = (s: string, n: number) => (s.length <= n ? s : s.slice(0, n - 1) + '…')

export default function ChainViewDAG({ projectId }: Props) {
  const [chains, setChains] = useState<CapturedChain[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selection, setSelection] = useState<Selection | null>(null)

  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    setLoading(true)
    fetchCapturedChains(projectId, 50)
      .then((data) => {
        if (cancelled) return
        setChains(data.chains || [])
        setLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e.message || 'Failed to load chains')
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [projectId])

  const layout = useMemo(() => {
    const maxNodes = Math.max(0, ...chains.map((c) => c.nodes?.length || 0))
    const width =
      PADDING_X * 2 + maxNodes * NODE_W + Math.max(0, maxNodes - 1) * NODE_GAP_X
    const height = chains.length * ROW_HEIGHT + PADDING_X
    return { width, height, maxNodes }
  }, [chains])

  if (loading) {
    return (
      <div className="text-sm" style={{ color: '#64748b' }}>
        Loading captured chains…
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
  if (chains.length === 0) {
    return (
      <div
        className="rounded-lg p-6 text-sm"
        style={{ background: '#0f0f23', border: '1px solid #1e1e3a', color: '#64748b' }}
      >
        <div className="mb-2 font-medium" style={{ color: '#cbd5e1' }}>
          No chains captured yet
        </div>
        Trigger UI Discovery from the project page to harvest chains. Each
        captured chain shows the API call sequence + provides/consumes links
        between endpoints.
      </div>
    )
  }

  const sel = selection ? chains[selection.chainIdx]?.nodes?.[selection.nodeIdx] : null

  return (
    <div className="flex gap-4">
      <div
        className="flex-1 min-w-0 rounded-xl overflow-hidden"
        style={{ background: '#0f0f23', border: '1px solid #1e1e3a' }}
      >
        <div
          className="px-5 py-3 flex items-center justify-between"
          style={{ borderBottom: '1px solid #1e1e3a' }}
        >
          <div>
            <div className="font-semibold text-sm text-white">
              Captured Call Chains
            </div>
            <div className="text-xs mt-0.5" style={{ color: '#64748b' }}>
              {chains.length} chain{chains.length === 1 ? '' : 's'} sorted by
              occurrence_count. Click a node to inspect.
            </div>
          </div>
          <div className="flex items-center gap-3 text-[10px]">
            {Object.entries(METHOD_COLOR).map(([m, c]) => (
              <div key={m} className="flex items-center gap-1.5">
                <span
                  className="w-3 h-3 rounded-sm"
                  style={{ background: c }}
                />
                <span style={{ color: '#94a3b8' }}>{m}</span>
              </div>
            ))}
          </div>
        </div>

        <div style={{ overflow: 'auto', maxHeight: '70vh' }}>
          <svg
            width={layout.width}
            height={layout.height}
            style={{ display: 'block' }}
          >
            {chains.map((chain, ci) => {
              const rowY = ci * ROW_HEIGHT + PADDING_X
              const nodes = chain.nodes || []
              return (
                <g key={chain.chain_id}>
                  {/* Chain label */}
                  <text
                    x={PADDING_X}
                    y={rowY - 6}
                    fill="#64748b"
                    fontSize={10}
                    fontFamily="monospace"
                  >
                    chain {chain.chain_id.slice(0, 8)} · {chain.occurrence_count}× ·{' '}
                    {chain.source_test_id || '(no test)'}
                  </text>

                  {/* Inter-node sequence connectors */}
                  {nodes.map((_, ni) => {
                    if (ni === 0) return null
                    const x1 = PADDING_X + ni * (NODE_W + NODE_GAP_X) - NODE_GAP_X
                    const x2 = PADDING_X + ni * (NODE_W + NODE_GAP_X)
                    const y = rowY + NODE_H / 2
                    return (
                      <line
                        key={`seq-${ci}-${ni}`}
                        x1={x1}
                        y1={y}
                        x2={x2}
                        y2={y}
                        stroke="#1e1e3a"
                        strokeWidth={2}
                      />
                    )
                  })}

                  {/* Provides/consumes arcs */}
                  {nodes.map((node, ni) =>
                    Object.entries(node.consumes || {}).map(([varName, providerIdx]) => {
                      if (typeof providerIdx !== 'number' || providerIdx >= ni) return null
                      const x1 =
                        PADDING_X + providerIdx * (NODE_W + NODE_GAP_X) + NODE_W / 2
                      const x2 = PADDING_X + ni * (NODE_W + NODE_GAP_X) + NODE_W / 2
                      const y = rowY
                      const arcY = y - NODE_GAP_Y
                      const midX = (x1 + x2) / 2
                      return (
                        <g key={`arc-${ci}-${ni}-${varName}`}>
                          <path
                            d={`M ${x1} ${y} Q ${midX} ${arcY} ${x2} ${y}`}
                            fill="none"
                            stroke="#22d3ee"
                            strokeWidth={1}
                            strokeDasharray="3 3"
                            opacity={0.7}
                          />
                          <text
                            x={midX}
                            y={arcY + 8}
                            fill="#22d3ee"
                            fontSize={9}
                            fontFamily="monospace"
                            textAnchor="middle"
                          >
                            {varName}
                          </text>
                        </g>
                      )
                    }),
                  )}

                  {/* Nodes */}
                  {nodes.map((node, ni) => {
                    const x = PADDING_X + ni * (NODE_W + NODE_GAP_X)
                    const y = rowY
                    const color = METHOD_COLOR[node.method] || '#94a3b8'
                    const isSelected =
                      selection?.chainIdx === ci && selection?.nodeIdx === ni
                    return (
                      <g
                        key={`node-${ci}-${ni}`}
                        style={{ cursor: 'pointer' }}
                        onClick={() => setSelection({ chainIdx: ci, nodeIdx: ni })}
                      >
                        <rect
                          x={x}
                          y={y}
                          width={NODE_W}
                          height={NODE_H}
                          rx={6}
                          fill={isSelected ? color : '#0a0a1c'}
                          stroke={color}
                          strokeWidth={isSelected ? 2 : 1}
                          opacity={isSelected ? 0.9 : 1}
                        />
                        <text
                          x={x + 8}
                          y={y + 16}
                          fill={isSelected ? '#0a0a1c' : color}
                          fontSize={11}
                          fontFamily="monospace"
                          fontWeight="600"
                        >
                          {node.method}
                        </text>
                        <text
                          x={x + 8}
                          y={y + 32}
                          fill={isSelected ? '#0a0a1c' : '#cbd5e1'}
                          fontSize={9}
                          fontFamily="monospace"
                        >
                          {truncate(node.path_template, 16)}
                        </text>
                        {node.status > 0 && (
                          <text
                            x={x + NODE_W - 6}
                            y={y + 14}
                            fill={
                              node.status >= 400
                                ? '#f87171'
                                : isSelected
                                ? '#0a0a1c'
                                : '#64748b'
                            }
                            fontSize={9}
                            fontFamily="monospace"
                            textAnchor="end"
                          >
                            {node.status}
                          </text>
                        )}
                      </g>
                    )
                  })}
                </g>
              )
            })}
          </svg>
        </div>
      </div>

      {/* Side panel — selected node detail */}
      {sel && selection && (
        <div
          className="w-96 rounded-xl overflow-hidden"
          style={{ background: '#0f0f23', border: '1px solid #1e1e3a' }}
        >
          <div
            className="px-5 py-3 flex items-center justify-between"
            style={{ borderBottom: '1px solid #1e1e3a' }}
          >
            <div className="font-semibold text-sm text-white">Node Detail</div>
            <button
              onClick={() => setSelection(null)}
              className="text-xs px-2 py-1 rounded"
              style={{ background: '#1e1e3a', color: '#94a3b8' }}
            >
              ✕
            </button>
          </div>
          <div className="px-5 py-4 space-y-3 text-xs">
            <div>
              <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
                Endpoint
              </div>
              <div className="font-medium mt-1" style={{ color: '#e2e8f0', fontFamily: 'monospace' }}>
                <span style={{ color: METHOD_COLOR[sel.method] || '#94a3b8' }}>
                  {sel.method}
                </span>{' '}
                {sel.path_template}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
                Status / Duration
              </div>
              <div className="font-medium mt-1" style={{ color: '#e2e8f0', fontFamily: 'monospace' }}>
                {sel.status || '—'} · {sel.duration_ms}ms
              </div>
            </div>
            {Object.keys(sel.provides || {}).length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
                  Provides
                </div>
                <div className="mt-1 space-y-1">
                  {Object.entries(sel.provides).map(([k, v]) => (
                    <div
                      key={k}
                      className="flex justify-between"
                      style={{ fontFamily: 'monospace' }}
                    >
                      <span style={{ color: '#34d399' }}>{k}</span>
                      <span style={{ color: '#94a3b8' }}>{v}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {Object.keys(sel.consumes || {}).length > 0 && (
              <div>
                <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
                  Consumes
                </div>
                <div className="mt-1 space-y-1">
                  {Object.entries(sel.consumes).map(([k, providerIdx]) => (
                    <div
                      key={k}
                      className="flex justify-between"
                      style={{ fontFamily: 'monospace' }}
                    >
                      <span style={{ color: '#fbbf24' }}>{k}</span>
                      <span style={{ color: '#94a3b8' }}>
                        from step #{providerIdx}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {sel.response_body_shape && (
              <div>
                <div className="text-[10px] uppercase tracking-wide" style={{ color: '#64748b' }}>
                  Response shape
                </div>
                <pre
                  className="mt-1 p-2 rounded text-[10px] overflow-x-auto"
                  style={{
                    background: '#0a0a1c',
                    color: '#94a3b8',
                    fontFamily: 'monospace',
                    border: '1px solid #1e1e3a',
                  }}
                >
                  {JSON.stringify(sel.response_body_shape, null, 2)}
                </pre>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
