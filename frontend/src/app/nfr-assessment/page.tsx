'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import { useSearchParams } from 'next/navigation'
import Link from 'next/link'
import {
  fetchNFRAssessment,
  triggerRun,
  exportComplianceReport,
  getActiveRun,
  fetchDiscoveryStatus,
  type DiscoveryStatus,
} from '@/lib/api-client'
import { fetchAuthState, type AuthState } from '@/lib/auth-state'
import { useProject } from '@/lib/project-context'
import { useTasks } from '@/lib/task-context'
import { useRunGuard } from '@/lib/use-run-guard'
import RefreshAuthModal from '@/components/RefreshAuthModal'
import RunScopeSelector from '@/components/RunScopeSelector'

type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info'
type Status = 'PASS' | 'CONCERNS' | 'FAIL' | 'NOT_ASSESSED'

interface Finding {
  id: string
  description: string
  severity: Severity
}

interface NFRMetric {
  label: string
  value: string
  threshold: string
  pass?: boolean
}

interface NFRCategory {
  name: string
  icon: string
  status: Status
  score?: number
  data_source?: string
  confidence?: string
  metrics: NFRMetric[]
  findings: Finding[]
}

interface TestDetail {
  title: string
  status: string
  duration_ms: number
  error_message: string | null
  tool: string
}

interface DurationDistribution {
  p50: number
  p75: number
  p90: number
  p95: number
  p99: number
  max: number
}

const STATUS_COLOR: Record<Status, string> = {
  PASS: '#34d399',
  CONCERNS: '#fbbf24',
  FAIL: '#fb7185',
  NOT_ASSESSED: '#64748b',
}

const STATUS_BG: Record<Status, string> = {
  PASS: 'rgba(52,211,153,0.12)',
  CONCERNS: 'rgba(251,191,36,0.12)',
  FAIL: 'rgba(251,113,133,0.12)',
  NOT_ASSESSED: 'rgba(100,116,139,0.12)',
}

const SEVERITY_COLOR: Record<Severity, string> = {
  critical: '#fb7185',
  high: '#f97316',
  medium: '#fbbf24',
  low: '#60a5fa',
  info: '#94a3b8',
}

const TEST_STATUS_COLOR: Record<string, string> = {
  PASS: '#34d399',
  PASSED: '#34d399',
  FAIL: '#fb7185',
  FAILED: '#fb7185',
  ERROR: '#fb7185',
  FLAKY: '#fbbf24',
  SKIP: '#64748b',
  SKIPPED: '#64748b',
}

const CONFIDENCE_LABEL: Record<string, { text: string; color: string }> = {
  high: { text: 'Measured', color: '#34d399' },
  medium: { text: 'Partial data', color: '#fbbf24' },
  none: { text: 'Not tested', color: '#64748b' },
}

const BUILDS = [
  { id: 'build-487', label: 'Build #487' },
  { id: 'build-486', label: 'Build #486' },
  { id: 'build-485', label: 'Build #485' },
]

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

