'use client'

import { useState, useRef, useEffect } from 'react'
import { useNotifications } from '@/lib/notification-context'

function fmtTimestamp(iso: string) {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })
}

const ICON_COLORS: Record<string, string> = {
  '✓': '#34d399',
  '✗': '#fb7185',
  '⚕': '#a78bfa',
  '▼': '#fbbf24',
  '▶': '#06b6d4',
}

export default function NotificationBell() {
  const { notifications, unreadCount, markRead, markAllRead, dismiss } = useNotifications()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // Close on outside click
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div ref={ref} className="relative">
      {/* Bell button */}
      <button
        onClick={() => setOpen(!open)}
        className="relative flex items-center justify-center w-8 h-8 rounded-lg transition-colors hover:bg-white/5"
        aria-label="Notifications"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
        {unreadCount > 0 && (
          <span
            className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 flex items-center justify-center rounded-full text-[10px] font-bold text-white px-1"
            style={{ background: '#fb7185' }}
          >
            {unreadCount}
          </span>
        )}
      </button>

      {/* Dropdown panel */}
      {open && (
        <div
          className="absolute bottom-full left-0 mb-2 w-80 rounded-xl overflow-hidden z-50"
          style={{
            background: '#0d0d1a',
            border: '1px solid #2d2d4a',
            boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
          }}
        >
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: '1px solid #1e1e3a' }}>
            <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: '#94a3b8' }}>
              Notifications
            </span>
            {unreadCount > 0 && (
              <button
                onClick={markAllRead}
                className="text-[11px] hover:underline"
                style={{ color: '#6366f1' }}
              >
                Mark all read
              </button>
            )}
          </div>

          {/* Notification list */}
          <div className="overflow-y-auto" style={{ maxHeight: 400 }}>
            {notifications.length === 0 && (
              <div className="px-4 py-8 text-center text-xs" style={{ color: '#94a3b8' }}>
                No notifications
              </div>
            )}
            {notifications.map(n => (
              <div
                key={n.id}
                onClick={() => markRead(n.id)}
                className="flex items-start gap-3 px-4 py-3 cursor-pointer transition-colors hover:bg-white/[0.03]"
                style={{
                  borderBottom: '1px solid #1e1e3a',
                  background: n.read ? 'transparent' : 'rgba(99,102,241,0.06)',
                }}
              >
                {/* Icon */}
                <span
                  className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-sm"
                  style={{
                    background: `${ICON_COLORS[n.icon] || '#6366f1'}18`,
                    color: ICON_COLORS[n.icon] || '#6366f1',
                  }}
                >
                  {n.icon}
                </span>

                {/* Content */}
                <div className="flex-1 min-w-0">
                  <p className="text-xs leading-relaxed" style={{ color: n.read ? '#64748b' : '#e2e8f0' }}>
                    {n.message}
                  </p>
                  <p className="text-[10px] mt-1" style={{ color: '#94a3b8', fontFamily: "'Space Mono', monospace" }}>
                    {fmtTimestamp(n.timestamp)}
                  </p>
                </div>

                {/* Unread dot + dismiss */}
                <div className="flex flex-col items-center gap-1.5 flex-shrink-0 pt-0.5">
                  {!n.read && (
                    <span className="w-2 h-2 rounded-full" style={{ background: '#6366f1' }} />
                  )}
                  <button
                    onClick={(e) => { e.stopPropagation(); dismiss(n.id) }}
                    className="text-[11px] hover:text-white transition-colors"
                    style={{ color: '#94a3b8' }}
                    aria-label="Dismiss"
                  >
                    &times;
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
