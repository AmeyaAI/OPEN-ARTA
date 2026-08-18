'use client'

import { useEffect, useState, useRef, useMemo } from 'react'
import {
  fetchRequirements,
  correctRequirement,
  fetchRequirementChanges,
  fetchTests,
  previewUpload,
  resyncJira,
  generateTests,
  generateAllTests,
  streamGenerateAllStatus,
  getActiveGenerateJob,
  getLastCompletedJob,
  retryFailedTests,
  regenerateTests,
  regenerateByTool,
  executeByTool,
  discoverRequirements,
  requestHeal,
  fetchInFlightGenerations,
  bulkAddEnvironmentVariables,
  computeToolInventoryGaps,
  type Requirement,
  type RequirementChange,
  type TestCase,
  type GenerateAllJob,
  type InFlightGeneration,
  type ExpectedTool,
} from '@/lib/api-client'
import { useToast } from '@/components/ui/Toast'
import GherkinHighlighter from '@/components/GherkinHighlighter'
import ATDDSplitView from '@/components/ATDDSplitView'
import { useProject } from '@/lib/project-context'
import { useTasks } from '@/lib/task-context'
import GenerateAllResultsModal from '@/components/GenerateAllResultsModal'
import RegenerateByToolModal from '@/components/RegenerateByToolModal'
import ExecuteByToolModal from '@/components/ExecuteByToolModal'

/* ── Colour & Style Constants ─────────────────────────────────────────────── */

const PRIORITY_COLORS: Record<string, string> = {
  P0: '#fb7185', P1: '#f59e0b', P2: '#60a5fa', P3: '#94a3b8',
}

const SOURCE_META: Record<string, { icon: string; label: string; color: string }> = {
  document:   { icon: '\u{1F4C4}', label: 'Document',   color: '#8b5cf6' },
  jira:       { icon: '\u{1F517}', label: 'Jira',       color: '#06b6d4' },
  manual:     { icon: '\u270F\uFE0F', label: 'Manual',     color: '#10b981' },
  confluence: { icon: '\u{1F4CB}', label: 'Confluence', color: '#a78bfa' },
}

const CHANGE_DOT: Record<string, { color: string; label: string }> = {
  unchanged: { color: '#10b981', label: 'Unchanged' },
  modified:  { color: '#f59e0b', label: 'Modified' },
  new:       { color: '#60a5fa', label: 'New' },
}

const TOOL_COLORS: Record<string, string> = {
  playwright: '#06b6d4',
  newman:     '#10b981',
  k6:         '#8b5cf6',
  zap:        '#ef4444',
}

const TRIGGER_ICONS: Record<string, string> = {
  document_upload: '\u{1F4C4}',
  jira_sync:       '\u{1F517}',
  manual:          '\u270F\uFE0F',
  ai_correction:   '\u{1F916}',
}

/* ── Mock Data ────────────────────────────────────────────────────────────── */


/* ── Helpers ──────────────────────────────────────────────────────────────── */

function getSource(req: Requirement): string {
  const src = (req as any).source ?? (req as any).source_type
  return src && SOURCE_META[src] ? src : 'document'
}

function getChangeStatus(req: Requirement): string {
  const s = (req as any).change_status
  return s && CHANGE_DOT[s] ? s : 'unchanged'
}

function getCoverage(req: Requirement, tests?: any[]): number {
  // If tests are available, calculate coverage dynamically
  if (tests && tests.length > 0) {
    const reqId = req.req_id || req.id
    const matchingTests = tests.filter(t => t.requirement_id === reqId)
    if (matchingTests.length > 0) {
      const acCount = req.acceptance_criteria?.length || req.ac_count || matchingTests.length || 1
      return Math.min(100, Math.round((matchingTests.length / Math.max(acCount, matchingTests.length)) * 100))
    }
  }
  return typeof req.coverage_pct === 'number' ? req.coverage_pct : 0
}

function getACCount(req: Requirement): number {
  return req.acceptance_criteria?.length ?? req.ac_count ?? 0
}

function getReqId(req: Requirement): string {
  return req.req_id || req.id || ''
}

/* Requirement clarity band (stamped at import into metadata.quality). */
const BAND_META: Record<string, { color: string; label: string }> = {
  clear: { color: '#10b981', label: 'clear' },
  weak: { color: '#f59e0b', label: 'weak' },
  unclear: { color: '#fb7185', label: 'unclear' },
}

function getQuality(req: Requirement) {
  return req.metadata?.quality
}

/* ── Page Component ───────────────────────────────────────────────────────── */

