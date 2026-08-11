'use client'

import React, { Component, ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
  fallback?: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('[ARTA ErrorBoundary]', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback
      return (
        <div className="flex items-center justify-center min-h-[300px] p-8">
          <div className="rounded-xl p-6 max-w-md w-full text-center"
               style={{ background: '#12121f', border: '1px solid #7f1d1d' }}>
            <div className="w-12 h-12 rounded-full flex items-center justify-center mx-auto mb-4"
                 style={{ background: 'rgba(251,113,133,0.12)' }}>
              <span className="text-xl">!</span>
            </div>
            <h3 className="text-base font-semibold mb-2" style={{ color: '#fb7185' }}>
              Something went wrong
            </h3>
            <p className="text-sm mb-4" style={{ color: '#94a3b8' }}>
              {this.state.error?.message || 'An unexpected error occurred'}
            </p>
            <button
              onClick={() => this.setState({ hasError: false, error: null })}
              className="px-4 py-2 rounded-lg text-sm font-medium transition hover:opacity-90"
              style={{ background: '#6366f1', color: '#fff' }}
            >
              Try Again
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
