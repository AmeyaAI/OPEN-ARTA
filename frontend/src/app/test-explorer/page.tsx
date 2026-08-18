'use client'

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import { useSearchParams } from 'next/navigation'
import GherkinHighlighter from '@/components/GherkinHighlighter'
import FrozenDatasetPanel from '@/components/FrozenDatasetPanel'
import ChainViewDAG from '@/components/ChainViewDAG'
import {
  fetchTests,
  fetchAISuggestions,
  fetchPendingReviews,
  submitReviewDecision,
  submitTestFeedback,
  pushTestToGitHub,
  triggerRun,
  executeByTool,
  fetchRuns,
  getActiveRun,
  requestHeal,
  fetchTestVersions,
  fetchTestVersionDiff,
  fetchEnvironments,
  fetchDiscoveryStatus,
  computeToolInventoryGaps,
  type TestCase,
  type PendingReview,
  type EnvironmentSummary,
  type DiscoveryStatus,
} from '@/lib/api-client'
import { fetchAuthState, type AuthState } from '@/lib/auth-state'
import { useToast } from '@/components/ui/Toast'
import { useProject } from '@/lib/project-context'
import { useTasks } from '@/lib/task-context'
import { useRunGuard } from '@/lib/use-run-guard'
import RefreshAuthModal from '@/components/RefreshAuthModal'
import ExecuteByToolModal from '@/components/ExecuteByToolModal'

const STATUS_COLORS: Record<string, string> = {
  PASS: '#10b981', FAIL: '#f43f5e', SKIP: '#fbbf24',
  FLAKY: '#f59e0b', PENDING: '#6366f1', RUNNING: '#06b6d4',
}

const TOOL_LABELS: Record<string, string> = {
  playwright: 'Playwright', newman: 'Newman', k6: 'k6',
  selenium: 'Selenium', cypress: 'Cypress', zap: 'ZAP', manual: 'Manual',
}

const TOOL_COLORS: Record<string, string> = {
  playwright: '#06b6d4',
  newman: '#10b981',
  k6: '#8b5cf6',
  selenium: '#f59e0b',
  cypress: '#f43f5e',
  zap: '#ef4444',
  manual: '#64748b',
}

// Infer tool from test type when tool field is not available
function inferTool(testType: string): string {
  const t = testType.toLowerCase()
  if (t.includes('ui') || t.includes('e2e') || t.includes('functional')) return 'playwright'
  if (t.includes('api') || t.includes('integration')) return 'newman'
  if (t.includes('perf') || t.includes('load') || t.includes('stress')) return 'k6'
  if (t.includes('security') || t.includes('pen')) return 'zap'
  return 'playwright'
}

// F12-11: removed MOCK_DURATIONS (never-needed fake durations) and
// generateMockScript (~75-line fabricated code that ran when a test entry
// had empty `script_content`). Real generated tests now persist real script
// content via F8-1 + F9-2; missing content should render an empty state, not
// fake code that misleads the user about what the LLM produced.

