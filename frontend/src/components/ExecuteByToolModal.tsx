'use client'

// WS4 — Execute by Tool modal. The EXECUTE-arm counterpart to
// RegenerateByToolModal: runs ONE automation tool (optionally scoped to a
// single requirement) via /api/execution/execute-by-tool (executeByTool helper).
// Mirrors the regenerate-by-tool UX so generate + execute feel symmetric.

import { useEffect, useMemo, useState } from 'react'
import type { Requirement } from '@/lib/api-client'
import { usePermissions } from '@/lib/permissions'

interface ExecuteByToolModalProps {
  open: boolean
  onClose: () => void
  projectId: string
  requirements: Requirement[]
  defaultRequirementId?: string
  defaultTool?: string
  onSubmit: (params: { tool: string; environment: string; suiteType: string; requirementId?: string }) => Promise<void>
  running: boolean
}

interface ToolOption { id: string; label: string; caption: string; icon: string }

const TOOL_OPTIONS: ToolOption[] = [
  { id: 'playwright', label: 'Playwright', caption: 'UI / E2E', icon: '🎭' },
  { id: 'newman',     label: 'Newman',     caption: 'REST API',  icon: '📡' },
  { id: 'k6',         label: 'k6',         caption: 'Performance', icon: '⚡' },
  { id: 'zap',        label: 'ZAP',        caption: 'Security scan', icon: '🛡️' },
  { id: 'axe',        label: 'Axe',        caption: 'Accessibility', icon: '♿' },
  { id: 'pytest',     label: 'Pytest',     caption: 'Analytics / unit', icon: '🐍' },
]
const ENVIRONMENTS = ['local', 'staging', 'prod']
const SUITES = ['smoke', 'regression', 'full']

