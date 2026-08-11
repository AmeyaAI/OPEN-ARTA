'use client'

// R54.1 — Regenerate by Tool modal. Targeted regen for one automation
// tool across a project's requirements. Calls the R53 backend endpoint
// /api/tests/regenerate-by-tool via the regenerateByTool helper in
// api-client. Pairs with the "🔄 Regenerate by Tool" button on the
// Test Architecture page.

import { useEffect, useMemo, useState } from 'react'
import type { Requirement } from '@/lib/api-client'
import { usePermissions } from '@/lib/permissions'

interface RegenerateByToolModalProps {
  open: boolean
  onClose: () => void
  projectId: string
  requirements: Requirement[]
  defaultRequirementId?: string
  /** R57.11 — pre-select a tool when opening (e.g., from the inventory-empty banner). */
  defaultTool?: string
  onSubmit: (params: { tool: string; force: boolean; requirementId?: string }) => Promise<void>
  running: boolean
}

interface ToolOption {
  id: string
  label: string
  caption: string
  icon: string
}

const TOOL_OPTIONS: ToolOption[] = [
  { id: 'playwright', label: 'Playwright', caption: 'UI / E2E', icon: '🎭' },
  { id: 'newman',     label: 'Newman',     caption: 'REST API',  icon: '📡' },
  { id: 'k6',         label: 'k6',         caption: 'Performance', icon: '⚡' },
  { id: 'zap',        label: 'ZAP',        caption: 'Security scan', icon: '🛡️' },
  { id: 'axe',        label: 'Axe',        caption: 'Accessibility', icon: '♿' },
  { id: 'pytest',     label: 'Pytest',     caption: 'Analytics / unit', icon: '🐍' },
]