// Simple syntax highlighting via regex
function highlightCode(code: string): string {
  let html = code
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  // Comments
  html = html.replace(/(\/\/.*)/g, '<span style="color:#64748b">$1</span>')
  // Strings (double quotes)
  html = html.replace(/("(?:[^"\\]|\\.)*")/g, '<span style="color:#10b981">$1</span>')
  // Strings (single quotes)
  html = html.replace(/('(?:[^'\\]|\\.)*')/g, '<span style="color:#10b981">$1</span>')
  // Template literals
  html = html.replace(/(`(?:[^`\\]|\\.)*`)/g, '<span style="color:#10b981">$1</span>')
  // Keywords
  html = html.replace(
    /\b(const|let|var|async|await|function|import|from|export|default|test|expect|describe|it|if|else|return|throw|new|require)\b/g,
    '<span style="color:#6366f1">$1</span>',
  )
  return html
}

function StarRating({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <div className="flex gap-1">
      {[1, 2, 3, 4, 5].map(star => (
        <button
          key={star}
          onClick={() => onChange(star)}
          className="text-lg transition"
          style={{ color: star <= value ? '#f59e0b' : '#334155' }}
        >
          {star <= value ? '\u2605' : '\u2606'}
        </button>
      ))}
    </div>
  )
}

export default function TestExplorerPage() {
  const { currentProjectId } = useProject()
  const toast = useToast()
  const { addTask, updateTask, completeTask, failTask } = useTasks()
  const searchParams = useSearchParams()
  const preSelectTestId = searchParams.get('test_id')
  // R29.6 — pytest test_ids land in execution_results as nodeids
  // (e.g. `tests/req_am_010_e2e.py::test_foo`) which don't map to the
  // TC-shaped ids in the test_cases table. Accept `script_path` from
  // the NFR Assessment link as a fallback so pytest rows stay clickable.
  const preSelectScriptPath = searchParams.get('script_path')

  const [tests, setTests] = useState<TestCase[]>([])
  const [, setTotal] = useState(0)
  const [filter, setFilter] = useState<{ priority?: string; tool?: string }>({})
  const [selected, setSelected] = useState<TestCase | null>(null)
  const [loading, setLoading] = useState(true)
  const [aiSuggestions, setAiSuggestions] = useState<string[]>([])
  const [, setAiLoading] = useState(false)

  // Suite filter state
  const [suiteFilter, setSuiteFilter] = useState<'full' | 'smoke' | 'regression' | 'custom'>('full')

  // Status filter state
  const [statusFilter, setStatusFilter] = useState<'all' | 'PASS' | 'FAIL' | 'SKIP'>('all')

  // Tabbed code viewer state
  const [activeTab, setActiveTab] = useState<'code' | 'gherkin' | 'data'>('gherkin')

  // Phase J3 — page-level view mode: list of tests vs Chain View DAG.
  const [viewMode, setViewMode] = useState<'tests' | 'chains'>('tests')

  // Version history drawer state
  const [historyOpen, setHistoryOpen] = useState(false)
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null)
  const [testVersions, setTestVersions] = useState<{ version: number; date: string; summary: string; author: string }[]>([])
  const [versionDiff, setVersionDiff] = useState<{ diff: string; additions: number; deletions: number } | null>(null)

  // Fetch real version history when history drawer opens
  useEffect(() => {
    if (!historyOpen || !selected?.test_id) {
      setTestVersions([])
      setVersionDiff(null)
      return
    }
    fetchTestVersions(selected.test_id)
      .then(d => { if (d?.versions?.length) setTestVersions(d.versions) })
      .catch(() => {
        // Fallback: build a single-entry history from the test's generated_at
        if (selected?.generated_at) {
          setTestVersions([{
            version: 1,
            date: selected.generated_at.slice(0, 10),
            summary: 'Initial generation from requirement',
            author: 'ARTA AI',
          }])
        }
      })
  }, [historyOpen, selected?.test_id])

  // Fetch diff when a version is selected
  useEffect(() => {
    if (!selectedVersion || !selected?.test_id || testVersions.length < 2) {
      setVersionDiff(null)
      return
    }
    const prevVersion = selectedVersion - 1
    if (prevVersion < 1) { setVersionDiff(null); return }
    fetchTestVersionDiff(selected.test_id, prevVersion, selectedVersion)
      .then(d => setVersionDiff(d))
      .catch(() => setVersionDiff(null))
  }, [selectedVersion, selected?.test_id, testVersions.length])

  // Review gate state
  const [pendingReviews, setPendingReviews] = useState<PendingReview[]>([])
  const [selectedReview, setSelectedReview] = useState<PendingReview | null>(null)
  const [isEditing, setIsEditing] = useState(false)
  const [editedGherkin, setEditedGherkin] = useState<string>('')
  const [reviewReason, setReviewReason] = useState('')
  const [reviewSubmitting, setReviewSubmitting] = useState(false)
  const [showRejectReason, setShowRejectReason] = useState(false)

  // Feedback state
  const [feedbackRating, setFeedbackRating] = useState(0)
  const [feedbackText, setFeedbackText] = useState('')
  const [feedbackSubmitting, setFeedbackSubmitting] = useState(false)
  const [feedbackSuccess, setFeedbackSuccess] = useState(false)

  // Banner dismissed state
  const [bannerDismissed, setBannerDismissed] = useState(false)

  // Right column feedback/AI section collapsed state
  const [feedbackExpanded, setFeedbackExpanded] = useState(false)

  // Push to GitHub per-test state
  const [pushingTestId, setPushingTestId] = useState<string | null>(null)

  // R330 P1d — the SUT-Understanding panel CTA deep-links ?flag=needs_attention
  // so the list shows exactly the tests the gate flagged (guess ∪ untraceable).
  const flagParam = searchParams.get('flag') || undefined

  useEffect(() => {
    setLoading(true)
    const params = { ...filter, project_id: currentProjectId || undefined, flag: flagParam }
    fetchTests(params)
      .then(d => { setTests(d.tests); setTotal(d.total) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [filter, currentProjectId, flagParam])

  // Auto-select test from URL param (e.g., from Architecture page link).
  // R29.6 — when test_id doesn't match (e.g. pytest nodeid), fall back
  // to matching by script_path so NFR Assessment rows that point to
  // analytics tests still resolve.
  useEffect(() => {
    if ((preSelectTestId || preSelectScriptPath) && tests.length && !selected) {
      let match = preSelectTestId
        ? tests.find(t => t.id === preSelectTestId || t.test_id === preSelectTestId)
        : undefined
      if (!match && preSelectScriptPath) {
        match = tests.find((t: any) =>
          t.script_path === preSelectScriptPath
          || (t.script_path && preSelectScriptPath.endsWith((t.script_path as string).split('/').pop() || ''))
        )
      }
      if (match) setSelected(match)
    }
  }, [preSelectTestId, preSelectScriptPath, tests, selected])

  // Fetch pending reviews
  // F10-3: scope to active project so the demo seed (and other projects'
  // pending reviews) don't leak into this view.
  useEffect(() => {
    let cancelled = false
    fetchPendingReviews(currentProjectId || undefined)
      .then(reviews => { if (!cancelled) setPendingReviews(reviews) })
      .catch(() => { if (!cancelled) setPendingReviews([]) })
    return () => { cancelled = true }
  }, [currentProjectId])

  // Fetch AI suggestions when a test is selected
  useEffect(() => {
    if (!selected) { setAiSuggestions([]); return }
    setAiLoading(true)
    setAiSuggestions([])
    const context = `${selected.test_id}: ${selected.title} (${selected.test_type}, ${selected.priority})`
    fetchAISuggestions(context, 'generate-edge-cases')
      .then(r => setAiSuggestions(r.suggestions))
      .catch(() => setAiSuggestions([]))
      .finally(() => setAiLoading(false))
  }, [selected?.test_id])

  // Handle suite filter change
  function handleSuiteFilter(suite: 'full' | 'smoke' | 'regression' | 'custom') {
    setSuiteFilter(suite)
    if (suite === 'smoke') {
      setFilter(f => ({ ...f, priority: 'P0' }))
    } else if (suite === 'regression') {
      setFilter(f => ({ ...f, priority: undefined }))
    } else {
      setFilter(f => ({ ...f, priority: undefined }))
    }
  }

  // Filter tests for regression suite (P0 + P1) client-side
  let displayedTests = suiteFilter === 'regression'
    ? tests.filter(t => t.priority === 'P0' || t.priority === 'P1')
    : tests

  // Apply status filter
  if (statusFilter !== 'all') {
    displayedTests = displayedTests.filter(t => t.last_status === statusFilter)
  }

  // Compute status counts from the base suite-filtered set (before status filter)
  const suiteFiltered = suiteFilter === 'regression'
    ? tests.filter(t => t.priority === 'P0' || t.priority === 'P1')
    : tests
  const passCount = suiteFiltered.filter(t => t.last_status === 'PASS').length
  const failCount = suiteFiltered.filter(t => t.last_status === 'FAIL').length
  const skipCount = suiteFiltered.filter(t => t.last_status === 'SKIP').length

  // Open review panel for a pending review
  function openReview(review: PendingReview) {
    setSelectedReview(review)
    setEditedGherkin(review.gherkin_scenarios.join('\n\n'))
    setIsEditing(false)
    setReviewReason('')
    setShowRejectReason(false)
  }

  // Handle review decision
  async function handleReviewDecision(decision: 'approve' | 'reject') {
    if (!selectedReview) return
    if (decision === 'reject' && !showRejectReason) {
      setShowRejectReason(true)
      return
    }
    setReviewSubmitting(true)
    try {
      const scenarios = isEditing ? editedGherkin.split('\n\n').filter(s => s.trim()) : undefined
      await submitReviewDecision(selectedReview.workflow_id, decision, reviewReason, scenarios)
      setPendingReviews(prev => prev.filter(r => r.workflow_id !== selectedReview.workflow_id))
      setSelectedReview(null)
    } catch {
      // error handled silently
    } finally {
      setReviewSubmitting(false)
    }
  }

  // Handle feedback submission
  async function handleFeedbackSubmit() {
    if (!selected || feedbackRating === 0) return
    setFeedbackSubmitting(true)
    setFeedbackSuccess(false)
    try {
      await submitTestFeedback(selected.test_id, feedbackRating, feedbackText, false)
      setFeedbackSuccess(true)
      setFeedbackRating(0)
      setFeedbackText('')
      setTimeout(() => setFeedbackSuccess(false), 3000)
    } catch {
      // error handled silently
    } finally {
      setFeedbackSubmitting(false)
    }
  }

  // Handle push to GitHub (per test card)
  async function handlePushToGitHub(testId: string, e: React.MouseEvent) {
    e.stopPropagation()
    setPushingTestId(testId)
    try {
      await pushTestToGitHub(testId)
      toast.success(`${testId} pushed to GitHub`)
    } catch {
      toast.error('Push to GitHub failed')
    } finally {
      setPushingTestId(null)
    }
  }

  // Last run state — updated after suite completes
  const [lastRunId, setLastRunId] = useState<string | null>(null)
  const [lastRunSummary, setLastRunSummary] = useState<{ passed: number; total: number } | null>(null)

  // Handle run suite — triggers real execution with progress tracking
  const [suiteRunning, setSuiteRunning] = useState(false)
  const [runEnvironment, setRunEnvironment] = useState('staging')
  // WS4 — execute-by-tool (run a single tool, optionally one requirement).
  const [execByToolOpen, setExecByToolOpen] = useState(false)
  const [execByToolRunning, setExecByToolRunning] = useState(false)
  // Requirements for the modal's single-requirement scope, derived from the
  // loaded tests (test-explorer doesn't fetch a requirements list of its own).
  const execReqs = useMemo(() => {
    const seen = new Map<string, { req_id: string; id: string; title: string }>()
    for (const t of tests) {
      const rid = (t as any).requirement_id
      if (rid && !seen.has(rid)) seen.set(rid, { req_id: rid, id: rid, title: rid })
    }
    return Array.from(seen.values())
  }, [tests])
  // Part 5C — environments loaded from /api/projects/<id>/environments
  const [environments, setEnvironments] = useState<EnvironmentSummary[]>([])
  // Part 5B — per-card Run popover state. testId of the card whose
  // popover is open, or null when none is.
  const [runPopoverFor, setRunPopoverFor] = useState<string | null>(null)
  const [runningSingle, setRunningSingle] = useState<string | null>(null)

  // R15 — pre-run auth-state gate. Wraps triggerRun() with a pre-flight
  // check; opens RefreshAuthModal when the project's stored SUT cookie
  // is expired and queues the run for after the operator pastes a fresh
  // cookie.
  const runGuard = useRunGuard(currentProjectId, runEnvironment)

  // R40.1 — auth-state diagnostic for the page header banner +
  // reactive 409/412 handling. Independent of runGuard so the banner
  // can render BEFORE the operator clicks Run Suite (passive surface)
  // AND so we have a manual entry point into RefreshAuthModal that
  // doesn't require runGuard's pre-flight to fire first.
  const [discoveryStatus, setDiscoveryStatus] = useState<DiscoveryStatus | null>(null)
  const [extraAuthState, setExtraAuthState] = useState<AuthState | null>(null)
  const [showExtraRefreshAuth, setShowExtraRefreshAuth] = useState(false)

  useEffect(() => {
    if (!currentProjectId) {
      setDiscoveryStatus(null)
      return
    }
    let cancelled = false
    fetchDiscoveryStatus(currentProjectId, runEnvironment)
      .then(s => { if (!cancelled) setDiscoveryStatus(s) })
      .catch(() => { if (!cancelled) setDiscoveryStatus(null) })
    fetchAuthState(currentProjectId, runEnvironment)
      .then(s => { if (!cancelled) setExtraAuthState(s) })
      .catch(() => { if (!cancelled) setExtraAuthState(null) })
    return () => { cancelled = true }
  }, [currentProjectId, runEnvironment])

  async function handleExtraRefreshSuccess() {
    setShowExtraRefreshAuth(false)
    if (!currentProjectId) return
    try {
      const fresh = await fetchDiscoveryStatus(currentProjectId, runEnvironment)
      setDiscoveryStatus(fresh)
      toast.success('Auth refreshed — re-run discovery to auto-fill env vars.')
    } catch {
      toast.success('Auth refreshed.')
    }
  }

  useEffect(() => {
    if (!currentProjectId) return
    fetchEnvironments(currentProjectId)
      .then(envs => {
        setEnvironments(envs)
        // Default to first project-prefixed env, else first overall
        const def = envs.find(e => e.project_match) || envs[0]
        if (def) setRunEnvironment(def.id)
      })
      .catch(() => { /* fall back to hardcoded options below */ })
  }, [currentProjectId])

  // Part 5B — single-test execution. Triggers a run with test_ids=[test_id]
  // and the chosen environment. The backend resolves test_id → spec_path
  // + title and narrows TARGET_TEST_MATCH + TARGET_TEST_GREP.
  async function handleRunSingleTest(testId: string, envId: string) {
    if (runningSingle) return
    setRunningSingle(testId)
    setRunPopoverFor(null)
    // R15 — single-test triggers don't gate on auth-state because the
    // suite-level handleRunSuite already gated. If the operator runs
    // a single test directly without first running the suite, the
    // backend will still produce cascade-skipped results — operator
    // sees the same diagnostic message they'd see otherwise. Single-
    // test gating is a lower-priority follow-up if needed.
    try {
      const result = await triggerRun({
        suite_type: 'custom',
        environment: envId,
        project_id: currentProjectId || undefined,
        test_ids: [testId],
      })
      const taskId = `single-${testId}-${Date.now()}`
      addTask({ id: taskId, type: 'test_execution', label: `Running ${testId}`, result_url: '/test-explorer' })
      toast.success(`Started: ${testId}`)
      // Poll for completion (shorter window — single test)
      let attempts = 0
      const poll = setInterval(async () => {
        attempts++
        try {
          const runs = await fetchRuns({ project_id: currentProjectId || undefined })
          const run = (runs?.runs || []).find((r: any) => r.run_id === result.run_id)
          if (run?.status === 'completed' || run?.status === 'failed') {
            clearInterval(poll)
            setRunningSingle(null)
            const passed = run.passed || 0
            const total = run.total_tests || 0
            completeTask(taskId, { detail: `${passed}/${total} passed`, result_url: `/run-history?run_id=${result.run_id}` })
            toast.success(`${testId}: ${passed}/${total} passed`)
            const params = { ...filter, project_id: currentProjectId || undefined }
            fetchTests(params).then(d => { setTests(d.tests); setTotal(d.total) }).catch(() => {})
          }
        } catch {}
        if (attempts > 60) {  // 5 min cap for single test
          clearInterval(poll)
          setRunningSingle(null)
          failTask(taskId, 'Timed out')
        }
      }, 5000)
    } catch {
      setRunningSingle(null)
      toast.error(`Failed to run ${testId}`)
    }
  }
  // R15 — internal trigger function used by both the gated path (when
  // auth is valid) and the deferred-after-paste path (after operator
  // refreshes via RefreshAuthModal).
  async function _doRunSuite(): Promise<{ run_id: string; status: string }> {
    return triggerRun({
      suite_type: suiteFilter,
      environment: runEnvironment,
      project_id: currentProjectId || undefined,
    })
  }


  async function handleExecByTool({ tool, environment, suiteType, requirementId }: {
    tool: string; environment: string; suiteType: string; requirementId?: string
  }) {
    if (!currentProjectId) return
    setExecByToolRunning(true)
    const tid = `exec-${tool}-${Date.now()}`
    addTask({ id: tid, type: 'test_execution', label: `Executing ${tool}${requirementId ? ' for ' + requirementId : ''} (${environment})`, result_url: '/run-history' })
    try {
      const result = await executeByTool({ projectId: currentProjectId, tool, environment, suiteType, requirementId })
      completeTask(tid, { detail: `run ${result.run_id} · ${result.status} — see Run History` })
      setExecByToolOpen(false)
    } catch (err: unknown) {
      const e = err as { status?: number; message?: string }
      failTask(tid, e.status === 400 ? 'Unsupported tool' : (e.message || 'Execute by tool failed'))
    } finally {
      setExecByToolRunning(false)
    }
  }

  async function handleRunSuite() {
    if (suiteRunning) return // Prevent duplicate polling intervals
    setSuiteRunning(true)
    try {
      // R15 — pre-flight check; when auth is expired, opens the
      // RefreshAuthModal and queues `_doRunSuite` for after paste. The
      // captured.current is null when the run was deferred.
      const capturedRef: { current: { run_id: string; status: string } | null } = { current: null }
      await runGuard.runOrPromptRefresh(async () => {
        capturedRef.current = await _doRunSuite()
      })
      if (!capturedRef.current) {
        // Run was queued — modal handles the rest. Reset suiteRunning
        // so the operator can dismiss the modal and try again later.
        setSuiteRunning(false)
        return
      }
      const result: { run_id: string; status: string } = capturedRef.current
      const taskId = `suite-${Date.now()}`
      addTask({ id: taskId, type: 'test_execution', label: `Running ${suiteFilter} suite`, result_url: '/test-explorer' })
      toast.success(`Suite started: ${result.run_id}`)

      // Poll for completion
      let attempts = 0
      const poll = setInterval(async () => {
        attempts++
        try {
          const runs = await fetchRuns({ project_id: currentProjectId || undefined })
          const run = (runs?.runs || []).find((r: any) => r.run_id === result.run_id)
          if (run?.status === 'completed' || run?.status === 'failed') {
            clearInterval(poll)
            setSuiteRunning(false)
            setLastRunId(result.run_id)
            setLastRunSummary({ passed: run.passed || 0, total: run.total_tests || 0 })
            completeTask(taskId, { detail: `${run.passed || 0}/${run.total_tests || 0} passed`, result_url: '/test-explorer' })
            toast.success(`Suite complete: ${run.passed || 0}/${run.total_tests || 0} passed`)
            // Refresh test list
            const params = { ...filter, project_id: currentProjectId || undefined }
            fetchTests(params).then(d => { setTests(d.tests); setTotal(d.total) }).catch(() => {})
          } else {
            updateTask(taskId, { progress: Math.min(85, 5 + attempts * 3), detail: `Executing... (${attempts * 5}s)` })
          }
        } catch {}
        if (attempts > 120) {
          clearInterval(poll)
          setSuiteRunning(false)
          failTask(taskId, 'Timed out after 10 minutes')
        }
      }, 5000)
    } catch (err: any) {
      // R38.1 — surface the structured 409 config_incomplete payload so
      // operators see WHICH env vars need filling instead of an opaque
      // "Failed to start test run" toast. Mirrors the RunPipelineModal
      // handler — keeps every Run-suite entry point consistent.
      setSuiteRunning(false)
      const detail = (err as any)?.detail
      if (detail && detail.error === 'config_incomplete') {
        const sample = (detail.unfilled_vars || []).slice(0, 3).join(', ')
        const more = (detail.unfilled_count || 0) > 3
          ? ` + ${detail.unfilled_count - 3} more` : ''
        // R45.5 — deep-link includes the unfilled var names so the
        // Variables editor highlights them + scrolls to the first.
        const highlightQs = (detail.unfilled_vars || []).length > 0
          ? `&highlight=${encodeURIComponent((detail.unfilled_vars || []).join(','))}`
          : ''
        toast.error(
          `Refusing to run: ${detail.unfilled_count} env var(s) need values ` +
          `(${sample}${more}). Paste a fresh cookie below, or open ` +
          `Settings → Environments → Variables ` +
          `(/settings#environments?env=${encodeURIComponent(runEnvironment)}${highlightQs}) ` +
          `to fill manually.`,
        )
        // R40.1 — when 409 fires, fetch discovery-status to confirm the
        // root cause is auth-state-missing, then auto-open the
        // RefreshAuthModal so the operator can paste a fresh cookie
        // inline (no navigation away from this page). Eliminates the
        // dead-end-toast experience.
        if (currentProjectId) {
          try {
            const s = await fetchDiscoveryStatus(currentProjectId, runEnvironment)
            setDiscoveryStatus(s)
            if (!s.auth_state_present) {
              setShowExtraRefreshAuth(true)
            }
          } catch { /* silent — banner remains the fallback */ }
        }
      } else if (detail && detail.error === 'auth_state_missing') {
        // R39.4 412 — server-side rejected force=true. Open inline.
        toast.error('Run Anyway blocked — paste a fresh cookie first.')
        setShowExtraRefreshAuth(true)
      } else {
        const msg = typeof err?.message === 'string' && err.message
          ? `Failed to start test run: ${err.message}`
          : 'Failed to start test run'
        toast.error(msg)
      }
    }
  }

  // Resume polling if a test run is active (survives page navigation)
  useEffect(() => {
    if (!currentProjectId || suiteRunning) return
    let cancelled = false
    getActiveRun(currentProjectId).then(async (run) => {
      if (cancelled || run.status !== 'running' || !run.run_id) return
      setSuiteRunning(true)
      while (!cancelled) {
        await new Promise(r => setTimeout(r, 5000))
        try {
          const runs = await fetchRuns({ project_id: currentProjectId || undefined })
          const updated = (runs?.runs || []).find((r: any) => r.run_id === run.run_id)
          if (cancelled) break
          if (updated?.status === 'completed' || updated?.status === 'failed') {
            setLastRunId(run.run_id)
            setLastRunSummary({ passed: updated.passed || 0, total: updated.total_tests || 0 })
            toast.success(`Suite complete: ${updated.passed || 0}/${updated.total_tests || 0} passed`)
            const params = { ...filter, project_id: currentProjectId || undefined }
            fetchTests(params).then(d => { setTests(d.tests); setTotal(d.total) }).catch(() => {})
            break
          }
        } catch { break }
      }
      if (!cancelled) setSuiteRunning(false)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [currentProjectId])

  const selectedTool = selected?.automation_tool || inferTool(selected?.test_type || '')
  const hasRealScript = !!(selected?.script_content && selected.script_content.trim().length > 0)
  const isGenerationError = !!(selected?.generation_error)
  // F12-11: when the LLM didn't produce script_content, show empty rather
  // than fabricated mock code that misleads users about what was generated.
  const scriptContent: string = hasRealScript ? (selected!.script_content ?? '') : ''

  // F12-11: render a dash for unknown durations instead of a random fake.
  function getDuration(_testId: string): string {
    return '—'
  }

  // Map test_type to short label
  function getTypeLabel(testType: string): string {
    const t = testType.toLowerCase()
    if (t.includes('ui') || t.includes('e2e')) return 'UI'
    if (t.includes('api') || t.includes('integration')) return 'API'
    if (t.includes('perf') || t.includes('load')) return 'Perf'
    if (t.includes('security')) return 'Security'
    return 'UI'
  }

  // R57.11 — tool-inventory gaps (tools with 0 specs). Banner below
  // renders a deep-link to /architecture where the operator can use
  // R54's modal to regenerate. Test-explorer is read-only for this
  // concern — keeps the modal in one place (architecture page).
  const toolGaps = useMemo(() => computeToolInventoryGaps(tests), [tests])

  return (
    <div className="p-6 flex flex-col" style={{ height: '100vh', overflow: 'hidden' }}>
      {/* R57.11 — tool-inventory-empty banner. Deep-links to the
          Architecture page where R54's RegenerateByToolModal lives.
          Pre-R57.11 the operator could dispatch a 38min run and only
          notice afterwards that one tool had 0 specs. */}
      {tests.length > 0 && toolGaps.length > 0 && (
        <div
          className="rounded-xl p-3 mb-3 flex-shrink-0"
          style={{
            background: 'rgba(245,158,11,0.08)',
            border: '1px solid rgba(245,158,11,0.40)',
          }}
        >
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex-1 min-w-[260px]">
              <span className="text-xs font-semibold" style={{ color: '#f59e0b' }}>
                {'⚠'} Empty tool inventory: {toolGaps.map(g => g.tool).join(', ')}
              </span>
              <span className="ml-2 text-[11px]" style={{ color: '#94a3b8' }}>
                ({toolGaps.length} of 6 automation tools have 0 specs;
                runs against this project will produce 0 results for those tools)
              </span>
            </div>
            <Link
              href="/architecture"
              className="px-3 py-1.5 rounded-md text-xs font-semibold text-white"
              style={{ background: 'linear-gradient(135deg,#f59e0b,#fb923c)' }}
            >
              Regenerate via Architecture →
            </Link>
          </div>
        </div>
      )}
      {/* Header */}
      <div className="flex-shrink-0">
        <div className="flex items-start justify-between mb-0.5">
          <h1 className="text-xl font-bold" style={{ fontFamily: 'DM Sans, sans-serif' }}>Test Explorer</h1>
          {/* Phase J3 — view-mode toggle */}
          <div className="flex gap-1 rounded-lg overflow-hidden"
               style={{ border: '1px solid #1e1e3a' }}>
            <button
              onClick={() => setViewMode('tests')}
              className="px-3 py-1.5 text-xs font-medium transition-colors"
              style={{
                background: viewMode === 'tests' ? '#6366f1' : 'transparent',
                color: viewMode === 'tests' ? '#fff' : '#94a3b8',
              }}
            >
              Tests
            </button>
            <button
              onClick={() => setViewMode('chains')}
              className="px-3 py-1.5 text-xs font-medium transition-colors"
              style={{
                background: viewMode === 'chains' ? '#6366f1' : 'transparent',
                color: viewMode === 'chains' ? '#fff' : '#94a3b8',
              }}
            >
              Chains
            </button>
          </div>
        </div>
        <p className="text-sm mb-4" style={{ color: '#64748b', fontFamily: 'DM Sans, sans-serif' }}>
          {viewMode === 'tests'
            ? 'Browse, filter, and inspect test cases'
            : 'Captured API call chains with provides/consumes dataflow links'}
        </p>

        {/* Phase J3 — Chain View renders here when viewMode === 'chains' */}
        {viewMode === 'chains' && (
          <div className="flex-1 min-h-0 overflow-auto">
            {currentProjectId ? (
              <ChainViewDAG projectId={currentProjectId} />
            ) : (
              <div className="rounded-lg p-6 text-sm"
                   style={{ background: '#0f0f23', border: '1px solid #1e1e3a', color: '#64748b' }}>
                Select a project to view captured chains.
              </div>
            )}
          </div>
        )}
      </div>

      {/* Default Tests view continues below — hidden when viewMode is 'chains' */}
      <div style={{ display: viewMode === 'chains' ? 'none' : 'contents' }}>
      <div className="flex-shrink-0">

        {/* R40.4 — passive auth-state header card. Renders BEFORE the
            operator clicks Run Suite when discovery-status reports
            EITHER missing auth OR unfilled env vars. Both cases produce
            the same "Refusing to run: N env var(s) need values" toast
            from the gate; both recover via the Refresh Auth flow
            (paste cookie → re-run discovery → vars auto-fill).

            R43 — broadened the trigger: pre-fix the banner only fired
            when auth_state_present=false. With a stale storage_state
            file present but vars never harvested, auth_state_present
            evaluated true and the banner stayed hidden — even though
            the gate 409'd on unfilled vars. Now fires on EITHER signal. */}
        {discoveryStatus && (
          !discoveryStatus.auth_state_present
          || discoveryStatus.unfilled_count > 5
        ) && (
          <div className="mb-4 rounded-lg px-4 py-3 flex items-center justify-between"
               style={{ background: 'rgba(251,113,133,0.10)',
                        border: '1px solid rgba(251,113,133,0.40)' }}>
            <div className="flex items-center gap-3">
              <span style={{ color: '#fb7185', fontSize: 18 }}>⛔</span>
              <div>
                <div className="text-sm font-semibold" style={{ color: '#fb7185' }}>
                  {!discoveryStatus.auth_state_present
                    ? 'No fresh SUT auth — Run Suite will refuse'
                    : `${discoveryStatus.unfilled_count} env vars unfilled — Run Suite will refuse`}
                </div>
                <div className="text-[11px] mt-0.5" style={{ color: '#94a3b8' }}>
                  {!discoveryStatus.auth_state_present
                    ? `Discovery cannot authenticate against the SUT. Paste a fresh cookie below; discovery will auto-fill the ${discoveryStatus.unfilled_count} placeholder vars.`
                    : `Auth state present but ${discoveryStatus.unfilled_count} vars are placeholders — re-run discovery to harvest values from the SUT, OR paste a fresh cookie if the existing one is stale.`}
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setShowExtraRefreshAuth(true)}
              className="px-3 py-1.5 rounded-md text-xs font-semibold text-white"
              style={{ background: 'linear-gradient(135deg,#f59e0b,#fb7185)' }}
            >
              Paste Auth →
            </button>
          </div>
        )}

        {/* Pending Review Banner */}
        {pendingReviews.length > 0 && !bannerDismissed && (
          <div className="mb-4 rounded-lg px-4 py-3 flex items-center justify-between"
               style={{ background: '#f59e0b18', border: '1px solid #f59e0b44' }}>
            <div className="flex items-center gap-3">
              <span style={{ color: '#f59e0b', fontSize: 18 }}>{'\u26A0'}</span>
              <div>
                <div className="text-sm font-semibold" style={{ color: '#fbbf24' }}>
                  {pendingReviews.length} generated test suite{pendingReviews.length !== 1 ? 's' : ''} awaiting review
                </div>
                <div className="text-[11px] mt-0.5" style={{ color: '#92711a' }}>
                  {/* F10-4: derive from API data instead of hardcoded REQ ids — the
                       previous text claimed REQ-017/022/031 regardless of which
                       reviews actually existed. */}
                  Auto-generated from {pendingReviews.map((r: PendingReview) => r.requirement_id).join(', ')}
                  {' '}&middot; Requires human approval before automation
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => openReview(pendingReviews[0])}
                className="px-3 py-1.5 rounded-lg text-xs font-medium transition"
                style={{ background: '#f59e0b', color: '#0a0a14' }}
              >
                Review Now
              </button>
              <button
                onClick={() => setBannerDismissed(true)}
                className="w-6 h-6 flex items-center justify-center rounded transition"
                style={{ color: '#f59e0b80' }}
              >
                {'\u2715'}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Review Panel Modal */}
      {selectedReview && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: 'rgba(0,0,0,0.7)' }}>
          <div className="w-full max-w-3xl max-h-[90vh] overflow-y-auto rounded-xl p-6"
               style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            {/* Header */}
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold">Review Generated Tests</h2>
              <button onClick={() => setSelectedReview(null)} className="text-sm px-2 py-1 rounded"
                      style={{ color: '#64748b' }}>
                Close
              </button>
            </div>

            {/* Metadata */}
            <div className="flex gap-4 mb-4 text-xs" style={{ color: '#94a3b8' }}>
              <span>Requirement: <span style={{ color: '#a5b4fc' }}>{selectedReview.requirement_id}</span></span>
              <span>Risk Score: <span style={{ color: selectedReview.risk_profile.risk_score >= 8 ? '#ef4444' : '#f59e0b' }}>
                {selectedReview.risk_profile.risk_score}
              </span></span>
              <span>Priority: <span style={{ color: '#c4b5fd' }}>{selectedReview.risk_profile.priority}</span></span>
            </div>

            {/* Test Types */}
            <div className="flex gap-2 mb-4">
              {selectedReview.risk_profile.test_types.map(tt => (
                <span key={tt} className="text-[10px] px-2 py-0.5 rounded-full"
                      style={{ background: '#6366f122', color: '#a5b4fc', border: '1px solid #6366f144' }}>
                  {tt}
                </span>
              ))}
            </div>

            {/* Gherkin Scenarios */}
            <div className="mb-4">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-semibold" style={{ color: '#64748b' }}>Gherkin Scenarios</h3>
                <button
                  onClick={() => setIsEditing(!isEditing)}
                  className="text-[11px] px-2 py-1 rounded transition"
                  style={{ background: isEditing ? '#6366f1' : '#1e1e3a', color: '#e2e8f0' }}
                >
                  {isEditing ? 'Preview' : 'Edit'}
                </button>
              </div>
              {isEditing ? (
                <textarea
                  value={editedGherkin}
                  onChange={e => setEditedGherkin(e.target.value)}
                  className="w-full rounded-lg p-3 text-[11px] min-h-[200px] resize-y"
                  style={{
                    background: '#0a0a14', color: '#a5b4fc', border: '1px solid #1e1e3a',
                    fontFamily: 'JetBrains Mono, monospace',
                  }}
                />
              ) : (
                <pre className="text-[11px] p-3 rounded-lg overflow-x-auto whitespace-pre-wrap"
                     style={{ background: '#0a0a14', color: '#a5b4fc', fontFamily: 'JetBrains Mono, monospace' }}>
                  {editedGherkin}
                </pre>
              )}
            </div>

            {/* Test Data Table */}
            {selectedReview.test_data && selectedReview.test_data.columns.length > 0 && (
              <div className="mb-4">
                <h3 className="text-sm font-semibold mb-2" style={{ color: '#64748b' }}>Test Data</h3>
                <div className="overflow-x-auto rounded-lg" style={{ border: '1px solid #1e1e3a' }}>
                  <table className="w-full text-[11px]">
                    <thead>
                      <tr style={{ background: '#1e1e3a' }}>
                        {selectedReview.test_data.columns.map(col => (
                          <th key={col} className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>
                            {col}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {selectedReview.test_data.rows.map((row: any, idx: number) => (
                        <tr key={idx} style={{ borderTop: '1px solid #1e1e3a' }}>
                          {selectedReview.test_data.columns.map((col: string, colIdx: number) => (
                            <td key={col} className="px-3 py-2" style={{ color: '#e2e8f0' }}>
                              {String(Array.isArray(row) ? (row[colIdx] ?? '') : (row[col] ?? ''))}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Star Rating */}
            <div className="mb-4">
              <h3 className="text-sm font-semibold mb-2" style={{ color: '#64748b' }}>Quality Rating</h3>
              <StarRating value={feedbackRating} onChange={setFeedbackRating} />
            </div>

            {/* Reason / Feedback — shown when reject is clicked */}
            {showRejectReason && (
              <div className="mb-4">
                <h3 className="text-sm font-semibold mb-2" style={{ color: '#64748b' }}>Rejection Reason</h3>
                <textarea
                  value={reviewReason}
                  onChange={e => setReviewReason(e.target.value)}
                  placeholder="Explain why you are rejecting these tests..."
                  className="w-full rounded-lg p-3 text-xs min-h-[80px] resize-y"
                  style={{ background: '#0a0a14', color: '#e2e8f0', border: '1px solid #1e1e3a' }}
                />
              </div>
            )}

            {/* Decision Buttons */}
            <div className="flex gap-3">
              <button
                onClick={() => handleReviewDecision('approve')}
                disabled={reviewSubmitting}
                className="flex-1 py-2.5 rounded-lg text-sm font-medium transition disabled:opacity-50"
                style={{ background: '#10b981', color: '#fff' }}
              >
                {reviewSubmitting ? 'Submitting...' : 'Approve & Generate Scripts'}
              </button>
              <button
                onClick={() => handleReviewDecision('reject')}
                disabled={reviewSubmitting}
                className="flex-1 py-2.5 rounded-lg text-sm font-medium transition disabled:opacity-50"
                style={{ background: showRejectReason ? '#ef4444' : '#7f1d1d', color: '#fff' }}
              >
                {reviewSubmitting ? 'Submitting...' : showRejectReason ? 'Confirm Reject' : 'Reject & Regenerate'}
              </button>
            </div>

            {/* Other pending reviews list */}
            {pendingReviews.length > 1 && (
              <div className="mt-4 pt-4" style={{ borderTop: '1px solid #1e1e3a' }}>
                <h3 className="text-xs font-semibold mb-2" style={{ color: '#64748b' }}>
                  Other Pending Reviews ({pendingReviews.length - 1})
                </h3>
                <div className="space-y-1">
                  {pendingReviews
                    .filter(r => r.workflow_id !== selectedReview.workflow_id)
                    .map(r => (
                      <button
                        key={r.workflow_id}
                        onClick={() => openReview(r)}
                        className="w-full text-left rounded-lg px-3 py-2 text-xs transition hover:ring-1"
                        style={{ background: '#0a0a14', color: '#94a3b8' }}
                      >
                        {r.requirement_id} — Risk {r.risk_profile.risk_score} ({r.risk_profile.priority})
                      </button>
                    ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* F13-1: 2-column layout — list (1fr) + detail (3fr). The Quick
           Actions panel that used to occupy a 180px right sidebar has been
           moved to a single-row strip at the BOTTOM of the test-detail
           middle column. This frees the entire content width for reading
           generated Code / Gherkin / Test Data without horizontal scroll. */}
      <div
        className="flex-1 min-h-0"
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 3fr',
          gap: 16,
        }}
      >
        {/* ═══ LEFT COLUMN — Test List + Filters ═══ */}
        <div className="flex flex-col min-h-0">
          {/* Filter Card */}
          <div
            className="rounded-lg p-3 mb-3 flex-shrink-0"
            style={{
              background: 'rgba(18,18,31,0.7)',
              border: '1px solid #1e1e3a',
              backdropFilter: 'blur(12px)',
            }}
          >
            {/* Row 1 — Suite filters */}
            <div className="flex items-center gap-1.5 mb-2">
              <span className="text-[10px] mr-1" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>Suite:</span>
              {(['full', 'smoke', 'regression', 'custom'] as const).map(suite => (
                <button
                  key={suite}
                  onClick={() => handleSuiteFilter(suite)}
                  className="px-2.5 py-1 rounded-full text-[10px] font-medium transition"
                  style={{
                    background: suiteFilter === suite ? '#6366f1' : 'transparent',
                    border: `1px solid ${suiteFilter === suite ? '#6366f1' : '#1e1e3a'}`,
                    color: suiteFilter === suite ? '#fff' : '#94a3b8',
                  }}
                >
                  {suite === 'full' ? 'Full' : suite === 'smoke' ? 'Smoke(P0)' : suite === 'regression' ? 'Regression(P0+P1)' : 'Custom'}
                </button>
              ))}
              <div className="flex-1" />
              {/* Environment selector — populated from .arta/environments/*.json */}
              <select
                value={runEnvironment}
                onChange={e => setRunEnvironment(e.target.value)}
                className="px-2 py-1 rounded-lg text-[10px] outline-none cursor-pointer"
                style={{ background: '#0a0a14', color: '#94a3b8', border: '1px solid #1e1e3a', fontFamily: "'Space Mono', monospace" }}
              >
                {environments.length > 0 ? (
                  environments.map(env => (
                    <option key={env.id} value={env.id} title={env.base_url}>
                      {env.name || env.id}
                    </option>
                  ))
                ) : (
                  <>
                    <option value="local">local</option>
                    <option value="staging">staging</option>
                    <option value="production">production</option>
                  </>
                )}
              </select>
              <button
                onClick={handleRunSuite}
                disabled={suiteRunning}
                className="px-3 py-1 rounded-full text-[10px] font-medium transition flex items-center gap-1 disabled:opacity-50"
                style={{ background: suiteRunning ? '#6366f1' : '#10b981', color: '#fff' }}
              >
                {suiteRunning ? (
                  <><span className="animate-spin" style={{ fontSize: 8 }}>&#9696;</span> Running...</>
                ) : (
                  <><span style={{ fontSize: 8 }}>{'\u25B6'}</span> Run Suite</>
                )}
              </button>
              {/* WS4 \u2014 run a single tool (mirrors Regenerate by Tool). */}
              <button
                onClick={() => { if (!currentProjectId) return; setExecByToolOpen(true) }}
                disabled={execByToolRunning}
                className="px-3 py-1 rounded-full text-[10px] font-medium transition flex items-center gap-1 disabled:opacity-50"
                style={{ background: 'transparent', border: '1px solid #8b5cf644', color: '#c4b5fd' }}
                title="Execute a single tool (Playwright, Newman, k6, ZAP, Axe, Pytest)"
              >
                {execByToolRunning ? '\u23F3 Executing\u2026' : '\u25B6 Execute by Tool'}
              </button>
            </div>

            {/* Row 2 — Status filters */}
            <div className="flex items-center gap-1.5">
              {([
                { key: 'all' as const, label: `All (${suiteFiltered.length})` },
                { key: 'PASS' as const, label: `Pass (${passCount})` },
                { key: 'FAIL' as const, label: `Fail (${failCount})` },
                { key: 'SKIP' as const, label: `Skip (${skipCount})` },
              ]).map(item => (
                <button
                  key={item.key}
                  onClick={() => setStatusFilter(item.key)}
                  className="px-2.5 py-1 rounded-full text-[10px] font-medium transition"
                  style={{
                    background: statusFilter === item.key ? 'rgba(6,182,212,0.15)' : 'transparent',
                    color: statusFilter === item.key ? '#22d3ee' : '#94a3b8',
                    border: `1px solid ${statusFilter === item.key ? 'rgba(6,182,212,0.3)' : 'transparent'}`,
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>

          {/* Last Run banner — shown after a suite completes */}
          {lastRunId && lastRunSummary && (
            <div className="mb-2 px-3 py-2 rounded-lg flex items-center justify-between text-xs"
                 style={{ background: 'rgba(99,102,241,0.08)', border: '1px solid #6366f130' }}>
              <span style={{ color: '#a5b4fc' }}>
                Last run:{' '}
                <strong style={{ color: lastRunSummary.passed === lastRunSummary.total ? '#34d399' : '#fb7185' }}>
                  {lastRunSummary.passed}/{lastRunSummary.total}
                </strong>{' '}passed
              </span>
              <a href={`/run-history?run_id=${lastRunId}`} style={{ color: '#06b6d4' }}>
                View Results →
              </a>
            </div>
          )}

          {/* Test Cards List (scrollable) */}
          <div className="flex-1 overflow-y-auto space-y-1.5 pr-1" style={{ scrollbarWidth: 'thin', scrollbarColor: '#1e1e3a transparent' }}>
            {loading ? (
              <p className="text-sm py-8 text-center" style={{ color: '#94a3b8' }}>Loading tests...</p>
            ) : displayedTests.length === 0 ? (
              <p className="text-sm py-8 text-center" style={{ color: '#94a3b8' }}>No tests found</p>
            ) : displayedTests.map(tc => {
              const toolKey = tc.automation_tool || inferTool(tc.test_type)
              const toolColor = TOOL_COLORS[toolKey] || '#64748b'
              const toolLabel = TOOL_LABELS[toolKey] || toolKey
              const isSelected = selected?.test_id === tc.test_id
              const statusColor = STATUS_COLORS[tc.last_status] || '#94a3b8'
              const duration = getDuration(tc.test_id)
              const typeLabel = getTypeLabel(tc.test_type)

              // Priority colors
              const priorityColors: Record<string, string> = {
                P0: '#f43f5e', P1: '#f59e0b', P2: '#6366f1', P3: '#64748b',
              }
              const prioColor = priorityColors[tc.priority] || '#64748b'

              return (
                <div
                  key={tc.test_id}
                  onClick={() => { setSelected(tc); setActiveTab('gherkin'); setHistoryOpen(false); setSelectedVersion(null) }}
                  className="rounded-lg px-3 py-2.5 cursor-pointer transition"
                  style={{
                    background: isSelected ? 'rgba(99,102,241,0.12)' : '#12121f',
                    // F4: Subtle rose border for adversarial tests so they're visually distinct
                    border: isSelected ? '1px solid #6366f1'
                          : (tc.test_type === 'Adversarial' || tc.adversarial_category)
                              ? '1px solid rgba(251,113,133,0.35)'
                              : '1px solid #1e1e3a',
                    borderLeft: isSelected
                      ? '3px solid #6366f1'
                      : (tc.test_type === 'Adversarial' || tc.adversarial_category)
                          ? '3px solid #fb7185'
                          : `3px solid ${statusColor}`,
                    boxShadow: isSelected ? '0 0 12px rgba(99,102,241,0.15)' : 'none',
                  }}
                >
                  {/* Row 1: Status dot + TC-ID + Title */}
                  <div className="flex items-center gap-2 mb-1.5">
                    {/* Status dot (8px colored circle) */}
                    <span
                      className="flex-shrink-0 rounded-full"
                      style={{
                        width: 8,
                        height: 8,
                        background: statusColor,
                        boxShadow: `0 0 6px ${statusColor}66`,
                      }}
                    />
                    <span
                      className="text-[9px] px-1.5 py-0.5 rounded-full font-medium flex-shrink-0"
                      style={{ background: `${statusColor}22`, color: statusColor }}
                    >
                      {tc.last_status}
                    </span>
                    <span
                      className="text-[11px] font-medium flex-shrink-0"
                      style={{ color: '#06b6d4', fontFamily: 'JetBrains Mono, monospace' }}
                    >
                      {tc.test_id}
                    </span>
                    <span className="text-[11px] truncate flex-1" style={{ color: '#e2e8f0' }}>
                      {tc.title}
                    </span>

                    {/* Part 5B — per-test Run button + environment popover */}
                    <div className="relative flex-shrink-0" onClick={e => e.stopPropagation()}>
                      <button
                        onClick={() => setRunPopoverFor(runPopoverFor === tc.test_id ? null : tc.test_id)}
                        disabled={runningSingle === tc.test_id}
                        className="text-[10px] px-2 py-0.5 rounded-full font-medium transition disabled:opacity-50"
                        style={{
                          background: runningSingle === tc.test_id ? '#6366f1' : 'rgba(16,185,129,0.15)',
                          color: runningSingle === tc.test_id ? '#fff' : '#10b981',
                          border: '1px solid rgba(16,185,129,0.4)',
                        }}
                        title={runningSingle === tc.test_id ? 'Running…' : 'Run this test'}
                      >
                        {runningSingle === tc.test_id ? '…' : '▶ Run'}
                      </button>
                      {runPopoverFor === tc.test_id && (
                        <div
                          className="absolute right-0 top-full mt-1 z-50 rounded-lg p-2 shadow-xl"
                          style={{ background: '#0a0a14', border: '1px solid #1e1e3a', minWidth: 200 }}
                        >
                          <div className="text-[9px] mb-1.5" style={{ color: '#94a3b8' }}>
                            Environment
                          </div>
                          <select
                            value={runEnvironment}
                            onChange={e => setRunEnvironment(e.target.value)}
                            className="w-full px-2 py-1 rounded text-[10px] outline-none cursor-pointer mb-2"
                            style={{ background: '#12121f', color: '#e2e8f0', border: '1px solid #1e1e3a' }}
                          >
                            {environments.length > 0 ? environments.map(env => (
                              <option key={env.id} value={env.id}>
                                {env.name || env.id} {env.base_url ? `— ${env.base_url}` : ''}
                              </option>
                            )) : (
                              <>
                                <option value="staging">staging</option>
                                <option value="local">local</option>
                              </>
                            )}
                          </select>
                          <div className="flex gap-1.5">
                            <button
                              onClick={() => handleRunSingleTest(tc.test_id, runEnvironment)}
                              className="flex-1 text-[10px] px-2 py-1 rounded font-medium"
                              style={{ background: '#10b981', color: '#fff' }}
                            >
                              Run
                            </button>
                            <button
                              onClick={() => setRunPopoverFor(null)}
                              className="flex-1 text-[10px] px-2 py-1 rounded"
                              style={{ background: '#1e1e3a', color: '#94a3b8' }}
                            >
                              Cancel
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Row 2: Tool badge + Type badge + Duration + Priority */}
                  <div className="flex items-center gap-2">
                    <span
                      className="text-[9px] px-1.5 py-0.5 rounded-full font-medium"
                      style={{ background: `${toolColor}18`, color: toolColor, border: `1px solid ${toolColor}44` }}
                    >
                      {toolLabel}
                    </span>
                    <span
                      className="text-[9px] px-1.5 py-0.5 rounded-full"
                      style={{ background: '#1e1e3a', color: '#94a3b8' }}
                    >
                      {typeLabel}
                    </span>
                    <span className="text-[10px]" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
                      {duration}
                    </span>
                    <span
                      className="text-[9px] px-1.5 py-0.5 rounded-full font-semibold"
                      style={{ background: `${prioColor}18`, color: prioColor }}
                    >
                      {tc.priority}
                    </span>

                    {/* G2.3: Analytics + Traceability badges (conditional) */}
                    {tc.tier != null && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded-full font-semibold"
                            title={`Execution tier ${tc.tier}`}
                            style={{ background: '#1e293b', color: '#7dd3fc' }}>
                        T{tc.tier}
                      </span>
                    )}
                    {/* F4: Adversarial test badge — visually distinct (red/warning) */}
                    {(tc.test_type === 'Adversarial' || tc.adversarial_category) && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded-full font-semibold flex-shrink-0"
                            title={tc.adversarial_input
                              ? `Adversarial (${tc.adversarial_category}): ${tc.adversarial_input}`
                              : 'Adversarial analytics test'}
                            style={{
                              background: 'rgba(251,113,133,0.12)',
                              color: '#fb7185',
                              border: '1px solid rgba(251,113,133,0.35)',
                            }}>
                        ⚠ ADV{tc.adversarial_category ? `: ${tc.adversarial_category.replace(/_/g, ' ')}` : ''}
                      </span>
                    )}
                    {tc.analytics_layer && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded-full"
                            title={`Analytics layer: ${tc.analytics_layer}`}
                            style={{ background: '#1e1b3a', color: '#a5b4fc' }}>
                        {tc.analytics_layer.replace(/_/g, '→').replace('to→', '→')}
                      </span>
                    )}
                    {typeof tc.judge_score === 'number' && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded-full font-semibold"
                            title="LLM-as-judge score"
                            style={{
                              background: tc.judge_score >= 0.8 ? 'rgba(52,211,153,0.15)' : tc.judge_score >= 0.5 ? 'rgba(251,191,36,0.15)' : 'rgba(251,113,133,0.15)',
                              color: tc.judge_score >= 0.8 ? '#34d399' : tc.judge_score >= 0.5 ? '#fbbf24' : '#fb7185',
                            }}>
                        Judge: {tc.judge_score.toFixed(2)}
                      </span>
                    )}
                    {tc.red_phase_status && tc.red_phase_status !== 'PENDING_VERIFICATION' && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded-full font-semibold"
                            title={`ATDD red-phase: ${tc.red_phase_status}`}
                            style={{
                              background: tc.red_phase_status === 'RED' ? 'rgba(52,211,153,0.15)' :
                                          tc.red_phase_status === 'GREEN_UNEXPECTED' ? 'rgba(251,191,36,0.18)' :
                                          'rgba(251,113,133,0.15)',
                              color: tc.red_phase_status === 'RED' ? '#34d399' :
                                     tc.red_phase_status === 'GREEN_UNEXPECTED' ? '#fbbf24' : '#fb7185',
                            }}>
                        {tc.red_phase_status === 'RED' ? '🔴 RED' :
                         tc.red_phase_status === 'GREEN_UNEXPECTED' ? '⚠ GREEN?' :
                         tc.red_phase_status}
                      </span>
                    )}

                    <div className="flex-1" />

                    {/* Create PR on GitHub */}
                    <button
                      onClick={(e) => handlePushToGitHub(tc.test_id, e)}
                      disabled={pushingTestId === tc.test_id}
                      title="Create PR on GitHub"
                      className="w-6 h-6 flex items-center justify-center rounded-md transition disabled:opacity-50"
                      style={{
                        color: '#8b5cf6',
                        background: 'rgba(139,92,246,0.1)',
                        border: '1px solid rgba(139,92,246,0.25)',
                      }}
                      onMouseEnter={e => { e.currentTarget.style.background = 'rgba(139,92,246,0.2)'; e.currentTarget.style.borderColor = 'rgba(139,92,246,0.5)' }}
                      onMouseLeave={e => { e.currentTarget.style.background = 'rgba(139,92,246,0.1)'; e.currentTarget.style.borderColor = 'rgba(139,92,246,0.25)' }}
                    >
                      {pushingTestId === tc.test_id ? (
                        <span className="text-[10px]">{'\u23F3'}</span>
                      ) : (
                        <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
                          <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
                        </svg>
                      )}
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* ═══ CENTER COLUMN — Code Viewer ═══ */}
        <div
          className="rounded-xl flex flex-col min-h-0 overflow-hidden"
          style={{ background: '#12121f', border: '1px solid #1e1e3a' }}
        >
          {selected ? (
            <>
              {/* Header bar (sticky) */}
              <div
                className="flex items-center justify-between px-4 py-3 flex-shrink-0"
                style={{ borderBottom: '1px solid #1e1e3a' }}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span
                    className="text-sm font-semibold flex-shrink-0"
                    style={{ color: '#06b6d4', fontFamily: 'JetBrains Mono, monospace' }}
                  >
                    {selected.test_id}
                  </span>
                  <span style={{ color: '#94a3b8' }}>&middot;</span>
                  <span className="text-sm truncate" style={{ color: '#e2e8f0' }}>
                    {selected.title}
                  </span>
                  {isGenerationError && (
                    <span
                      className="flex-shrink-0 text-[10px] px-1.5 py-0.5 rounded font-medium"
                      style={{ background: 'rgba(239,68,68,0.15)', color: '#ef4444', border: '1px solid rgba(239,68,68,0.3)' }}
                      title={`Generation failed: ${selected.generation_error}`}
                    >
                      ⚠ Generation Error
                    </span>
                  )}
                  {!isGenerationError && !hasRealScript && selected && (
                    <span
                      className="flex-shrink-0 text-[10px] px-1.5 py-0.5 rounded font-medium"
                      style={{ background: 'rgba(245,158,11,0.12)', color: '#f59e0b', border: '1px solid rgba(245,158,11,0.25)' }}
                      title="Script not yet generated — showing preview template"
                    >
                      ◈ Preview
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1.5 flex-shrink-0 ml-2">
                  <button
                    onClick={async () => {
                      try {
                        await requestHeal(selected.test_id || selected.id || '', selected.error || '', undefined, currentProjectId || undefined, selected.title)
                        toast.success('Healing analysis requested — check Self-Healing page')
                      } catch { toast.error('Failed to request healing') }
                    }}
                    className="px-2.5 py-1 rounded-lg text-[11px] transition"
                    style={{
                      background: 'transparent',
                      border: '1px solid #1e1e3a',
                      color: '#f59e0b',
                    }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'rgba(245,158,11,0.1)'; e.currentTarget.style.borderColor = 'rgba(245,158,11,0.3)' }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.borderColor = '#1e1e3a' }}
                  >
                    {'\u27F3'} Request Heal
                  </button>
                  <button
                    onClick={handleRunSuite}
                    className="px-2.5 py-1 rounded-lg text-[11px] transition"
                    style={{
                      background: 'transparent',
                      border: '1px solid #1e1e3a',
                      color: '#10b981',
                    }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'rgba(16,185,129,0.1)'; e.currentTarget.style.borderColor = 'rgba(16,185,129,0.3)' }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.borderColor = '#1e1e3a' }}
                  >
                    {'\u25B7'} Run
                  </button>
                  <button
                    onClick={() => { setHistoryOpen(true); setSelectedVersion(null) }}
                    className="px-2.5 py-1 rounded-lg text-[11px] transition"
                    style={{
                      background: 'transparent',
                      border: '1px solid #1e1e3a',
                      color: '#94a3b8',
                    }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'rgba(6,182,212,0.1)'; e.currentTarget.style.color = '#06b6d4' }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = '#94a3b8' }}
                  >
                    {'\u2387'} History
                  </button>
                  {/* R320 \u2014 deep-link to the Refinement Copilot scoped to this test.
                      The tester's correction becomes durable SUT grounding. */}
                  <Link
                    href={`/ai-assistant?test_id=${encodeURIComponent(selected.test_id || selected.id || '')}`
                      + `&requirement_id=${encodeURIComponent(selected.requirement_id || '')}`
                      + `&tool=${encodeURIComponent(selectedTool || '')}`
                      + `&ac_id=${encodeURIComponent((selected as any).ac_id || '')}`
                      + `&title=${encodeURIComponent(selected.title || '')}`}
                    className="px-2.5 py-1 rounded-lg text-[11px] transition"
                    style={{ background: 'transparent', border: '1px solid rgba(99,102,241,0.4)', color: '#a5b4fc' }}
                    title="Correct this AI-generated test \u2014 ARTA grounds on your fix so future generations improve"
                  >
                    {'\u2728'} Refine with AI
                  </Link>
                </div>
              </div>

              {/* F3: Traceability strip — shows trace_id/model/prompt/dataset versions for debugging */}
              {(selected.trace_id || selected.model_version || selected.prompt_version || selected.dataset_version) && (
                <div
                  className="flex flex-wrap items-center gap-2 px-3 py-2 text-[10px] flex-shrink-0"
                  style={{
                    background: '#0a0a14',
                    borderBottom: '1px solid #1e1e3a',
                    color: '#64748b',
                    fontFamily: 'JetBrains Mono, monospace',
                  }}
                >
                  <span style={{ color: '#94a3b8' }}>Trace:</span>
                  {selected.trace_id && (
                    <>
                      <button
                        onClick={() => {
                          navigator.clipboard.writeText(selected.trace_id as string)
                          toast.success(`Copied trace_id: ${(selected.trace_id as string).slice(0, 8)}…`)
                        }}
                        title={`Full trace_id: ${selected.trace_id}\nClick to copy`}
                        className="px-1.5 py-0.5 rounded"
                        style={{ background: '#1e293b', color: '#7dd3fc', cursor: 'pointer' }}
                        onMouseEnter={e => { e.currentTarget.style.background = '#334155' }}
                        onMouseLeave={e => { e.currentTarget.style.background = '#1e293b' }}
                      >
                        {(selected.trace_id as string).slice(0, 8)} 📋
                      </button>
                      {/* F1-4: Jump to trace cohort view */}
                      <a
                        href={`/traceability/trace/${encodeURIComponent(selected.trace_id as string)}`}
                        title="View all tests from this generation request"
                        className="px-1.5 py-0.5 rounded"
                        style={{ background: '#1e1b3a', color: '#a5b4fc', cursor: 'pointer', textDecoration: 'none' }}
                      >
                        cohort →
                      </a>
                    </>
                  )}
                  {selected.model_version && (
                    <span title={`Model: ${selected.model_version}`}
                          className="px-1.5 py-0.5 rounded"
                          style={{ background: '#1e1b3a', color: '#c4b5fd' }}>
                      {selected.model_version}
                    </span>
                  )}
                  {selected.prompt_version && (
                    <span title={`Prompt template SHA: ${selected.prompt_version}`}
                          className="px-1.5 py-0.5 rounded"
                          style={{ background: '#1e1b3a', color: '#94a3b8' }}>
                      prompt {(selected.prompt_version as string).slice(0, 6)}
                    </span>
                  )}
                  {selected.dataset_version && (
                    <span title={`Frozen dataset SHA: ${selected.dataset_version}`}
                          className="px-1.5 py-0.5 rounded"
                          style={{ background: '#1e293b', color: '#67e8f9' }}>
                      dataset {(selected.dataset_version as string).slice(0, 6)}
                    </span>
                  )}
                  {selected.red_phase_status && selected.red_phase_status !== 'PENDING_VERIFICATION' && (
                    <span title={`ATDD red-phase: ${selected.red_phase_status}`}
                          className="px-1.5 py-0.5 rounded font-semibold"
                          style={{
                            background: selected.red_phase_status === 'RED' ? 'rgba(52,211,153,0.15)' :
                                       selected.red_phase_status === 'GREEN_UNEXPECTED' ? 'rgba(251,191,36,0.18)' :
                                       'rgba(251,113,133,0.15)',
                            color: selected.red_phase_status === 'RED' ? '#34d399' :
                                   selected.red_phase_status === 'GREEN_UNEXPECTED' ? '#fbbf24' : '#fb7185',
                          }}>
                      {selected.red_phase_status}
                    </span>
                  )}
                </div>
              )}

              {/* Tab bar — cyan accent */}
              <div className="flex flex-shrink-0" style={{ borderBottom: '1px solid #1e1e3a' }}>
                {(['code', 'gherkin', 'data'] as const).map(tab => (
                  <button
                    key={tab}
                    onClick={() => setActiveTab(tab)}
                    className="flex-1 px-3 py-2 text-[11px] font-medium transition"
                    style={{
                      background: activeTab === tab ? 'rgba(6,182,212,0.15)' : 'transparent',
                      color: activeTab === tab ? '#22d3ee' : '#64748b',
                      borderBottom: activeTab === tab ? '2px solid #06b6d4' : '2px solid transparent',
                    }}
                  >
                    {tab === 'code' ? 'Code' : tab === 'gherkin' ? 'Gherkin' : 'Test Data'}
                  </button>
                ))}
              </div>

              {/* Tab content (scrollable) */}
              <div className="flex-1 overflow-y-auto">
                {activeTab === 'code' && (
                  <pre
                    className="text-[11px] p-4 whitespace-pre h-full"
                    style={{ background: '#0a0a14', fontFamily: 'JetBrains Mono, monospace' }}
                    dangerouslySetInnerHTML={{ __html: highlightCode(scriptContent) }}
                  />
                )}

                {activeTab === 'gherkin' && (
                  <GherkinHighlighter code={selected.gherkin_scenario || ''} maxHeight={400} />
                )}

                {activeTab === 'data' && (() => {
                  const testData = selected?.test_data || null;
                  const hasInline = testData && testData.columns && testData.columns.length > 0;
                  return (
                    <div className="p-3 space-y-4 overflow-auto h-full" style={{ background: '#0a0a14' }}>
                      {/* Frozen-dataset panel — shows file-based fixtures parsed from the
                          Gherkin (e.g. `frozen_dataset("fixtures/...")`). Renders nothing
                          when no refs are found, so inline-only tests stay clean. */}
                      <FrozenDatasetPanel testId={selected?.test_id || null} active={activeTab === 'data'} />

                      {hasInline && (
                        <div>
                          <div className="text-[10px] uppercase tracking-wider mb-1" style={{ color: '#64748b' }}>
                            Inline scenario data
                          </div>
                          <div className="rounded-lg overflow-auto" style={{ border: '1px solid #1e1e3a' }}>
                            <table className="w-full text-[11px]" style={{ fontFamily: 'Space Mono, monospace' }}>
                              <thead>
                                <tr style={{ background: '#12121f' }}>
                                  {testData.columns.map((col: string) => (
                                    <th key={col} className="px-3 py-2 text-left font-medium" style={{ color: '#94a3b8' }}>
                                      {col}
                                    </th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {testData.rows.map((row: string[], idx: number) => (
                                  <tr key={idx} style={{ borderTop: '1px solid #1e1e3a' }}>
                                    {row.map((v: string, j: number) => (
                                      <td key={j} className="px-3 py-2" style={{ color: '#e2e8f0' }}>
                                        {v}
                                      </td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}

                      {!hasInline && (
                        <div className="text-center text-[11px] py-4" style={{ color: '#64748b' }}>
                          No inline scenario data. Frozen datasets (if any) are shown above.
                        </div>
                      )}
                    </div>
                  );
                })()}
              </div>
            </>
          ) : (
            <div className="flex items-center justify-center h-full">
              <p className="text-sm" style={{ color: '#94a3b8' }}>
                Select a test to view details
              </p>
            </div>
          )}

          {/* F13-1: Quick Actions — moved from a 180px right sidebar to a
               single-row strip at the bottom of the test detail. Frees the
               full content width for reading Code / Gherkin / Test Data. */}
          {selected && (
            <div className="flex-shrink-0 mt-3 rounded-lg px-3 py-2 flex items-center gap-3 flex-wrap"
                 style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
              {/* Last execution status badge */}
              <div className="flex items-center gap-1.5 text-[11px]">
                <span style={{ color: '#64748b' }}>Last status:</span>
                <span className="px-2 py-0.5 rounded font-semibold text-[10px]"
                      style={{
                        background: `${STATUS_COLORS[selected.last_status] || '#94a3b8'}18`,
                        color: STATUS_COLORS[selected.last_status] || '#94a3b8',
                      }}>
                  {selected.last_status || 'PENDING'}
                </span>
              </div>
              {/* Run history link */}
              <a href={`/run-history${lastRunId ? `?run_id=${lastRunId}` : ''}`}
                 className="flex items-center gap-1.5 px-2 py-1 rounded text-[11px] transition hover:opacity-80"
                 style={{ background: '#12121f', color: '#94a3b8' }}>
                <span>{'\u{1F4CA}'}</span> View Run History
              </a>
              {/* Heal request when failed */}
              {selected.last_status === 'FAIL' && (
                <a href="/healing"
                   className="flex items-center gap-1.5 px-2 py-1 rounded text-[11px] transition hover:opacity-80"
                   style={{ background: '#f59e0b12', color: '#f59e0b', border: '1px solid #f59e0b22' }}>
                  <span>{'\u27F3'}</span> Request Self-Heal
                </a>
              )}
              {/* Failure hint inline (was a card before) */}
              {selected.last_status === 'FAIL' && (
                <span className="text-[10px]" style={{ color: '#fb7185' }}>
                  See Run History for failure classification + healing suggestions.
                </span>
              )}
              {/* Feedback toggle pushes right */}
              <button
                onClick={() => setFeedbackExpanded(!feedbackExpanded)}
                className="ml-auto flex items-center gap-1.5 px-2 py-1 rounded text-[11px] transition hover:opacity-80"
                style={{ color: '#94a3b8', background: '#12121f' }}
              >
                <span>{'\u2726'}</span> Feedback &amp; AI Suggestions
                <span style={{ fontSize: 9, transform: feedbackExpanded ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 0.2s' }}>
                  {'\u25BC'}
                </span>
              </button>
            </div>
          )}

          {/* F13-1: Feedback drawer expands beneath the strip when toggled */}
          {feedbackExpanded && selected && (
            <div className="flex-shrink-0 mt-2 rounded-lg p-3" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
              <div className="flex items-start gap-4">
                <div className="flex-1">
                  <h4 className="text-[10px] font-semibold mb-1" style={{ color: '#64748b' }}>Test Feedback</h4>
                  <StarRating value={feedbackRating} onChange={setFeedbackRating} />
                  <textarea
                    value={feedbackText}
                    onChange={e => setFeedbackText(e.target.value)}
                    placeholder="Corrections or comments..."
                    className="w-full rounded-lg p-2 text-[11px] min-h-[40px] resize-y mt-1"
                    style={{ background: '#05050a', color: '#e2e8f0', border: '1px solid #1e1e3a' }}
                  />
                  <button
                    onClick={handleFeedbackSubmit}
                    disabled={feedbackSubmitting || feedbackRating === 0}
                    className="px-3 py-1 rounded-lg text-[11px] font-medium transition disabled:opacity-50 mt-1"
                    style={{ background: feedbackSuccess ? '#10b981' : '#6366f1', color: '#fff' }}
                  >
                    {feedbackSubmitting ? 'Submitting...' : feedbackSuccess ? 'Sent' : 'Submit'}
                  </button>
                </div>
                {aiSuggestions?.length > 0 && (
                  <div className="flex-1">
                    <h4 className="text-[10px] font-semibold mb-1 flex items-center gap-1" style={{ color: '#8b5cf6' }}>
                      <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
                      AI Edge Cases
                    </h4>
                    <div className="space-y-1">
                      {aiSuggestions.map((s, i) => (
                        <div key={i} className="text-[11px] px-2 py-1 rounded"
                             style={{ background: '#05050a', color: '#c4b5fd' }}>
                          {s}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Version History Drawer */}
      {historyOpen && selected && (
        <>
          {/* Backdrop */}
          <div
            className="fixed inset-0 z-40"
            style={{ background: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(4px)' }}
            onClick={() => setHistoryOpen(false)}
          />
          {/* Drawer */}
          <div
            className="fixed top-0 right-0 z-50 h-full overflow-y-auto"
            style={{
              width: 400,
              background: '#0d0d18',
              borderLeft: '1px solid #1e1e3a',
              transition: 'transform 0.3s ease',
            }}
          >
            {/* Drawer Header */}
            <div className="px-5 py-4 flex items-center justify-between" style={{ borderBottom: '1px solid #1e1e3a' }}>
              <div>
                <h3 className="text-sm font-semibold">Version History</h3>
                <span className="text-[10px] font-mono" style={{ color: '#64748b' }}>{selected.test_id}</span>
              </div>
              <button
                onClick={() => setHistoryOpen(false)}
                className="w-7 h-7 flex items-center justify-center rounded-lg transition"
                style={{ background: '#1e1e3a', color: '#94a3b8' }}
              >
                {'\u2715'}
              </button>
            </div>

            {/* Version List */}
            <div className="p-4 space-y-2">
              {testVersions.length === 0 && (
                <p className="text-[11px] text-center py-4" style={{ color: '#64748b' }}>No version history available</p>
              )}
              {testVersions.map(v => (
                <button
                  key={v.version}
                  onClick={() => setSelectedVersion(selectedVersion === v.version ? null : v.version)}
                  className="w-full text-left rounded-lg px-4 py-3 transition"
                  style={{
                    background: selectedVersion === v.version ? '#1a1a2e' : '#12121f',
                    border: `1px solid ${selectedVersion === v.version ? '#6366f1' : '#1e1e3a'}`,
                  }}
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-semibold" style={{ color: '#a5b4fc' }}>
                      v{v.version}
                    </span>
                    <span className="text-[10px] font-mono" style={{ color: '#94a3b8' }}>{v.date}</span>
                  </div>
                  <p className="text-[11px]" style={{ color: '#94a3b8' }}>{v.summary}</p>
                  <span className="text-[10px]" style={{ color: '#64748b' }}>by {v.author}</span>
                </button>
              ))}
            </div>

            {/* Diff Display — real data from API */}
            {selectedVersion && versionDiff && (
              <div className="px-4 pb-4">
                <h4 className="text-xs font-semibold mb-2" style={{ color: '#64748b' }}>
                  Changes in v{selectedVersion}
                </h4>
                <div className="rounded-lg overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
                  <pre
                    className="text-[10px] p-3 overflow-x-auto"
                    style={{ background: '#0a0a14', fontFamily: 'JetBrains Mono, monospace' }}
                  >
                    {versionDiff.diff.split('\n').map((line, i) => {
                      const isAdd = line.startsWith('+')
                      const isDel = line.startsWith('-')
                      return (
                        <div
                          key={i}
                          className="flex"
                          style={{
                            background: isAdd ? 'rgba(16,185,129,0.1)' : isDel ? 'rgba(239,68,68,0.1)' : undefined,
                          }}
                        >
                          <span className="w-8 text-right pr-2 select-none flex-shrink-0" style={{ color: '#94a3b8' }}>
                            {isAdd ? '+' : isDel ? '-' : i + 1}
                          </span>
                          <span style={{ color: isAdd ? '#10b981' : isDel ? '#ef4444' : '#94a3b8' }}>{line}</span>
                        </div>
                      )
                    })}
                  </pre>
                </div>
                <div className="flex gap-3 mt-2 text-[10px]" style={{ color: '#64748b' }}>
                  <span style={{ color: '#10b981' }}>+{versionDiff.additions} additions</span>
                  <span style={{ color: '#ef4444' }}>-{versionDiff.deletions} removals</span>
                </div>
              </div>
            )}
          </div>
        </>
      )}
      </div>{/* Phase J3 — close `viewMode === 'tests'` display wrapper */}

      {/* R15 — pre-run auth refresh modal; opens when useRunGuard pre-flight
          detects an expired SUT cookie. onSuccess() fires the queued
          run set up by handleRunSuite. */}
      <RefreshAuthModal
        open={runGuard.needsRefresh}
        projectId={currentProjectId}
        environment={runEnvironment}
        initialState={runGuard.authState}
        onClose={runGuard.dismissRefresh}
        onSuccess={runGuard.onRefreshSuccess}
      />

      {/* R40.1 — second mount point for the Paste Auth flow that fires
          on the REACTIVE 409/412 path (handleRunSuite catch handler) +
          the passive header banner CTA. The R15 modal above only fires
          when runGuard's pre-flight detects an EXPIRED cookie; the R40
          modal fires when the cookie is MISSING/redacted/never-set.
          Both share the same RefreshAuthModal component but with
          independent open-state so they don't fight each other. */}
      <RefreshAuthModal
        open={showExtraRefreshAuth}
        projectId={currentProjectId}
        environment={runEnvironment}
        initialState={extraAuthState}
        onClose={() => setShowExtraRefreshAuth(false)}
        onSuccess={handleExtraRefreshSuccess}
      />

      {/* WS4 — Execute by Tool (run a single tool, optionally one requirement). */}
      <ExecuteByToolModal
        open={execByToolOpen}
        onClose={() => setExecByToolOpen(false)}
        projectId={currentProjectId || ''}
        requirements={execReqs as any}
        onSubmit={handleExecByTool}
        running={execByToolRunning}
      />
    </div>
  )
}
