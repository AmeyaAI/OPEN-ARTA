'use client'

import { AuthProvider, useAuth } from '@/lib/auth-context'
import { ProjectProvider, useProject } from '@/lib/project-context'
import { NotificationProvider } from '@/lib/notification-context'
import { TaskProvider, useTasks } from '@/lib/task-context'
import { ToastProvider } from './ui/Toast'
import ErrorBoundary from './ErrorBoundary'
import Sidebar from './Sidebar'
import CommandPalette from './CommandPalette'
import AuthStalenessBadge from './AuthStalenessBadge'
import ReadOnlyBanner from './ReadOnlyBanner'
import { usePathname, useRouter } from 'next/navigation'
import { useEffect, useState, type ReactNode } from 'react'
import { fetchAdminHealth } from '@/lib/api-client'

// Pages reachable WITHOUT an authenticated session. `/accept-invite` is public by design —
// the invite token in the URL is the credential; the invitee is (correctly) not logged in
// yet, so the auth guard must NOT bounce them to /login (that redirect was why invite links
// dead-ended at the login page).
const PUBLIC_PATHS = ['/login', '/oauth-callback', '/onboarding', '/accept-invite']

function AuthenticatedShell({ children }: { children: ReactNode }) {
  const { user, isLoading } = useAuth()
  const pathname = usePathname()
  const router = useRouter()

  const isPublicPage = PUBLIC_PATHS.some(p => pathname.startsWith(p))

  useEffect(() => {
    if (!isLoading && !user && !isPublicPage) {
      router.replace('/login')
      // Fallback: if client-side routing doesn't complete, force hard navigation
      const timeout = setTimeout(() => {
        if (typeof window !== 'undefined') window.location.href = '/login'
      }, 2000)
      return () => clearTimeout(timeout)
    }
  }, [isLoading, user, isPublicPage, router])

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-sm" style={{ color: '#64748b' }}>Loading ARTA…</div>
      </div>
    )
  }

  // Show sidebar when authenticated — wrap with ProjectProvider
  if (user) {
    return (
      <ProjectProvider>
        <NotificationProvider>
          <TaskProvider>
            <ProjectGuard>
              <div className="flex min-h-screen">
                <Sidebar />
                <div className="flex-1 md:ml-56">
                  <ServiceHealthBanner />
                  <ReadOnlyBanner />
                  <AuthStalenessBadge />
                  <GlobalProgressBar />
                  {children}
                </div>
                <CommandPalette />
                {/* Floating AI Assistant Button */}
                <a href="/ai-assistant"
                   className="fixed bottom-6 right-6 z-40 w-12 h-12 rounded-full flex items-center justify-center shadow-lg transition-all hover:scale-110"
                   style={{
                     background: 'linear-gradient(135deg, #6366f1, #8b5cf6)',
                     color: '#fff',
                     fontSize: 18,
                     boxShadow: '0 4px 20px rgba(99,102,241,0.4)',
                   }}
                   title="AI Assistant">
                  {'◈'}
                </a>
              </div>
            </ProjectGuard>
          </TaskProvider>
        </NotificationProvider>
      </ProjectProvider>
    )
  }

  // Public pages (login, oauth callback) render without sidebar
  if (isPublicPage) {
    return <>{children}</>
  }

  // Redirecting to login...
  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="text-sm" style={{ color: '#64748b' }}>Redirecting to login…</div>
    </div>
  )
}

function ProjectGuard({ children }: { children: ReactNode }) {
  const { projects, isLoading } = useProject()
  const router = useRouter()
  const pathname = usePathname()

  useEffect(() => {
    if (!isLoading && projects.length === 0 && !pathname.startsWith('/onboarding') && !pathname.startsWith('/settings')) {
      router.replace('/onboarding')
    }
  }, [isLoading, projects.length, pathname, router])

  if (!isLoading && projects.length === 0 && !pathname.startsWith('/onboarding') && !pathname.startsWith('/settings')) {
    return null // Don't render while redirecting
  }

  return <>{children}</>
}

// F3-5: Polls /api/admin/health every 60s and renders a red banner when any
// backing service is degraded. Doesn't block functionality — warn-only.
function ServiceHealthBanner() {
  const [degraded, setDegraded] = useState<{ services: { name: string; status: string }[] } | null>(null)

  useEffect(() => {
    let cancelled = false
    async function probe() {
      try {
        const result: any = await fetchAdminHealth()
        if (cancelled) return
        if (result?.degraded) {
          setDegraded({ services: result.services || [] })
        } else {
          setDegraded(null)
        }
      } catch {
        // Network error itself is a degradation signal — surface it.
        if (!cancelled) {
          setDegraded({ services: [{ name: 'ARTA API', status: 'Disconnected' }] })
        }
      }
    }
    probe()
    const id = setInterval(probe, 60_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  if (!degraded) return null

  const failing = degraded.services.filter(s => s.status === 'Disconnected').map(s => s.name)
  if (failing.length === 0) return null

  return (
    <div
      role="alert"
      className="px-4 py-2 text-[12px] font-medium flex items-center gap-2"
      style={{
        background: 'rgba(239,68,68,0.12)',
        borderBottom: '1px solid rgba(239,68,68,0.4)',
        color: '#fecaca',
        fontFamily: 'Space Mono, monospace',
      }}
    >
      <span style={{ color: '#ef4444' }}>●</span>
      <span>
        Service degraded — {failing.join(', ')} unreachable.
        Some features may be unavailable.
      </span>
      <a href="/admin" className="ml-auto underline" style={{ color: '#fca5a5' }}>
        View status
      </a>
    </div>
  )
}

function GlobalProgressBar() {
  const { activeTasks } = useTasks()
  if (activeTasks.length === 0) return null

  const task = activeTasks[0]
  const progress = task.progress ?? 0
  const statusColor = '#6366f1'
  const bgGradient = `linear-gradient(90deg, ${statusColor}, #8b5cf6)`

  return (
    <div className="sticky top-0 z-50" style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}>
      {/* Thin animated progress track */}
      <div className="h-1 w-full" style={{ background: '#1e1e3a' }}>
        <div
          className="h-full transition-all duration-500 ease-out"
          style={{
            width: progress > 0 ? `${progress}%` : '100%',
            background: bgGradient,
            ...(progress === 0 ? { animation: 'indeterminate 1.5s infinite ease-in-out' } : {}),
          }}
        />
      </div>
      {/* Status strip */}
      <div className="flex items-center justify-between px-4 py-1.5">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full animate-pulse" style={{ background: statusColor }} />
          <span className="text-[11px] font-medium" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
            {task.label}
          </span>
          {task.detail && (
            <span className="text-[10px]" style={{ color: '#94a3b8' }}>
              — {task.detail}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {progress > 0 && (
            <span className="text-[10px] font-bold" style={{ color: statusColor }}>
              {progress}%
            </span>
          )}
          {activeTasks.length > 1 && (
            <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: '#6366f120', color: '#6366f1', border: '1px solid #6366f130' }}>
              +{activeTasks.length - 1} more
            </span>
          )}
        </div>
      </div>
      <style>{`
        @keyframes indeterminate {
          0% { transform: translateX(-100%); width: 40%; }
          50% { transform: translateX(50%); width: 60%; }
          100% { transform: translateX(200%); width: 40%; }
        }
      `}</style>
    </div>
  )
}

export default function AppShell({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <ToastProvider>
        <ErrorBoundary>
          <AuthenticatedShell>{children}</AuthenticatedShell>
        </ErrorBoundary>
      </ToastProvider>
    </AuthProvider>
  )
}