export default function ExecuteByToolModal({
  open, onClose, projectId, requirements, defaultRequirementId, defaultTool, onSubmit, running,
}: ExecuteByToolModalProps) {
  const [tool, setTool] = useState<string>('playwright')
  const [scope, setScope] = useState<'all' | 'single'>('all')
  const [reqId, setReqId] = useState<string>('')
  const [environment, setEnvironment] = useState<string>('staging')
  const [suiteType, setSuiteType] = useState<string>('full')

  const sortedRequirements = useMemo(() =>
    [...requirements].sort((a, b) => ((a.req_id || a.id || '').localeCompare(b.req_id || b.id || ''))),
    [requirements])

  useEffect(() => {
    if (!open) return
    setTool(defaultTool || 'playwright')
    setEnvironment('staging'); setSuiteType('full')
    if (defaultRequirementId) { setScope('single'); setReqId(defaultRequirementId) }
    else { setScope('all'); setReqId('') }
  }, [open, defaultRequirementId, defaultTool])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !running) onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, running, onClose])

  // RBAC (UX mirror of the backend gate): viewers can't execute tests.
  const { canWrite } = usePermissions(projectId)

  if (!open) return null
  const canSubmit = !!projectId && !running && canWrite && (scope === 'all' || !!reqId)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!canSubmit) return
    await onSubmit({ tool, environment, suiteType, requirementId: scope === 'single' ? reqId : undefined })
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" role="dialog" aria-modal="true" aria-labelledby="exec-by-tool-title">
      <button type="button" aria-label="Close dialog" onClick={() => { if (!running) onClose() }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-default" />
      <div className="relative w-full max-w-2xl mx-4 rounded-2xl p-6 shadow-2xl"
           style={{ background: '#12121f', border: '1px solid #1e1e3a', backdropFilter: 'blur(20px)' }}>
        <div className="flex items-center justify-between mb-2">
          <h2 id="exec-by-tool-title" className="text-lg font-bold" style={{ color: '#e2e8f0', fontFamily: 'DM Sans, sans-serif' }}>
            ▶ Execute Tests by Tool
          </h2>
          <button onClick={() => { if (!running) onClose() }} disabled={running}
                  className="w-8 h-8 rounded-lg flex items-center justify-center text-sm hover:opacity-80 transition-opacity"
                  style={{ background: '#1e1e3a', color: '#94a3b8', opacity: running ? 0.4 : 1 }} aria-label="Close">&#x2715;</button>
        </div>
        <p className="text-xs mb-5" style={{ color: '#94a3b8' }}>
          Run ONE automation tool (optionally one requirement), the execute-side counterpart to Regenerate by Tool.
          Useful to re-run just k6 / Newman / Playwright without dispatching the whole suite.
        </p>
        <form onSubmit={handleSubmit} className="space-y-5">
          {/* Tool picker */}
          <div>
            <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>Tool</label>
            <div className="grid grid-cols-3 gap-2">
              {TOOL_OPTIONS.map(opt => {
                const selected = tool === opt.id
                return (
                  <button key={opt.id} type="button" onClick={() => setTool(opt.id)} disabled={running}
                          className="flex flex-col items-start px-3 py-2.5 rounded-lg transition-colors text-left"
                          style={{ background: selected ? '#7c3aed22' : '#0a0a14', border: selected ? '1px solid #8b5cf6' : '1px solid #1e1e3a',
                                   color: selected ? '#c4b5fd' : '#cbd5e1', cursor: running ? 'not-allowed' : 'pointer', opacity: running ? 0.6 : 1 }}>
                    <div className="flex items-center gap-2 text-sm font-medium"><span>{opt.icon}</span><span>{opt.label}</span></div>
                    <span className="text-[11px] mt-0.5" style={{ color: '#64748b' }}>{opt.caption}</span>
                  </button>
                )
              })}
            </div>
          </div>
          {/* Scope selector */}
          <div>
            <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>Scope</label>
            <div className="space-y-2">
              <label className="flex items-center gap-2 text-sm cursor-pointer" style={{ color: '#cbd5e1' }}>
                <input type="radio" name="exec-scope" value="all" checked={scope === 'all'} onChange={() => setScope('all')} disabled={running} className="cursor-pointer" />
                All requirements <span className="text-[11px]" style={{ color: '#64748b' }}>({sortedRequirements.length})</span>
              </label>
              <div className="flex items-center gap-3">
                <label className="flex items-center gap-2 text-sm cursor-pointer whitespace-nowrap" style={{ color: '#cbd5e1' }}>
                  <input type="radio" name="exec-scope" value="single" checked={scope === 'single'} onChange={() => setScope('single')} disabled={running} className="cursor-pointer" />
                  Single requirement:
                </label>
                <select value={reqId} onChange={e => { setReqId(e.target.value); if (e.target.value) setScope('single') }}
                        disabled={running || scope !== 'single'} className="flex-1 px-3 py-2 rounded-lg text-sm"
                        style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: scope === 'single' ? '#e2e8f0' : '#475569' }}>
                  <option value="">Select a requirement</option>
                  {sortedRequirements.map(r => { const key = r.req_id || r.id || ''; return <option key={key} value={key}>{key}: {r.title}</option> })}
                </select>
              </div>
            </div>
          </div>
          {/* Environment + suite */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>Environment</label>
              <select value={environment} onChange={e => setEnvironment(e.target.value)} disabled={running}
                      className="w-full px-3 py-2 rounded-lg text-sm" style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}>
                {ENVIRONMENTS.map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>Suite</label>
              <select value={suiteType} onChange={e => setSuiteType(e.target.value)} disabled={running}
                      className="w-full px-3 py-2 rounded-lg text-sm" style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}>
                {SUITES.map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </div>
          </div>
          {/* Action row */}
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" onClick={() => { if (!running) onClose() }} disabled={running}
                    className="px-4 py-2 rounded-lg text-sm" style={{ background: '#1e1e3a', color: '#94a3b8', cursor: running ? 'not-allowed' : 'pointer', opacity: running ? 0.6 : 1 }}>Cancel</button>
            <button type="submit" disabled={!canSubmit} className="px-4 py-2 rounded-lg text-sm font-medium"
                    title={!canWrite ? 'You have read-only (viewer) access. Ask a project admin for tester access to execute.' : undefined}
                    style={{ background: canSubmit ? 'linear-gradient(135deg, #7c3aed 0%, #8b5cf6 100%)' : '#3b3b5a', color: '#fff', cursor: canSubmit ? 'pointer' : 'not-allowed', opacity: canSubmit ? 1 : 0.6 }}>
              {running ? '⏳ Executing…' : 'Execute'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