export default function RegenerateByToolModal({
  open,
  onClose,
  projectId,
  requirements,
  defaultRequirementId,
  defaultTool,
  onSubmit,
  running,
}: RegenerateByToolModalProps) {
  const [tool, setTool] = useState<string>('playwright')
  const [scope, setScope] = useState<'all' | 'single'>('all')
  const [reqId, setReqId] = useState<string>('')
  const [force, setForce] = useState<boolean>(true)

  // Sort requirements for predictable dropdown ordering.
  const sortedRequirements = useMemo(() => {
    return [...requirements].sort((a, b) => {
      const aKey = a.req_id || a.id || ''
      const bKey = b.req_id || b.id || ''
      return aKey.localeCompare(bKey)
    })
  }, [requirements])

  // Reset form whenever the modal opens. Honours both defaultRequirementId
  // (operator clicked a requirement row first) and defaultTool (R57.11 —
  // operator clicked the inventory-empty banner for a specific tool).
  useEffect(() => {
    if (!open) return
    // R57.11 — pre-select the tool when defaultTool is supplied (e.g.,
    // operator clicked "Regenerate k6" on the inventory-empty banner).
    // Falls back to Playwright (most-common starting tool) otherwise.
    setTool(defaultTool || 'playwright')
    setForce(true)
    if (defaultRequirementId) {
      setScope('single')
      setReqId(defaultRequirementId)
    } else {
      setScope('all')
      setReqId('')
    }
  }, [open, defaultRequirementId, defaultTool])

  // Escape closes the modal — keyboard parity with backdrop click.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !running) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, running, onClose])

  // RBAC (UX mirror of the backend gate): viewers can't regenerate tests.
  const { canWrite } = usePermissions(projectId)

  if (!open) return null

  const canSubmit = !!projectId && !running && canWrite && (scope === 'all' || !!reqId)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!canSubmit) return
    await onSubmit({
      tool,
      force,
      requirementId: scope === 'single' ? reqId : undefined,
    })
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-labelledby="regen-by-tool-title"
    >
      <button
        type="button"
        aria-label="Close dialog"
        onClick={() => { if (!running) onClose() }}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-default"
      />

      <div
        className="relative w-full max-w-2xl mx-4 rounded-2xl p-6 shadow-2xl"
        style={{ background: '#12121f', border: '1px solid #1e1e3a', backdropFilter: 'blur(20px)' }}
      >
        <div className="flex items-center justify-between mb-2">
          <h2
            id="regen-by-tool-title"
            className="text-lg font-bold"
            style={{ color: '#e2e8f0', fontFamily: 'DM Sans, sans-serif' }}
          >
            🔄 Regenerate Tests by Tool
          </h2>
          <button
            onClick={() => { if (!running) onClose() }}
            disabled={running}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-sm hover:opacity-80 transition-opacity"
            style={{ background: '#1e1e3a', color: '#94a3b8', opacity: running ? 0.4 : 1 }}
            aria-label="Close"
          >
            &#x2715;
          </button>
        </div>

        <p className="text-xs mb-5" style={{ color: '#94a3b8' }}>
          Targeted regen for one automation tool. Use after a generator fix lands
          (e.g., R51 k6 brace-balance autofix) or when one tool&apos;s specs need refresh
          without re-running the others.
        </p>

        <form onSubmit={handleSubmit} className="space-y-5">
          {/* Tool picker — six segmented-control cards */}
          <div>
            <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>
              Tool
            </label>
            <div className="grid grid-cols-3 gap-2">
              {TOOL_OPTIONS.map(opt => {
                const selected = tool === opt.id
                return (
                  <button
                    key={opt.id}
                    type="button"
                    onClick={() => setTool(opt.id)}
                    disabled={running}
                    className="flex flex-col items-start px-3 py-2.5 rounded-lg transition-colors text-left"
                    style={{
                      background: selected ? '#7c3aed22' : '#0a0a14',
                      border: selected ? '1px solid #8b5cf6' : '1px solid #1e1e3a',
                      color: selected ? '#c4b5fd' : '#cbd5e1',
                      cursor: running ? 'not-allowed' : 'pointer',
                      opacity: running ? 0.6 : 1,
                    }}
                  >
                    <div className="flex items-center gap-2 text-sm font-medium">
                      <span>{opt.icon}</span>
                      <span>{opt.label}</span>
                    </div>
                    <span className="text-[11px] mt-0.5" style={{ color: '#64748b' }}>
                      {opt.caption}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Scope selector */}
          <div>
            <label className="block text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>
              Scope
            </label>
            <div className="space-y-2">
              <label
                className="flex items-center gap-2 text-sm cursor-pointer"
                style={{ color: '#cbd5e1' }}
              >
                <input
                  type="radio"
                  name="regen-scope"
                  value="all"
                  checked={scope === 'all'}
                  onChange={() => setScope('all')}
                  disabled={running}
                  className="cursor-pointer"
                />
                All requirements{' '}
                <span className="text-[11px]" style={{ color: '#64748b' }}>
                  ({sortedRequirements.length})
                </span>
              </label>
              <div className="flex items-center gap-3">
                <label
                  className="flex items-center gap-2 text-sm cursor-pointer whitespace-nowrap"
                  style={{ color: '#cbd5e1' }}
                >
                  <input
                    type="radio"
                    name="regen-scope"
                    value="single"
                    checked={scope === 'single'}
                    onChange={() => setScope('single')}
                    disabled={running}
                    className="cursor-pointer"
                  />
                  Single requirement:
                </label>
                <select
                  value={reqId}
                  onChange={e => {
                    setReqId(e.target.value)
                    if (e.target.value) setScope('single')
                  }}
                  disabled={running || scope !== 'single'}
                  className="flex-1 px-3 py-2 rounded-lg text-sm"
                  style={{
                    background: '#0a0a14',
                    border: '1px solid #1e1e3a',
                    color: scope === 'single' ? '#e2e8f0' : '#475569',
                  }}
                >
                  <option value="">— pick a requirement —</option>
                  {sortedRequirements.map(r => {
                    const key = r.req_id || r.id || ''
                    return (
                      <option key={key} value={key}>
                        {key} — {r.title}
                      </option>
                    )
                  })}
                </select>
              </div>
            </div>
          </div>

          {/* Force toggle */}
          <div>
            <label className="flex items-start gap-2 text-sm cursor-pointer" style={{ color: '#cbd5e1' }}>
              <input
                type="checkbox"
                checked={force}
                onChange={e => setForce(e.target.checked)}
                disabled={running}
                className="cursor-pointer mt-0.5"
              />
              <span>
                Force regenerate{' '}
                <span style={{ color: '#94a3b8' }}>(clear existing specs first)</span>
                <span className="block text-[11px] mt-0.5" style={{ color: '#64748b' }}>
                  When unchecked: only fills gaps where the requirement has no spec for this tool yet.
                </span>
              </span>
            </label>
          </div>

          {/* Action row */}
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => { if (!running) onClose() }}
              disabled={running}
              className="px-4 py-2 rounded-lg text-sm"
              style={{
                background: '#1e1e3a',
                color: '#94a3b8',
                cursor: running ? 'not-allowed' : 'pointer',
                opacity: running ? 0.6 : 1,
              }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              title={!canWrite ? 'You have read-only (viewer) access — ask a project admin for tester access to regenerate.' : undefined}
              className="px-4 py-2 rounded-lg text-sm font-medium"
              style={{
                background: canSubmit
                  ? 'linear-gradient(135deg, #7c3aed 0%, #8b5cf6 100%)'
                  : '#3b3b5a',
                color: '#fff',
                cursor: canSubmit ? 'pointer' : 'not-allowed',
                opacity: canSubmit ? 1 : 0.6,
              }}
            >
              {running ? '⏳ Regenerating…' : 'Regenerate'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
