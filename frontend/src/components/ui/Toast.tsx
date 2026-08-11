'use client'

import React, { createContext, useCallback, useContext, useState, ReactNode, useEffect } from 'react'

type ToastType = 'success' | 'error' | 'info' | 'warning'

interface Toast {
  id: string
  message: string
  type: ToastType
  duration?: number
}

interface ToastContextValue {
  toast: (message: string, type?: ToastType, duration?: number) => void
  success: (message: string) => void
  error: (message: string) => void
  info: (message: string) => void
  warning: (message: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

const TYPE_STYLES: Record<ToastType, { bg: string; border: string; color: string; icon: string }> = {
  success: { bg: '#0a1a0a', border: '#166534', color: '#34d399', icon: '\u2713' },
  error:   { bg: '#1a0a0a', border: '#7f1d1d', color: '#fb7185', icon: '!' },
  info:    { bg: '#0a0a1a', border: '#1e3a5f', color: '#60a5fa', icon: 'i' },
  warning: { bg: '#1a1a0a', border: '#78350f', color: '#fbbf24', icon: '\u26A0' },
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const removeToast = useCallback((id: string) => {
    setToasts(prev => prev.filter(t => t.id !== id))
  }, [])

  const addToast = useCallback((message: string, type: ToastType = 'info', duration = 4000) => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    setToasts(prev => [...prev, { id, message, type, duration }])
    if (duration > 0) {
      setTimeout(() => removeToast(id), duration)
    }
  }, [removeToast])

  const value: ToastContextValue = {
    toast: addToast,
    success: (msg) => addToast(msg, 'success'),
    error: (msg) => addToast(msg, 'error'),
    info: (msg) => addToast(msg, 'info'),
    warning: (msg) => addToast(msg, 'warning'),
  }

  return (
    <ToastContext.Provider value={value}>
      {children}
      {/* Toast container */}
      <div className="fixed bottom-4 right-4 z-[9999] flex flex-col-reverse gap-2 pointer-events-none"
           style={{ maxWidth: 380 }}>
        {toasts.map(t => (
          <ToastItem key={t.id} toast={t} onDismiss={() => removeToast(t.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  )
}

function ToastItem({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const s = TYPE_STYLES[toast.type]
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true))
  }, [])

  return (
    <div
      className="pointer-events-auto flex items-start gap-3 px-4 py-3 rounded-lg text-sm shadow-lg transition-all duration-300"
      style={{
        background: s.bg,
        border: `1px solid ${s.border}`,
        opacity: visible ? 1 : 0,
        transform: visible ? 'translateX(0)' : 'translateX(100%)',
      }}
    >
      <span className="font-bold text-xs mt-0.5 w-4 h-4 rounded-full flex items-center justify-center flex-shrink-0"
            style={{ background: `${s.color}22`, color: s.color }}>
        {s.icon}
      </span>
      <span style={{ color: '#e2e8f0' }} className="flex-1">{toast.message}</span>
      <button onClick={onDismiss} className="text-xs ml-2 flex-shrink-0 hover:opacity-70"
              style={{ color: '#94a3b8' }}>\u2715</button>
    </div>
  )
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within a ToastProvider')
  return ctx
}
