'use client'

import { useEffect, useRef, useState } from 'react'
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

import { fetchDashboardTrends, fetchRuns, exportCoverageReport, exportComplianceReport, type DashboardTrends, type TestRun } from '@/lib/api-client'
import { useProject } from '@/lib/project-context'
import { authHeaders } from '@/lib/auth-state'
import { useToast } from '@/components/ui/Toast'
import RunPipelineModal from '@/components/RunPipelineModal'
import { sutVerdict, attributablePassRate, a11yVerdict, actionableCtas, type MissionReport } from '@/lib/missionReport'

const API = ''
const _TONE_COLOR: Record<string, string> = { good: '#34d399', warn: '#fbbf24', bad: '#fb7185', muted: '#94a3b8' }

type HealthData = { status: string; platform: string; methodology: string } | null
// F5-9: Aligned with backend dashboard() response — adds the 4 priority-rollup
// fields the UI was already trying to read via `data?.p0_count` etc. Without
// these in the type, the dashboard rendered TS2339 errors hidden by
// next.config.js `ignoreBuildErrors: true`.
type DashboardData = {
  quality_score: number
  coverage_pct: number
  pass_rate: number
  open_defects: number
  p0_defects: number
  total_scenarios: number
  active_agents: string[]
  pipeline: { build_id: string; stages: { name: string; status: string; duration_s?: number }[] }
  p0_count?: number | null
  p1_count?: number | null
  p0_coverage_pct?: number | null
  p1_coverage_pct?: number | null
} | null

const STATUS_COLOR: Record<string, string> = {
  done:   'bg-emerald-500',
  active: 'bg-indigo-500 animate-pulse',
  wait:   'bg-slate-600',
  fail:   'bg-rose-500',
}

const GATE_COLORS: Record<string, string> = {
  PASS:     '#34d399',
  CONCERNS: '#fbbf24',
  FAIL:     '#fb7185',
  WAIVED:   '#a78bfa',
}

function getLetterGrade(score: number): { letter: string; color: string } {
  if (score >= 90) return { letter: 'A', color: '#34d399' }
  if (score >= 80) return { letter: 'B', color: '#6366f1' }
  if (score >= 70) return { letter: 'C', color: '#fbbf24' }
  if (score >= 60) return { letter: 'D', color: '#f97316' }
  return { letter: 'F', color: '#fb7185' }
}

type AgentEvent = {
  type: string
  agent?: string
  status?: string
  message?: string
  timestamp?: string
}

/** Custom dot for quality trend — highlights regression drops >5% */
function RegressionDot(props: any) {
  const { cx, cy, index, payload } = props
  if (!payload || index === undefined) return null
  const dataKey = props.dataKey as string
  const data = props.data as { day: string; quality: number; coverage: number }[] | undefined
  if (!data || index === 0) return null

  const prev = (data[index - 1] as any)?.[dataKey]
  const curr = (payload as any)?.[dataKey]
  if (prev == null || curr == null) return null

  const drop = prev - curr
  if (drop > 5) {
    return (
      <g>
        <circle cx={cx} cy={cy} r={6} fill="#fb7185" fillOpacity={0.3} stroke="#fb7185" strokeWidth={2} />
        <circle cx={cx} cy={cy} r={2.5} fill="#fb7185" />
        <text x={cx} y={cy - 12} textAnchor="middle" fill="#fb7185" fontSize={9} fontFamily="Space Mono, monospace">
          -{drop.toFixed(0)}%
        </text>
      </g>
    )
  }
  return null
}

// ── CI/CD Pipeline Stages ────────────────────────────────────────────────────

const CICD_STAGES = [
  { key: 'build',    label: 'Build',    icon: '\u2699' },
  { key: 'ai_gen',   label: 'AI Gen',   icon: '\u25C8' },
  { key: 'execute',  label: 'Execute',  icon: '\u25B6' },
  { key: 'security', label: 'Security', icon: '\uD83D\uDD12' },
  { key: 'perf',     label: 'Perf',     icon: '\u26A1' },
  { key: 'gate',     label: 'Gate',     icon: '\u25C6' },
  { key: 'deploy',   label: 'Deploy',   icon: '\u2B06' },
]

const CICD_STATUS_COLORS: Record<string, string> = {
  completed: '#10b981',
  active:    '#06b6d4',
  waiting:   '#334155',
  failed:    '#ef4444',
}

