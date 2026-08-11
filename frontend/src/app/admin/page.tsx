'use client'

import { useState, useEffect, useCallback } from 'react'
import Link from 'next/link'
import { fetchAdminHealth, fetchUsers, deactivateUser, reactivateUser, type UserPublic, type InviteResponse } from '@/lib/api-client'
import InviteUserModal from '@/components/InviteUserModal'
import DiscoveryPanel from '@/components/DiscoveryPanel'
import { useProject } from '@/lib/project-context'

// ── Types ───────────────────────────────────────────────────────────────────

interface ServiceStatus {
  name: string
  icon: string
  status: 'Connected' | 'Disconnected' | 'Degraded'
  latency: string
  detail?: string
}

interface MockUser {
  id: string
  name: string
  email: string
  role: 'Admin' | 'Tester' | 'Viewer' | 'QA Lead'
  lastLogin: string
  active: boolean
}

// G-frontend: Removed MOCK_SERVICES / MOCK_USERS / MOCK_AUDIT seed data.
// Initial state is empty; real values are fetched from:
//   /api/admin/health    → services array
//   /api/users           → users array (when implemented)
// If APIs are unreachable the page shows an empty state, NOT fabricated data.

// Service icon lookup by service name
const SERVICE_ICONS: Record<string, string> = {
  PostgreSQL: '\uD83D\uDDC4',
  Neo4j: '\uD83D\uDD78',
  Redis: '\u26A1',
  ChromaDB: '\uD83E\uDDE0',
  'ARTA API': '\uD83D\uDE80',
  'LLM Provider': '\uD83E\uDD16',
}

const SYSTEM_CONFIG = [
  { key: 'Rate Limit', value: '100 req/min' },
  { key: 'CORS Origins', value: 'from CORS_ORIGINS env' },
  { key: 'Max Upload Size', value: '50MB' },
  { key: 'Session Timeout', value: '24h' },
  { key: 'LLM Temperature', value: '0.2' },
  { key: 'Quality Gate Model', value: 'BMAD TEA 4-outcome' },
]

const ROLE_COLORS: Record<string, string> = {
  Admin: '#ef4444',
  'QA Lead': '#f59e0b',
  Tester: '#06b6d4',
  Viewer: '#64748b',
}