export default function NFRAssessmentPage() {
  const { currentProjectId, isLoading: projectLoading } = useProject()
  const sp = useSearchParams()
  // R68.5 — run_id from URL. RunScopeSelector auto-selects latest run
  // on mount when URL has no run_id param; threads URL on switch.
  const runIdFromUrl = sp?.get('run_id') || ''
  const [runId, setRunId] = useState<string>(runIdFromUrl)
  useEffect(() => { setRunId(runIdFromUrl) }, [runIdFromUrl])
  const { addTask, updateTask, completeTask, failTask, activeTasks } = useTasks()
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [data, setData] = useState<NFRCategory[] | null>(null)
  const [needsExecution, setNeedsExecution] = useState(false)
  const [dataSource, setDataSource] = useState<string | null>(null)
  const [selectedBuild, setSelectedBuild] = useState(BUILDS[0].id)
  const [toast, setToast] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [nfrEnvironment, setNfrEnvironment] = useState('local')
  const [exporting, setExporting] = useState(false)

  // R15 — pre-run auth-state gate.
  const runGuard = useRunGuard(currentProjectId, nfrEnvironment)
  const [detailExpanded, setDetailExpanded] = useState(true)

  // R40.1 — auth-state diagnostic for the NFR-page header banner +
  // reactive 409/412 handling. Mirrors test-explorer; same component
  // re-mount pattern so both pages produce identical operator UX.
  const [discoveryStatus, setDiscoveryStatus] = useState<DiscoveryStatus | null>(null)
  const [extraAuthState, setExtraAuthState] = useState<AuthState | null>(null)
  const [showExtraRefreshAuth, setShowExtraRefreshAuth] = useState(false)

  useEffect(() => {
    if (!currentProjectId) {
      setDiscoveryStatus(null)
      return
    }
    let cancelled = false
    fetchDiscoveryStatus(currentProjectId, nfrEnvironment)
      .then(s => { if (!cancelled) setDiscoveryStatus(s) })
      .catch(() => { if (!cancelled) setDiscoveryStatus(null) })
    fetchAuthState(currentProjectId, nfrEnvironment)
      .then(s => { if (!cancelled) setExtraAuthState(s) })
      .catch(() => { if (!cancelled) setExtraAuthState(null) })
    return () => { cancelled = true }
  }, [currentProjectId, nfrEnvironment])

  async function handleExtraRefreshSuccess() {
    setShowExtraRefreshAuth(false)
    if (!currentProjectId) return
    try {
      const fresh = await fetchDiscoveryStatus(currentProjectId, nfrEnvironment)
      setDiscoveryStatus(fresh)
    } catch { /* silent */ }
  }

  // Detailed test data from API
  const [testDetails, setTestDetails] = useState<TestDetail[]>([])
  const [slowestTests, setSlowestTests] = useState<TestDetail[]>([])
  const [failedTests, setFailedTests] = useState<TestDetail[]>([])
  const [durationDist, setDurationDist] = useState<DurationDistribution | null>(null)
  const [totalTests, setTotalTests] = useState(0)
  const [passedCount, setPassedCount] = useState(0)
  const [failedCount, setFailedCount] = useState(0)
  const [toolBreakdown, setToolBreakdown] = useState<Record<string, { total: number; passed: number; failed: number; skipped: number; details: TestDetail[] }>>({})
  const [selectedToolTab, setSelectedToolTab] = useState<string>('all')

  // Refs for cleanup
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Cleanup intervals/timers on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
    }
  }, [])

  const showToast = useCallback((msg: string) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
    setToast(msg)
    toastTimerRef.current = setTimeout(() => setToast(null), 3000)
  }, [])

  const applyNFRResult = (result: any) => {
    if (result?.categories) {
      setData(result.categories)
      setNeedsExecution(false)
      setDataSource(result.source || 'execution')
      setTestDetails(result.test_details || [])
      setSlowestTests(result.slowest_tests || [])
      setFailedTests(result.failed_tests || [])
      setDurationDist(result.duration_distribution || null)
      setTotalTests(result.total_tests || 0)
      setPassedCount(result.passed || 0)
      setFailedCount(result.failed || 0)
      setToolBreakdown(result.tool_breakdown || {})
      setSelectedToolTab('all')
    }
  }

  const loadNFRData = async () => {
    try {
      const result = await fetchNFRAssessment(selectedBuild, currentProjectId || undefined, runId || undefined)
      if (result?.needs_execution) {
        setData(null)
        setNeedsExecution(true)
        setDataSource(null)
      } else {
        applyNFRResult(result)
      }
    } catch {
      setData(null)
      setNeedsExecution(true)
      setDataSource(null)
    }
  }

  // Load data from API. R68.5 — re-fetch when runId changes too.
  useEffect(() => {
    if (projectLoading) return
    loadNFRData()
  }, [selectedBuild, currentProjectId, projectLoading, runId])

  const toggle = (name: string) => {
    setExpanded(prev => ({ ...prev, [name]: !prev[name] }))
  }

  const isRunning = activeTasks.some(t => t.type === 'test_execution')

  const handleRunScan = async () => {
    setScanning(true)
    const taskId = `nfr-scan-${Date.now()}`
    addTask({ id: taskId, type: 'test_execution', label: 'Running NFR test suite', result_url: '/nfr-assessment' })

    try {
      // R15 — pre-flight auth check; opens RefreshAuthModal when expired
      // and queues this run for after operator paste.
      const capturedRef: { current: { run_id: string; status: string } | null } = { current: null }
      await runGuard.runOrPromptRefresh(async () => {
        capturedRef.current = await triggerRun({ suite_type: 'full', environment: nfrEnvironment, project_id: currentProjectId || undefined })
      })
      if (!capturedRef.current) {
        // Run was queued — modal will fire it after paste. Reset UI state.
        setScanning(false)
        return
      }
      const result = capturedRef.current
      showToast(`Tests started: ${result.run_id}`)
      updateTask(taskId, { detail: `Run ${result.run_id} in progress...`, progress: 5 })

      // Poll for completion — no hard timeout, keep going while run is active
      let attempts = 0
      const stopPoll = () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null } }
      pollRef.current = setInterval(async () => {
        attempts++
        try {
          const nfr = await fetchNFRAssessment(selectedBuild, currentProjectId || undefined, runId || undefined)

          if (nfr?.categories && nfr?.source === 'execution') {
            stopPoll()
            applyNFRResult(nfr)
            setScanning(false)
            completeTask(taskId, { detail: `${nfr.total_tests || 0} tests executed`, result_url: '/nfr-assessment' })
            return
          }

          if (nfr?.running) {
            const elapsed = attempts * 5
            const pct = Math.min(85, 5 + Math.floor(elapsed / 3))
            updateTask(taskId, { progress: pct, detail: `Tests executing... (${elapsed}s elapsed)` })
            return
          }

          if (nfr?.failed) {
            stopPoll()
            setScanning(false)
            setNeedsExecution(true)
            failTask(taskId, nfr.error || 'Test execution failed')
            showToast(nfr.error || 'Test execution failed. Check if the target application is running')
            return
          }

          if (attempts > 120) {
            stopPoll()
            setScanning(false)
            failTask(taskId, 'Timed out after 10 minutes')
          }
        } catch {
          if (attempts > 120) {
            stopPoll()
            setScanning(false)
            failTask(taskId, 'Lost connection to server')
          }
        }
      }, 5000)
    } catch (err: any) {
      // R38.1 — surface the structured 409 config_incomplete payload so
      // operators can SEE which env vars need filling.
      const detail = err?.detail
      let message = 'Failed to start test run'
      if (detail && detail.error === 'config_incomplete') {
        const sample = (detail.unfilled_vars || []).slice(0, 3).join(', ')
        const more = (detail.unfilled_count || 0) > 3
          ? ` + ${detail.unfilled_count - 3} more` : ''
        // R45.5 — deep-link with `?highlight=` so the editor pre-selects
        // the unfilled vars instead of forcing the operator to hunt.
        const highlightQs = (detail.unfilled_vars || []).length > 0
          ? `&highlight=${encodeURIComponent((detail.unfilled_vars || []).join(','))}`
          : ''
        message =
          `Refusing to run: ${detail.unfilled_count} env var(s) need values ` +
          `(${sample}${more}). Paste a fresh cookie below, or open ` +
          `Settings → Environments → Variables ` +
          `(/settings#environments?env=${encodeURIComponent(nfrEnvironment)}${highlightQs}) ` +
          `to fill manually.`
        // R40.1 — when 409 fires, fetch discovery-status + auto-open
        // RefreshAuthModal inline so the operator can paste a fresh
        // cookie without leaving this page.
        if (currentProjectId) {
          try {
            const s = await fetchDiscoveryStatus(currentProjectId, nfrEnvironment)
            setDiscoveryStatus(s)
            if (!s.auth_state_present) {
              setShowExtraRefreshAuth(true)
            }
          } catch { /* silent */ }
        }
      } else if (detail && detail.error === 'auth_state_missing') {
        message = 'Run Anyway blocked. Paste a fresh cookie first.'
        setShowExtraRefreshAuth(true)
      } else if (typeof err?.message === 'string' && err.message) {
        message = `Failed to start test run: ${err.message}`
      }
      showToast(message)
      setScanning(false)
      failTask(taskId, message)
    }
  }

  // Resume polling if NFR test run is active (survives page navigation)
  useEffect(() => {
    if (!currentProjectId || scanning) return
    let cancelled = false
    getActiveRun(currentProjectId).then(async (run) => {
      if (cancelled || run.status !== 'running') return
      setScanning(true)
      while (!cancelled) {
        await new Promise(r => setTimeout(r, 5000))
        try {
          const nfr = await fetchNFRAssessment(selectedBuild, currentProjectId || undefined, runId || undefined)
          if (cancelled) break
          if (nfr?.categories && nfr?.source === 'execution') {
            applyNFRResult(nfr)
            break
          }
          if (nfr?.failed) break
        } catch { break }
      }
      if (!cancelled) setScanning(false)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [currentProjectId])

  const handleExport = async () => {
    setExporting(true)
    try {
      await exportComplianceReport('pdf')
      showToast('PDF report exported')
    } catch {
      showToast('Export queued \u2014 report will be available shortly')
    } finally {
      setExporting(false)
    }
  }

  // Summary counts including NOT_ASSESSED
  const statusCounts = data ? {
    PASS: data.filter(c => c.status === 'PASS').length,
    CONCERNS: data.filter(c => c.status === 'CONCERNS').length,
    FAIL: data.filter(c => c.status === 'FAIL').length,
    NOT_ASSESSED: data.filter(c => c.status === 'NOT_ASSESSED').length,
  } : null

  return (
    <div className="min-h-screen" style={{ background: '#05050a', color: '#e2e8f0' }}>
      {/* Toast */}
      {toast && (
        <div className="fixed top-4 right-4 z-50 px-4 py-3 rounded-xl text-sm font-medium shadow-lg animate-in"
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
        <span style={{ color: '#94a3b8' }} className="text-sm">NFR Assessment</span>
        <div className="flex-1" />

        {/* Build selector */}
        <select
          value={selectedBuild}
          onChange={e => setSelectedBuild(e.target.value)}
          className="text-xs px-3 py-1.5 rounded-lg cursor-pointer"
          style={{ background: '#1e1e3a', color: '#c7d2fe', border: '1px solid #2d2d4a' }}
        >
          {BUILDS.map(b => (
            <option key={b.id} value={b.id}>{b.label}</option>
          ))}
        </select>

        {/* Environment + Run NFR Scan */}
        <select
          value={nfrEnvironment}
          onChange={e => setNfrEnvironment(e.target.value)}
          className="text-xs px-2 py-1.5 rounded-lg outline-none cursor-pointer"
          style={{ background: '#0a0a14', color: '#94a3b8', border: '1px solid #1e1e3a', fontFamily: "'Space Mono', monospace" }}
        >
          <option value="local">local</option>
          <option value="staging">staging</option>
          <option value="production">production</option>
        </select>
        <button
          onClick={handleRunScan}
          disabled={scanning}
          className="text-xs px-4 py-1.5 rounded-lg font-medium transition-colors disabled:opacity-50"
          style={{ background: '#6366f1', color: '#fff' }}
        >
          {scanning ? 'Scanning...' : 'Run NFR Scan'}
        </button>

        {/* Export PDF */}
        <button
          onClick={handleExport}
          disabled={exporting}
          className="text-xs px-3 py-1.5 rounded-lg font-medium transition-colors disabled:opacity-50"
          style={{ background: '#1e1e3a', color: '#94a3b8', border: '1px solid #2d2d4a' }}
        >
          {exporting ? 'Exporting...' : 'Export PDF'}
        </button>
      </header>

      <main className="px-8 py-8 max-w-7xl mx-auto">
        {/* Page title */}
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-1">
            <h1 className="text-2xl font-bold text-white">Non-Functional Requirements Assessment</h1>
            {dataSource === 'execution' && (
              <span className="text-[10px] px-2 py-0.5 rounded-full" style={{ background: '#10b98120', color: '#10b981', border: '1px solid #10b98130' }}>
                Source: Latest Run
              </span>
            )}
          </div>
          <p className="text-sm" style={{ color: '#64748b' }}>
            BMAD TEA Layer 8: the NFR-gate detail (perf · security · a11y · reliability · compatibility).{' '}
            For the overall SUT-quality verdict see{' '}
            <a href="/sut-quality" style={{ color: '#a5b4fc' }}>SUT Quality →</a>.
          </p>
        </div>

        {/* R68.5 — run-scope selector. allowAllRuns=false because NFR
            doesn't aggregate cleanly across runs (each run produces an
            atomic per-run snapshot); operator MUST pick one. Defaults
            to latest completed run via autoSelectLatest=true. */}
        <RunScopeSelector
          runId={runId}
          onRunChange={setRunId}
          projectId={currentProjectId || undefined}
          allowAllRuns={false}
          autoSelectLatest={true}
        />

        {/* R40.4 — passive auth-state header card. NFR scans hit
            Playwright/Newman/k6/ZAP — all need authenticated SUT
            access. R43: broadened trigger to ALSO fire when env vars
            are unfilled (regardless of auth-state file presence) since
            the gate 409s on either condition. */}
        {discoveryStatus && (
          !discoveryStatus.auth_state_present
          || discoveryStatus.unfilled_count > 5
        ) && (
          <div className="mb-6 rounded-lg px-4 py-3 flex items-center justify-between"
               style={{ background: 'rgba(251,113,133,0.10)',
                        border: '1px solid rgba(251,113,133,0.40)' }}>
            <div className="flex items-center gap-3">
              <span style={{ color: '#fb7185', fontSize: 18 }}>⛔</span>
              <div>
                <div className="text-sm font-semibold" style={{ color: '#fb7185' }}>
                  No fresh SUT auth: NFR scan will refuse
                </div>
                <div className="text-[11px] mt-0.5" style={{ color: '#94a3b8' }}>
                  Discovery cannot authenticate against the SUT, so{' '}
                  {discoveryStatus.unfilled_count} env vars stay placeholder.
                  {' '}Paste a fresh cookie to unblock.
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

        {/* Scanning progress */}
        {isRunning && (
          <div className="mb-6 rounded-xl p-4 flex items-center gap-4" style={{ background: '#6366f110', border: '1px solid #6366f130' }}>
            <span className="inline-block w-3 h-3 rounded-full animate-pulse" style={{ background: '#6366f1' }} />
            <div className="flex-1">
              <p className="text-sm font-medium" style={{ color: '#c7d2fe' }}>Test execution in progress...</p>
              <p className="text-xs mt-0.5" style={{ color: '#64748b' }}>Results will appear here when complete. You can navigate away. Check the progress bar or notification bell.</p>
            </div>
          </div>
        )}

        {/* Empty state — no execution data */}
        {needsExecution && !isRunning && (
          <div className="rounded-xl p-12 text-center mb-8" style={{ background: '#12121f', border: '1px dashed #2d2d4a' }}>
            <div className="text-4xl mb-4">{'\u{1F4CA}'}</div>
            <h2 className="text-lg font-semibold text-white mb-2">No NFR Data Yet</h2>
            <p className="text-sm mb-6 max-w-md mx-auto" style={{ color: '#64748b' }}>
              Run your test suite to generate a real NFR assessment. The platform will analyze test performance, reliability,
              security implications, and maintainability from actual execution results.
            </p>
            <div className="flex justify-center gap-3 mb-6">
              {['Performance', 'Security', 'Accessibility', 'Reliability', 'Maintainability'].map(cat => (
                <span key={cat} className="text-[10px] px-2 py-1 rounded" style={{ background: '#1e1e3a', color: '#94a3b8', border: '1px solid #2d2d4a' }}>
                  {cat}
                </span>
              ))}
            </div>
            <div className="flex items-center justify-center gap-3">
              <button onClick={handleRunScan} disabled={scanning}
                      className="px-6 py-2.5 rounded-lg font-medium text-sm transition-all disabled:opacity-50"
                      style={{ background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff' }}>
                {scanning ? 'Starting...' : 'Run NFR Scan'}
              </button>
              <Link href="/run-history"
                    className="px-4 py-2.5 rounded-lg text-sm font-medium transition-colors no-underline"
                    style={{ background: '#1e1e3a', color: '#94a3b8', border: '1px solid #2d2d4a' }}>
                View Run History
              </Link>
            </div>
          </div>
        )}

        {/* Summary strip — only when data exists */}
        {data && statusCounts && (() => {
          const visible = (['PASS', 'CONCERNS', 'FAIL', 'NOT_ASSESSED'] as Status[]).filter(
            s => s !== 'NOT_ASSESSED' || statusCounts[s] > 0,
          )
          return <div className="grid gap-4 mb-8" style={{ gridTemplateColumns: `repeat(${visible.length}, minmax(0, 1fr))` }}>
          {visible.map(s => {
            const count = statusCounts[s]
            const label = s === 'NOT_ASSESSED' ? 'NOT ASSESSED' : s
            return (
              <div key={s} className="rounded-xl p-4 flex items-center gap-4"
                   style={{ background: '#12121f', border: `1px solid ${STATUS_COLOR[s]}33` }}>
                <span className="text-3xl font-bold" style={{ color: STATUS_COLOR[s], fontFamily: "'Space Mono', monospace" }}>
                  {count}
                </span>
                <div>
                  <p className="font-semibold text-sm" style={{ color: STATUS_COLOR[s] }}>{label}</p>
                  <p className="text-xs" style={{ color: '#94a3b8' }}>
                    {count === 1 ? '1 category' : `${count} categories`}
                  </p>
                </div>
              </div>
            )
          })}
        </div>
          })()}

        {/* NFR Cards */}
        {data && <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3 mb-8">
          {data.map(cat => {
            const isExpanded = expanded[cat.name] || false
            const confidence = cat.confidence ? CONFIDENCE_LABEL[cat.confidence] : null
            return (
              <div key={cat.name} className="rounded-xl overflow-hidden flex flex-col"
                   style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                {/* Card header */}
                <div className="px-5 py-4 flex items-center justify-between"
                     style={{ borderBottom: '1px solid #1e1e3a' }}>
                  <div className="flex items-center gap-3">
                    <span className="text-xl">{cat.icon}</span>
                    <div>
                      <span className="font-semibold text-white">{cat.name}</span>
                      {confidence && (
                        <span className="ml-2 text-[9px] px-1.5 py-0.5 rounded"
                              style={{ background: `${confidence.color}15`, color: confidence.color, border: `1px solid ${confidence.color}30` }}>
                          {confidence.text}
                        </span>
                      )}
                    </div>
                  </div>
                  <span className="text-xs font-bold px-2.5 py-1 rounded-full"
                        style={{ background: STATUS_BG[cat.status], color: STATUS_COLOR[cat.status] }}>
                    {cat.status === 'NOT_ASSESSED' ? 'N/A' : cat.status}
                  </span>
                </div>

                {/* Metrics */}
                <div className="px-5 py-4 space-y-3 flex-1">
                  {cat.metrics.map(m => (
                    <div key={m.label} className="flex items-center justify-between">
                      <span className="text-xs" style={{ color: '#64748b' }}>{m.label}</span>
                      <div className="flex items-center gap-2">
                        {typeof m.pass === 'boolean' && (
                          <span className="text-[10px]">{m.pass ? '\u2705' : '\u274C'}</span>
                        )}
                        <div className="text-right">
                          <span className="text-sm font-bold" style={{ color: '#e2e8f0', fontFamily: "'Space Mono', monospace" }}>
                            {m.value}
                          </span>
                          <span className="text-[10px] ml-2" style={{ color: '#94a3b8' }}>
                            (threshold: {m.threshold})
                          </span>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Score bar (when available) */}
                {typeof cat.score === 'number' && cat.status !== 'NOT_ASSESSED' && (
                  <div className="px-5 pb-3">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[10px]" style={{ color: '#94a3b8' }}>Score</span>
                      <span className="text-[10px] font-bold" style={{ color: STATUS_COLOR[cat.status], fontFamily: "'Space Mono', monospace" }}>
                        {cat.score}/100
                      </span>
                    </div>
                    <div className="w-full h-1.5 rounded-full" style={{ background: '#1e1e3a' }}>
                      <div className="h-full rounded-full transition-all" style={{
                        width: `${Math.max(2, cat.score)}%`,
                        background: STATUS_COLOR[cat.status],
                      }} />
                    </div>
                  </div>
                )}

                {/* Expandable findings */}
                <div style={{ borderTop: '1px solid #1e1e3a' }}>
                  <button
                    onClick={() => toggle(cat.name)}
                    className="w-full px-5 py-3 flex items-center justify-between text-xs transition-colors hover:bg-white/[0.03]"
                    style={{ color: '#6366f1' }}
                  >
                    <span>{cat.findings.length} finding{cat.findings.length !== 1 ? 's' : ''}</span>
                    <span style={{ fontSize: 10 }}>{isExpanded ? '\u25B2' : '\u25BC'}</span>
                  </button>

                  {isExpanded && (
                    <div className="px-5 pb-4 space-y-2">
                      {cat.findings.map(f => (
                        <div key={f.id} className="flex items-start gap-2 px-3 py-2 rounded-lg"
                             style={{ background: '#0d0d1a' }}>
                          <span className="text-[10px] font-bold px-1.5 py-0.5 rounded flex-shrink-0 uppercase mt-0.5"
                                style={{
                                  background: `${SEVERITY_COLOR[f.severity]}18`,
                                  color: SEVERITY_COLOR[f.severity],
                                }}>
                            {f.severity}
                          </span>
                          <span className="text-xs leading-relaxed" style={{ color: '#94a3b8' }}>
                            {f.description}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>}

        {/* ══════════════════════════════════════════════════════════════════════
            DETAILED TEST RESULTS SECTION
           ══════════════════════════════════════════════════════════════════════ */}
        {data && testDetails.length > 0 && (
          <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            {/* Section header */}
            <button
              onClick={() => setDetailExpanded(!detailExpanded)}
              className="w-full px-6 py-4 flex items-center justify-between hover:bg-white/[0.02] transition-colors"
              style={{ borderBottom: detailExpanded ? '1px solid #1e1e3a' : 'none' }}
            >
              <div className="flex items-center gap-3">
                <span className="text-lg">{'\u{1F4CB}'}</span>
                <span className="font-semibold text-white">Test Results Detail</span>
                <span className="text-xs px-2 py-0.5 rounded-full" style={{ background: '#6366f115', color: '#6366f1', border: '1px solid #6366f130' }}>
                  {totalTests} tests
                </span>
                <span className="text-xs" style={{ color: '#34d399' }}>{passedCount} passed</span>
                {failedCount > 0 && <span className="text-xs" style={{ color: '#fb7185' }}>{failedCount} failed</span>}
              </div>
              <span className="text-xs" style={{ color: '#6366f1' }}>{detailExpanded ? '\u25B2' : '\u25BC'}</span>
            </button>

            {detailExpanded && (
              <div className="p-6 space-y-6">
                {/* Per-tool tabs */}
                {Object.keys(toolBreakdown).length > 0 && (
                  <div className="flex items-center gap-2 flex-wrap">
                    <button
                      onClick={() => setSelectedToolTab('all')}
                      className="text-xs px-3 py-1.5 rounded-lg font-medium transition-colors"
                      style={{
                        background: selectedToolTab === 'all' ? '#6366f1' : '#1e1e3a',
                        color: selectedToolTab === 'all' ? '#fff' : '#94a3b8',
                        border: '1px solid #2d2d4a',
                      }}
                    >
                      All ({totalTests})
                    </button>
                    {Object.entries(toolBreakdown).map(([tool, stats]: [string, { total: number; passed: number; failed: number; skipped: number; details: TestDetail[] }]) => {
                      const toolLabels: Record<string, string> = {
                        playwright: 'E2E UI', newman: 'API', k6: 'Performance', zap: 'Security', pytest: 'Analytics',
                      }
                      const toolColors: Record<string, string> = {
                        playwright: '#6366f1', newman: '#06b6d4', k6: '#f59e0b', zap: '#fb7185', pytest: '#8b5cf6',
                      }
                      return (
                        <button
                          key={tool}
                          onClick={() => setSelectedToolTab(tool)}
                          className="text-xs px-3 py-1.5 rounded-lg font-medium transition-colors flex items-center gap-1.5"
                          style={{
                            background: selectedToolTab === tool ? (toolColors[tool] || '#6366f1') : '#1e1e3a',
                            color: selectedToolTab === tool ? '#fff' : '#94a3b8',
                            border: `1px solid ${selectedToolTab === tool ? (toolColors[tool] || '#6366f1') : '#2d2d4a'}`,
                          }}
                        >
                          {toolLabels[tool] || tool} ({stats.total})
                          {stats.failed > 0 && <span style={{ color: selectedToolTab === tool ? '#fecdd3' : '#fb7185' }}>{stats.failed}F</span>}
                        </button>
                      )
                    })}
                  </div>
                )}

                {/* Duration Distribution */}
                {durationDist && (
                  <div>
                    <h3 className="text-sm font-semibold text-white mb-3">Duration Distribution</h3>
                    <div className="rounded-lg p-4" style={{ background: '#0d0d1a', border: '1px solid #1e1e3a' }}>
                      <div className="flex items-end gap-1" style={{ height: 60 }}>
                        {[
                          { label: 'p50', value: durationDist.p50, color: '#34d399' },
                          { label: 'p75', value: durationDist.p75, color: '#06b6d4' },
                          { label: 'p90', value: durationDist.p90, color: '#6366f1' },
                          { label: 'p95', value: durationDist.p95, color: '#fbbf24' },
                          { label: 'p99', value: durationDist.p99, color: '#f97316' },
                          { label: 'max', value: durationDist.max, color: '#fb7185' },
                        ].map(bar => {
                          const maxVal = durationDist.max || 1
                          const height = Math.max(4, (bar.value / maxVal) * 48)
                          return (
                            <div key={bar.label} className="flex-1 flex flex-col items-center gap-1">
                              <span className="text-[9px] font-mono" style={{ color: bar.color }}>
                                {formatDuration(bar.value)}
                              </span>
                              <div className="w-full rounded-t" style={{ height, background: bar.color, opacity: 0.7 }} />
                              <span className="text-[9px]" style={{ color: '#94a3b8' }}>{bar.label}</span>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                )}

                {/* Failed Tests */}
                {failedTests.length > 0 && (
                  <div>
                    <h3 className="text-sm font-semibold mb-3" style={{ color: '#fb7185' }}>
                      Failed Tests ({failedTests.length})
                    </h3>
                    <div className="space-y-2">
                      {failedTests.map((t, i) => (
                        <div key={`fail-${t.title}-${i}`} className="rounded-lg p-3" style={{ background: '#fb718508', border: '1px solid #fb718520' }}>
                          <div className="flex items-center justify-between mb-1">
                            <span className="text-xs font-medium text-white">{t.title}</span>
                            <div className="flex items-center gap-2">
                              <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: '#1e1e3a', color: '#94a3b8' }}>
                                {t.tool}
                              </span>
                              <span className="text-[10px] font-mono" style={{ color: '#64748b' }}>
                                {formatDuration(t.duration_ms)}
                              </span>
                            </div>
                          </div>
                          {t.error_message && (
                            <pre className="text-[10px] mt-1 p-2 rounded overflow-x-auto" style={{ background: '#0d0d1a', color: '#fb7185', fontFamily: "'JetBrains Mono', monospace" }}>
                              {t.error_message}
                            </pre>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Slowest Tests */}
                {slowestTests.length > 0 && (
                  <div>
                    <h3 className="text-sm font-semibold text-white mb-3">
                      Slowest Tests (Top 5)
                    </h3>
                    <div className="space-y-1">
                      {slowestTests.map((t, i) => {
                        const maxDuration = slowestTests[0]?.duration_ms || 1
                        const barWidth = Math.max(5, (t.duration_ms / maxDuration) * 100)
                        return (
                          <div key={`slow-${t.title}-${i}`} className="flex items-center gap-3 py-1.5">
                            <span className="text-[10px] w-4 text-right font-mono" style={{ color: '#94a3b8' }}>{i + 1}</span>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2 mb-0.5">
                                <span className="text-xs truncate" style={{ color: '#cbd5e1' }}>{t.title}</span>
                                <span className="text-[9px] px-1 py-0.5 rounded flex-shrink-0"
                                      style={{ background: `${TEST_STATUS_COLOR[t.status] || '#64748b'}18`, color: TEST_STATUS_COLOR[t.status] || '#64748b' }}>
                                  {t.status}
                                </span>
                              </div>
                              <div className="w-full h-1 rounded-full" style={{ background: '#1e1e3a' }}>
                                <div className="h-full rounded-full" style={{
                                  width: `${barWidth}%`,
                                  background: t.duration_ms > 5000 ? '#fb7185' : t.duration_ms > 2000 ? '#fbbf24' : '#34d399',
                                }} />
                              </div>
                            </div>
                            <span className="text-[10px] font-mono flex-shrink-0" style={{ color: '#94a3b8' }}>
                              {formatDuration(t.duration_ms)}
                            </span>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )}

                {/* All Test Results Table (filtered by selected tool tab) */}
                <div>
                  <h3 className="text-sm font-semibold text-white mb-3">
                    {selectedToolTab === 'all' ? 'All Test Results' : `${selectedToolTab.charAt(0).toUpperCase() + selectedToolTab.slice(1)} Test Results`}
                  </h3>
                  <div className="rounded-lg overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
                    {/* Table header */}
                    <div className="grid grid-cols-[1fr_80px_90px_70px] gap-2 px-4 py-2 text-[10px] font-semibold uppercase"
                         style={{ background: '#0d0d1a', color: '#94a3b8', borderBottom: '1px solid #1e1e3a' }}>
                      <span>Test Name</span>
                      <span className="text-center">Status</span>
                      <span className="text-right">Duration</span>
                      <span className="text-center">Tool</span>
                    </div>
                    {/* Table rows */}
                    <div style={{ maxHeight: 400, overflowY: 'auto' }}>
                      {testDetails.filter(t => selectedToolTab === 'all' || t.tool === selectedToolTab).map((t, i) => {
                        // R28.7 / R29.6 — when test_id OR script_path is
                        // present, wrap the row in a Link to Test Explorer
                        // so operators can jump to the test source. R29.6
                        // adds `script_path` as a fallback URL param so
                        // pytest nodeids (`foo.py::test_bar`) resolve via
                        // filename match when their test_id doesn't appear
                        // in the test_cases table.
                        const _tid = (t as any).test_id
                        const _sp = (t as any).script_path
                        const _qs: string[] = []
                        if (_tid) _qs.push(`test_id=${encodeURIComponent(_tid)}`)
                        if (_sp) _qs.push(`script_path=${encodeURIComponent(_sp)}`)
                        const RowEl: any = (_tid || _sp) ? Link : 'div'
                        const rowProps: any = (_tid || _sp)
                          ? { href: `/test-explorer?${_qs.join('&')}` }
                          : {}
                        return (
                        <RowEl key={`td-${t.title}-${t.tool}-${i}`}
                             {...rowProps}
                             className="grid grid-cols-[1fr_80px_90px_70px] gap-2 px-4 py-2 items-center hover:bg-white/[0.04] transition-colors"
                             style={{
                               borderBottom: '1px solid #1e1e3a08',
                               background: i % 2 === 0 ? 'transparent' : '#0d0d1a08',
                               cursor: (t as any).test_id ? 'pointer' : 'default',
                             }}
                             title={(t as any).test_id ? `Open ${(t as any).test_id} in Test Explorer` : t.title}
                        >
                          <span className="text-xs truncate" style={{ color: '#cbd5e1' }}>
                            {t.title}
                          </span>
                          <span className="text-center">
                            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded"
                                  style={{
                                    background: `${TEST_STATUS_COLOR[t.status] || '#64748b'}15`,
                                    color: TEST_STATUS_COLOR[t.status] || '#64748b',
                                  }}>
                              {t.status}
                            </span>
                          </span>
                          <span className="text-[10px] text-right font-mono" style={{ color: '#94a3b8' }}>
                            {formatDuration(t.duration_ms)}
                          </span>
                          <span className="text-[10px] text-center" style={{ color: '#64748b' }}>
                            {t.tool}
                          </span>
                        </RowEl>
                        )
                      })}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </main>

      {/* R15 — pre-run auth refresh modal */}
      <RefreshAuthModal
        open={runGuard.needsRefresh}
        projectId={currentProjectId}
        environment={nfrEnvironment}
        initialState={runGuard.authState}
        onClose={runGuard.dismissRefresh}
        onSuccess={runGuard.onRefreshSuccess}
      />

      {/* R40.1 — second modal mount for the missing-cookie flow.
          R15 fires when cookie is EXPIRED; R40 fires when cookie is
          MISSING/redacted/never-set (the actual operator state per
          the live audit). Same component, independent open-state. */}
      <RefreshAuthModal
        open={showExtraRefreshAuth}
        projectId={currentProjectId}
        environment={nfrEnvironment}
        initialState={extraAuthState}
        onClose={() => setShowExtraRefreshAuth(false)}
        onSuccess={handleExtraRefreshSuccess}
      />
    </div>
  )
}
