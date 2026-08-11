'use client'

import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { useAuth } from '@/lib/auth-context'
import { useProject } from '@/lib/project-context'
import React, { useState, useRef, useEffect } from 'react'
import NotificationBell from './NotificationBell'

const NAV_ITEMS = [
  { href: '/',               label: 'Dashboard',        icon: '◉' },
  { href: '/architecture',   label: 'Test Architecture', icon: '△' },
  { href: '/test-explorer',  label: 'Test Explorer',     icon: '◧' },
  { href: '/ai-assistant',   label: 'AI Assistant',      icon: '◈' },
  { href: '/defects',        label: 'Defect Intel',      icon: '⚡' },
  { href: '/triage',         label: 'Triage Queue',      icon: '⚖' },
  { href: '/risk-matrix',    label: 'Risk Matrix',       icon: '◆' },
  { href: '/traceability',   label: 'Traceability',      icon: '⬡' },
  { href: '/run-history',    label: 'Run History',        icon: '◷' },
  // R306.E: /exploratory deprovisioned (broken DB-mode SBTM prototype, disconnected
  // from ARTA's autonomous pillars). Route 308-redirects to /run-history.
  { href: '/healing',       label: 'Self-Healing',        icon: '⚕' },
  { href: '/integrations',  label: 'Integrations',         icon: '⊞' },
  { href: '/nfr-assessment', label: 'NFR Assessment',     icon: '⛊' },
  { href: '/sut-quality',    label: 'SUT Quality',         icon: '◉' },
  { href: '/settings',       label: 'Settings',           icon: '⚙' },
  { href: '/settings/llm',   label: 'LLM Provider',       icon: '▤' },
  { href: '/admin',          label: 'Admin',              icon: '⛨' },
]

const LLM_LABELS: Record<string, string> = {
  anthropic: 'Anthropic Claude',
  google_gemini: 'Google Gemini',
  ollama: 'Ollama (local)',
  openai: 'OpenAI',
  azure_openai: 'Azure OpenAI',
}

function ProjectSwitcher() {
  const { projects, currentProject, switchProject } = useProject()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const router = useRouter()

  // Close on outside click
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  if (!currentProject) return null

  return (
    <div ref={ref} className="relative px-3 pb-2">
      {/* Trigger */}
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-left transition-colors"
        style={{
          background: '#0f0f1e',
          border: '1px solid #1e1e3a',
        }}
      >
        <div className="w-[18px] h-[18px] rounded flex-shrink-0"
             style={{ background: currentProject.color || '#6366f1' }} />
        <div className="flex-1 min-w-0">
          <div className="text-xs font-medium text-white truncate">{currentProject.name}</div>
        </div>
        <span className="text-[10px]" style={{ color: '#94a3b8' }}>{open ? '⌃' : '⌄'}</span>
      </button>

      {/* Dropdown */}
      {open && (
        <div className="absolute left-3 right-3 top-[calc(100%-2px)] z-50 rounded-lg overflow-hidden"
             style={{
               background: '#08081a',
               border: '1px solid #2d2d4a',
               boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
             }}>
          {projects.map(p => {
            const isActive = p.id === currentProject.id
            const prov = (p.llm_config as any)?.provider || ''
            return (
              <button
                key={p.id}
                onClick={() => { switchProject(p.id); setOpen(false) }}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors"
                style={{
                  background: isActive ? 'rgba(99,102,241,0.12)' : 'transparent',
                }}
              >
                <div className="w-[22px] h-[22px] rounded flex items-center justify-center text-xs flex-shrink-0"
                     style={{ background: p.color || '#6366f1' }}>
                  {p.icon || ''}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-xs font-medium truncate"
                       style={{ color: isActive ? '#a5b4fc' : '#e2e8f0' }}>
                    {p.name}
                  </div>
                  <div className="text-[9px] truncate" style={{ color: '#94a3b8' }}>
                    {LLM_LABELS[prov] || prov || 'Not configured'}
                  </div>
                </div>
                {isActive && <span className="text-[10px]" style={{ color: '#6366f1' }}>✓</span>}
              </button>
            )
          })}
          <button
            onClick={() => { router.push('/onboarding'); setOpen(false) }}
            className="w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors"
            style={{
              borderTop: '1px solid #2d2d4a',
              color: '#6366f1',
            }}
          >
            <span className="text-sm leading-none">+</span>
            <span className="text-xs font-medium">New Project</span>
          </button>
        </div>
      )}
    </div>
  )
}

type AgentStatus = 'idle' | 'running' | 'error'

interface AgentInfo {
  name: string
  status: AgentStatus
}

