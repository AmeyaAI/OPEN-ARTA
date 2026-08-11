'use client'

/**
 * R28.2 — reusable React Error Boundary for `dynamic(() => import(...))`
 * call sites. Catches `ChunkLoadError` (and any other rendering error)
 * so a single broken chunk doesn't crash the whole route.
 *
 * Wrap any client-side dynamic import:
 *
 *   <ChunkLoadErrorBoundary>
 *     <TraceabilityGraph data={...} />
 *   </ChunkLoadErrorBoundary>
 *
 * On error, renders a Reload button (clearing the asset cache fixes
 * 90% of ChunkLoadError cases — usually a stale HTML referencing a
 * chunk hash from a previous build).
 */
import React from 'react'

interface Props {
  children: React.ReactNode
  fallbackTitle?: string
}

interface State {
  error: Error | null
}

export class ChunkLoadErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error) {
    // Surface to telemetry when wired; for now console.
    // eslint-disable-next-line no-console
    console.error('ChunkLoadErrorBoundary caught:', error)
  }

  reset = () => {
    this.setState({ error: null })
  }

  render() {
    if (this.state.error) {
      const isChunk = /ChunkLoadError|Loading chunk|Failed to fetch dynamically imported module/.test(
        this.state.error.message,
      )
      const title = this.props.fallbackTitle || (isChunk ? 'Could not load this view' : 'Something went wrong')
      return (
        <div
          className="rounded-xl p-6 text-sm"
          style={{ background: '#1a0d12', border: '1px solid #4a1521', color: '#fda4af' }}
        >
          <div className="font-semibold mb-1">{title}</div>
          <div className="text-xs mb-3" style={{ color: '#94a3b8' }}>
            {isChunk
              ? 'The browser cached an older asset URL. Reloading clears it.'
              : this.state.error.message}
          </div>
          <button
            onClick={() => {
              this.reset()
              if (typeof window !== 'undefined') location.reload()
            }}
            className="px-3 py-1 rounded text-xs"
            style={{ background: '#4a1521', color: '#fda4af', border: '1px solid #6b1f33' }}
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