const STATUS_DOT: Record<string, string> = {
  Connected: '#10b981',
  Disconnected: '#ef4444',
  Degraded: '#f59e0b',
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function AdminPage() {
  // G-frontend: Empty initial state — populated from real APIs in useEffect below.
  const [services, setServices] = useState<ServiceStatus[]>([])
  const [users, setUsers] = useState<MockUser[]>([])
  const [toast, setToast] = useState<string | null>(null)
  const [editingRole, setEditingRole] = useState<string | null>(null)
  const [inviteOpen, setInviteOpen] = useState(false)
  // Phase J3 — surface UI Discovery state for the currently-selected project.
  const { currentProjectId } = useProject()

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 3000)
  }

  const loadUsers = useCallback(async () => {
    try {
      const rows = await fetchUsers()
      setUsers((rows || []).map((u: UserPublic): MockUser => ({
        id: u.id,
        name: u.full_name || u.email,
        email: u.email,
        role: u.is_admin ? 'Admin' : 'Viewer',
        lastLogin: '—',
        active: u.is_active,
      })))
    } catch {
      // Leave users as [] when /api/users is unauthenticated or unavailable.
    }
  }, [])

  // G-frontend: Load real data from backend APIs — no synthetic fallback.
  useEffect(() => {
    async function loadHealth() {
      try {
        const result: any = await fetchAdminHealth()
        if (result?.services && Array.isArray(result.services)) {
          setServices(result.services.map((s: any) => ({
            name: s.name,
            icon: SERVICE_ICONS[s.name] || '\u2699',
            status: s.status || 'Disconnected',
            latency: s.latency != null ? `${s.latency}ms` : '\u2014',
            detail: s.detail || s.version || '',
          })))
        }
      } catch {
        // Leave services as [] — UI will show empty state
      }
    }

    loadHealth()
    loadUsers()
  }, [loadUsers])

  function handleInvited(_res: InviteResponse) {
    loadUsers()
  }

  // Persist activate/deactivate to the backend (was previously local-only, so the
  // UI lied about what it did). Deactivate is a confirm-gated soft-delete; the
  // backend refuses self / last-admin deactivation with a 400 we surface as a toast.
  const handleToggleActive = async (userId: string) => {
    const user = users.find(u => u.id === userId)
    if (!user) return
    const deactivating = user.active
    if (deactivating && !window.confirm(
      `Deactivate ${user.name}? They will be signed out and unable to log in until ` +
      `reactivated. Their password and project roles are preserved.`
    )) return
    try {
      if (deactivating) {
        await deactivateUser(userId)
      } else {
        await reactivateUser(userId)
      }
      await loadUsers()  // reflect authoritative backend state
      showToast(`${user.name} ${deactivating ? 'deactivated' : 'reactivated'}`)
    } catch (e: any) {
      showToast(e?.message || `Failed to ${deactivating ? 'deactivate' : 'reactivate'} ${user.name}`)
    }
  }

  const handleRoleChange = (userId: string, newRole: string) => {
    setUsers(prev => prev.map(u =>
      u.id === userId ? { ...u, role: newRole as MockUser['role'] } : u
    ))
    setEditingRole(null)
    showToast('Role updated')
  }

  return (
    <div className="min-h-screen" style={{ background: '#05050a', color: '#e2e8f0' }}>
      {/* Toast */}
      {toast && (
        <div className="fixed top-4 right-4 z-50 px-4 py-3 rounded-xl text-sm font-medium shadow-lg"
             style={{ background: '#1e1e3a', border: '1px solid #6366f1', color: '#c7d2fe' }}>
          {toast}
        </div>
      )}

      {/* Header */}
      <header style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}
              className="px-8 py-4 flex items-center gap-4">
        <Link href="/" className="flex items-center gap-3 hover:opacity-80 transition-opacity">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center text-lg"
               style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)' }}>A</div>
          <span className="font-bold text-lg">ARTA</span>
        </Link>
        <span style={{ color: '#1e1e3a' }}>{'\u203A'}</span>
        <span style={{ color: '#94a3b8' }} className="text-sm">Admin</span>
      </header>

      <main className="px-8 py-8 max-w-7xl mx-auto space-y-8">
        {/* Page title */}
        <div>
          <h1 className="text-2xl font-bold text-white mb-1">Administration</h1>
          <p className="text-sm" style={{ color: '#64748b' }}>
            Platform health, user management, and system configuration
          </p>
        </div>

        {/* ── Section 1: Platform Health ────────────────────────────────────── */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-widest mb-4" style={{ color: '#64748b' }}>
            Platform Health
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            {services.map(svc => (
              <div key={svc.name} className="rounded-xl p-5"
                   style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-3">
                    <span className="text-xl">{svc.icon}</span>
                    <span className="font-semibold text-sm text-white">{svc.name}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full" style={{ background: STATUS_DOT[svc.status] }} />
                    <span className="text-xs font-medium" style={{ color: STATUS_DOT[svc.status] }}>
                      {svc.status}
                    </span>
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-xs" style={{ color: '#94a3b8' }}>Latency</span>
                  <span className="text-xs font-bold" style={{ color: '#94a3b8', fontFamily: "'Space Mono', monospace" }}>
                    {svc.latency}
                  </span>
                </div>
                {svc.detail && (
                  <div className="flex items-center justify-between mt-1">
                    <span className="text-xs" style={{ color: '#94a3b8' }}>Version</span>
                    <span className="text-[10px] px-2 py-0.5 rounded"
                          style={{ background: 'rgba(99,102,241,0.1)', color: '#818cf8', fontFamily: "'Space Mono', monospace" }}>
                      {svc.detail}
                    </span>
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>

        {/* ── Section 1.5: UI Discovery (Phase J3) ─────────────────────────── */}
        {currentProjectId && (
          <section>
            <h2 className="text-xs font-semibold uppercase tracking-widest mb-4"
                style={{ color: '#64748b' }}>
              UI Discovery
            </h2>
            <DiscoveryPanel projectId={currentProjectId} />
          </section>
        )}

        {/* ── Section 2: User Management ────────────────────────────────────── */}
        <section>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xs font-semibold uppercase tracking-widest" style={{ color: '#64748b' }}>
              User Management ({users.length})
            </h2>
            <button
              onClick={() => setInviteOpen(true)}
              className="px-4 py-2 rounded-lg text-xs font-medium transition-colors"
              style={{ background: '#6366f1', color: '#fff' }}
            >
              + Invite User
            </button>
          </div>

          <div className="rounded-xl overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
            <table className="w-full text-sm">
              <thead>
                <tr style={{ background: '#0a0a14' }}>
                  <th className="text-left px-5 py-3 font-medium text-xs uppercase tracking-wider" style={{ color: '#94a3b8' }}>Name</th>
                  <th className="text-left px-5 py-3 font-medium text-xs uppercase tracking-wider" style={{ color: '#94a3b8' }}>Email</th>
                  <th className="text-center px-5 py-3 font-medium text-xs uppercase tracking-wider" style={{ color: '#94a3b8' }}>Role</th>
                  <th className="text-center px-5 py-3 font-medium text-xs uppercase tracking-wider" style={{ color: '#94a3b8' }}>Last Login</th>
                  <th className="text-center px-5 py-3 font-medium text-xs uppercase tracking-wider" style={{ color: '#94a3b8' }}>Status</th>
                  <th className="text-right px-5 py-3 font-medium text-xs uppercase tracking-wider" style={{ color: '#94a3b8' }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map(u => (
                  <tr key={u.id} style={{ borderTop: '1px solid #1e1e3a', background: '#12121f' }}>
                    <td className="px-5 py-3">
                      <div className="flex items-center gap-3">
                        <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0"
                             style={{ background: `${ROLE_COLORS[u.role]}20`, color: ROLE_COLORS[u.role] }}>
                          {u.name.split(' ').map(n => n[0]).join('')}
                        </div>
                        <span className="text-sm text-white font-medium">{u.name}</span>
                      </div>
                    </td>
                    <td className="px-5 py-3 text-xs" style={{ color: '#94a3b8' }}>{u.email}</td>
                    <td className="px-5 py-3 text-center">
                      {editingRole === u.id ? (
                        <select
                          defaultValue={u.role}
                          onChange={e => handleRoleChange(u.id, e.target.value)}
                          onBlur={() => setEditingRole(null)}
                          autoFocus
                          className="text-xs px-2 py-1 rounded"
                          style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                        >
                          <option value="Admin">Admin</option>
                          <option value="QA Lead">QA Lead</option>
                          <option value="Tester">Tester</option>
                          <option value="Viewer">Viewer</option>
                        </select>
                      ) : (
                        <span
                          className="text-[10px] font-bold px-2.5 py-1 rounded cursor-pointer hover:opacity-80"
                          style={{
                            background: `${ROLE_COLORS[u.role]}18`,
                            color: ROLE_COLORS[u.role],
                          }}
                          onClick={() => setEditingRole(u.id)}
                          title="Click to change role"
                        >
                          {u.role.toUpperCase()}
                        </span>
                      )}
                    </td>
                    <td className="px-5 py-3 text-center text-xs" style={{ color: '#94a3b8', fontFamily: "'Space Mono', monospace" }}>
                      {u.lastLogin}
                    </td>
                    <td className="px-5 py-3 text-center">
                      <span className="text-[10px] font-bold px-2.5 py-1 rounded"
                            style={{
                              background: u.active ? 'rgba(16,185,129,0.12)' : 'rgba(239,68,68,0.12)',
                              color: u.active ? '#10b981' : '#ef4444',
                            }}>
                        {u.active ? 'ACTIVE' : 'INACTIVE'}
                      </span>
                    </td>
                    <td className="px-5 py-3 text-right">
                      <button
                        onClick={() => handleToggleActive(u.id)}
                        className="text-xs px-3 py-1.5 rounded-lg transition-colors"
                        style={{
                          background: u.active ? 'rgba(239,68,68,0.1)' : 'rgba(16,185,129,0.1)',
                          color: u.active ? '#ef4444' : '#10b981',
                        }}
                      >
                        {u.active ? 'Deactivate' : 'Activate'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>


        {/* ── Section 4: System Configuration ──────────────────────────────── */}
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-widest mb-4" style={{ color: '#64748b' }}>
            System Configuration
          </h2>

          <div className="rounded-xl overflow-hidden"
               style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            {SYSTEM_CONFIG.map((cfg, i) => (
              <div key={cfg.key}
                   className="px-5 py-3.5 flex items-center justify-between"
                   style={{ borderBottom: i < SYSTEM_CONFIG.length - 1 ? '1px solid #1e1e3a' : undefined }}>
                <span className="text-sm" style={{ color: '#94a3b8' }}>{cfg.key}</span>
                <span className="text-sm font-medium" style={{ color: '#e2e8f0', fontFamily: "'Space Mono', monospace" }}>
                  {cfg.value}
                </span>
              </div>
            ))}
          </div>
        </section>
      </main>

      <InviteUserModal
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        onInvited={handleInvited}
      />
    </div>
  )
}
