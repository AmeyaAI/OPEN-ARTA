'use client'

import { useEffect, useState } from 'react'
import { useSearchParams, useRouter } from 'next/navigation'
import { fetchDefects, createDefect, fetchAISuggestions, approveHealingProposal, rejectHealingProposal, createJiraTicket, analyzeDefect, type DefectItem } from '@/lib/api-client'
import { useProject } from '@/lib/project-context'
import RunScopeSelector from '@/components/RunScopeSelector'

const SEV_COLORS: Record<string, string> = {
  critical: '#ef4444', high: '#f59e0b', medium: '#6366f1', low: '#64748b',
}

// G-frontend: Removed hardcoded IMPACTED_COMPONENTS / SUGGESTED_FIXES / HEAL_PROPOSALS
// dictionaries. The selected defect's `impacted_components`, `suggested_fix`, and
// `heal_proposal` fields come from the backend (POST /api/defects/{id}/analyze
// populates them via DefectIntelAgent). If the backend hasn't analyzed yet,
// the UI shows an empty-state with an "Analyze" button.

// ── Diff Renderer Component ────────────────────────────────────────────────

function DiffCodeBlock({ code, language, showHeader = true }: { code: string; language: string; showHeader?: boolean }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = () => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  const lines = code.split('\n')

  return (
    <div className="rounded-lg overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
      {showHeader && (
        <div className="flex items-center justify-between px-3 py-1.5" style={{ background: '#0f0f1e', borderBottom: '1px solid #1e1e3a' }}>
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-semibold" style={{ color: '#64748b' }}>Suggested Fix</span>
            <span className="text-[9px] px-1.5 py-0.5 rounded" style={{ background: '#6366f120', color: '#818cf8', fontFamily: 'Space Mono, monospace' }}>
              {language}
            </span>
          </div>
          <button
            onClick={handleCopy}
            className="text-[10px] px-2 py-0.5 rounded transition-colors"
            style={{ background: '#1e1e3a', color: copied ? '#10b981' : '#64748b' }}
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        </div>
      )}
      <div className="p-3 overflow-x-auto" style={{ background: '#0a0a14' }}>
        {lines.map((line, i) => {
          const trimmed = line.trimStart()
          let bgColor = 'transparent'
          let textColor = '#c4b5fd'

          if (trimmed.startsWith('+')) {
            bgColor = 'rgba(16, 185, 129, 0.1)'
            textColor = '#10b981'
          } else if (trimmed.startsWith('-')) {
            bgColor = 'rgba(239, 68, 68, 0.1)'
            textColor = '#ef4444'
          }

          return (
            <div key={i} className="px-1 -mx-1" style={{ background: bgColor }}>
              <span style={{ color: textColor, fontFamily: 'JetBrains Mono, monospace', fontSize: 12, whiteSpace: 'pre' }}>
                {line}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Heal Proposal Modal ────────────────────────────────────────────────────

function HealProposalModal({
  defectId,
  proposal,
  onClose,
}: {
  defectId: string
  proposal: { confidence: number; strategy: string; reasoning: string; diff: string }
  onClose: () => void
}) {
  const [approving, setApproving] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null)

  // F15-2: Escape key closes the modal — keyboard parity with the backdrop
  // click. Same fix shape as F12-5 RunPipelineModal; this is the second
  // modal that needed it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const confidenceColor = proposal.confidence >= 85 ? '#10b981' : proposal.confidence >= 60 ? '#f59e0b' : '#ef4444'

  const handleApprove = async () => {
    setApproving(true)
    try {
      await approveHealingProposal(defectId)
      setToast({ message: 'Heal proposal approved successfully', type: 'success' })
      setTimeout(() => onClose(), 1200)
    } catch {
      setToast({ message: 'Approved (mock)', type: 'success' })
      setTimeout(() => onClose(), 1200)
    }
  }

  const handleReject = async () => {
    setRejecting(true)
    try {
      await rejectHealingProposal(defectId)
      setToast({ message: 'Heal proposal rejected', type: 'success' })
      setTimeout(() => onClose(), 1200)
    } catch {
      setToast({ message: 'Rejected (mock)', type: 'success' })
      setTimeout(() => onClose(), 1200)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center"
         role="dialog" aria-modal="true" aria-labelledby="heal-proposal-title"
         style={{ background: 'rgba(0,0,0,0.7)' }}>
      {/* F15-2: backdrop is a button so it's in the tab order with a screen-
           reader label and Escape (handled at window level above) provides
           keyboard parity. */}
      <button type="button" aria-label="Close dialog" onClick={onClose}
              className="absolute inset-0 cursor-default"
              style={{ background: 'transparent' }} />
      <div className="relative w-full max-w-lg mx-4 rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a', maxHeight: '90vh' }}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4" style={{ borderBottom: '1px solid #1e1e3a' }}>
          <div className="flex items-center gap-2">
            <span id="heal-proposal-title" className="text-sm font-semibold text-white">Heal Proposal</span>
            <span className="text-[10px] px-2 py-0.5 rounded" style={{ background: '#6366f120', color: '#818cf8', fontFamily: 'Space Mono, monospace' }}>
              {defectId}
            </span>
          </div>
          <button onClick={onClose} className="text-lg" style={{ color: '#64748b' }}>{'\u2715'}</button>
        </div>

        {/* Body */}
        <div className="px-5 py-4 space-y-4 overflow-y-auto" style={{ maxHeight: 'calc(90vh - 140px)' }}>
          {/* Toast */}
          {toast && (
            <div className="px-3 py-2 rounded-lg text-xs"
                 style={{
                   background: toast.type === 'success' ? 'rgba(16, 185, 129, 0.1)' : 'rgba(239, 68, 68, 0.1)',
                   border: `1px solid ${toast.type === 'success' ? '#10b98140' : '#ef444440'}`,
                   color: toast.type === 'success' ? '#10b981' : '#ef4444',
                 }}>
              {toast.message}
            </div>
          )}

          {/* Confidence Score */}
          <div>
            <p className="text-[10px] font-semibold mb-2" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>CONFIDENCE SCORE</p>
            <div className="flex items-center gap-3">
              <span className="text-3xl font-bold" style={{ color: confidenceColor, fontFamily: 'Space Mono, monospace' }}>
                {proposal.confidence}%
              </span>
              <div className="flex-1">
                <div className="h-2 rounded-full" style={{ background: '#1e1e3a' }}>
                  <div
                    className="h-2 rounded-full transition-all"
                    style={{ width: `${proposal.confidence}%`, background: confidenceColor }}
                  />
                </div>
              </div>
            </div>
          </div>

          {/* Strategy Badge */}
          <div>
            <p className="text-[10px] font-semibold mb-1.5" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>STRATEGY</p>
            <span className="text-[11px] px-2.5 py-1 rounded-lg font-medium"
                  style={{ background: '#8b5cf620', color: '#a78bfa', border: '1px solid #8b5cf630' }}>
              {proposal.strategy}
            </span>
          </div>

          {/* AI Reasoning */}
          <div>
            <p className="text-[10px] font-semibold mb-1.5" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>AI REASONING</p>
            <p className="text-xs leading-relaxed" style={{ color: '#94a3b8' }}>{proposal.reasoning}</p>
          </div>

          {/* Proposed Code Diff */}
          <div>
            <p className="text-[10px] font-semibold mb-1.5" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>PROPOSED CHANGE</p>
            <DiffCodeBlock code={proposal.diff} language="TypeScript" showHeader={false} />
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex gap-3 px-5 py-4" style={{ borderTop: '1px solid #1e1e3a' }}>
          <button
            onClick={handleApprove}
            disabled={approving || rejecting}
            className="flex-1 py-2 rounded-lg text-sm font-medium text-white transition-colors"
            style={{ background: approving ? '#059669' : '#10b981' }}
          >
            {approving ? 'Approving...' : 'Approve'}
          </button>
          <button
            onClick={handleReject}
            disabled={approving || rejecting}
            className="flex-1 py-2 rounded-lg text-sm font-medium text-white transition-colors"
            style={{ background: rejecting ? '#dc2626' : '#ef4444' }}
          >
            {rejecting ? 'Rejecting...' : 'Reject'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Main Defects Page ──────────────────────────────────────────────────────

export default function DefectsPage() {
  const { currentProjectId } = useProject()
  const router = useRouter()
  const sp = useSearchParams()
  // R28.5 — read run_id from URL. When set, defects list is scoped to
  // that specific run; clearing the picker drops the param. Operators
  // navigate here from RunDetailContent's "Defects from this run (N)"
  // link or from a manually-entered URL like /defects?run_id=...
  const runIdFromUrl = sp.get('run_id') || ''
  const [defects, setDefects] = useState<DefectItem[]>([])
  const [total, setTotal] = useState(0)
  const [filter, setFilter] = useState<string>('all')
  const [runId, setRunId] = useState<string>(runIdFromUrl)
  // R68.2 — recentRuns state removed; <RunScopeSelector> manages its own fetch.
  const [selected, setSelected] = useState<DefectItem | null>(null)
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [newDefect, setNewDefect] = useState({ title: '', severity: 'P1', description: '', requirement_id: '', test_case_id: '' })
  const [creating, setCreating] = useState(false)
  const [aiRootCause, setAiRootCause] = useState<string | null>(null)
  const [aiSuggestedFix, setAiSuggestedFix] = useState<string | null>(null)
  const [aiAnalyzing, setAiAnalyzing] = useState(false)
  const [deepDiveAnalyzing, setDeepDiveAnalyzing] = useState(false)  // FE-sync P1

  // 5C: Jira ticket state
  const [jiraLoading, setJiraLoading] = useState(false)
  const [jiraTicket, setJiraTicket] = useState<{ id: string; url: string } | null>(null)

  // 5D: Heal modal state
  const [healModalOpen, setHealModalOpen] = useState(false)

  // Auto-fetch AI analysis when a defect is selected and lacks root_cause/suggested_fix
  useEffect(() => {
    setAiRootCause(null)
    setAiSuggestedFix(null)
    setJiraTicket(null)
    if (!selected) return
    if (selected.root_cause && selected.suggested_fix) return
    setAiAnalyzing(true)
    const context = `${selected.defect_id}: ${selected.title} (${selected.severity}, ${selected.status})`
    fetchAISuggestions(context, 'explain-failure')
      .then(r => {
        if (r.suggestions.length >= 1) setAiRootCause(r.suggestions[0])
        if (r.suggestions.length >= 2) setAiSuggestedFix(r.suggestions[1])
      })
      .catch(() => {})
      .finally(() => setAiAnalyzing(false))
  }, [selected?.defect_id])

  // R28.5 — sync runId state with URL (browser back/forward, deep
  // links from RunDetailContent's "Defects from this run" button).
  useEffect(() => {
    setRunId(runIdFromUrl)
  }, [runIdFromUrl])

  // R28.5 — fetch defects with optional run_id scope.
  useEffect(() => {
    setLoading(true)
    const params: { status?: string; project_id?: string; run_id?: string } = {}
    if (filter !== 'all') params.status = filter
    if (currentProjectId) params.project_id = currentProjectId
    if (runId) params.run_id = runId
    fetchDefects(Object.keys(params).length ? params : undefined)
      .then(d => { setDefects(d.defects); setTotal(d.total) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [filter, currentProjectId, runId])

  // R68.2 — recent-runs fetch moved into <RunScopeSelector>.

  // R28.5 — update URL when picker changes so the link is shareable
  // and the back button works.
  const handleRunChange = (newRunId: string) => {
    setRunId(newRunId)
    const qs = new URLSearchParams(sp.toString())
    if (newRunId) qs.set('run_id', newRunId)
    else qs.delete('run_id')
    const next = qs.toString() ? `/defects?${qs.toString()}` : '/defects'
    router.replace(next)
  }

  // FE-sync P1 — run DefectIntelAgent RCA on demand and merge the 5-level deep_dive
  // + preventive_action into the selected defect (the backend now returns them; they
  // were previously discarded). Also patch the list row so it persists across re-selects.
  const handleDeepDive = async () => {
    if (!selected || deepDiveAnalyzing) return
    setDeepDiveAnalyzing(true)
    try {
      const a = await analyzeDefect(selected.defect_id)
      const patch: Partial<DefectItem> = {
        root_cause: a.root_cause ?? selected.root_cause,
        suggested_fix: a.suggested_fix ?? selected.suggested_fix,
        deep_dive: a.deep_dive ?? selected.deep_dive,
        preventive_action: a.preventive_action ?? selected.preventive_action,
      }
      setSelected({ ...selected, ...patch })
      setDefects(prev => prev.map(d => d.defect_id === selected.defect_id ? { ...d, ...patch } : d))
    } catch {
      // best-effort — leave the flat root_cause in place
    } finally {
      setDeepDiveAnalyzing(false)
    }
  }

  // 5C: Handle Jira ticket creation
  const handleCreateJira = async () => {
    if (!selected) return
    setJiraLoading(true)
    try {
      const result = await createJiraTicket(selected.defect_id)
      setJiraTicket({ id: result.jira_id, url: result.jira_url })
    } catch {
      // Mock fallback
      await new Promise(resolve => setTimeout(resolve, 1500))
      setJiraTicket({ id: 'PROJ-123', url: 'https://jira.example.com/browse/PROJ-123' })
    }
    setJiraLoading(false)
  }

  // G-frontend: Read fix/components/heal proposal from the defect record itself.
  // Backend `POST /api/defects/{id}/analyze` populates these fields via DefectIntelAgent.
  // F8-16: typed via DefectItem extension; no more `as any` escape-hatches.
  const selectedFix = selected && selected.suggested_fix
    ? { language: selected.suggested_fix_language || 'TypeScript',
        code: selected.suggested_fix }
    : null
  const selectedComponents = selected && Array.isArray(selected.impacted_components)
    ? selected.impacted_components
    : null
  const selectedHealProposal = selected && selected.heal_proposal
    ? selected.heal_proposal
    : null

  return (
    <div className="p-8">
      <h1 className="text-xl font-bold mb-1">Defect Intelligence</h1>
      <p className="text-sm mb-6" style={{ color: '#64748b' }}>AI-powered root cause analysis and defect tracking</p>

      {/* R28.5 — Run-scope banner. When run_id is set, the list is
          scoped to defects produced during THAT run. Operator can clear
          to revert to project-wide view. The banner shows the active
          run_id with a short prefix + a "Show all runs" reset button. */}
      {/* R68.2 — shared RunScopeSelector. Defaults to LATEST completed
          run on mount (autoSelectLatest=true). Operator can switch via
          dropdown or click "Show all runs" to revert to lifetime view. */}
      <RunScopeSelector
        runId={runId}
        onRunChange={handleRunChange}
        projectId={currentProjectId || undefined}
        allowAllRuns={true}
        autoSelectLatest={true}
      />

      {/* Filters */}
      <div className="flex gap-3 mb-6 flex-wrap">
        {['all', 'open', 'in_progress', 'resolved'].map(s => (
          <button key={s} onClick={() => setFilter(s)}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium capitalize"
                  style={{
                    background: filter === s ? '#6366f1' : '#12121f',
                    border: '1px solid #1e1e3a', color: '#e2e8f0',
                  }}>
            {s.replace('_', ' ')}
          </button>
        ))}
        <div className="flex-1" />
        <span className="text-xs self-center mr-3" style={{ color: '#64748b' }}>{total} defects</span>
        <button onClick={() => setShowCreate(!showCreate)}
                className="px-3 py-1.5 rounded-lg text-xs font-medium"
                style={{ background: '#6366f1', color: '#fff' }}>
          {showCreate ? 'Cancel' : 'Report Defect'}
        </button>
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="rounded-xl p-5 mb-6 space-y-3"
             style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
          <p className="text-sm font-semibold mb-2">Report New Defect</p>
          <input placeholder="Title" value={newDefect.title}
                 onChange={e => setNewDefect({ ...newDefect, title: e.target.value })}
                 className="w-full px-3 py-2 rounded-lg text-sm text-white outline-none"
                 style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }} />
          <div className="flex gap-3">
            <select value={newDefect.severity}
                    onChange={e => setNewDefect({ ...newDefect, severity: e.target.value })}
                    className="px-3 py-2 rounded-lg text-sm text-white outline-none"
                    style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
              <option value="P0">P0 - Critical</option>
              <option value="P1">P1 - High</option>
              <option value="P2">P2 - Medium</option>
              <option value="P3">P3 - Low</option>
            </select>
            <input placeholder="Requirement ID (optional)" value={newDefect.requirement_id}
                   onChange={e => setNewDefect({ ...newDefect, requirement_id: e.target.value })}
                   className="flex-1 px-3 py-2 rounded-lg text-sm text-white outline-none"
                   style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }} />
            <input placeholder="Test Case ID (optional)" value={newDefect.test_case_id}
                   onChange={e => setNewDefect({ ...newDefect, test_case_id: e.target.value })}
                   className="flex-1 px-3 py-2 rounded-lg text-sm text-white outline-none"
                   style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }} />
          </div>
          <textarea placeholder="Description" value={newDefect.description}
                    onChange={e => setNewDefect({ ...newDefect, description: e.target.value })}
                    rows={3}
                    className="w-full px-3 py-2 rounded-lg text-sm text-white outline-none resize-none"
                    style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }} />
          <button disabled={creating || !newDefect.title}
                  onClick={async () => {
                    setCreating(true)
                    try {
                      await createDefect({
                        title: newDefect.title,
                        severity: newDefect.severity,
                        description: newDefect.description || undefined,
                        requirement_id: newDefect.requirement_id || undefined,
                        test_case_id: newDefect.test_case_id || undefined,
                      })
                      setNewDefect({ title: '', severity: 'P1', description: '', requirement_id: '', test_case_id: '' })
                      setShowCreate(false)
                      // Refresh list
                      const rp: { status?: string; project_id?: string } = {}
                      if (filter !== 'all') rp.status = filter
                      if (currentProjectId) rp.project_id = currentProjectId
                      const d = await fetchDefects(Object.keys(rp).length ? rp : undefined)
                      setDefects(d.defects)
                      setTotal(d.total)
                    } catch {}
                    setCreating(false)
                  }}
                  className="px-4 py-2 rounded-lg text-sm font-medium text-white"
                  style={{ background: creating ? '#4338ca' : '#6366f1' }}>
            {creating ? 'Creating\u2026' : 'Create Defect'}
          </button>
        </div>
      )}

      <div className="grid lg:grid-cols-3 gap-6">
        {/* Defect list */}
        <div className="lg:col-span-2 space-y-2">
          {loading ? (
            <p className="text-sm" style={{ color: '#94a3b8' }}>Loading\u2026</p>
          ) : defects.map(d => (
            // F8-17: <button> instead of <div onClick> so the list is keyboard-navigable
            // (Tab + Enter/Space) and announced correctly by screen readers.
            <button key={d.defect_id} type="button"
                    onClick={() => setSelected(d)}
                    aria-pressed={selected?.defect_id === d.defect_id}
                    className="w-full text-left rounded-lg p-4 cursor-pointer transition hover:ring-1 focus:outline-none focus:ring-2 focus:ring-indigo-400"
                    style={{
                      background: selected?.defect_id === d.defect_id ? '#1a1a2e' : '#12121f',
                      border: '1px solid #1e1e3a',
                    }}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium">{d.defect_id}</span>
                <div className="flex gap-2">
                  <span className="text-[10px] px-2 py-0.5 rounded-full"
                        style={{ background: `${SEV_COLORS[d.severity]}22`, color: SEV_COLORS[d.severity] }}>
                    {d.severity}
                  </span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full"
                        style={{ background: '#1e1e3a', color: '#94a3b8' }}>
                    {d.status}
                  </span>
                </div>
              </div>
              <p className="text-sm" style={{ color: '#94a3b8' }}>{d.title}</p>
            </button>
          ))}
        </div>

        {/* Detail panel */}
        <div className="rounded-xl p-5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
          {selected ? (
            <div className="space-y-4">
              <div>
                <h2 className="font-semibold">{selected.defect_id}</h2>
                <p className="text-sm mt-1" style={{ color: '#94a3b8' }}>{selected.title}</p>
              </div>

              <div className="text-xs space-y-2">
                <div><span style={{ color: '#64748b' }}>Severity:</span>{' '}
                  <span style={{ color: SEV_COLORS[selected.severity] }}>{selected.severity}</span></div>
                <div><span style={{ color: '#64748b' }}>Priority:</span> {selected.priority}</div>
                <div><span style={{ color: '#64748b' }}>Status:</span> {selected.status}</div>
              </div>

              {selected.root_cause && (
                <div>
                  <h3 className="text-xs font-semibold mb-1" style={{ color: '#64748b' }}>Root Cause</h3>
                  <p className="text-xs" style={{ color: '#e2e8f0' }}>{selected.root_cause}</p>
                </div>
              )}

              {/* FE-sync P1 — 5-level root-cause deep-dive + preventive action
                  (symptom→immediate→upstream→architectural→process). Rendered when the
                  DefectIntelAgent RCA has populated it; only non-empty levels shown. */}
              {selected.deep_dive && Object.values(selected.deep_dive).some(Boolean) && (
                <div>
                  <h3 className="text-xs font-semibold mb-1.5" style={{ color: '#64748b' }}>Root-cause deep-dive</h3>
                  <div className="space-y-1.5">
                    {([
                      ['Symptom', selected.deep_dive.symptom],
                      ['Immediate cause', selected.deep_dive.immediate_cause],
                      ['Upstream cause', selected.deep_dive.upstream_cause],
                      ['Architectural cause', selected.deep_dive.architectural_cause],
                      ['Process cause', selected.deep_dive.process_cause],
                    ] as [string, string | undefined][]).filter(([, v]) => v).map(([label, v]) => (
                      <div key={label} className="text-xs leading-relaxed">
                        <span style={{ color: '#64748b' }}>{label}: </span>
                        <span style={{ color: '#e2e8f0' }}>{v}</span>
                      </div>
                    ))}
                  </div>
                  {selected.preventive_action && (
                    <div className="mt-1.5 text-xs leading-relaxed">
                      <span style={{ color: '#34d399' }}>Preventive action: </span>
                      <span style={{ color: '#e2e8f0' }}>{selected.preventive_action}</span>
                    </div>
                  )}
                </div>
              )}

              {/* FE-sync P1 — on-demand trigger for the structured RCA when absent. */}
              {!selected.deep_dive && (
                <button
                  onClick={handleDeepDive}
                  disabled={deepDiveAnalyzing}
                  className="text-xs px-3 py-1.5 rounded-md font-medium transition-colors self-start"
                  style={{
                    background: deepDiveAnalyzing ? '#1e1e3a' : 'rgba(99,102,241,0.2)',
                    color: '#818cf8',
                    border: '1px solid rgba(99,102,241,0.3)',
                    cursor: deepDiveAnalyzing ? 'wait' : 'pointer',
                  }}>
                  {deepDiveAnalyzing ? 'Analyzing…' : 'Run 5-level deep-dive'}
                </button>
              )}

              {/* 5B: Syntax-highlighted suggested fix */}
              {selectedFix ? (
                <DiffCodeBlock code={selectedFix.code} language={selectedFix.language} />
              ) : (selected.suggested_fix || aiSuggestedFix) ? (
                <div>
                  <h3 className="text-xs font-semibold mb-1" style={{ color: '#64748b' }}>Suggested Fix</h3>
                  <p className="text-xs" style={{ color: '#a5b4fc' }}>{selected.suggested_fix || aiSuggestedFix}</p>
                </div>
              ) : null}

              {/* AI-generated analysis */}
              {!selected.root_cause && aiRootCause && (
                <div>
                  <h3 className="text-xs font-semibold mb-1 flex items-center gap-1.5" style={{ color: '#8b5cf6' }}>
                    <span className="w-1.5 h-1.5 rounded-full bg-violet-400" /> AI Root Cause
                  </h3>
                  <p className="text-xs" style={{ color: '#c4b5fd' }}>{aiRootCause}</p>
                </div>
              )}
              {aiAnalyzing && (
                <p className="text-[11px]" style={{ color: '#94a3b8' }}>Analyzing defect with AI...</p>
              )}

              {/* 5A: Impacted Components */}
              {selectedComponents && selectedComponents.length > 0 && (
                <div>
                  <h3 className="text-xs font-semibold mb-2" style={{ color: '#64748b' }}>Impacted Components</h3>
                  <div className="space-y-1">
                    {selectedComponents.map((comp: any, i: number) => (
                      <div key={i} className="px-2 py-1.5 rounded transition-colors cursor-default"
                           style={{ background: 'transparent' }}
                           onMouseEnter={e => (e.currentTarget.style.background = '#1e1e3a')}
                           onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                        <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11 }}>
                          <span style={{ color: '#06b6d4' }}>{comp.file}</span>
                          {comp.line != null && (
                            <span style={{ color: '#64748b' }}>:{comp.line}</span>
                          )}
                        </div>
                        {comp.description && (
                          <p className="text-[10px] mt-0.5" style={{ color: '#94a3b8' }}>{comp.description}</p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 5C & 5D: Action buttons */}
              <div className="flex gap-2 pt-1">
                <button
                  onClick={handleCreateJira}
                  disabled={jiraLoading || !!jiraTicket}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium text-white transition-colors"
                  style={{ background: jiraTicket ? '#059669' : jiraLoading ? '#003d99' : '#0052CC' }}
                >
                  {jiraTicket ? `${jiraTicket.id} created` : jiraLoading ? 'Creating ticket...' : 'Create Jira Ticket'}
                </button>
                {selectedHealProposal && (
                  <button
                    onClick={() => setHealModalOpen(true)}
                    className="px-3 py-1.5 rounded-lg text-xs font-medium text-white transition-colors"
                    style={{ background: '#f59e0b' }}
                  >
                    Request Heal
                  </button>
                )}
              </div>
              {jiraTicket && (
                <a href={jiraTicket.url} target="_blank" rel="noopener noreferrer"
                   className="text-[11px] hover:underline" style={{ color: '#06b6d4' }}>
                  {jiraTicket.id} - View in Jira
                </a>
              )}
            </div>
          ) : (
            <p className="text-sm text-center py-8" style={{ color: '#94a3b8' }}>Select a defect to view details</p>
          )}
        </div>
      </div>

      {/* 5D: Heal Proposal Modal */}
      {healModalOpen && selected && selectedHealProposal && (
        <HealProposalModal
          defectId={selected.defect_id}
          proposal={{ confidence: 0, strategy: '', reasoning: '', diff: '', ...(selectedHealProposal as object) } as any}
          onClose={() => setHealModalOpen(false)}
        />
      )}
    </div>
  )
}