export default function ArchitecturePage() {
  /* ── Project Context ────────────────────────────────────────────────────── */
  const { currentProjectId } = useProject()
  const { addTask, completeTask, failTask, activeTasks } = useTasks()

  /* ── State ──────────────────────────────────────────────────────────────── */
  const [requirements, setRequirements] = useState<Requirement[]>([])
  const [selectedReqId, setSelectedReqId] = useState<string | null>(null)
  const [sourceFilter, setSourceFilter] = useState<string>('all')
  const [bandFilter, setBandFilter] = useState<string>('all')
  const [reqTests, setReqTests] = useState<TestCase[]>([])
  const [changeLog, setChangeLog] = useState<RequirementChange[]>([])
  const [changeLogOpen, setChangeLogOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [addManualOpen, setAddManualOpen] = useState(false)
  const [editMode, setEditMode] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadPreview, setUploadPreview] = useState<any[] | null>(null)
  const [generating, setGenerating] = useState(false)
  // Per-AC in-flight tracking — when an AC's generation is running, its
  // "Generate Tests for this AC" button shows a spinner + disabled state,
  // and the AC card border turns indigo. Other ACs remain clickable.
  const [generatingACs, setGeneratingACs] = useState<Set<string>>(new Set())
  // Server-driven in-flight registry, polled every 5s from /api/tests/generations/in-flight.
  // Keys: per-req `${req_id}::*`, per-AC `${req_id}::${ac_id}`.
  // Survives page reloads + cross-tab navigation — the user never has to wonder
  // whether a generation they kicked off 8 minutes ago is still running.
  const [serverInFlight, setServerInFlight] = useState<Map<string, InFlightGeneration>>(new Map())
  // Fix R: track active bulk Generate All Tests jobs so the bulk button
  // disables itself while one is running. Without this, the user can
  // click multiple times → spawns duplicate background tasks that hit
  // 409 dedup on every req → 21 false-failures per duplicate click.
  // Verified live: user clicked 5 times in 32s → 5 bulk jobs, 84 false
  // failures. Polled every 5s from /api/tests/generate-all/active.
  const [bulkActive, setBulkActive] = useState(false)

  useEffect(() => {
    if (!currentProjectId) return
    let stopped = false
    const tick = async () => {
      try {
        const job = await getActiveGenerateJob(currentProjectId)
        if (stopped) return
        setBulkActive(!!(job && job.status === 'running'))
      } catch { /* ignore — keep last known state */ }
    }
    tick()
    const id = setInterval(tick, 5000)
    return () => { stopped = true; clearInterval(id) }
  }, [currentProjectId])

  // Poll server-side in-flight registry every 5s. Stops polling once nothing
  // is running so we don't hit the API needlessly; resumes the next time the
  // user clicks Generate (handlers immediately reseed the state via fetch).
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const items = await fetchInFlightGenerations()
        if (cancelled) return
        const next = new Map<string, InFlightGeneration>()
        for (const it of items) {
          const key = `${it.requirement_id}::${it.ac_id ?? '*'}`
          next.set(key, it)
        }
        setServerInFlight(prev => {
          // Auto-refresh requirements/tests when an in-flight item just disappeared
          // (i.e., the generation completed) so the AC card flips from "Generating…"
          // to showing its new tests without the user having to refresh manually.
          const completed: string[] = []
          prev.forEach((_, k) => { if (!next.has(k)) completed.push(k) })
          if (completed.length > 0 && currentProjectId) {
            Promise.all([
              fetchRequirements({ project_id: currentProjectId }),
              fetchTests({ project_id: currentProjectId }),
            ]).then(([reqData, testData]) => {
              setRequirements(reqData.requirements)
              setReqTests(testData.tests)
              completed.forEach(k => {
                const [reqId, acMarker] = k.split('::')
                const target = acMarker === '*' ? reqId : `${reqId}/${acMarker}`
                toast.success(`Generation completed: ${target}`)
              })
            }).catch(() => { /* poll loop continues */ })
          }
          return next
        })
      } catch {
        // Network blip — ignore; next tick retries
      }
    }
    tick()  // immediate first poll so the UI doesn't lag 5s
    const id = setInterval(tick, 5000)
    return () => { cancelled = true; clearInterval(id) }
  }, [currentProjectId])

  const isAcInFlight = (reqId: string | null, acId: string): boolean => {
    if (!reqId) return false
    return serverInFlight.has(`${reqId}::${acId}`)
      || serverInFlight.has(`${reqId}::*`)  // full-req gen covers all ACs too
  }
  const inFlightForReq = (reqId: string | null): InFlightGeneration | null => {
    if (!reqId) return null
    const direct = serverInFlight.get(`${reqId}::*`)
    if (direct) return direct
    const values: InFlightGeneration[] = Array.from(serverInFlight.values())
    return values.find((v: InFlightGeneration) => v.requirement_id === reqId) ?? null
  }
  const [genProgress, setGenProgress] = useState<{
    completed: number
    total: number
    current: string | null
    tests: number
    rateLimitReset?: number
    // F11-3: per-stage progress so the user sees activity between requirement boundaries
    stage?: 'risk_scoring' | 'atdd' | 'automation' | 'writing' | null
    stageStartedAt?: string | null
    // F11-6: rolling ETA from completed reqs
    etaSeconds?: number | null
    // F11-4: distinguish "all unchanged" from "all errored"
    skipped?: number
  } | null>(null)
  // F11-1: explicit user-controlled force toggle. Replaces the auto-force
  // heuristic at the old onClick — the user must opt-in to discard tests.
  const [forceRegen, setForceRegen] = useState(false)
  // R54.3 — per-tool regen modal state
  const [regenByToolOpen, setRegenByToolOpen] = useState(false)
  const [regenByToolRunning, setRegenByToolRunning] = useState(false)
  const [execByToolOpen, setExecByToolOpen] = useState(false)
  const [execByToolRunning, setExecByToolRunning] = useState(false)
  // R57.11 — pre-selected tool for the modal when operator clicks the
  // inventory-empty banner. Cleared when the modal closes via onClose.
  const [bannerToolToRegen, setBannerToolToRegen] = useState<ExpectedTool | null>(null)
  const [generatedResult, setGeneratedResult] = useState<any>(null)
  const [discovering, setDiscovering] = useState(false)
  const [jobResultsOpen, setJobResultsOpen] = useState(false)
  const [lastJob, setLastJob] = useState<GenerateAllJob | null>(null)
  const [_rateLimitTick, setRateLimitTick] = useState(0)

  // Manual form fields
  const [manualTitle, setManualTitle] = useState('')
  const [manualDesc, setManualDesc] = useState('')
  const [manualPriority, setManualPriority] = useState('P1')
  const [manualAC, setManualAC] = useState('')

  // Edit form fields
  const [editTitle, setEditTitle] = useState('')
  const [editDesc, setEditDesc] = useState('')

  const fileInputRef = useRef<HTMLInputElement>(null)
  const toast = useToast()

  // Drive 1-second re-renders while rate limit countdown is active
  useEffect(() => {
    if (!genProgress?.rateLimitReset) return
    const id = setInterval(() => setRateLimitTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [genProgress?.rateLimitReset])

  /* ── Data Fetching ──────────────────────────────────────────────────────── */

  useEffect(() => {
    fetchRequirements({ project_id: currentProjectId || undefined })
      .then(d => setRequirements(d.requirements))
      .catch(() => toast.error('Failed to load requirements'))
      .finally(() => setLoading(false))
  }, [currentProjectId])

  // Resume polling if there's an active generate-all job for this project
  useEffect(() => {
    if (!currentProjectId || generating) return
    let cancelled = false
    let stopStream: (() => void) | null = null
    getActiveGenerateJob(currentProjectId).then(async (job) => {
      if (cancelled) return
      if (job.status !== 'running' || !job.job_id) {
        // F12-4: there's no active job, so any "test_generation" tasks left
        // in the task drawer are stale (likely from a previous SSE-error path
        // before F12-4 added failTask there). Mark them failed so the banner
        // clears instead of saying "Generating tests..." forever.
        for (const t of activeTasks) {
          if (t.type === 'test_generation') {
            failTask(t.id, 'Generation appears to have ended (no active job found)')
          }
        }
        return
      }
      setGenerating(true)
      setGenProgress({ completed: job.completed, total: job.total_requirements, current: job.current_requirement, tests: job.total_tests_generated })

      // F1: Stream job progress via SSE (replaces 3s polling loop)
      stopStream = streamGenerateAllStatus(job.job_id, async (status) => {
        if (cancelled) return
        setGenProgress({
          completed: status.completed,
          total: status.total_requirements,
          current: status.current_requirement ?? null,
          tests: status.total_tests_generated,
          rateLimitReset: status.rate_limit_reset,
          // F11-3 / F11-6 / F11-4
          stage: status.current_stage ?? null,
          stageStartedAt: status.current_stage_started_at ?? null,
          etaSeconds: status.eta_seconds ?? null,
          skipped: status.skipped ?? 0,
        })
        if (status.status === 'completed') {
          setLastJob(status)
          setJobResultsOpen(true)
          try {
            const [reqData, testData] = await Promise.all([
              fetchRequirements({ project_id: currentProjectId || undefined }),
              fetchTests({ priority: undefined, tool: undefined, project_id: currentProjectId || undefined }),
            ])
            setRequirements(reqData.requirements)
            setReqTests(testData.tests)
          } catch {}
          setGenerating(false)
          setGenProgress(null)
        } else if (['failed', 'aborted', 'crashed'].includes(status.status as string)) {
          setGenerating(false)
          setGenProgress(null)
        }
      })
    }).catch(() => {})
    return () => {
      cancelled = true
      if (stopStream) try { stopStream() } catch {}
    }
  }, [currentProjectId])

  // Fetch last completed generation job for persistent status
  useEffect(() => {
    if (!currentProjectId) return
    getLastCompletedJob(currentProjectId).then(job => {
      if (job && job.status !== 'none') setLastJob(job)
    }).catch(() => {})
  }, [currentProjectId])

  // Retry/regenerate handlers
  const handleRetry = async (jobId: string, fromRequirement?: string) => {
    setJobResultsOpen(false)
    setGenerating(true)
    setGenProgress(null)
    try {
      const job = await retryFailedTests(jobId, fromRequirement)
      if (job.status === 'running') {
        // F1: Stream retry progress via SSE (replaces 3s polling loop)
        await new Promise<void>((resolve) => {
          const stop = streamGenerateAllStatus(job.job_id, (status) => {
            setGenProgress({
              completed: status.completed,
              total: status.total_requirements,
              current: status.current_requirement ?? null,
              tests: status.total_tests_generated,
              rateLimitReset: status.rate_limit_reset,
              // F11-3 / F11-6 / F11-4
              stage: status.current_stage ?? null,
              stageStartedAt: status.current_stage_started_at ?? null,
              etaSeconds: status.eta_seconds ?? null,
              skipped: status.skipped ?? 0,
            })
            if (status.status !== 'running') {
              setLastJob(status)
              setJobResultsOpen(true)
              stop()
              resolve()
            }
          }, (_err) => {
            stop()
            resolve()
          })
        })
      }
    } catch (err: any) {
      toast.error(err.message || 'Retry failed')
    } finally {
      setGenerating(false)
      setGenProgress(null)
      // Refresh requirements + tests
      fetchRequirements({ project_id: currentProjectId || undefined }).then(d => setRequirements(d.requirements || []))
      fetchTests({ project_id: currentProjectId || undefined }).then(d => setReqTests(d.tests))
    }
  }

  const handleRegenerate = async (requirementId: string, feedback?: string) => {
    setJobResultsOpen(false)
    try {
      await regenerateTests(requirementId, true, feedback || undefined)
      toast.success(`Regenerated tests for ${requirementId}`)
      fetchTests({ project_id: currentProjectId || undefined }).then(d => setReqTests(d.tests))
      fetchRequirements({ project_id: currentProjectId || undefined }).then(d => setRequirements(d.requirements || []))
    } catch (err: any) {
      toast.error(err.message || 'Regeneration failed')
    }
  }

  // R54.3 — submit handler for the per-tool regen modal. Wires the R53
  // backend endpoint into the existing useTasks/toast UX so progress is
  // visible in the same task panel operators already use for "Generate
  // All Tests".
  const handleRegenByTool = async ({ tool, force, requirementId }: {
    tool: string
    force: boolean
    requirementId?: string
  }) => {
    if (!currentProjectId) return
    setRegenByToolRunning(true)
    const tid = `regen-${tool}-${Date.now()}`
    const scopeLabel = requirementId ? `for ${requirementId}` : `for all requirements`
    addTask({
      id: tid,
      type: 'test_generation',
      label: `Regenerating ${tool} tests ${scopeLabel}`,
      result_url: '/test-explorer',
    })
    try {
      const result = await regenerateByTool({
        projectId: currentProjectId,
        tool,
        force,
        requirementId,
      })
      if (result.status === 'no_work') {
        completeTask(tid, { detail: 'No matching requirements' })
        toast.success(`No ${tool} requirements to regen`)
      } else {
        const tests = result.results.reduce((acc, r) => acc + (r.tests_generated || 0), 0)
        const failedPart = result.failed > 0 ? ` · ${result.failed} failed` : ''
        const detail = `${result.succeeded}/${result.requirements_targeted} req(s), ${tests} ${tool} spec(s)${failedPart}`
        completeTask(tid, { detail })
        toast.success(`Regenerated ${tests} ${tool} spec(s)`)
        // Refresh requirement list so test counts update
        try {
          const data = await fetchRequirements({ project_id: currentProjectId })
          setRequirements(data.requirements || [])
        } catch {}
      }
      setRegenByToolOpen(false)
    } catch (err: unknown) {
      const e = err as { status?: number; message?: string }
      const msg = e.status === 404
        ? `Requirement not found`
        : (e.message || `Regen by tool failed`)
      failTask(tid, msg)
      toast.error(msg)
    } finally {
      setRegenByToolRunning(false)
    }
  }

  // WS4 — EXECUTE a single tool (optionally one requirement); the run-side
  // counterpart to handleRegenByTool. Reuses the execute-by-tool endpoint.
  const handleExecByTool = async ({ tool, environment, suiteType, requirementId }: {
    tool: string; environment: string; suiteType: string; requirementId?: string
  }) => {
    if (!currentProjectId) return
    setExecByToolRunning(true)
    const tid = `exec-${tool}-${Date.now()}`
    const scopeLabel = requirementId ? `for ${requirementId}` : 'for all requirements'
    addTask({ id: tid, type: 'test_execution', label: `Executing ${tool} ${scopeLabel} (${environment})`, result_url: '/run-history' })
    try {
      const result = await executeByTool({ projectId: currentProjectId, tool, environment, suiteType, requirementId })
      completeTask(tid, { detail: `run ${result.run_id} · ${result.status}` })
      toast.success(`Dispatched ${tool} run ${result.run_id}`)
      setExecByToolOpen(false)
    } catch (err: unknown) {
      const e = err as { status?: number; message?: string }
      const msg = e.status === 400 ? `Unsupported tool` : (e.message || 'Execute by tool failed')
      failTask(tid, msg)
      toast.error(msg)
    } finally {
      setExecByToolRunning(false)
    }
  }

  const selectedReq = useMemo(
    () => requirements.find(r => getReqId(r) === selectedReqId) ?? null,
    [requirements, selectedReqId],
  )

  // Fetch tests + changelog when selection changes
  useEffect(() => {
    if (!selectedReqId) {
      setReqTests([])
      setChangeLog([])
      setChangeLogOpen(false)
      setEditMode(false)
      return
    }
    // Fetch tests
    fetchTests({ priority: undefined, tool: undefined, project_id: currentProjectId || undefined })
      .then(d => setReqTests(d.tests))
      .catch(() => setReqTests([]))
    // Fetch changelog
    fetchRequirementChanges(selectedReqId)
      .then(data => setChangeLog(data))
      .catch(() => setChangeLog([]))
  }, [selectedReqId])

  /* ── Tool Inventory Gaps (R57.11) ─────────────────────────────────────── */

  // Surface tools with 0 specs in the loaded TestCase set. Empty array
  // when every tool has ≥1 spec (the happy path). Each entry has the
  // tool name + count=0 for display.
  const toolGaps = useMemo(
    () => computeToolInventoryGaps(reqTests),
    [reqTests],
  )

  /* ── Filtered & Sorted Requirements ─────────────────────────────────────── */

  const filteredReqs = useMemo(() => {
    let list = requirements
    if (sourceFilter !== 'all') {
      list = list.filter(r => getSource(r) === sourceFilter)
    }
    if (bandFilter !== 'all') {
      list = list.filter(r => getQuality(r)?.band === bandFilter)
    }
    // Sort: priority (P0 first), then coverage ascending
    const prioOrder: Record<string, number> = { P0: 0, P1: 1, P2: 2, P3: 3 }
    return [...list].sort((a, b) => {
      const pa = prioOrder[a.priority] ?? 4
      const pb = prioOrder[b.priority] ?? 4
      if (pa !== pb) return pa - pb
      return getCoverage(a, reqTests) - getCoverage(b, reqTests)
    })
  }, [requirements, sourceFilter, bandFilter])

  /* ── Ingestion Summary Counts ───────────────────────────────────────────── */

  const counts = useMemo(() => {
    const total = requirements.length
    const docs = requirements.filter(r => getSource(r) === 'document').length
    const jira = requirements.filter(r => getSource(r) === 'jira').length
    const manual = requirements.filter(r => getSource(r) === 'manual').length
    const changed = requirements.filter(r => getChangeStatus(r) !== 'unchanged').length
    return { total, docs, jira, manual, changed }
  }, [requirements])

  /* ── Action Handlers ────────────────────────────────────────────────────── */

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const preview = await previewUpload(file)
      setUploadPreview(preview)
    } catch {
      toast.error('Failed to parse document')
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const confirmUpload = () => {
    if (!uploadPreview) return
    // Add preview items to requirements list
    const newReqs: Requirement[] = uploadPreview.map((item: any, i: number) => ({
      req_id: item.req_id || `REQ-NEW-${Date.now()}-${i}`,
      title: item.title || 'Untitled',
      description: item.description,
      priority: item.priority || 'P2',
      risk_score: item.risk_score ?? 5,
      acceptance_criteria: item.acceptance_criteria,
      coverage_pct: 0,
      source: 'document',
      change_status: 'new',
    } as any))
    setRequirements(prev => [...prev, ...newReqs])
    setUploadPreview(null)
    toast.success(`${newReqs.length} requirements imported`)
  }

  const handleResyncJira = async () => {
    const taskId = `jira-sync-${Date.now()}`
    addTask({ id: taskId, type: 'jira_sync', label: 'Re-syncing Jira requirements', result_url: '/architecture' })
    try {
      await resyncJira('default')
      const data = await fetchRequirements({ project_id: currentProjectId || undefined })
      setRequirements(data.requirements)
      completeTask(taskId, { detail: `${data.requirements.length} requirements synced` })
      toast.success('Jira re-sync complete')
    } catch {
      failTask(taskId, 'Jira sync failed')
      toast.error('Jira sync failed')
    }
  }

  const handleSaveManual = () => {
    if (!manualTitle.trim()) return
    const acList = manualAC.split('\n').filter(l => l.trim()).map((line, i) => ({
      id: `AC-${String(i + 1).padStart(3, '0')}`,
      statement: line.trim(),
      covered: false,
    }))
    const newReq: any = {
      req_id: `REQ-${String(requirements.length + 1).padStart(3, '0')}`,
      title: manualTitle.trim(),
      description: manualDesc.trim(),
      priority: manualPriority,
      risk_score: manualPriority === 'P0' ? 9 : manualPriority === 'P1' ? 7 : 4,
      acceptance_criteria: acList,
      coverage_pct: 0,
      source: 'manual',
      change_status: 'new',
    }
    setRequirements(prev => [newReq, ...prev])
    setAddManualOpen(false)
    setManualTitle('')
    setManualDesc('')
    setManualPriority('P1')
    setManualAC('')
    toast.success('Requirement added')
  }

  const handleFixExtraction = async () => {
    if (!selectedReq || !selectedReqId) return
    if (editMode) {
      // Save
      try {
        await correctRequirement(selectedReqId, { title: editTitle, description: editDesc })
        setRequirements(prev =>
          prev.map(r => getReqId(r) === selectedReqId ? { ...r, title: editTitle, description: editDesc } : r),
        )
        setEditMode(false)
        toast.success('Requirement updated')
      } catch {
        toast.error('Failed to save correction')
      }
    } else {
      setEditTitle(selectedReq.title)
      setEditDesc(selectedReq.description ?? '')
      setEditMode(true)
    }
  }

  const handleGenerateTests = async (force = false) => {
    if (!selectedReqId) return
    if (generating) {
      // Re-entrancy guard — user clicking the button twice was kicking off
      // concurrent pipelines after the same button was clicked 3x).
      toast.info('Generation already running for this requirement…')
      return
    }
    setGenerating(true)
    setGeneratedResult(null)
    const taskId = `gen-tests-${Date.now()}`
    const forcedSuffix = force ? ' (forced)' : ''
    addTask({ id: taskId, type: 'test_generation', label: `Generating tests for ${selectedReqId}${forcedSuffix}`, result_url: '/test-explorer' })
    // Set expectation up-front — for P0 reqs with chunked Newman generation,
    // total wall time is typically 3-5 min. Without this the user sees no
    // feedback for several minutes and assumes the click did nothing.
    toast.info(`Generating tests for ${selectedReqId}${forcedSuffix} — this can take 3-5 min for P0 reqs`)
    try {
      // Per-req button passes its own explicit `force` flag (no longer
      // depends on the global checkbox state — that checkbox now governs
      // only the bulk Generate All Tests button).
      const result = await generateTests(selectedReqId, undefined, force)
      setGeneratedResult(result)
      completeTask(taskId, { detail: `${result.test_count} test scenarios generated${forcedSuffix}` })
      toast.success(`Generated ${result.test_count} test scenarios${forcedSuffix}`)
      // Refresh both requirements (for coverage_pct) and tests (for detail panel)
      const [reqData, testData] = await Promise.all([
        fetchRequirements({ project_id: currentProjectId || undefined }),
        fetchTests({ priority: undefined, tool: undefined, project_id: currentProjectId || undefined }),
      ])
      setRequirements(reqData.requirements)
      setReqTests(testData.tests)
    } catch (err: any) {
      // 409 dedup: another generation is already running for this requirement.
      // Show a friendly informational toast instead of an error — the user
      // can wait or check Run History for the in-flight workflow.
      if (err?.status === 409 && err?.detail?.error === 'generation_in_flight') {
        const wf = err.detail.workflow_id
        const ago = err.detail.started_seconds_ago ?? 0
        completeTask(taskId, { detail: `Already running (workflow=${wf}, started ${ago}s ago)` })
        toast.info(`Generation already running for ${selectedReqId} (workflow ${wf}, ${ago}s in flight)`)
      } else {
        failTask(taskId, err.message || 'Generation failed')
        toast.error(err.message || 'Generation failed')
      }
    } finally {
      setGenerating(false)
    }
  }

  /* ── AC-to-Test Mapping ─────────────────────────────────────────────────── */

  const getTestsForAC = (acId: string): { id: string; title: string; status: string; tool: string }[] => {
    const testId = (t: any) => t.test_id || t.id || ''
    const toEntry = (t: any) => ({
      id: testId(t),
      title: t.title || 'Untitled test',
      status: t.last_status || t.status || 'PENDING',
      tool: t.automation_tool || t.tool || 'playwright',
    })

    // Match by AC ID — generated tests have ac_id set
    const realMatches = reqTests
      .filter(t => t.ac_id === acId)
      .map(toEntry)
      .filter(t => t.id)
    if (realMatches.length > 0) return realMatches

    // Fallback: distribute requirement-level tests evenly across ACs by index
    if (selectedReqId && selectedReq?.acceptance_criteria?.length) {
      const acIndex = selectedReq.acceptance_criteria.findIndex((ac: any) => ac.id === acId)
      if (acIndex < 0) return []
      const allReqTests = reqTests
        .filter(t => t.requirement_id === selectedReqId)
        .map(toEntry)
        .filter(t => t.id)
      if (allReqTests.length === 0) return []
      const acCount = selectedReq.acceptance_criteria.length
      const perAc = Math.ceil(allReqTests.length / acCount)
      return allReqTests.slice(acIndex * perAc, (acIndex + 1) * perAc)
    }

    return []
  }

  /* ── Render ─────────────────────────────────────────────────────────────── */

  return (
    <div className="p-8">
      {/* Page Header */}
      <h1 className="text-xl font-bold mb-1">Test Architecture</h1>
      <p className="text-sm mb-6" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
        Requirements &rarr; Acceptance Criteria &rarr; Test Cases
      </p>

      {/* ── Section 1: Ingestion Summary Cards ────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4 mb-6">
        {[
          { label: 'Total Requirements', value: counts.total, color: '#94a3b8', icon: '\u{1F4CA}' },
          { label: 'From Documents', value: counts.docs, color: '#8b5cf6', icon: '\u{1F4C4}' },
          { label: 'From Jira', value: counts.jira, color: '#06b6d4', icon: '\u{1F517}' },
          { label: 'Manual Entry', value: counts.manual, color: '#10b981', icon: '\u270F\uFE0F' },
          { label: 'Changed Since Sync', value: counts.changed, color: '#f59e0b', icon: '\u26A0' },
        ].map(card => (
          <div
            key={card.label}
            className="rounded-xl px-4 py-4"
            style={{
              background: '#12121f',
              border: '1px solid #1e1e3a',
              borderLeft: `4px solid ${card.color}`,
            }}
          >
            <div className="text-sm mb-1">{card.icon}</div>
            <div className="text-xs mb-1" style={{ color: '#64748b' }}>{card.label}</div>
            <div className="text-2xl font-bold" style={{ color: card.color === '#94a3b8' ? '#e2e8f0' : card.color }}>
              {loading ? '--' : card.value}
            </div>
          </div>
        ))}
      </div>

      {/* R57.11 — tool-inventory-empty banner. Surfaces when any of the
          6 expected automation tools has 0 specs. One-click recovery via
          R54's modal pre-selected with the missing tool. Pre-R57.11 the
          operator could dispatch a 38-minute run that produced 0 k6
          results because there are no k6 specs to dispatch. */}
      {currentProjectId && toolGaps.length > 0 && (
        <div
          className="rounded-xl p-4 mb-6"
          style={{
            background: 'rgba(245,158,11,0.08)',
            border: '1px solid rgba(245,158,11,0.40)',
          }}
        >
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div className="flex-1 min-w-[280px]">
              <p className="text-sm font-semibold" style={{ color: '#f59e0b' }}>
                {'⚠'} Empty tool inventory: {toolGaps.map(g => g.tool).join(', ')}
              </p>
              <p className="text-[11px] mt-1" style={{ color: '#94a3b8' }}>
                {toolGaps.length} of 6 automation tool{toolGaps.length === 1 ? ' has' : 's have'} 0 generated specs.
                Click below to regenerate the missing tool&apos;s specs (uses R53 backend; force-clean + regen for all requirements by default).
              </p>
            </div>
            <div className="flex gap-2 flex-wrap">
              {toolGaps.map(g => (
                <button
                  key={g.tool}
                  onClick={() => {
                    setBannerToolToRegen(g.tool)
                    setRegenByToolOpen(true)
                  }}
                  disabled={regenByToolRunning}
                  className="px-3 py-1.5 rounded-md text-xs font-semibold text-white transition-opacity"
                  style={{
                    background: 'linear-gradient(135deg,#f59e0b,#fb923c)',
                    opacity: regenByToolRunning ? 0.5 : 1,
                    cursor: regenByToolRunning ? 'not-allowed' : 'pointer',
                  }}
                  title={`Open RegenerateByTool modal with ${g.tool} pre-selected`}
                >
                  Regenerate {g.tool}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ── Section 2: Action Bar ─────────────────────────────────────────── */}
      <div className="flex items-center justify-between mb-6 flex-wrap gap-3">
        <div className="flex items-center gap-3">
          {/* Upload Document */}
          <input ref={fileInputRef} type="file" className="hidden"
                 accept=".docx,.xlsx,.pdf,.md,.txt,.json,.yaml"
                 onChange={handleFileUpload} />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{
              background: 'transparent',
              border: '1px solid #8b5cf644',
              color: '#a78bfa',
              opacity: uploading ? 0.5 : 1,
            }}
          >
            {'\u{1F4C4}'} {uploading ? 'Parsing...' : 'Upload Document'}
          </button>

          {/* Re-sync Jira */}
          <button
            onClick={handleResyncJira}
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{ background: 'transparent', border: '1px solid #06b6d444', color: '#67e8f9' }}
          >
            {'\u{1F517}'} Re-sync from Jira
          </button>

          {/* Discover from GitHub */}
          <button
            onClick={async () => {
              if (!currentProjectId) return
              setDiscovering(true)
              const tid = `discover-${Date.now()}`
              addTask({ id: tid, type: 'requirement_discovery', label: 'Discovering requirements from GitHub', result_url: '/architecture' })
              try {
                const result = await discoverRequirements(currentProjectId)
                completeTask(tid, { detail: `${result.requirements_discovered} requirements discovered` })
                toast.success(`Discovered ${result.requirements_discovered} requirements from GitHub`)
                const fresh = await fetchRequirements({ project_id: currentProjectId })
                setRequirements(fresh.requirements || [])
              } catch (err: any) {
                failTask(tid, err.message || 'Discovery failed')
                toast.error(err.message || 'Discovery failed')
              } finally {
                setDiscovering(false)
              }
            }}
            disabled={discovering}
            className="px-3 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1.5"
            style={{ background: '#1e1e3a', border: '1px solid #6366f1', color: '#a5b4fc', opacity: discovering ? 0.5 : 1 }}
          >
            {discovering ? 'Discovering...' : '\u{1F50D} Discover from GitHub'}
          </button>

          {/* Add Manual */}
          <button
            onClick={() => setAddManualOpen(!addManualOpen)}
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{
              background: addManualOpen ? '#10b98122' : 'transparent',
              border: `1px solid ${addManualOpen ? '#10b981' : '#10b98144'}`,
              color: '#6ee7b7',
            }}
          >
            {'\u270F\uFE0F'} Add Manual
          </button>

          {/* Generate All Tests — checkbox lifted out below the toolbar (F12-2) */}
          <button
              onClick={async () => {
                if (!currentProjectId) return
                // F11-5: client-side guard — if a job is already running for
                // this project, redirect the user's attention to the existing
                // progress instead of firing a doomed second request.
                if (generating || genProgress) {
                  toast.error('A generation job is already running — see progress on the button')
                  return
                }
                // F11-1: force is now driven by the explicit checkbox the
                // user controls. The old auto-force-when-tests-exist heuristic
                // has been removed so unchanged requirements are reused.
                const shouldForce = forceRegen
                setGenerating(true)
                setGenProgress(null)
                const tid = `gen-all-${Date.now()}`
                addTask({ id: tid, type: 'test_generation', label: shouldForce ? 'Regenerating all tests (force)' : 'Generating tests for all requirements', result_url: '/test-explorer' })
                try {
                  const job = await generateAllTests(currentProjectId, shouldForce)
                  const jobId = job.job_id
                  setGenProgress({ completed: 0, total: job.total_requirements, current: null, tests: 0 })

                  // F1: Stream progress via SSE (replaces 3s polling loop)
                  await new Promise<void>((resolve) => {
                    const stop = streamGenerateAllStatus(jobId, async (status) => {
                      setGenProgress({
                        completed: status.completed,
                        total: status.total_requirements,
                        current: status.current_requirement ?? null,
                        tests: status.total_tests_generated,
                        rateLimitReset: status.rate_limit_reset,
                        // F11-3 / F11-6 / F11-4 — surface the new SSE fields
                        stage: status.current_stage ?? null,
                        stageStartedAt: status.current_stage_started_at ?? null,
                        etaSeconds: status.eta_seconds ?? null,
                        skipped: status.skipped ?? 0,
                      })
                      if (status.status === 'completed') {
                        // F11-4: clearer success message when nothing needed regen
                        const skipped = status.skipped ?? 0
                        const tests = status.total_tests_generated ?? 0
                        const detail = (skipped > 0 && tests === 0)
                          ? `All ${status.total_requirements} requirements unchanged — no regeneration needed`
                          : `${tests} tests for ${status.total_requirements} requirements${skipped > 0 ? ` (${skipped} unchanged)` : ''}`
                        completeTask(tid, { detail })
                        if (skipped > 0 && tests === 0) {
                          toast.success(detail)
                        }
                        setLastJob(status)
                        setJobResultsOpen(true)
                        try {
                          const data = await fetchRequirements({ project_id: currentProjectId || undefined })
                          setRequirements(data.requirements)
                        } catch {}
                        stop()
                        resolve()
                      } else if (['failed', 'aborted', 'crashed'].includes(status.status as string)) {
                        failTask(tid, `Job ended with status: ${status.status}`)
                        setLastJob(status)
                        setJobResultsOpen(true)
                        stop()
                        resolve()
                      }
                    }, (errMsg) => {
                      // F12-4: fail the task too so the "Generating tests..."
                      // banner clears instead of sitting forever when SSE
                      // errors persistently (e.g., the F12-1 401 cascade).
                      console.warn('Generate-all stream error:', errMsg)
                      failTask(tid, `Stream error: ${errMsg}`)
                      stop()
                      resolve()
                    })
                  })
                } catch (err: unknown) {
                  // F11-2: 429 → friendly toast (vs generic "fetch failed")
                  const e = err as { status?: number; message?: string }
                  const msg = e.status === 429
                    ? 'A generation job is already running for this project. Wait for it to finish or abort it from the dashboard.'
                    : (e.message || 'Batch generation failed')
                  failTask(tid, msg)
                  toast.error(msg)
                } finally {
                  setGenerating(false)
                  setGenProgress(null)
                }
              }}
              disabled={generating || bulkActive}
              title={bulkActive
                ? 'A bulk generation is already running for this project \u2014 wait for it to complete or abort it before starting another.'
                : undefined}
              className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors"
              style={{
                background: (generating || bulkActive) ? '#7c3aed44' : 'transparent',
                border: '1px solid #8b5cf644',
                color: '#c4b5fd',
                opacity: (generating || bulkActive) ? 0.7 : 1,
                cursor: (generating || bulkActive) ? 'not-allowed' : 'pointer',
              }}
            >
              {(() => {
                if (bulkActive && !generating) return '\u29d7 Bulk run in progress\u2026'
                if (!generating) return '\u2726 Generate All Tests'
                if (!genProgress) return 'Starting generation...'
                if (genProgress.rateLimitReset && genProgress.rateLimitReset > Date.now() / 1000) {
                  return `\u23F3 Rate limited — retrying in ${Math.ceil(genProgress.rateLimitReset - Date.now() / 1000)}s`
                }
                // F11-3: per-stage label so the user sees activity within each requirement
                const stageLabels: Record<string, string> = {
                  risk_scoring: 'Risk Scoring',
                  atdd: 'ATDD Gherkin',
                  automation: 'Automation Scripts',
                  writing: 'Writing Tests',
                }
                const stageTxt = genProgress.stage ? ` · ${stageLabels[genProgress.stage] || genProgress.stage}` : ''
                let elapsedTxt = ''
                if (genProgress.stageStartedAt) {
                  const elapsedSec = Math.max(0, Math.floor((Date.now() - new Date(genProgress.stageStartedAt).getTime()) / 1000))
                  elapsedTxt = ` · ${elapsedSec}s`
                }
                // F11-6: ETA from rolling per-req average
                const etaTxt = (genProgress.etaSeconds && genProgress.etaSeconds > 0)
                  ? ` · ETA ~${Math.ceil(genProgress.etaSeconds / 60)}m`
                  : ''
                return `${genProgress.completed}/${genProgress.total} · ${genProgress.tests ?? 0} tests${genProgress.current ? ` · ${genProgress.current}` : ''}${stageTxt}${elapsedTxt}${etaTxt}`
              })()}
            </button>

            {/* R54.3 — per-tool regen entry point. Opens RegenerateByToolModal
                which calls the R53 /api/tests/regenerate-by-tool endpoint. */}
            <button
              onClick={() => {
                if (!currentProjectId) { toast.error('Select a project first'); return }
                setRegenByToolOpen(true)
              }}
              disabled={generating || bulkActive || regenByToolRunning}
              className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors"
              style={{
                background: regenByToolRunning ? '#7c3aed44' : 'transparent',
                border: '1px solid #8b5cf644',
                color: '#c4b5fd',
                opacity: (generating || bulkActive || regenByToolRunning) ? 0.7 : 1,
                cursor: (generating || bulkActive || regenByToolRunning) ? 'not-allowed' : 'pointer',
              }}
              title="Regenerate tests for a single automation tool (Playwright, Newman, k6, ZAP, Axe, Pytest)"
            >
              {regenByToolRunning ? '⏳ Regenerating…' : '\u{1F504} Regenerate by Tool'}
            </button>

            {/* WS4 — per-tool EXECUTE entry point (mirrors Regenerate by Tool). */}
            <button
              onClick={() => {
                if (!currentProjectId) { toast.error('Select a project first'); return }
                setExecByToolOpen(true)
              }}
              disabled={generating || bulkActive || execByToolRunning}
              className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors"
              style={{
                background: execByToolRunning ? '#7c3aed44' : 'transparent',
                border: '1px solid #8b5cf644',
                color: '#c4b5fd',
                opacity: (generating || bulkActive || execByToolRunning) ? 0.7 : 1,
                cursor: (generating || bulkActive || execByToolRunning) ? 'not-allowed' : 'pointer',
              }}
              title="Execute tests for a single automation tool (Playwright, Newman, k6, ZAP, Axe, Pytest)"
            >
              {execByToolRunning ? '⏳ Executing…' : '▶ Execute by Tool'}
            </button>
        </div>

        {/* F12-2: Lifted out of the button row so the toolbar buttons stay
            on a single aligned baseline. The checkbox controls the most
            recent button (Generate All Tests), so it sits directly under
            the toolbar in its own row. */}
        <div className="flex items-center gap-3 -mt-3 mb-4 ml-1">
          <label className="flex items-center gap-2 text-[11px] cursor-pointer select-none"
                 style={{ color: '#94a3b8' }}>
            <input
              type="checkbox"
              checked={forceRegen}
              onChange={e => setForceRegen(e.target.checked)}
              disabled={generating}
              className="cursor-pointer"
            />
            Force regenerate (discard existing tests)
          </label>
          {forceRegen && (() => {
            const existingCount = requirements.reduce((acc: number, r: Requirement) => acc + (r.test_count ?? 0), 0)
            return (
              <span className="text-[11px]" style={{ color: '#f59e0b' }}>
                Will discard {existingCount} existing tests and rebuild
              </span>
            )
          })()}
        </div>

        {/* Last Job Summary */}
        {lastJob && !generating && lastJob.status !== 'none' && (() => {
          const successCount = lastJob.results?.filter(r => {
            const tools = r.tools || {}
            return Object.values(tools).every((t: any) => t.status === 'generated')
          }).length || 0
          const failedCount = lastJob.failed || 0
          const partialCount = (lastJob.results?.length || 0) - successCount
          return (
            <div className="w-full flex items-center gap-3 px-4 py-2 rounded-lg text-xs"
                 style={{ background: '#12121f88', border: '1px solid #1e1e3a' }}>
              <span style={{ color: '#94a3b8' }}>
                Last generation: <span style={{ color: '#10b981' }}>{successCount} complete</span>
                {partialCount > 0 && <>, <span style={{ color: '#f59e0b' }}>{partialCount} partial</span></>}
                {failedCount > 0 && <>, <span style={{ color: '#fb7185' }}>{failedCount} failed</span></>}
              </span>
              <button onClick={() => setJobResultsOpen(true)}
                      className="px-2 py-0.5 rounded transition-colors hover:bg-white/5"
                      style={{ color: '#8b5cf6' }}>
                View Details
              </button>
              {(failedCount > 0 || partialCount > 0) && (
                <button onClick={() => handleRetry(lastJob.job_id)}
                        className="px-2 py-0.5 rounded transition-colors hover:bg-white/5"
                        style={{ color: '#f59e0b' }}>
                  Retry Failed
                </button>
              )}
            </div>
          )
        })()}

        {/* Source Filter */}
        <select
          value={sourceFilter}
          onChange={e => setSourceFilter(e.target.value)}
          className="px-3 py-2 rounded-lg text-sm outline-none"
          style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#94a3b8' }}
        >
          <option value="all">All Sources</option>
          <option value="document">Documents</option>
          <option value="jira">Jira</option>
          <option value="manual">Manual</option>
          <option value="confluence">Confluence</option>
        </select>

        {/* Clarity Band Filter + summary strip (client-side, from loaded rows) */}
        <select
          value={bandFilter}
          onChange={e => setBandFilter(e.target.value)}
          className="px-3 py-2 rounded-lg text-sm outline-none"
          style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#94a3b8' }}
        >
          <option value="all">All Clarity</option>
          <option value="clear">Clear</option>
          <option value="weak">Weak</option>
          <option value="unclear">Unclear</option>
        </select>
        {(() => {
          const scored = requirements.filter(r => getQuality(r))
          if (!scored.length) return null
          const by = (b: string) => scored.filter(r => getQuality(r)?.band === b).length
          const avg = Math.round(scored.reduce((s, r) => s + (getQuality(r)?.score ?? 0), 0) / scored.length)
          return (
            <span className="text-[11px] px-2 py-1 rounded-lg"
                  style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#64748b' }}
                  title="Requirement clarity bands (heuristic, stamped at import)">
              <span style={{ color: BAND_META.clear.color }}>{by('clear')} clear</span>
              {' · '}
              <span style={{ color: BAND_META.weak.color }}>{by('weak')} weak</span>
              {' · '}
              <span style={{ color: BAND_META.unclear.color }}>{by('unclear')} unclear</span>
              {' · avg '}{avg}
            </span>
          )
        })()}
      </div>

      {/* ── Upload Preview Modal ──────────────────────────────────────────── */}
      {uploadPreview && (
        <div className="fixed inset-0 z-50 flex items-center justify-center"
             style={{ background: 'rgba(0,0,0,0.7)' }}>
          <div className="rounded-xl p-6 w-full max-w-lg max-h-[80vh] overflow-y-auto"
               style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <h3 className="text-lg font-bold mb-4">Parsed Requirements Preview</h3>
            <div className="space-y-3 mb-6">
              {uploadPreview.map((item: any, i: number) => (
                <div key={i} className="p-3 rounded-lg" style={{ background: '#0a0a14' }}>
                  <div className="text-sm font-medium mb-1">{item.title || 'Untitled'}</div>
                  <div className="text-xs" style={{ color: '#64748b' }}>
                    {item.priority || 'P2'} &middot; {(item.acceptance_criteria ?? []).length} ACs
                  </div>
                </div>
              ))}
            </div>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setUploadPreview(null)}
                className="px-4 py-2 rounded-lg text-sm"
                style={{ background: '#1e1e3a', color: '#94a3b8' }}
              >
                Cancel
              </button>
              <button
                onClick={confirmUpload}
                className="px-4 py-2 rounded-lg text-sm font-medium"
                style={{ background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff' }}
              >
                Import {uploadPreview.length} Requirements
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Section 3: Two-Panel Layout ───────────────────────────────────── */}
      <div className="flex gap-6" style={{ maxHeight: 'calc(100vh - 280px)' }}>
        {/* ── Left Panel: Requirements List ───────────────────────────────── */}
        <div className="w-[340px] flex-shrink-0 overflow-y-auto pr-1 space-y-2"
             style={{ maxHeight: 'calc(100vh - 280px)' }}>

          {/* Add Manual Form */}
          {addManualOpen && (
            <div className="rounded-xl p-4 mb-2" style={{ background: '#12121f', border: '1px solid #10b98144' }}>
              <h4 className="text-sm font-semibold mb-3" style={{ color: '#6ee7b7' }}>New Requirement</h4>
              <input
                placeholder="Title *"
                value={manualTitle}
                onChange={e => setManualTitle(e.target.value)}
                className="w-full px-3 py-2 rounded-lg text-sm mb-2 outline-none"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
              <textarea
                placeholder="Description"
                value={manualDesc}
                onChange={e => setManualDesc(e.target.value)}
                rows={2}
                className="w-full px-3 py-2 rounded-lg text-sm mb-2 outline-none resize-none"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
              <select
                value={manualPriority}
                onChange={e => setManualPriority(e.target.value)}
                className="w-full px-3 py-2 rounded-lg text-sm mb-2 outline-none"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              >
                <option value="P0">P0 — Critical</option>
                <option value="P1">P1 — High</option>
                <option value="P2">P2 — Medium</option>
                <option value="P3">P3 — Low</option>
              </select>
              <textarea
                placeholder="Acceptance criteria (one per line)"
                value={manualAC}
                onChange={e => setManualAC(e.target.value)}
                rows={3}
                className="w-full px-3 py-2 rounded-lg text-sm mb-3 outline-none resize-none"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => setAddManualOpen(false)}
                  className="px-3 py-1.5 rounded-lg text-xs"
                  style={{ background: '#1e1e3a', color: '#94a3b8' }}
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveManual}
                  disabled={!manualTitle.trim()}
                  className="px-3 py-1.5 rounded-lg text-xs font-medium"
                  style={{
                    background: '#10b981',
                    color: '#fff',
                    opacity: manualTitle.trim() ? 1 : 0.4,
                  }}
                >
                  Save
                </button>
              </div>
            </div>
          )}

          {/* Loading State */}
          {loading && (
            <div className="flex items-center justify-center py-12">
              <div className="w-6 h-6 border-2 border-t-transparent rounded-full animate-spin"
                   style={{ borderColor: '#6366f1', borderTopColor: 'transparent' }} />
            </div>
          )}

          {/* Empty State */}
          {!loading && filteredReqs.length === 0 && (
            <div className="rounded-xl p-6 text-center" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <div className="text-3xl mb-3">{'\u{1F4CB}'}</div>
              <p className="text-sm" style={{ color: '#64748b' }}>
                No requirements ingested yet — upload a document or sync from Jira to get started
              </p>
            </div>
          )}

          {/* Requirements Cards */}
          {filteredReqs.map(req => {
            const id = getReqId(req)
            const source = getSource(req)
            const changeStatus = getChangeStatus(req)
            const coverage = getCoverage(req, reqTests)
            const acCount = getACCount(req)
            const isSelected = selectedReqId === id
            const srcMeta = SOURCE_META[source]
            const changeMeta = CHANGE_DOT[changeStatus]

            let leftBorder = '#1e1e3a'
            if (isSelected) leftBorder = '#6366f1'
            else if (coverage === 0) leftBorder = '#fb7185'
            else if (coverage < 80) leftBorder = '#f59e0b'

            return (
              <button
                key={id}
                onClick={() => setSelectedReqId(id)}
                className="w-full text-left rounded-xl p-3.5 transition-all"
                style={{
                  background: isSelected ? '#1a1a2e' : '#12121f',
                  border: '1px solid #1e1e3a',
                  borderLeft: `4px solid ${leftBorder}`,
                }}
              >
                {/* Row 1: ID + Priority */}
                <div className="flex items-center justify-between mb-1.5">
                  <span className="font-mono font-bold text-sm" style={{ color: '#06b6d4' }}>{id}</span>
                  <span className="flex items-center gap-1.5">
                    {(() => {
                      const q = getQuality(req)
                      const bm = q && BAND_META[q.band]
                      if (!q || !bm) return null
                      return (
                        <span
                          className="text-[10px] font-semibold px-2 py-0.5 rounded-full"
                          title={`clarity ${q.score}/100${q.flags?.length ? ` — ${q.flags.slice(0, 3).join('; ')}` : ''}`}
                          style={{ background: `${bm.color}18`, color: bm.color }}
                        >
                          {bm.label}
                        </span>
                      )
                    })()}
                    <span
                      className="text-[10px] font-semibold px-2 py-0.5 rounded-full"
                      style={{ background: `${PRIORITY_COLORS[req.priority]}22`, color: PRIORITY_COLORS[req.priority] }}
                    >
                      {req.priority}
                    </span>
                  </span>
                </div>

                {/* Row 2: Title */}
                <p className="text-sm mb-2 line-clamp-2" style={{ color: '#cbd5e1' }}>{req.title}</p>

                {/* Row 3: Source + Change + Coverage + ACs */}
                <div className="flex items-center gap-2 flex-wrap">
                  <span
                    className="inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full"
                    style={{ background: `${srcMeta.color}18`, color: srcMeta.color }}
                  >
                    {srcMeta.icon} {srcMeta.label}
                  </span>
                  <span
                    className="w-2 h-2 rounded-full inline-block"
                    title={changeMeta.label}
                    style={{ background: changeMeta.color }}
                  />
                  <span className="text-[10px]" style={{ color: '#64748b' }}>{coverage}%</span>
                  <span className="text-[10px]" style={{ color: '#64748b' }}>{acCount} ACs</span>
                </div>
              </button>
            )
          })}
        </div>

        {/* ── Right Panel: Traceability Detail ────────────────────────────── */}
        <div className="flex-1 overflow-y-auto" style={{ maxHeight: 'calc(100vh - 280px)' }}>
          {!selectedReq ? (
            /* Default placeholder */
            <div className="flex flex-col items-center justify-center h-full rounded-xl"
                 style={{ background: '#12121f', border: '1px solid #1e1e3a', minHeight: 400 }}>
              <div className="text-5xl mb-4 opacity-30">{'\u{1F50D}'}</div>
              <p className="text-sm" style={{ color: '#64748b' }}>
                Select a requirement from the list to view its traceability chain
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              {/* ── A. Header Bar ──────────────────────────────────────────── */}
              <div className="rounded-xl p-5 sticky top-0 z-10"
                   style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                {editMode ? (
                  /* Edit form */
                  <div className="space-y-3">
                    <input
                      value={editTitle}
                      onChange={e => setEditTitle(e.target.value)}
                      className="w-full px-3 py-2 rounded-lg text-sm outline-none font-semibold"
                      style={{ background: '#0a0a14', border: '1px solid #6366f1', color: '#e2e8f0' }}
                    />
                    <textarea
                      value={editDesc}
                      onChange={e => setEditDesc(e.target.value)}
                      rows={3}
                      className="w-full px-3 py-2 rounded-lg text-sm outline-none resize-none"
                      style={{ background: '#0a0a14', border: '1px solid #6366f1', color: '#e2e8f0' }}
                    />
                    <div className="flex gap-2 justify-end">
                      <button onClick={() => setEditMode(false)}
                              className="px-3 py-1.5 rounded-lg text-xs"
                              style={{ background: '#1e1e3a', color: '#94a3b8' }}>
                        Cancel
                      </button>
                      <button onClick={handleFixExtraction}
                              className="px-3 py-1.5 rounded-lg text-xs font-medium"
                              style={{ background: '#6366f1', color: '#fff' }}>
                        Save Changes
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    {/* Line 1 */}
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-3">
                        <span className="font-mono font-bold text-lg" style={{ color: '#06b6d4' }}>
                          {getReqId(selectedReq)}
                        </span>
                        <span className="text-xs font-semibold px-2.5 py-0.5 rounded-full"
                              style={{
                                background: `${PRIORITY_COLORS[selectedReq.priority]}22`,
                                color: PRIORITY_COLORS[selectedReq.priority],
                              }}>
                          {selectedReq.priority}
                        </span>
                        {/* Risk badge */}
                        {(() => {
                          const rs = selectedReq.risk_score
                          const rColor = rs >= 8 ? '#fb7185' : rs >= 6 ? '#f59e0b' : '#10b981'
                          const rLabel = rs >= 8 ? 'AUTO-FAIL' : rs >= 6 ? 'MITIGATE' : `Risk ${rs}`
                          return (
                            <span className="text-[10px] font-bold px-2 py-0.5 rounded-full"
                                  style={{ background: `${rColor}18`, color: rColor }}>
                              {rLabel}
                            </span>
                          )
                        })()}
                        {/* Clarity band */}
                        {(() => {
                          const q = getQuality(selectedReq)
                          const bm = q && BAND_META[q.band]
                          if (!q || !bm) return null
                          return (
                            <span className="text-[10px] font-bold px-2 py-0.5 rounded-full"
                                  title={`clarity ${q.score}/100${q.flags?.length ? ` — ${q.flags.slice(0, 3).join('; ')}` : ''}`}
                                  style={{ background: `${bm.color}18`, color: bm.color }}>
                              clarity: {bm.label} {q.score}
                            </span>
                          )
                        })()}
                      </div>
                      <div className="flex items-center gap-2">
                        {/* R320 refinement entry, pre-scoped to this requirement —
                            the remediation path for weak/unclear bands. */}
                        {getQuality(selectedReq) && getQuality(selectedReq)!.band !== 'clear' && (
                          <a href={`/ai-assistant?requirement_id=${encodeURIComponent(getReqId(selectedReq))}`}
                             className="px-3 py-1.5 rounded-lg text-xs font-medium transition"
                             title="Refine this requirement's acceptance criteria — corrections become durable grounding"
                             style={{ background: 'transparent', border: '1px solid rgba(99,102,241,0.4)', color: '#a5b4fc' }}>
                            {'✨'} Refine with AI
                          </a>
                        )}
                        <button onClick={handleFixExtraction}
                                className="px-3 py-1.5 rounded-lg text-xs font-medium"
                                style={{ background: '#1e1e3a', color: '#94a3b8', border: '1px solid #2a2a4a' }}>
                          Fix Extraction
                        </button>
                        {(() => {
                          // Server-side in-flight detection — banner stays correct
                          // even after a page reload while the workflow is still running.
                          const serverInflight = inFlightForReq(selectedReqId)
                          const isGenerating = generating || !!serverInflight
                          const elapsedSuffix = serverInflight
                            ? ` ${Math.floor(serverInflight.started_seconds_ago / 60)}m${serverInflight.started_seconds_ago % 60}s`
                            : ''
                          return (
                        <>
                        <button onClick={() => handleGenerateTests(false)}
                                disabled={isGenerating}
                                className="px-4 py-1.5 rounded-lg text-xs font-medium transition"
                                style={{
                                  background: isGenerating
                                    ? '#4338ca'
                                    : 'linear-gradient(135deg, #6366f1, #8b5cf6)',
                                  color: '#fff',
                                }}
                                title={isGenerating
                                  ? `Generation in progress${serverInflight ? ` — workflow ${serverInflight.workflow_id} (${serverInflight.started_seconds_ago}s elapsed)` : ''}`
                                  : 'Generate tests (existing tests preserved if requirement unchanged).'}>
                          {isGenerating
                            ? `⧗ Generating…${elapsedSuffix}`
                            : 'Generate Tests'}
                        </button>
                        <button onClick={() => handleGenerateTests(true)}
                                disabled={isGenerating}
                                className="ml-2 px-4 py-1.5 rounded-lg text-xs font-medium transition"
                                style={{
                                  background: isGenerating
                                    ? '#7f1d1d'
                                    : 'linear-gradient(135deg, #f43f5e, #fb7185)',
                                  color: '#fff',
                                }}
                                title="Discard ALL existing tests for this requirement and regenerate from scratch.">
                          ↻ Force Regenerate
                        </button>
                        </>
                          )
                        })()}
                      </div>
                    </div>

                    {/* Fix E: Newman missing-path-params banner. Verified live in
                        run-170e18: 25 unresolved vars produced 400 silent SKIPs.
                        Surface them here with one-click bulk-add to current env. */}
                    {generatedResult?.missing_path_params && generatedResult.missing_path_params.length > 0 && (() => {
                      const missing: string[] = generatedResult.missing_path_params
                      // Architecture page doesn't track an active env (the run-suite
                      // selector lives on Test Explorer). Default to "staging" — the
                      // Settings → Environments after the bulk-add lands.
                      const envForAdd = 'staging'
                      return (
                        <div className="mt-3 rounded-lg p-3" style={{
                          background: 'rgba(245,158,11,0.1)',
                          border: '1px solid #f59e0b55',
                        }}>
                          <div className="text-xs font-medium mb-1" style={{ color: '#fbbf24' }}>
                            \u26a0 {missing.length} unresolved path-param(s) \u2014 these tests will SKIP at runtime
                          </div>
                          <div className="text-[10px] mb-2" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
                            {missing.slice(0, 12).join(', ')}{missing.length > 12 ? `, \u2026 (+${missing.length - 12} more)` : ''}
                          </div>
                          <button
                            onClick={async () => {
                              if (!currentProjectId) return
                              try {
                                const res = await bulkAddEnvironmentVariables(currentProjectId, envForAdd, missing)
                                toast.success(`Added ${res.added.length} blank variable(s) to ${envForAdd} (skipped ${res.skipped.length} already-existing). Open Settings \u2192 Environments to fill in real values.`)
                              } catch (err: any) {
                                toast.error(err?.message || 'Bulk-add failed')
                              }
                            }}
                            className="px-3 py-1 rounded text-[10px] font-medium"
                            style={{ background: '#f59e0b', color: '#0a0a14' }}
                          >
                            + Add all {missing.length} to {envForAdd} variables
                          </button>
                        </div>
                      )
                    })()}

                    {/* In-flight generation banner \u2014 server-driven, survives page reload */}
                    {(() => {
                      const inflight = inFlightForReq(selectedReqId)
                      if (!inflight) return null
                      const m = Math.floor(inflight.started_seconds_ago / 60)
                      const s = inflight.started_seconds_ago % 60
                      const target = inflight.ac_id
                        ? `${inflight.requirement_id}/${inflight.ac_id}`
                        : inflight.requirement_id
                      return (
                        <div className="mt-3 rounded-lg p-3 flex items-center gap-3"
                             style={{ background: 'rgba(99,102,241,0.08)', border: '1px solid #6366f155' }}>
                          <span className="animate-spin text-lg" style={{ color: '#a5b4fc' }}>\u29d7</span>
                          <div className="flex-1">
                            <div className="text-xs font-medium" style={{ color: '#a5b4fc' }}>
                              Generating tests for {target}
                            </div>
                            <div className="text-[10px] mt-0.5" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
                              workflow {inflight.workflow_id} \u00b7 {m}m {s}s elapsed \u00b7 typically completes in 3-13 min
                            </div>
                          </div>
                        </div>
                      )
                    })()}

                    {/* Generated Test Results */}
                    {generatedResult && (
                      <div className="mt-4 rounded-lg p-4" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-xs font-medium" style={{ color: '#10b981' }}>
                            {'\u2713'} {generatedResult.test_count} scenarios generated
                          </span>
                          <span className="text-[10px]" style={{ color: '#94a3b8' }}>
                            {generatedResult.scripts?.length || 0} scripts
                          </span>
                        </div>
                        {generatedResult.gherkin_scenarios?.map((g: string, i: number) => (
                          <details key={i} className="mt-2">
                            <summary className="text-xs cursor-pointer" style={{ color: '#94a3b8' }}>
                              Scenario {i + 1}
                            </summary>
                            <div className="mt-1">
                              <GherkinHighlighter code={g} maxHeight={300} />
                            </div>
                          </details>
                        ))}
                      </div>
                    )}

                    {/* Line 2 */}
                    <div className="flex items-center gap-3 text-xs" style={{ color: '#64748b' }}>
                      {(() => {
                        const src = getSource(selectedReq)
                        const meta = SOURCE_META[src]
                        return (
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full"
                                style={{ background: `${meta.color}18`, color: meta.color }}>
                            {meta.icon} {meta.label}
                          </span>
                        )
                      })()}
                      <span>Confidence: {(selectedReq as any).confidence ?? 92}%</span>
                      <span>Ingested: {(selectedReq as any).ingested_at ? new Date((selectedReq as any).ingested_at).toLocaleDateString() : 'Mar 10, 2026'}</span>
                    </div>

                    {/* Title */}
                    <p className="text-sm mt-3" style={{ color: '#e2e8f0' }}>{selectedReq.title}</p>
                    {selectedReq.description && (
                      <p className="text-xs mt-1" style={{ color: '#64748b' }}>{selectedReq.description}</p>
                    )}
                  </>
                )}
              </div>

              {/* ── B. Source Provenance Card ──────────────────────────────── */}
              {(() => {
                const src = getSource(selectedReq)
                const meta = SOURCE_META[src]
                const confidence = (selectedReq as any).confidence ?? 92
                return (
                  <div className="rounded-xl p-4" style={{
                    background: '#12121f',
                    border: '1px solid #1e1e3a',
                    borderTop: `4px solid ${meta.color}`,
                  }}>
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-lg">{meta.icon}</span>
                      <span className="text-sm font-medium" style={{ color: '#e2e8f0' }}>
                        Source: {meta.label}
                      </span>
                    </div>
                    <p className="text-xs mb-2" style={{ color: '#64748b' }}>
                      {src === 'jira' ? 'Jira Cloud' : src === 'document' ? 'requirements.docx' : 'Manual entry'}
                    </p>
                    {/* Confidence bar */}
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-[10px]" style={{ color: '#64748b' }}>Confidence</span>
                      <div className="flex-1 h-1.5 rounded-full" style={{ background: '#1e1e3a' }}>
                        <div className="h-full rounded-full" style={{
                          width: `${confidence}%`,
                          background: meta.color,
                        }} />
                      </div>
                      <span className="text-[10px] font-mono" style={{ color: meta.color }}>{confidence}%</span>
                    </div>
                    <p className="text-[10px] mt-1" style={{ color: '#94a3b8' }}>
                      Ingested {(selectedReq as any).ingested_at ? new Date((selectedReq as any).ingested_at).toLocaleDateString() : 'Mar 10, 2026'}
                      {' \u00B7 Last synced '}
                      {(selectedReq as any).last_synced ? new Date((selectedReq as any).last_synced).toLocaleDateString() : 'Mar 15, 2026'}
                    </p>
                  </div>
                )
              })()}

              {/* ── C. Coverage Status Banner ─────────────────────────────── */}
              {(() => {
                const coverage = getCoverage(selectedReq, reqTests)
                const acCount = getACCount(selectedReq)
                const coveredCount = selectedReq.acceptance_criteria?.filter(ac => ac.covered).length ?? Math.round(acCount * coverage / 100)
                let bannerBg: string, bannerBorder: string, bannerText: string, bannerColor: string
                if (coverage === 0) {
                  bannerBg = 'rgba(251,113,133,0.08)'
                  bannerBorder = '#7f1d1d'
                  bannerText = `0% covered \u2014 no tests generated yet`
                  bannerColor = '#fb7185'
                } else if (coverage < 80) {
                  bannerBg = 'rgba(245,158,11,0.08)'
                  bannerBorder = '#78350f'
                  bannerText = `${coverage}% covered \u2014 ${coveredCount} of ${acCount} acceptance criteria have tests`
                  bannerColor = '#f59e0b'
                } else {
                  bannerBg = 'rgba(16,185,129,0.08)'
                  bannerBorder = '#065f46'
                  bannerText = `${coverage}% covered \u2014 ${coveredCount} of ${acCount} acceptance criteria have tests`
                  bannerColor = '#10b981'
                }
                return (
                  <div className="rounded-xl p-4" style={{ background: bannerBg, border: `1px solid ${bannerBorder}` }}>
                    <p className="text-sm font-medium mb-2" style={{ color: bannerColor }}>{bannerText}</p>
                    <div className="h-2 rounded-full" style={{ background: '#1e1e3a' }}>
                      <div className="h-full rounded-full transition-all" style={{
                        width: `${Math.min(coverage, 100)}%`,
                        background: bannerColor,
                      }} />
                    </div>
                  </div>
                )
              })()}

              {/* ── D. Stale Test Warning ──────────────────────────────────── */}
              {getChangeStatus(selectedReq) === 'modified' && getCoverage(selectedReq, reqTests) > 0 && (
                <div className="rounded-xl p-4 flex items-start gap-3"
                     style={{ background: 'rgba(245,158,11,0.08)', border: '1px solid #78350f' }}>
                  <span className="text-lg">{'\u26A0\uFE0F'}</span>
                  <div className="flex-1">
                    <p className="text-sm font-medium mb-1" style={{ color: '#fbbf24' }}>
                      Tests may be outdated
                    </p>
                    <p className="text-xs mb-2" style={{ color: '#94a3b8' }}>
                      This requirement was modified after tests were generated — tests may need to be re-generated.
                    </p>
                    <button
                      onClick={() => handleGenerateTests()}
                      className="px-3 py-1.5 rounded-lg text-xs font-medium"
                      style={{ background: '#f59e0b22', border: '1px solid #f59e0b44', color: '#fbbf24' }}
                    >
                      Re-generate Tests
                    </button>
                  </div>
                </div>
              )}

              {/* ── ATDD Split View (Requirement → Generated Tests) ──────── */}
              {reqTests.length > 0 && reqTests.some((t: any) => t.gherkin_scenario) && (
                <ATDDSplitView
                  // F7-3: Requirement.id is optional in the API type but the prop
                  // expects it required. selectedReq is only set after we verify
                  // an id exists, so coerce here rather than make every Requirement
                  // type narrowly require id (which would break list rendering paths).
                  requirement={{ ...selectedReq, id: selectedReq.id || '' }}
                  gherkin={reqTests.filter((t: any) => t.gherkin_scenario).map((t: any) => t.gherkin_scenario).join('\n\n')}
                  scriptContent={reqTests[0]?.script_content || ''}
                />
              )}

              {/* ── E. Acceptance Criteria -> Test Cases Tree ──────────────── */}
              <div className="space-y-3">
                <h3 className="text-sm font-semibold" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
                  Acceptance Criteria &rarr; Test Cases
                </h3>

                {(!selectedReq.acceptance_criteria || selectedReq.acceptance_criteria.length === 0) && (
                  <div className="rounded-xl p-4 text-center" style={{ background: '#12121f', border: '1px dashed #f59e0b44' }}>
                    <p className="text-sm" style={{ color: '#64748b' }}>
                      No acceptance criteria defined for this requirement
                    </p>
                  </div>
                )}

                {selectedReq.acceptance_criteria?.map(ac => {
                  const tests = getTestsForAC(ac.id)
                  const hasTests = tests.length > 0
                  const allPass = hasTests && tests.every(t => t.status === 'PASS')
                  const anyFail = hasTests && tests.some(t => t.status === 'FAIL')

                  let borderColor = '#f59e0b' // no tests
                  let borderStyle = 'dashed'
                  if (hasTests && allPass) { borderColor = '#10b981'; borderStyle = 'solid' }
                  else if (hasTests && anyFail) { borderColor = '#fb7185'; borderStyle = 'solid' }
                  else if (hasTests) { borderColor = '#10b981'; borderStyle = 'solid' }

                  return (
                    <div
                      key={ac.id}
                      className="rounded-xl p-4"
                      style={{
                        background: '#12121f',
                        border: `1px solid #1e1e3a`,
                        borderLeft: `4px ${borderStyle} ${borderColor}`,
                      }}
                    >
                      {/* AC Header */}
                      <div className="flex items-center justify-between mb-3">
                        <div className="flex items-center gap-2 flex-1 min-w-0">
                          <span className="font-mono text-xs font-bold flex-shrink-0" style={{ color: '#06b6d4' }}>
                            {ac.id}
                          </span>
                          <span className="text-sm truncate" style={{ color: '#e2e8f0' }}>{ac.statement}</span>
                          {getQuality(selectedReq)?.ac_flags?.includes(ac.id) && (
                            <span className="text-[10px] font-medium px-2 py-0.5 rounded-full flex-shrink-0"
                                  title="No measurable/observable criterion in this AC — generated assertions stay read-side and never invent thresholds"
                                  style={{ background: '#fb718518', color: '#fda4af' }}>
                              not measurable
                            </span>
                          )}
                        </div>
                        <span
                          className="text-[10px] font-medium px-2 py-0.5 rounded-full flex-shrink-0 ml-2"
                          style={{
                            background: hasTests ? '#10b98118' : '#f59e0b18',
                            color: hasTests ? '#6ee7b7' : '#fbbf24',
                          }}
                        >
                          {hasTests ? `${tests.length} test${tests.length > 1 ? 's' : ''}` : '\u26A0 no tests'}
                        </span>
                      </div>

                      {/* Test Cases */}
                      {hasTests ? (
                        <div className="space-y-1.5 ml-3">
                          {tests.map(tc => {
                            const tcId = tc.id || ''
                            const statusColor = tc.status === 'PASS' ? '#10b981' : tc.status === 'FAIL' ? '#fb7185' : '#f59e0b'
                            const statusIcon = tc.status === 'PASS' ? '\u2713' : tc.status === 'FAIL' ? '\u2717' : '\u2013'
                            const toolColor = TOOL_COLORS[(tc.tool || 'playwright').toLowerCase()] ?? '#64748b'
                            return (
                              <a key={tcId} href={`/test-explorer?test_id=${tcId}`}
                                 className="flex items-center gap-2 py-1 px-2 rounded-lg hover:bg-white/[0.04] cursor-pointer transition"
                                 title={`Open ${tcId} in Test Explorer`}
                                 onClick={(e) => { if ((e.target as HTMLElement).closest('button')) e.preventDefault() }}>
                                <span className="w-4 h-4 rounded-full flex items-center justify-center text-[10px] font-bold"
                                      style={{ background: `${statusColor}22`, color: statusColor }}>
                                  {statusIcon}
                                </span>
                                <span className="font-mono text-[10px] font-medium" style={{ color: '#64748b' }}>
                                  {tcId}
                                </span>
                                <span className="text-xs flex-1 truncate" style={{ color: '#94a3b8' }}>
                                  {tc.title}
                                </span>
                                <span className="text-[10px] font-medium px-1.5 py-0.5 rounded"
                                      style={{ background: `${toolColor}18`, color: toolColor }}>
                                  {tc.tool || 'Unknown'}
                                </span>
                                {tc.status === 'FAIL' && (
                                  <button
                                    onClick={async (e) => {
                                      e.preventDefault()
                                      e.stopPropagation()
                                      try {
                                        await requestHeal(tcId, '', undefined, currentProjectId || undefined, tc.title)
                                        toast.success('Healing analysis requested')
                                      } catch { toast.error('Heal request failed') }
                                    }}
                                    className="text-[9px] px-1.5 py-0.5 rounded font-medium flex-shrink-0"
                                    style={{ background: '#f59e0b18', color: '#fbbf24', border: '1px solid #f59e0b30' }}
                                  >
                                    Heal
                                  </button>
                                )}
                                <span className="text-[10px]" style={{ color: '#94a3b8' }}>{'\u2192'}</span>
                              </a>
                            )
                          })}
                        </div>
                      ) : (() => {
                          // Combine local optimistic state (just-clicked) with server-side
                          // poll state (survives page reload). EITHER says "generating"
                          // means the AC card should show in-progress UI.
                          const local = generatingACs.has(ac.id)
                          const server = isAcInFlight(selectedReqId, ac.id)
                          const generatingThisAC = local || server
                          const inflight = serverInFlight.get(`${selectedReqId}::${ac.id}`)
                            ?? serverInFlight.get(`${selectedReqId}::*`)
                          return (
                        <div className="ml-3 py-3 text-center rounded-lg" style={{
                          border: generatingThisAC
                            ? '1px solid #6366f1'
                            : '1px dashed #fb718544',
                          background: generatingThisAC ? 'rgba(99,102,241,0.06)' : 'transparent',
                        }}>
                          <p className="text-xs mb-2" style={{
                            color: generatingThisAC ? '#a5b4fc' : '#fb7185',
                          }}>
                            {generatingThisAC
                              ? (inflight
                                  ? `Generating tests for ${inflight.ac_id ?? ac.id} — workflow ${inflight.workflow_id} (${Math.floor(inflight.started_seconds_ago / 60)}m ${inflight.started_seconds_ago % 60}s elapsed)`
                                  : `Generating tests for ${ac.id} — typically 3-5 min`)
                              : 'No automation coverage — coverage gap'}
                          </p>
                          <button
                            disabled={generatingThisAC}
                            onClick={async () => {
                              if (!selectedReqId) return
                              if (generatingACs.has(ac.id)) return  // prevent double-click
                              const forcedSuffix = forceRegen ? ' (forced)' : ''
                              setGeneratingACs(prev => new Set(prev).add(ac.id))
                              toast.info(`Generating tests for ${ac.id}${forcedSuffix} — this can take 3-5 min for P0 reqs`)
                              try {
                                // Direct /api/tests/generate call with ac_id + force, replacing
                                // the old /api/assistant/command dispatcher path which silently
                                // dropped ac_id (regenerated the whole requirement instead).
                                const result = await generateTests(
                                  selectedReqId,
                                  undefined,
                                  forceRegen,
                                  ac.id,
                                )
                                toast.success(`Generated ${result.test_count} test(s) for ${ac.id}${forcedSuffix}`)
                                // Refresh AC coverage badge + test list
                                const [reqData, testData] = await Promise.all([
                                  fetchRequirements({ project_id: currentProjectId || undefined }),
                                  fetchTests({ priority: undefined, tool: undefined, project_id: currentProjectId || undefined }),
                                ])
                                setRequirements(reqData.requirements)
                                setReqTests(testData.tests)
                              } catch (err: any) {
                                // 409 dedup: another generation is already running for this AC
                                if (err?.status === 409 && err?.detail?.error === 'generation_in_flight') {
                                  const wf = err.detail.workflow_id
                                  const ago = err.detail.started_seconds_ago ?? 0
                                  toast.info(`Already generating ${ac.id} (workflow ${wf}, ${ago}s in flight)`)
                                } else {
                                  toast.error(err?.message || `Generation failed for ${ac.id}`)
                                }
                              } finally {
                                setGeneratingACs(prev => {
                                  const n = new Set(prev)
                                  n.delete(ac.id)
                                  return n
                                })
                              }
                            }}
                            className="px-3 py-1 rounded-lg text-[10px] font-medium disabled:opacity-60 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
                            style={{
                              background: generatingACs.has(ac.id)
                                ? '#6366f1'
                                : forceRegen ? '#fb718522' : '#6366f122',
                              color: generatingACs.has(ac.id)
                                ? '#fff'
                                : forceRegen ? '#fda4af' : '#a5b4fc',
                              border: `1px solid ${forceRegen ? '#fb718544' : '#6366f144'}`,
                            }}
                          >
                            {generatingThisAC ? (
                              <>
                                <span className="animate-spin" style={{ fontSize: 10 }}>{'⧗'}</span>
                                Generating…
                              </>
                            ) : forceRegen ? '↻ Force Regenerate this AC' : 'Generate Tests for this AC'}
                          </button>
                        </div>
                          )
                        })()}
                    </div>
                  )
                })}
              </div>

              {/* ── F. Change Log Timeline ─────────────────────────────────── */}
              <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <button
                  onClick={() => setChangeLogOpen(!changeLogOpen)}
                  className="w-full flex items-center justify-between px-5 py-3 text-sm font-medium"
                  style={{ color: '#94a3b8' }}
                >
                  <span>Change History ({changeLog.length} entries)</span>
                  <span style={{ transform: changeLogOpen ? 'rotate(180deg)' : 'rotate(0)', transition: 'transform 0.2s' }}>
                    {'\u25BC'}
                  </span>
                </button>

                {changeLogOpen && (
                  <div className="px-5 pb-5">
                    {changeLog.length === 0 ? (
                      <p className="text-xs py-4" style={{ color: '#64748b' }}>No changes recorded.</p>
                    ) : (
                      <div className="relative ml-3 pl-6 space-y-4"
                           style={{ borderLeft: '2px solid #6366f133' }}>
                        {changeLog
                          .sort((a, b) => b.version - a.version)
                          .map((entry, idx) => {
                            const trigger = entry.trigger || 'manual'
                            const icon = TRIGGER_ICONS[trigger] ?? '\u{1F4DD}'
                            return (
                              <div key={idx} className="relative">
                                {/* Timeline dot */}
                                <div
                                  className="absolute -left-[31px] top-0.5 w-3 h-3 rounded-full border-2"
                                  style={{ background: '#12121f', borderColor: '#6366f1' }}
                                />
                                <div className="text-[10px] font-mono mb-0.5" style={{ color: '#94a3b8' }}>
                                  {new Date(entry.changed_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                                </div>
                                <div className="flex items-center gap-2">
                                  <span>{icon}</span>
                                  <span className="text-xs flex-1" style={{ color: '#94a3b8' }}>{entry.summary}</span>
                                  <span className="text-[10px] font-mono px-1.5 py-0.5 rounded"
                                        style={{ background: '#6366f118', color: '#a5b4fc' }}>
                                    v{entry.version}
                                  </span>
                                </div>
                              </div>
                            )
                          })}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Generation Results Modal */}
      {jobResultsOpen && (
        <GenerateAllResultsModal
          open={jobResultsOpen}
          onClose={() => setJobResultsOpen(false)}
          job={lastJob}
          onRetry={handleRetry}
          onRegenerate={handleRegenerate}
        />
      )}

      {/* R54.3 — Regenerate by Tool modal. R57.11 — defaultTool is set
          when the operator clicks the inventory-empty banner; cleared
          when the modal closes. */}
      <RegenerateByToolModal
        open={regenByToolOpen}
        onClose={() => {
          setRegenByToolOpen(false)
          setBannerToolToRegen(null)
        }}
        projectId={currentProjectId || ''}
        requirements={requirements}
        defaultRequirementId={selectedReqId ?? undefined}
        defaultTool={bannerToolToRegen ?? undefined}
        onSubmit={handleRegenByTool}
        running={regenByToolRunning}
      />

      {/* WS4 — Execute by Tool modal (run-side counterpart). */}
      <ExecuteByToolModal
        open={execByToolOpen}
        onClose={() => setExecByToolOpen(false)}
        projectId={currentProjectId || ''}
        requirements={requirements}
        defaultRequirementId={selectedReqId ?? undefined}
        onSubmit={handleExecByTool}
        running={execByToolRunning}
      />
    </div>
  )
}
