'use client'

import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from 'react'

export interface Notification {
  id: string
  icon: string
  message: string
  timestamp: string
  read: boolean
}

interface NotificationState {
  notifications: Notification[]
  unreadCount: number
  addNotification: (n: Omit<Notification, 'id' | 'read'>) => void
  markRead: (id: string) => void
  markAllRead: () => void
  dismiss: (id: string) => void
}

const NotificationContext = createContext<NotificationState>({
  notifications: [],
  unreadCount: 0,
  addNotification: () => {},
  markRead: () => {},
  markAllRead: () => {},
  dismiss: () => {},
})

const STORAGE_KEY = 'arta_notifications'

const INITIAL_NOTIFICATIONS: Notification[] = [
  {
    id: 'n1',
    icon: '✓',
    message: 'Quality gate PASSED for build #1247 — all criteria met',
    timestamp: '2026-03-17T09:32:00Z',
    read: false,
  },
  {
    id: 'n2',
    icon: '✗',
    message: 'Test failure: checkout-flow.spec.ts — payment timeout after 30s',
    timestamp: '2026-03-17T09:15:00Z',
    read: false,
  },
  {
    id: 'n3',
    icon: '⚕',
    message: 'Self-healing proposal: selector update for #add-to-cart button (92% confidence)',
    timestamp: '2026-03-17T08:47:00Z',
    read: false,
  },
  {
    id: 'n4',
    icon: '▼',
    message: 'Coverage dropped to 76.2% on feature/auth-refactor — below 80% threshold',
    timestamp: '2026-03-16T22:10:00Z',
    read: false,
  },
  {
    id: 'n5',
    icon: '▶',
    message: 'New test run started: build #1248 on staging (triggered by CI)',
    timestamp: '2026-03-16T21:55:00Z',
    read: false,
  },
]

export function NotificationProvider({ children }: { children: ReactNode }) {
  const [notifications, setNotifications] = useState<Notification[]>(INITIAL_NOTIFICATIONS)

  // Load read state from localStorage on mount
  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY)
      if (stored) {
        const parsed: Notification[] = JSON.parse(stored)
        setNotifications(parsed)
      }
    } catch {
      // ignore
    }
  }, [])

  // Persist to localStorage on change
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(notifications))
    } catch {
      // ignore
    }
  }, [notifications])

  const unreadCount = notifications.filter(n => !n.read).length

  const addNotification = useCallback((n: Omit<Notification, 'id' | 'read'>) => {
    setNotifications(prev => [
      { ...n, id: `n-${Date.now()}`, read: false },
      ...prev,
    ])
  }, [])

  const markRead = useCallback((id: string) => {
    setNotifications(prev => prev.map(n => n.id === id ? { ...n, read: true } : n))
  }, [])

  const markAllRead = useCallback(() => {
    setNotifications(prev => prev.map(n => ({ ...n, read: true })))
  }, [])

  const dismiss = useCallback((id: string) => {
    setNotifications(prev => prev.filter(n => n.id !== id))
  }, [])

  return (
    <NotificationContext.Provider value={{ notifications, unreadCount, addNotification, markRead, markAllRead, dismiss }}>
      {children}
    </NotificationContext.Provider>
  )
}

export function useNotifications() {
  return useContext(NotificationContext)
}
