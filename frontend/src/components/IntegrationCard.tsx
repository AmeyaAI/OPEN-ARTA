'use client'

import { ReactNode } from 'react'

type IntegrationCardProps = {
  icon: string
  name: string
  category?: string
  description: string
  connected: boolean
  onConfigure?: () => void
  onTest?: () => void
  children?: ReactNode
}

export default function IntegrationCard({
  icon,
  name,
  category,
  description,
  connected,
  onConfigure,
  onTest,
  children,
}: IntegrationCardProps) {
  return (
    <div
      className="rounded-xl overflow-hidden transition-colors"
      style={{
        background: '#12121f',
        border: '1px solid #1e1e3a',
        backdropFilter: 'blur(12px)',
      }}
    >
      {/* Header */}
      <div className="px-5 py-4 flex items-center gap-4" style={{ borderBottom: '1px solid #1e1e3a' }}>
        <span className="text-2xl">{icon}</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm text-white">{name}</span>
            {category && (
              <span
                className="text-[10px] px-2 py-0.5 rounded-full font-medium"
                style={{ background: 'rgba(99,102,241,0.12)', color: '#818cf8' }}
              >
                {category}
              </span>
            )}
          </div>
          <p className="text-xs mt-1 leading-relaxed" style={{ color: '#64748b' }}>
            {description}
          </p>
        </div>
      </div>

      {/* Status + Actions */}
      <div className="px-5 py-3 flex items-center justify-between">
        {/* Status indicator */}
        <div className="flex items-center gap-2">
          <span
            className="w-2 h-2 rounded-full"
            style={{ background: connected ? '#10b981' : '#94a3b8' }}
          />
          <span
            className="text-xs font-medium"
            style={{ color: connected ? '#10b981' : '#64748b' }}
          >
            {connected ? 'Connected' : 'Not Configured'}
          </span>
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-2">
          {onTest && (
            <button
              onClick={onTest}
              className="text-xs px-3 py-1.5 rounded-lg transition-colors hover:bg-white/[0.03]"
              style={{ color: '#06b6d4' }}
            >
              Test
            </button>
          )}
          {onConfigure && (
            <button
              onClick={onConfigure}
              className="text-xs px-3 py-1.5 rounded-lg transition-colors hover:bg-white/[0.05]"
              style={{ color: '#94a3b8', border: '1px solid #1e1e3a' }}
            >
              Configure
            </button>
          )}
        </div>
      </div>

      {/* Expandable children slot */}
      {children && (
        <div className="px-5 pb-4" style={{ borderTop: '1px solid #1e1e3a' }}>
          <div className="pt-4">
            {children}
          </div>
        </div>
      )}
    </div>
  )
}

export type { IntegrationCardProps }