function AgentStatusPanel() {
  const [collapsed, setCollapsed] = useState(false)
  const [agents, setAgents] = useState<AgentInfo[]>([
    { name: 'Risk Analyzer', status: 'idle' },
    { name: 'Test Generator', status: 'idle' },
    { name: 'Execution Agent', status: 'idle' },
  ])

  // Poll real agent status from backend every 10 seconds
  useEffect(() => {
    const fetchStatus = () => {
      fetch('/api/agents/status')
        .then(r => r.json())
        .then(data => {
          if (data.agents) setAgents(data.agents)
        })
        .catch(() => {})
    }
    fetchStatus()
    const interval = setInterval(fetchStatus, 10000)
    return () => clearInterval(interval)
  }, [])

  const activeCount = agents.filter(a => a.status === 'running').length

  const dotStyle = (status: AgentStatus): React.CSSProperties => {
    const base: React.CSSProperties = {
      width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
    }
    if (status === 'running') return { ...base, background: '#10b981', boxShadow: '0 0 6px #10b981' }
    if (status === 'error') return { ...base, background: '#ef4444' }
    return { ...base, background: '#94a3b8' }
  }

  const statusLabel = (status: AgentStatus) => {
    if (status === 'running') return 'running'
    if (status === 'error') return 'error'
    return 'idle'
  }

  const statusColor = (status: AgentStatus) => {
    if (status === 'running') return '#10b981'
    if (status === 'error') return '#ef4444'
    return '#94a3b8'
  }

  return (
    <div className="border-t" style={{ borderColor: '#1e1e3a' }}>
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-left"
      >
        <span className="text-[11px] font-semibold" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>Agents</span>
        <div className="flex items-center gap-2">
          {activeCount > 0 && (
            <span className="text-[9px] px-1.5 py-0.5 rounded"
                  style={{ background: 'rgba(16, 185, 129, 0.15)', color: '#10b981' }}>
              {activeCount} active
            </span>
          )}
          <span className="text-[10px]" style={{ color: '#94a3b8' }}>{collapsed ? '\u25B6' : '\u25BC'}</span>
        </div>
      </button>
      {!collapsed && (
        <div className="px-4 pb-3 space-y-1.5">
          <style>{`@keyframes agentPulse{0%,100%{opacity:1}50%{opacity:0.4}}`}</style>
          {agents.map(agent => (
            <div key={agent.name} className="flex items-center gap-2.5">
              <div style={{
                ...dotStyle(agent.status),
                ...(agent.status === 'running' ? { animation: 'agentPulse 1.5s ease-in-out infinite' } : {}),
              }} />
              <span className="text-[11px] flex-1" style={{ color: '#94a3b8' }}>{agent.name}</span>
              <span className="text-[9px]" style={{ color: statusColor(agent.status), fontFamily: 'Space Mono, monospace' }}>
                {statusLabel(agent.status)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Sidebar() {
  const pathname = usePathname()
  const { user, logout } = useAuth()
  const [mobileOpen, setMobileOpen] = useState(false)

  // Close mobile sidebar on navigation
  useEffect(() => { setMobileOpen(false) }, [pathname])

  const sidebarContent = (
    <>
      {/* Logo */}
      <div className="px-5 py-5 flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg flex items-center justify-center text-lg font-bold"
             style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)', color: '#fff' }}>
          A
        </div>
        <div>
          <div className="font-bold text-sm leading-none text-white">ARTA</div>
          <div className="text-[10px] mt-0.5" style={{ color: '#6366f1' }}>BMAD TEA Platform</div>
        </div>
        <button className="ml-auto md:hidden text-lg" style={{ color: '#64748b' }}
                onClick={() => setMobileOpen(false)}>{'\u2715'}</button>
      </div>

      <ProjectSwitcher />

      {/* Navigation */}
      <nav className="flex-1 px-3 py-2 space-y-0.5 overflow-y-auto">
        {NAV_ITEMS.map(item => {
          const isActive = pathname === item.href || (item.href !== '/' && pathname.startsWith(item.href))
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-all ${
                isActive ? 'text-white' : 'hover:text-white'
              }`}
              style={{
                background: isActive ? 'rgba(99,102,241,0.15)' : 'transparent',
                color: isActive ? '#c7d2fe' : '#64748b',
              }}
            >
              <span className="text-base" style={{ color: isActive ? '#818cf8' : '#94a3b8' }}>
                {item.icon}
              </span>
              {item.label}
            </Link>
          )
        })}
      </nav>

      {/* Agent Status Panel */}
      <AgentStatusPanel />

      {/* User */}
      {user && (
        <div className="px-4 py-4 border-t" style={{ borderColor: '#1e1e3a' }}>
          <div className="flex items-center gap-2 mb-2">
            <div className="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold"
                 style={{ background: '#6366f1', color: '#fff' }}>
              {user.full_name.charAt(0)}
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-xs font-medium text-white truncate">{user.full_name}</div>
              <div className="text-[10px] truncate" style={{ color: '#64748b' }}>{user.email}</div>
            </div>
            <NotificationBell />
          </div>
          <button onClick={logout} className="text-xs w-full text-left px-2 py-1 rounded hover:bg-white/5"
                  style={{ color: '#64748b' }}>
            Sign out
          </button>
        </div>
      )}
    </>
  )

  return (
    <>
      {/* Mobile hamburger */}
      <button
        className="fixed top-4 left-4 z-50 md:hidden w-10 h-10 rounded-lg flex items-center justify-center"
        style={{ background: '#12121f', border: '1px solid #1e1e3a' }}
        onClick={() => setMobileOpen(true)}
      >
        <span className="text-lg" style={{ color: '#94a3b8' }}>{'\u2630'}</span>
      </button>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 md:hidden" style={{ background: 'rgba(0,0,0,0.6)' }}
             onClick={() => setMobileOpen(false)} />
      )}

      {/* Desktop sidebar */}
      <aside className="hidden md:flex fixed left-0 top-0 h-screen w-56 flex-col"
             style={{ background: '#0a0a14', borderRight: '1px solid #1e1e3a' }}>
        {sidebarContent}
      </aside>

      {/* Mobile sidebar */}
      <aside className={`fixed left-0 top-0 h-screen w-64 flex flex-col z-50 md:hidden transition-transform duration-300 ${
        mobileOpen ? 'translate-x-0' : '-translate-x-full'
      }`} style={{ background: '#0a0a14', borderRight: '1px solid #1e1e3a' }}>
        {sidebarContent}
      </aside>
    </>
  )
}