function mapPipelineToCICD(stages: { name: string; status: string; duration_s?: number }[]): { key: string; status: string; duration: string }[] {
  // Map TEA pipeline stages to 7-stage CI/CD model
  const teaStatusMap: Record<string, string> = {}
  stages.forEach(s => {
    const normalized = s.status === 'done' ? 'completed' : s.status === 'active' ? 'active' : s.status === 'fail' ? 'failed' : 'waiting'
    teaStatusMap[s.name.toLowerCase()] = normalized
    if (s.duration_s) teaStatusMap[`${s.name.toLowerCase()}_dur`] = `${s.duration_s}s`
  })

  // Derive CICD stage status from TEA stages
  const stageMapping: Record<string, string[]> = {
    build:    ['requirement intelligence', 'requirements'],
    ai_gen:   ['risk-based strategy', 'atdd design', 'risk scoring'],
    execute:  ['automation engineering', 'automation', 'test execution', 'execution'],
    security: ['non-functional validation', 'nfr'],
    perf:     ['evidence collection', 'evidence'],
    gate:     ['quality gate', 'gate', 'traceability'],
    deploy:   ['deploy'],
  }

  return CICD_STAGES.map(stage => {
    const mappedNames = stageMapping[stage.key] || []
    let status = 'waiting'
    let duration = '--'

    for (const name of mappedNames) {
      if (teaStatusMap[name]) {
        status = teaStatusMap[name]
        if (teaStatusMap[`${name}_dur`]) duration = teaStatusMap[`${name}_dur`]
        break
      }
    }

    return { key: stage.key, status, duration }
  })
}

