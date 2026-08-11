'use client'

import { useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'

const COMMANDS = [
  { label: 'Dashboard', href: '/', keywords: 'home overview metrics' },
  { label: 'Test Architecture', href: '/architecture', keywords: 'requirements layers tea' },
  { label: 'Test Explorer', href: '/test-explorer', keywords: 'tests gherkin scenarios' },
  { label: 'AI Assistant', href: '/ai-assistant', keywords: 'chat ai commands generate' },
  { label: 'Defect Intel', href: '/defects', keywords: 'bugs defects root cause' },
  { label: 'Risk Matrix', href: '/risk-matrix', keywords: 'risk impact probability priority' },
  { label: 'Traceability', href: '/traceability', keywords: 'graph coverage matrix requirements' },
  { label: 'Run History', href: '/run-history', keywords: 'runs execution results pipeline' },
  // R306.E: 'Exploratory Testing' deprovisioned — route 308-redirects to /run-history.
  { label: 'Self-Healing', href: '/healing', keywords: 'heal fix selectors proposals' },
  { label: 'NFR Assessment', href: '/nfr-assessment', keywords: 'performance security accessibility' },
  { label: 'SUT Quality', href: '/sut-quality', keywords: 'sut quality verdict gauge grade violations regression endpoint health credibility' },
  { label: 'Settings', href: '/settings', keywords: 'config project llm integrations' },
  { label: 'Admin', href: '/admin', keywords: 'users roles permissions' },
  { label: 'Create New Project', href: '/onboarding', keywords: 'new project onboarding setup' },
]

export default function CommandPalette() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const router = useRouter()

  // Global keyboard shortcut: Cmd+K / Ctrl+K
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault()
        setOpen(prev => !prev)
      }
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [])

  useEffect(() => {
    if (open) {
      setQuery('')
      setSelectedIndex(0)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [open])

  const filtered = COMMANDS.filter(cmd =>
    cmd.label.toLowerCase().includes(query.toLowerCase()) ||
    cmd.keywords.includes(query.toLowerCase())
  )

  const handleSelect = (href: string) => {
    setOpen(false)
    router.push(href)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSelectedIndex(i => Math.min(i + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSelectedIndex(i => Math.max(i - 1, 0))
    } else if (e.key === 'Enter' && filtered[selectedIndex]) {
      handleSelect(filtered[selectedIndex].href)
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[100] flex items-start justify-center pt-[20vh]"
         onClick={() => setOpen(false)}>
      <div className="absolute inset-0" style={{ background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)' }} />
      <div className="relative w-full max-w-md rounded-xl overflow-hidden shadow-2xl"
           style={{ background: '#12121f', border: '1px solid #1e1e3a' }}
           onClick={e => e.stopPropagation()}>
        {/* Search input */}
        <div className="flex items-center gap-3 px-4 py-3" style={{ borderBottom: '1px solid #1e1e3a' }}>
          <span style={{ color: '#94a3b8' }}>&#x2315;</span>
          <input
            ref={inputRef}
            value={query}
            onChange={e => { setQuery(e.target.value); setSelectedIndex(0) }}
            onKeyDown={handleKeyDown}
            placeholder="Search pages..."
            className="flex-1 bg-transparent text-sm text-white outline-none placeholder-gray-600"
          />
          <kbd className="text-[9px] px-1.5 py-0.5 rounded" style={{ background: '#1e1e3a', color: '#94a3b8' }}>
            ESC
          </kbd>
        </div>

        {/* Results */}
        <div className="max-h-64 overflow-y-auto p-2">
          {filtered.map((cmd, i) => (
            <button
              key={cmd.href}
              onClick={() => handleSelect(cmd.href)}
              className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-left transition-colors"
              style={{
                background: i === selectedIndex ? 'rgba(99,102,241,0.15)' : 'transparent',
                color: i === selectedIndex ? '#c7d2fe' : '#94a3b8',
              }}
            >
              <span className="text-xs" style={{ color: '#94a3b8' }}>&#x2192;</span>
              {cmd.label}
            </button>
          ))}
          {filtered.length === 0 && (
            <div className="text-xs text-center py-4" style={{ color: '#94a3b8' }}>
              No results found
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 flex items-center gap-4 text-[10px]"
             style={{ background: '#0a0a14', borderTop: '1px solid #1e1e3a', color: '#94a3b8' }}>
          <span>&#x2191;&#x2193; Navigate</span>
          <span>&#x23CE; Open</span>
          <span>ESC Close</span>
        </div>
      </div>
    </div>
  )
}