function CICDPipelineCard({ data, latestRun }: { data: NonNullable<DashboardData>; latestRun: TestRun | null }) {
  const pipelineStages = data?.pipeline?.stages ?? []
  const cicdStages = mapPipelineToCICD(pipelineStages)
  const buildId = latestRun?.build_id || data?.pipeline?.build_id || 'Build #487'

  return (
    <div className="rounded-xl p-6 col-span-2" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <h2 className="font-semibold" style={{ color: '#94a3b8' }}>CI/CD Pipeline</h2>
          <span className="text-xs px-2 py-0.5 rounded-lg font-mono"
                style={{ background: 'rgba(99,102,241,0.15)', color: '#818cf8', border: '1px solid #6366f130' }}>
            {buildId}
          </span>
        </div>
        <span className="text-[10px] px-2 py-0.5 rounded"
              style={{ background: 'rgba(99,102,241,0.1)', color: '#6366f1', border: '1px solid #6366f120' }}>
          GitHub Actions
        </span>
      </div>

      {/* Pipeline visualization */}
      <div className="flex items-center justify-between">
        {cicdStages.map((stage, i) => {
          const def = CICD_STAGES[i]
          const fillColor = CICD_STATUS_COLORS[stage.status] || '#334155'
          const isActive = stage.status === 'active'
          const isCompleted = stage.status === 'completed'

          return (
            <div key={stage.key} className="flex items-center flex-1">
              {/* Stage node */}
              <div className="flex flex-col items-center flex-shrink-0" style={{ minWidth: 56 }}>
                <div
                  className={`w-10 h-10 rounded-full flex items-center justify-center text-sm mb-1.5 ${isActive ? 'animate-pulse' : ''}`}
                  style={{
                    background: stage.status === 'waiting' ? 'transparent' : `${fillColor}20`,
                    border: `2px solid ${fillColor}`,
                    color: fillColor,
                  }}
                >
                  {def.icon}
                </div>
                <span className="text-[10px] font-medium" style={{ color: '#94a3b8' }}>{def.label}</span>
                <span className="text-[9px] font-mono" style={{ color: '#94a3b8' }}>{stage.duration}</span>
              </div>

              {/* Connecting line (not after last) */}
              {i < cicdStages.length - 1 && (
                <div className="flex-1 h-0.5 mx-1"
                     style={{
                       background: isCompleted ? '#10b981' : '#334155',
                       ...(stage.status === 'waiting' ? { backgroundImage: 'repeating-linear-gradient(90deg, #334155 0px, #334155 4px, transparent 4px, transparent 8px)', background: 'transparent' } : {}),
                     }}>
                  {stage.status === 'waiting' && (
                    <div className="w-full h-full" style={{ backgroundImage: 'repeating-linear-gradient(90deg, #33415580 0px, #33415580 4px, transparent 4px, transparent 8px)' }} />
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Coverage Donut Chart ─────────────────────────────────────────────────────

interface CoverageSegment {
  label: string
  count: number
  color: string
}

function CoverageDonutChart({ trends }: { trends: DashboardTrends | null }) {
  // Derive from trends.pass_fail_by_type or use mock data
  const segments: CoverageSegment[] = (() => {
    if (trends?.pass_fail_by_type?.length) {
      const colors = ['#6366f1', '#06b6d4', '#8b5cf6', '#ef4444']
      return trends.pass_fail_by_type.map((t, i) => ({
        label: t.name,
        count: t.passed + t.failed,
        color: colors[i % colors.length],
      }))
    }
    return []
  })()

  const total = segments.reduce((s, seg) => s + seg.count, 0)
  const radius = 50
  const strokeWidth = 12
  const circumference = 2 * Math.PI * radius

  let accumulatedOffset = 0

  return (
    <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      <h2 className="font-semibold mb-4" style={{ color: '#94a3b8' }}>Coverage by Type</h2>
      <div className="flex items-center gap-6">
        {/* SVG donut */}
        <svg width="140" height="140" viewBox="0 0 140 140" className="flex-shrink-0">
          {segments.map((seg) => {
            const pct = total > 0 ? seg.count / total : 0
            const dashLength = pct * circumference
            const dashGap = circumference - dashLength
            const offset = accumulatedOffset
            accumulatedOffset += dashLength

            return (
              <circle
                key={seg.label}
                cx="70"
                cy="70"
                r={radius}
                fill="none"
                stroke={seg.color}
                strokeWidth={strokeWidth}
                strokeDasharray={`${dashLength} ${dashGap}`}
                strokeDashoffset={-offset}
                strokeLinecap="round"
                transform="rotate(-90 70 70)"
                style={{ transition: 'stroke-dasharray 0.5s ease' }}
              />
            )
          })}
          {/* Center text */}
          <text x="70" y="66" textAnchor="middle" fill="#e2e8f0" fontSize="22" fontWeight="bold"
                fontFamily="Space Mono, monospace">
            {total}
          </text>
          <text x="70" y="82" textAnchor="middle" fill="#94a3b8" fontSize="9"
                fontFamily="Space Mono, monospace">
            TESTS
          </text>
        </svg>

        {/* Legend */}
        <div className="space-y-2 flex-1">
          {segments.map(seg => (
            <div key={seg.label} className="flex items-center gap-2">
              <div className="w-2.5 h-2.5 rounded-sm flex-shrink-0" style={{ background: seg.color }} />
              <span className="text-xs flex-1" style={{ color: '#94a3b8' }}>{seg.label}</span>
              <span className="text-xs font-mono" style={{ color: '#e2e8f0' }}>{seg.count}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ── AI Architect Insights Card ───────────────────────────────────────────────

const AI_INSIGHTS = [
  { icon: '\u26A0', color: '#f59e0b', bg: 'rgba(245,158,11,0.1)', text: 'Requirement coverage gap detected: acceptance criteria uncovered' },
  { icon: '\u26A0', color: '#f59e0b', bg: 'rgba(245,158,11,0.1)', text: 'Flaky test pattern identified: intermittent failures in last 3 runs' },
  { icon: '\u2713', color: '#10b981', bg: 'rgba(16,185,129,0.1)', text: 'P0 coverage trend improving, up 5% this sprint' },
  { icon: 'i',      color: '#06b6d4', bg: 'rgba(6,182,212,0.1)',  text: 'Quality gate flagged concerns. Review blocking checks before release' },
]

function AIArchitectInsightsCard({ toast, projectId: _projectId, data }: { toast: any; projectId: string | null; data: DashboardData | null }) {
  const insights = data ? [
    data.coverage_pct < 80
      ? { icon: '⚠', color: '#f59e0b', bg: 'rgba(245,158,11,0.1)', text: `Coverage at ${Math.round(data.coverage_pct)}%: ${data.coverage_pct < 60 ? 'critical gap' : 'below 80% threshold'}. Review uncovered acceptance criteria.` }
      : { icon: '✓', color: '#10b981', bg: 'rgba(16,185,129,0.1)', text: `Coverage at ${Math.round(data.coverage_pct)}% is above threshold. Maintain momentum.` },
    data.open_defects > 0
      ? { icon: '⚠', color: '#f59e0b', bg: 'rgba(245,158,11,0.1)', text: `${data.open_defects} open defect${data.open_defects !== 1 ? 's' : ''} detected${data.p0_defects > 0 ? ` (${data.p0_defects} P0 critical)` : ''}. Review Defect Intelligence.` }
      : { icon: '✓', color: '#10b981', bg: 'rgba(16,185,129,0.1)', text: 'No open defects detected. Quality gate is clean.' },
    data.pass_rate < 85
      ? { icon: '⚠', color: '#fb7185', bg: 'rgba(251,113,133,0.1)', text: `Pass rate ${Math.round(data.pass_rate)}% is below 85% threshold. Check Run History for failure patterns.` }
      : { icon: '✓', color: '#10b981', bg: 'rgba(16,185,129,0.1)', text: `Pass rate ${Math.round(data.pass_rate)}% is healthy. No regression detected.` },
    { icon: 'i', color: '#06b6d4', bg: 'rgba(6,182,212,0.1)', text: data.quality_score >= 70 ? `Quality score ${data.quality_score}%: gate eligible for PASS decision.` : `Quality score ${data.quality_score}%: gate may block release. Address P0 coverage gaps.` },
  ] : AI_INSIGHTS
  const handleExportPDF = async () => {
    try {
      await exportCoverageReport('pdf')
      toast.success('Coverage report exported (PDF)')
    } catch {
      toast.error('Failed to export PDF report')
    }
  }
  const handleExportXLSX = async () => {
    try {
      await exportComplianceReport('xlsx')
      toast.success('Compliance report exported (XLSX)')
    } catch {
      toast.error('Failed to export XLSX report')
    }
  }

  return (
    <div className="rounded-xl p-6 col-span-2" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <h2 className="font-semibold" style={{ color: '#94a3b8' }}>AI Architect Insights</h2>
          <span className="text-[10px] px-2 py-0.5 rounded"
                style={{ background: '#0a0a14', color: '#94a3b8', border: '1px solid #1e1e3a' }}>
            {new Date().toLocaleDateString('en-US', { month: 'short', year: 'numeric' })}
          </span>
        </div>
        <div className="flex gap-2">
          <button onClick={handleExportPDF}
                  className="px-3 py-1.5 rounded-lg text-[10px] font-medium transition-all"
                  style={{ background: '#0a0a14', color: '#94a3b8', border: '1px solid #1e1e3a' }}>
            Export PDF
          </button>
          <button onClick={handleExportXLSX}
                  className="px-3 py-1.5 rounded-lg text-[10px] font-medium transition-all"
                  style={{ background: '#0a0a14', color: '#94a3b8', border: '1px solid #1e1e3a' }}>
            Export XLSX
          </button>
        </div>
      </div>

      <div className="grid md:grid-cols-2 gap-3">
        {insights.map((insight, i) => (
          <div key={i} className="flex items-start gap-3 px-4 py-3 rounded-lg"
               style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
            <span className="w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0 mt-0.5"
                  style={{ background: insight.bg, color: insight.color }}>
              {insight.icon}
            </span>
            <span className="text-xs" style={{ color: '#94a3b8', lineHeight: 1.5 }}>
              {insight.text}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Dashboard() {
  const { currentProject, currentProjectId, isLoading: projectLoading } = useProject()
  const toast = useToast()
  const [health, setHealth]     = useState<HealthData>(null)
  const [data, setData]         = useState<DashboardData>(null)
  const [error, setError]       = useState<string | null>(null)
  const [agentEvents, setAgentEvents] = useState<AgentEvent[]>([])
  const [sseConnected, setSseConnected] = useState(false)
  const esRef = useRef<EventSource | null>(null)
  const [trends, setTrends] = useState<DashboardTrends | null>(null)
  const [latestRun, setLatestRun] = useState<TestRun | null>(null)
  const [sutReport, setSutReport] = useState<MissionReport | null>(null)
  const [pipelineModalOpen, setPipelineModalOpen] = useState(false)

  useEffect(() => {
    if (projectLoading) return  // Wait for project context to hydrate
    Promise.all([
      fetch(`${API}/health`).then(r => r.json()),
      fetch(`${API}/api/dashboard${currentProjectId ? `?project_id=${currentProjectId}` : ''}`).then(r => r.json()),
      fetchDashboardTrends(7, currentProjectId || undefined).catch(() => null),
      fetchRuns({ project_id: currentProjectId || undefined }).catch(() => null),
    ])
      .then(([h, d, t, runsData]) => {
        setHealth(h)
        setData(d)
        setTrends(t)
        setLatestRun(null)
        if (runsData?.runs?.length) {
          const sorted = [...runsData.runs].sort((a, b) =>
            (b.started_at ?? '').localeCompare(a.started_at ?? ''))
          setLatestRun(sorted[0])
        }
      })
      .catch(e => setError(String(e)))
  }, [currentProjectId, projectLoading])

  // SUT-health hero — fetch the latest run's 4-pillar mission-report so the
  // operator sees the SUT-quality VERDICT + its CREDIBILITY at a glance (the
  // mission's "thereby report SUT quality" surface). Degrades gracefully.
  useEffect(() => {
    const rid = latestRun?.run_id || latestRun?.id
    if (!rid) { setSutReport(null); return }
    fetch(`${API}/api/execution/runs/${rid}/mission-report`, { headers: authHeaders() })
      .then(r => (r.ok ? r.json() : null))
      .then(d => setSutReport(d || null))
      .catch(() => setSutReport(null))
  }, [latestRun?.run_id, latestRun?.id])

  // SSE — real-time agent events with exponential-backoff reconnect
  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0
    let cancelled = false

    function connect() {
      if (cancelled) return
      const es = new EventSource(`${API}/api/dashboard/events`)
      esRef.current = es
      es.onopen = () => { setSseConnected(true); attempt = 0 }
      es.onmessage = (e) => {
        try {
          const evt: AgentEvent = JSON.parse(e.data)
          if (evt.type === 'heartbeat') return
          setAgentEvents(prev => [evt, ...prev].slice(0, 50))
        } catch {}
      }
      es.onerror = () => {
        setSseConnected(false)
        es.close()
        if (!cancelled) {
          const delay = Math.min(1000 * Math.pow(2, attempt), 30000)
          attempt++
          reconnectTimer = setTimeout(connect, delay)
        }
      }
    }

    connect()
    return () => {
      cancelled = true
      esRef.current?.close()
      if (reconnectTimer) clearTimeout(reconnectTimer)
    }
  }, [])

  const grade = data ? getLetterGrade(data.quality_score) : null
  const gateDecision = latestRun?.gate_decision?.toUpperCase() ?? null
  const gateColor = gateDecision ? GATE_COLORS[gateDecision] ?? '#94a3b8' : null

  // P0/P1 coverage — only use explicit backend values; never derive/guess
  const p0Scenarios = data?.p0_count ?? null
  const p1Scenarios = data?.p1_count ?? null
  const p0Coverage = data?.p0_coverage_pct ?? null
  const p1Coverage = data?.p1_coverage_pct ?? null

  // F6-18: defensive formatter — `${value}` interpolation renders the literal
  // string "null" when a backend field is unexpectedly null. Use this for any
  // numeric metric so the UI shows "—" instead of leaking "null" to users.
  const fmt = (n: number | null | undefined): string =>
    n == null || Number.isNaN(n) ? '\u2014' : `${n}`

  return (
    <div className="min-h-screen" style={{ background: '#05050a', color: '#e2e8f0' }}>
      {/* Header */}
      <header style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}
              className="px-8 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center text-lg"
               style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)' }}>A</div>
          <div>
            <h1 className="font-bold text-lg leading-none">ARTA</h1>
            <p className="text-xs" style={{ color: '#6366f1' }}>
              {currentProject ? currentProject.name : 'BMAD TEA Platform'}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-4">
          {/* Run Pipeline button */}
          <button
            onClick={() => setPipelineModalOpen(true)}
            className="px-4 py-2 rounded-lg text-sm font-semibold text-white transition-all hover:opacity-90 flex items-center gap-2"
            style={{ background: 'linear-gradient(135deg, #6366f1, #8b5cf6)' }}
          >
            <span style={{ fontSize: 16 }}>&#9654;</span>
            Run Pipeline
          </button>
          <div className="flex items-center gap-2 text-sm">
            <span className={`w-2 h-2 rounded-full ${health?.status === 'ok' ? 'bg-emerald-400' : 'bg-rose-400'}`} />
            <span style={{ color: '#94a3b8' }}>
              {health ? `${health.platform} · ${health.methodology}` : 'Connecting\u2026'}
            </span>
          </div>
        </div>
      </header>

      <main className="px-8 py-8 max-w-6xl mx-auto">
        {error && (
          <div className="mb-6 px-4 py-3 rounded-lg text-sm"
               style={{ background: '#1a0a0a', border: '1px solid #7f1d1d', color: '#fca5a5' }}>
            API unreachable: {error}
            <span className="ml-2" style={{ color: '#94a3b8' }}>
              Make sure <code>arta-api</code> is running on port 8000
            </span>
          </div>
        )}

        {/* SUT-health HERO — the at-a-glance answer: "what's the SUT status,
            and can I trust it?" Verdict (Pillar 4) framed by its credibility
            (attributable rate, Pillars 1/1b/2) + a11y verdict + top next-step. */}
        {sutReport && (() => {
          const v = sutVerdict(sutReport)
          const attr = attributablePassRate(sutReport)
          const a11y = a11yVerdict(sutReport)
          const ctas = actionableCtas(sutReport)
          const vColor = _TONE_COLOR[v.tone]
          const rid = latestRun?.run_id || latestRun?.id
          const headline = v.sutCritical > 0
            ? `SUT DEGRADED · ${v.sutCritical} critical regression${v.sutCritical > 1 ? 's' : ''}`
            : (v.tone === 'good' ? 'SUT HEALTHY' : `SUT ${v.band}`)
          return (
            <div className="rounded-xl p-5 mb-6"
                 style={{ background: `${vColor}0d`, border: `1px solid ${vColor}33` }}>
              <div className="flex items-center justify-between flex-wrap gap-3">
                <div className="flex items-center gap-3 flex-wrap">
                  <div className="pr-2">
                    <p className="text-xs" style={{ color: '#64748b' }}>SUT Status · latest run</p>
                    <p className="text-xl font-bold" style={{ color: vColor, fontFamily: 'Space Mono, monospace' }}>
                      {headline}
                    </p>
                  </div>
                  {/* credibility chip — can we TRUST this verdict? */}
                  <div className="rounded-lg px-3 py-1.5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}
                       title="ARTA-attributable pass rate = PASS / (TOTAL − SUT bugs − BLOCKED). Low = verdict built on few attributable tests.">
                    <p className="text-[10px]" style={{ color: '#64748b' }}>Verdict credibility</p>
                    <p className="text-sm font-mono" style={{ color: attr !== null ? (attr >= 90 ? '#34d399' : '#fbbf24') : '#94a3b8' }}>
                      {attr !== null ? `${Math.round(attr)}% attributable` : 'n/a'}
                    </p>
                  </div>
                  {/* a11y verdict chip */}
                  <div className="rounded-lg px-3 py-1.5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                    <p className="text-[10px]" style={{ color: '#64748b' }}>Accessibility</p>
                    <p className="text-sm" style={{ color: _TONE_COLOR[a11y.tone] }}>{a11y.label}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <a href="/sut-quality" className="px-3 py-1.5 rounded-lg text-sm font-semibold"
                     style={{ background: '#6366f1', color: '#fff' }}>View SUT Quality →</a>
                  {rid && (
                    <a href={`/run-history?run_id=${rid}`} className="px-3 py-1.5 rounded-lg text-sm"
                       style={{ border: '1px solid #1e1e3a', color: '#a5b4fc' }}>Latest run report →</a>
                  )}
                </div>
              </div>
              {ctas.length > 0 && (
                <p className="text-xs mt-3" style={{ color: '#94a3b8' }}>
                  <span style={{ color: '#64748b' }}>Next step · </span>{ctas[0]}
                </p>
              )}
            </div>
          )
        })()}

        {/* Gate Decision + Quality Grade banner */}
        {data && (
          <div className="flex items-center gap-4 mb-6">
            {/* Letter Grade */}
            <div className="flex items-center gap-3 rounded-xl px-5 py-3"
                 style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <div
                className="w-12 h-12 rounded-xl flex items-center justify-center text-2xl font-bold"
                style={{
                  background: `${grade!.color}15`,
                  color: grade!.color,
                  border: `2px solid ${grade!.color}40`,
                  fontFamily: 'Space Mono, monospace',
                }}
              >
                {grade!.letter}
              </div>
              <div>
                <p className="text-xs" style={{ color: '#64748b' }}>Quality Grade</p>
                <p className="text-lg font-bold" style={{ color: grade!.color, fontFamily: 'Space Mono, monospace' }}>
                  {data.quality_score}%
                </p>
              </div>
            </div>

            {/* Gate Decision Badge */}
            {gateDecision && (
              <div className="flex items-center gap-3 rounded-xl px-5 py-3"
                   style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                <div
                  className="px-3 py-1.5 rounded-lg text-sm font-bold"
                  style={{
                    background: `${gateColor}15`,
                    color: gateColor!,
                    border: `1px solid ${gateColor}40`,
                    fontFamily: 'Space Mono, monospace',
                  }}
                >
                  {gateDecision}
                </div>
                <div>
                  <p className="text-xs" style={{ color: '#64748b' }}>Gate Decision</p>
                  <p className="text-xs" style={{ color: '#94a3b8' }}>
                    Build {latestRun?.build_id ?? '—'}
                  </p>
                </div>
              </div>
            )}

            {/* P0 vs P1 Coverage */}
            <div className="flex items-center gap-4 rounded-xl px-5 py-3 ml-auto"
                 style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <div className="text-center">
                <p className="text-xs mb-1" style={{ color: '#64748b' }}>P0 Coverage</p>
                <p className="text-lg font-bold" style={{ color: '#ef4444', fontFamily: 'Space Mono, monospace' }}>
                  {p0Coverage !== null ? `${p0Coverage}%` : '\u2014'}
                </p>
                <p className="text-[10px]" style={{ color: '#94a3b8' }}>{p0Scenarios !== null ? `${p0Scenarios} scenarios` : 'no data'}</p>
              </div>
              <div className="w-px h-10" style={{ background: '#1e1e3a' }} />
              <div className="text-center">
                <p className="text-xs mb-1" style={{ color: '#64748b' }}>P1 Coverage</p>
                <p className="text-lg font-bold" style={{ color: '#f59e0b', fontFamily: 'Space Mono, monospace' }}>
                  {p1Coverage !== null ? `${p1Coverage}%` : '\u2014'}
                </p>
                <p className="text-[10px]" style={{ color: '#94a3b8' }}>{p1Scenarios !== null ? `${p1Scenarios} scenarios` : 'no data'}</p>
              </div>
            </div>
          </div>
        )}

        {/* Metric cards — F6-18: use fmt() so a null backend field renders "—", not "null" */}
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
          {[
            { label: 'Quality Score',   value: fmt(data?.quality_score),    unit: '%', color: '#6366f1' },
            { label: 'Coverage',        value: fmt(data?.coverage_pct),     unit: '%', color: '#8b5cf6' },
            { label: 'Pass Rate',       value: fmt(data?.pass_rate),        unit: '%', color: '#10b981' },
            { label: 'Open Defects',    value: fmt(data?.open_defects),     unit: '',  color: '#f59e0b' },
            { label: 'P0 Defects',      value: fmt(data?.p0_defects),       unit: '',  color: '#ef4444' },
            { label: 'Test Scenarios',  value: fmt(data?.total_scenarios),  unit: '',  color: '#06b6d4' },
          ].map(m => (
            <div key={m.label} className="rounded-xl p-4"
                 style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <p className="text-xs mb-1" style={{ color: '#64748b' }}>{m.label}</p>
              <p className="text-2xl font-bold" style={{ color: m.color }}>
                {m.value}<span className="text-sm">{m.unit}</span>
              </p>
            </div>
          ))}
        </div>

        {/* Risk Heatmap + CI/CD Pipeline + Coverage Donut */}
        <div className="grid md:grid-cols-3 gap-6 mb-8">
          {/* Compact 3×3 Risk Heatmap */}
          <div className="rounded-xl p-5" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold" style={{ color: '#94a3b8' }}>Risk Heatmap</h2>
              <a href="/risk-matrix" className="text-[10px] px-2 py-0.5 rounded" style={{ color: '#6366f1', border: '1px solid #6366f130' }}>Full View</a>
            </div>
            <div className="grid grid-cols-3 gap-1" style={{ fontFamily: 'Space Mono, monospace' }}>
              {[3, 2, 1].map(impact => (
                [1, 2, 3].map(prob => {
                  const score = impact * prob
                  const bg = score >= 8 ? '#fb718520' : score >= 6 ? '#f59e0b20' : score >= 4 ? '#fbbf2415' : '#10b98110'
                  const border = score >= 8 ? '#fb718540' : score >= 6 ? '#f59e0b40' : score >= 4 ? '#fbbf2430' : '#10b98130'
                  const color = score >= 8 ? '#fb7185' : score >= 6 ? '#f59e0b' : score >= 4 ? '#fbbf24' : '#10b981'
                  return (
                    <div key={`${impact}-${prob}`} className="rounded p-1.5 text-center" style={{ background: bg, border: `1px solid ${border}` }}>
                      <span className="text-[10px] font-bold" style={{ color }}>{score}</span>
                    </div>
                  )
                })
              ))}
            </div>
            <div className="flex justify-between mt-2">
              <span className="text-[9px]" style={{ color: '#94a3b8' }}>Probability →</span>
              <span className="text-[9px]" style={{ color: '#94a3b8' }}>Impact ↑</span>
            </div>
          </div>
          {data && <CICDPipelineCard data={data} latestRun={latestRun} />}
          {/* Latest Run Quick Access */}
          {latestRun && (
            <a href={`/run-history?run_id=${latestRun.run_id || latestRun.id}`}
               className="rounded-xl p-5 block no-underline transition hover:ring-1"
               style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
              <div className="flex items-center justify-between mb-3">
                <h2 className="font-semibold text-sm" style={{ color: '#94a3b8' }}>Latest Run</h2>
                {(() => {
                  // F7-3: gate_decision is optional → its toUpperCase() is undefined when absent.
                  // Resolve once with a 'N/A' fallback so the GATE_COLORS lookup is index-safe.
                  const gateKey = (latestRun.gate_decision?.toUpperCase()) || 'N/A'
                  const c = GATE_COLORS[gateKey] || '#94a3b8'
                  return (
                    <span className="text-[10px] px-2 py-0.5 rounded font-bold"
                          style={{
                            background: `${c}15`,
                            color: c,
                            border: `1px solid ${c}40`,
                            fontFamily: 'Space Mono, monospace',
                          }}>
                      {latestRun.gate_decision || 'N/A'}
                    </span>
                  )
                })()}
              </div>
              <div className="flex items-center gap-4">
                <span className="text-xs font-mono" style={{ color: '#a5b4fc' }}>{latestRun.run_id || latestRun.id}</span>
                <span className="text-xs" style={{ color: '#94a3b8' }}>{latestRun.environment}</span>
              </div>
              <div className="flex items-center gap-3 mt-2">
                <span className="text-xs font-mono" style={{ color: '#34d399' }}>{latestRun.passed ?? 0} pass</span>
                <span className="text-xs font-mono" style={{ color: '#fb7185' }}>{latestRun.failed ?? 0} fail</span>
                <span className="text-xs ml-auto" style={{ color: '#6366f1' }}>View details →</span>
              </div>
            </a>
          )}
          <CoverageDonutChart trends={trends} />
        </div>

        {/* Charts */}
        <div className="grid md:grid-cols-2 gap-6 mb-8">
          {/* Quality trend with regression markers */}
          <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="flex items-center justify-between mb-4">
              <h2 className="font-semibold" style={{ color: '#94a3b8' }}>Quality Trend</h2>
              <span className="text-[10px] px-2 py-0.5 rounded-full" style={{ background: '#fb718515', color: '#fb7185', border: '1px solid #fb718530' }}>
                &#x25CF; Regression &gt;5%
              </span>
            </div>
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={trends?.quality_trends ?? []}>
                <defs>
                  <linearGradient id="gQuality" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="gCoverage" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <XAxis dataKey="day" tick={{ fill: '#94a3b8', fontSize: 11 }} axisLine={false} tickLine={false} />
                <YAxis domain={[50, 100]} tick={{ fill: '#94a3b8', fontSize: 11 }} axisLine={false} tickLine={false} width={30} />
                <Tooltip
                  contentStyle={{ background: '#1e1e3a', border: '1px solid #2d2d4a', borderRadius: 8, color: '#e2e8f0', fontSize: 12 }}
                  labelStyle={{ color: '#94a3b8' }}
                />
                <Area
                  type="monotone"
                  dataKey="quality"
                  stroke="#6366f1"
                  fill="url(#gQuality)"
                  strokeWidth={2}
                  name="Quality %"
                  dot={<RegressionDot data={trends?.quality_trends ?? []} dataKey="quality" />}
                  activeDot={{ r: 4, fill: '#6366f1' }}
                />
                <Area
                  type="monotone"
                  dataKey="coverage"
                  stroke="#8b5cf6"
                  fill="url(#gCoverage)"
                  strokeWidth={2}
                  name="Coverage %"
                  dot={<RegressionDot data={trends?.quality_trends ?? []} dataKey="coverage" />}
                  activeDot={{ r: 4, fill: '#8b5cf6' }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          {/* Pass/Fail by type */}
          <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <h2 className="font-semibold mb-4" style={{ color: '#94a3b8' }}>Pass / Fail by Type</h2>
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={trends?.pass_fail_by_type ?? []} barGap={2}>
                <XAxis dataKey="name" tick={{ fill: '#94a3b8', fontSize: 11 }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: '#94a3b8', fontSize: 11 }} axisLine={false} tickLine={false} width={30} />
                <Tooltip
                  contentStyle={{ background: '#1e1e3a', border: '1px solid #2d2d4a', borderRadius: 8, color: '#e2e8f0', fontSize: 12 }}
                  labelStyle={{ color: '#94a3b8' }}
                />
                <Bar dataKey="passed" fill="#10b981" radius={[4, 4, 0, 0]} name="Passed" />
                <Bar dataKey="failed" fill="#ef4444" radius={[4, 4, 0, 0]} name="Failed" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="grid md:grid-cols-2 gap-6">
          {/* Pipeline */}
          <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <h2 className="font-semibold mb-4" style={{ color: '#94a3b8' }}>
              Pipeline &middot; Build {data?.pipeline?.build_id ?? '\u2014'}
            </h2>
            <div className="space-y-2">
              {(data?.pipeline?.stages ?? []).map((s: { name: string; status: string; duration_s?: number }) => (
                <div key={s.name} className="flex items-center gap-3">
                  <span className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${STATUS_COLOR[s.status] ?? 'bg-slate-600'}`} />
                  <span className="flex-1 text-sm">{s.name}</span>
                  <span className="text-xs" style={{ color: '#94a3b8' }}>
                    {s.duration_s ? `${s.duration_s}s` : s.status}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Active agents + live feed */}
          <div className="rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <div className="flex items-center gap-2 mb-4">
              <h2 className="font-semibold" style={{ color: '#94a3b8' }}>Agent Activity</h2>
              <span className={`w-2 h-2 rounded-full ${sseConnected ? 'bg-emerald-400 animate-pulse' : 'bg-slate-600'}`} />
              <span className="text-[10px]" style={{ color: '#94a3b8' }}>{sseConnected ? 'Live' : 'Offline'}</span>
            </div>

            {/* Static agent list */}
            <div className="space-y-2 mb-4">
              {(data?.active_agents ?? []).map((a: string) => (
                <div key={a} className="flex items-center gap-2 text-sm">
                  <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
                  <span style={{ color: '#c7d2fe' }}>{a}</span>
                </div>
              ))}
              {!data && <p className="text-sm" style={{ color: '#94a3b8' }}>Loading\u2026</p>}
            </div>

            {/* Live event feed */}
            {agentEvents.length > 0 && (
              <div className="mb-4 max-h-32 overflow-y-auto space-y-1 rounded-lg p-2"
                   style={{ background: '#0a0a14' }}>
                {agentEvents.map((evt, i) => (
                  <div key={i} className="flex items-center gap-2 text-[11px]">
                    <span className="w-1 h-1 rounded-full flex-shrink-0"
                          style={{ background: evt.status === 'error' ? '#ef4444' : evt.status === 'running' ? '#6366f1' : '#10b981' }} />
                    <span style={{ color: '#818cf8' }}>{evt.agent || 'system'}</span>
                    <span style={{ color: '#94a3b8' }}>{evt.message || evt.type}</span>
                  </div>
                ))}
              </div>
            )}

            <h2 className="font-semibold mb-3" style={{ color: '#94a3b8' }}>Quick Links</h2>
            <div className="space-y-2">
              <a href="/run-history"
                 className="flex items-center gap-2 text-sm hover:underline"
                 style={{ color: '#06b6d4' }}>
                &#x25F7; Run History &amp; Metrics
              </a>
              {[
                { href: `${API}/docs`,          label: 'API Docs (Swagger UI)' },
                { href: `${API}/api/dashboard`, label: 'Dashboard JSON' },
                { href: `${API}/health`,        label: 'Health Check' },
              ].map(l => (
                <a key={l.href} href={l.href} target="_blank" rel="noopener noreferrer"
                   className="flex items-center gap-2 text-sm hover:underline"
                   style={{ color: '#6366f1' }}>
                  &#x2197; {l.label}
                </a>
              ))}
            </div>
          </div>
        </div>

        {/* AI Architect Insights */}
        <div className="grid grid-cols-2 gap-6 mt-8">
          <AIArchitectInsightsCard toast={toast} projectId={currentProjectId} data={data} />
        </div>

        <p className="mt-8 text-center text-xs" style={{ color: '#334155' }}>
          Full UI prototype &rarr; serve <code>arta-platform.html</code> with{' '}
          <code>python3 -m http.server 8888</code>
        </p>
      </main>

      {/* Pipeline Modal */}
      <RunPipelineModal open={pipelineModalOpen} onClose={() => setPipelineModalOpen(false)} />
    </div>
  )
}
