'use client'
import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { requestHeal } from '@/lib/api-client'
import { authHeaders } from '@/lib/auth-state'
import CallSequenceTimeline from '@/components/CallSequenceTimeline'
import SutQualityDrilldowns from '@/components/SutQualityDrilldowns'
import { sumCategory, severityCount, attributablePassRate, a11yVerdict, reportingFidelity } from '@/lib/missionReport'

const GATE_COLOR: Record<string, string> = { PASS: '#34d399', CONCERNS: '#fbbf24', FAIL: '#fb7185', WAIVED: '#a78bfa' }
const GATE_BG: Record<string, string> = { PASS: 'rgba(52,211,153,0.12)', CONCERNS: 'rgba(251,191,36,0.12)', FAIL: 'rgba(251,113,133,0.12)', WAIVED: 'rgba(167,139,250,0.12)' }
const STATUS_COLOR: Record<string, string> = { PASS: '#34d399', FAIL: '#fb7185', SKIP: '#fbbf24' }
const TOOL_LABELS: Record<string, string> = { playwright: 'E2E UI', newman: 'API', k6: 'Performance', zap: 'Security', manual: 'Manual' }
const TOOL_COLORS: Record<string, string> = { playwright: '#6366f1', newman: '#06b6d4', k6: '#f59e0b', zap: '#ef4444', manual: '#64748b' }

function fmtDuration(s: number) {
  if (s === undefined || s === null || isNaN(s)) return '\u2014'
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.round(s % 60)
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
  return `${m}:${String(sec).padStart(2, '0')}`
}

function fmtDate(iso: string) {
  if (!iso) return '\u2014'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '\u2014'
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) + ' ' +
    d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function RunDetailContent({ selected, isLive }: { selected: any; isLive?: boolean }) {
  const router = useRouter()
  const [showArtifacts, setShowArtifacts] = useState(false)
  const [artifacts, setArtifacts] = useState<any[]>([])
  const [loadingArtifacts, setLoadingArtifacts] = useState(false)
  const [healingTestId, setHealingTestId] = useState<string | null>(null)
  const [healToast, setHealToast] = useState<string | null>(null)
  // R28.1 — replace hardcoded "Open P0 defects: 0" with a real query
  // against /api/defects?run_id=...&priority=P0&status=open. Initial
  // state is null ("loading..."); the query falls back to
  // ?priority=P0&status=open (project-scoped) for older API versions
  // that don't yet accept run_id.
  const [openP0Defects, setOpenP0Defects] = useState<number | null>(null)
  // R115.E — track BOTH per-run AND all-time P0 counts separately so the
  // dashboard can distinguish "this run created N P0s" vs "project has M
  // open P0s overall". Pre-R115.E: when run-scoped query returned 0, the
  // tile fell back to project-scoped (showed all-time = 107 in run-8da91d)
  // — operator confused "did this run create 107 P0?" R115.E keeps both
  // counts visible.
  const [allTimeP0Defects, setAllTimeP0Defects] = useState<number | null>(null)
  const [openP0Error, setOpenP0Error] = useState<string | null>(null)
  useEffect(() => {
    const runId = selected?.run_id || selected?.id
    if (!runId) return
    setOpenP0Error(null)
    const headers: Record<string, string> = {}
    const key = typeof window !== 'undefined' ? localStorage.getItem('arta_api_key') || '' : ''
    const token = typeof window !== 'undefined' ? localStorage.getItem('arta_token') || '' : ''
    if (token) headers['Authorization'] = `Bearer ${token}`
    else if (key) headers['X-API-Key'] = key

    // R77.4 — defensive 401/403 handling. Pre-R77.4 the fetch's
    // `.then(r => r.ok ? r.json() : null)` swallowed auth failures and
    // the chain ended with `setOpenP0Defects(0)` → tile rendered "0 P0
    // defects" even when the API actually had real P0s, just with an
    // expired session. R77.4 distinguishes:
    //   - auth failure → preserve `null` (tile shows "—") + record the
    //     error string so the tile can tooltip-explain
    //   - real backend error → preserve `null` similarly
    //   - genuine "no defects" → set 0
    // Also tolerates BOTH `items` and `defects` response shapes since the
    // /api/defects endpoint returns `total` + both arrays (R75.x audit).
    const _countFromPayload = (d: any): number => {
      if (typeof d?.total === 'number') return d.total
      if (Array.isArray(d?.items)) return d.items.length
      if (Array.isArray(d?.defects)) return d.defects.length
      return 0
    }
    const _runUrl = `/api/defects?run_id=${encodeURIComponent(runId)}&priority=P0&status=open&limit=200`
    fetch(_runUrl, { headers })
      .then(async r => {
        if (r.status === 401 || r.status === 403) {
          setOpenP0Error(`auth_failure_${r.status}`)
          return { _r77_4_auth_failed: true }
        }
        if (!r.ok) {
          setOpenP0Error(`http_${r.status}`)
          return null
        }
        return r.json()
      })
      .then(d => {
        if (d && (d as any)._r77_4_auth_failed) return   // preserve null
        // R115.E — ALWAYS set run-scoped count (even if 0). No more
        // fall-through to project-scoped for the primary "P0 from this run"
        // tile. Separate query below fills the "All-time open P0" tile.
        const runCount = d ? _countFromPayload(d) : null
        setOpenP0Defects(runCount ?? 0)
      })
      .catch((err) => {
        // Network failure — preserve null + log; don't masquerade as 0.
        setOpenP0Error(`network: ${err?.message || 'unknown'}`)
      })

    // R115.E — separate query for project-wide (all-time) P0 count.
    // Renders as a secondary "All-time open P0" tile.
    const pid = selected?.project_id || ''
    if (pid) {
      const _pidUrl = `/api/defects?project_id=${encodeURIComponent(pid)}&priority=P0&status=open&limit=200`
      fetch(_pidUrl, { headers })
        .then(async rr => {
          if (rr.status === 401 || rr.status === 403) return null
          if (!rr.ok) return null
          return rr.json()
        })
        .then(dd => {
          if (dd === null) return
          setAllTimeP0Defects(_countFromPayload(dd))
        })
        .catch(() => { /* preserve null */ })
    }
  }, [selected?.run_id, selected?.id, selected?.project_id])

  // R115.F — self-heal queue ETA tile. Fetches /api/health/regen-consumer
  // and surfaces (queue_depth, drain_rate_per_cycle=20, cycle_secs=300).
  // ETA = (queue_depth × cycle_secs) / drain_rate_per_cycle / 60 minutes.
  const [regenQueueDepth, setRegenQueueDepth] = useState<number | null>(null)
  const [regenQueueEta, setRegenQueueEta] = useState<number | null>(null)  // minutes

  // R115.G — Mission Outcome (4-pillar consolidated report). Fetches
  // /api/execution/runs/{run_id}/mission-report when run is selected.
  // Pre-R115.G: operator inspected Pillar 1/1b/2/4 metrics by running
  // ad-hoc DB queries. R115.G surfaces all four pillars on one page
  // with actionable CTAs derived from the run's specific failure mix.
  const [missionReport, setMissionReport] = useState<any | null>(null)
  const [, setMissionReportError] = useState<string | null>(null)
  useEffect(() => {
    const rid = selected?.run_id || selected?.id
    if (!rid) return
    setMissionReport(null)
    setMissionReportError(null)
    fetch(`/api/execution/runs/${rid}/mission-report`, { headers: authHeaders() })
      .then(async r => {
        if (!r.ok) { setMissionReportError(`HTTP ${r.status}`); return null }
        return r.json()
      })
      .then(d => { if (d) setMissionReport(d) })
      .catch(err => setMissionReportError(String(err)))
  }, [selected?.run_id, selected?.id])
  useEffect(() => {
    fetch('/api/health/regen-consumer')
      .then(async r => {
        if (!r.ok) return null
        return r.json()
      })
      .then(d => {
        if (!d) return
        const depth = d.queue_depth ?? null
        const drain = d.max_per_cycle ?? 20
        const cycleSec = d.cycle_secs ?? 300
        setRegenQueueDepth(depth)
        if (depth !== null && drain > 0) {
          // ETA minutes = depth / drain × cycleSec / 60
          setRegenQueueEta(Math.round(depth / drain * cycleSec / 60))
        }
      })
      .catch(() => { /* preserve null */ })
  }, [selected?.run_id, selected?.id])

  const loadArtifacts = async () => {
    if (showArtifacts) { setShowArtifacts(false); return }
    setLoadingArtifacts(true)
    try {
      const runId = selected.run_id || selected.id
      const res = await fetch(`/api/execution/runs/${runId}/artifacts`)
      const data = await res.json()
      setArtifacts(data.artifacts || [])
      setShowArtifacts(true)
    } catch { setArtifacts([]) }
    setLoadingArtifacts(false)
  }

  const results = (selected.results || []) as any[]
  const total = results.length
  const passedCount = results.filter(t => t.status === 'PASS').length
  const failedCount = results.filter(t => t.status === 'FAIL').length
  // WS2 — prefer the backend pass_rate (consistent with dashboard /api/dashboard.pass_rate
  // + run-history summary avg_pass_rate); fall back to a client recompute only when absent.
  // R306.A — the fallback is OVER EXECUTED tests (passed + failed), matching the
  // backend + summary report; `total` (which includes BLOCKED/SKIP) would
  // understate it (run-26aa5f: 87/172=50.6% total vs 87/157=55.4% executed).
  const executedCount = passedCount + failedCount
  const passRate = typeof selected.pass_rate === 'number'
    ? Math.round(selected.pass_rate)
    : (executedCount > 0 ? Math.round(passedCount / executedCount * 100) : 0)

  // F20-30: backend serializes the DB column as `automation_tool` (since
  // F20-17 fixed the result-persistence key names). Reading only `t.tool`
  // returned undefined for every row, so:
  //   • the `|| 'playwright'` fallback wrongly bucketed ALL results
  //     as Playwright → "E2E UI: 249 pass / 711 fail" (bogus)
  //   • the `t.tool === 'newman'` check was always false → "API tests:
  //     not executed" even with 762 newman rows in DB
  // Helper reads BOTH keys so mixed payloads (DB rows + in-memory rows
  // still using `tool`) all work.
  const _toolOf = (t: any): string => (t.automation_tool || t.tool || 'unknown') as string

  // Group by tool
  const grouped: Record<string, any[]> = {}
  for (const t of results) {
    const tool = _toolOf(t)
    if (!grouped[tool]) grouped[tool] = []
    grouped[tool].push(t)
  }

  // Browser breakdown
  const browsers: Record<string, { passed: number; failed: number; total: number }> = {}
  for (const t of results) {
    const b = t.browser || 'unknown'
    if (!browsers[b]) browsers[b] = { passed: 0, failed: 0, total: 0 }
    browsers[b].total++
    if (t.status === 'PASS') browsers[b].passed++
    else if (t.status === 'FAIL') browsers[b].failed++
  }
  const browserEntries = Object.entries(browsers).filter(([k]) => k !== 'unknown')

  // F20-30: use _toolOf helper so we read the right key (DB returns automation_tool, in-memory may use tool)
  const hasPw = results.some(t => _toolOf(t) === 'playwright')
  const hasApi = results.some(t => _toolOf(t) === 'newman')
  const hasPerf = results.some(t => _toolOf(t) === 'k6')
  const hasSec = results.some(t => _toolOf(t) === 'zap')
  const hasA11y = results.some(t => _toolOf(t) === 'axe')
  const hasAnalytics = results.some(t => _toolOf(t) === 'pytest')

  const pwFail = results.filter(t => _toolOf(t) === 'playwright' && t.status === 'FAIL').length
  const pwPass = results.filter(t => _toolOf(t) === 'playwright' && t.status === 'PASS').length
  const pwSkip = results.filter(t => _toolOf(t) === 'playwright' && t.status === 'SKIP').length
  const apiFail = results.filter(t => _toolOf(t) === 'newman' && t.status === 'FAIL').length
  const apiPass = results.filter(t => _toolOf(t) === 'newman' && t.status === 'PASS').length
  const apiSkip = results.filter(t => _toolOf(t) === 'newman' && t.status === 'SKIP').length
  const perfFail = results.filter(t => _toolOf(t) === 'k6' && t.status === 'FAIL').length
  const perfPass = results.filter(t => _toolOf(t) === 'k6' && t.status === 'PASS').length
  const perfSkip = results.filter(t => _toolOf(t) === 'k6' && t.status === 'SKIP').length
  const secFail = results.filter(t => _toolOf(t) === 'zap' && t.status === 'FAIL').length
  const secPass = results.filter(t => _toolOf(t) === 'zap' && t.status === 'PASS').length
  const secSkip = results.filter(t => _toolOf(t) === 'zap' && t.status === 'SKIP').length
  const a11yFail = results.filter(t => _toolOf(t) === 'axe' && t.status === 'FAIL').length
  const a11yPass = results.filter(t => _toolOf(t) === 'axe' && t.status === 'PASS').length
  const a11ySkip = results.filter(t => _toolOf(t) === 'axe' && t.status === 'SKIP').length
  const anFail = results.filter(t => _toolOf(t) === 'pytest' && t.status === 'FAIL').length
  const anPass = results.filter(t => _toolOf(t) === 'pytest' && t.status === 'PASS').length
  const anSkip = results.filter(t => _toolOf(t) === 'pytest' && t.status === 'SKIP').length

  // R29.3c \u2014 BLOCKED counters per tool. BLOCKED is its own state for
  // config-gap rows (R29.3a pre-dispatch filter when env vars are
  // unresolvable). Distinct from SKIP (suite-selection-driven exclusion)
  // and FAIL (real test failure). Excluded from pass-rate denominator.
  const pwBlocked  = results.filter(t => _toolOf(t) === 'playwright' && t.status === 'BLOCKED').length
  const apiBlocked = results.filter(t => _toolOf(t) === 'newman'     && t.status === 'BLOCKED').length
  const perfBlocked = results.filter(t => _toolOf(t) === 'k6'        && t.status === 'BLOCKED').length
  const secBlocked = results.filter(t => _toolOf(t) === 'zap'        && t.status === 'BLOCKED').length
  const a11yBlocked = results.filter(t => _toolOf(t) === 'axe'       && t.status === 'BLOCKED').length
  const anBlocked  = results.filter(t => _toolOf(t) === 'pytest'     && t.status === 'BLOCKED').length
  const totalBlocked = pwBlocked + apiBlocked + perfBlocked + secBlocked + a11yBlocked + anBlocked

  // R71.5 — derive the dominant blocked_reason per tool so tile copy
  // reflects the actual remediation step. Pre-R71.5 every BLOCKED tile
  // said "fill env vars" — wrong for discovery_empty (needs auth refresh)
  // and no_k6_specs_in_inventory (needs Regenerate-by-Tool).
  const _dominantBlockedReasonOf = (toolName: string): string | null => {
    const blockedRows = results.filter(t => _toolOf(t) === toolName && t.status === 'BLOCKED')
    if (blockedRows.length === 0) return null
    const counts: Record<string, number> = {}
    for (const r of blockedRows) {
      const md = (r as any).metadata || (r as any).metadata_ || {}
      const mdObj = typeof md === 'string' ? (() => { try { return JSON.parse(md) } catch { return {} } })() : md
      const reason = ((mdObj?.blocked_reason || mdObj?.blockedReason) ?? 'unknown')
        .toString().toLowerCase()
      counts[reason] = (counts[reason] || 0) + 1
    }
    return Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0]
  }
  const pwBlockedReason   = _dominantBlockedReasonOf('playwright')
  const apiBlockedReason  = _dominantBlockedReasonOf('newman')
  const perfBlockedReason = _dominantBlockedReasonOf('k6')
  const secBlockedReason  = _dominantBlockedReasonOf('zap')
  const a11yBlockedReason = _dominantBlockedReasonOf('axe')
  const anBlockedReason   = _dominantBlockedReasonOf('pytest')

  // R33.6 / R33.8 \u2014 extract triage_category from any of the three
  // possible row paths (top-level field, nested triage object,
  // metadata.operator_triage.category). FAIL rows classified as
  // sut_regression are excluded from the effective-pass-rate
  // denominator: ARTA's tool quality is independent of SUT bugs it
  // correctly detected.
  const _triageCatOf = (t: any): string | null => {
    const md = t?.metadata || t?.metadata_ || {}
    const mdObj = (typeof md === 'string' ? (() => {
      try { return JSON.parse(md) } catch { return {} }
    })() : md) as any
    const op = (mdObj && typeof mdObj === 'object') ? (mdObj.operator_triage || {}) : {}
    return (
      (op && op.category)
      || t?.triage_category
      || (mdObj && mdObj.triage_category)
      || (t?.triage && t.triage.triage_category)
      || null
    )
  }
  const _isSutRegression = (t: any): boolean =>
    (t.status === 'FAIL' || t.status === 'ERROR') && _triageCatOf(t) === 'sut_regression'
  // R34.3 — test_gen_bug rows feed ARTA's self-heal pipeline (R30.3 +
  // Tier-1 autofix) → exclude from the effective denominator so we
  // don't double-attribute "fail in gate + queued for regen + auto
  // healed". Surfaced as a separate counter (auto-heal queue depth).
  const _isTestGenBug = (t: any): boolean =>
    (t.status === 'FAIL' || t.status === 'ERROR') && _triageCatOf(t) === 'test_gen_bug'
  const pwSutBugs   = results.filter(t => _toolOf(t) === 'playwright' && _isSutRegression(t)).length
  const apiSutBugs  = results.filter(t => _toolOf(t) === 'newman'     && _isSutRegression(t)).length
  const perfSutBugs = results.filter(t => _toolOf(t) === 'k6'         && _isSutRegression(t)).length
  const secSutBugs  = results.filter(t => _toolOf(t) === 'zap'        && _isSutRegression(t)).length
  const a11ySutBugs = results.filter(t => _toolOf(t) === 'axe'        && _isSutRegression(t)).length
  const anSutBugs   = results.filter(t => _toolOf(t) === 'pytest'     && _isSutRegression(t)).length
  const totalSutBugs = pwSutBugs + apiSutBugs + perfSutBugs + secSutBugs + a11ySutBugs + anSutBugs
  const pwTestGenBugs   = results.filter(t => _toolOf(t) === 'playwright' && _isTestGenBug(t)).length
  const apiTestGenBugs  = results.filter(t => _toolOf(t) === 'newman'     && _isTestGenBug(t)).length
  const perfTestGenBugs = results.filter(t => _toolOf(t) === 'k6'         && _isTestGenBug(t)).length
  const secTestGenBugs  = results.filter(t => _toolOf(t) === 'zap'        && _isTestGenBug(t)).length
  const a11yTestGenBugs = results.filter(t => _toolOf(t) === 'axe'        && _isTestGenBug(t)).length
  const anTestGenBugs   = results.filter(t => _toolOf(t) === 'pytest'     && _isTestGenBug(t)).length
  const totalTestGenBugs = pwTestGenBugs + apiTestGenBugs + perfTestGenBugs + secTestGenBugs + a11yTestGenBugs + anTestGenBugs


  // R33.8 + R34.3 \u2014 effective pass rate per tool: pass / (pass + real_fail),
  // where real_fail = FAIL count - sut_regression - test_gen_bug. The
  // user's bar is \u226595% per tool. Returns the percent rounded to 1
  // decimal, or null when there are no judge-able rows.
  const _effectivePct = (
    pass: number, fail: number, sutBugs: number, testGenBugs: number = 0,
  ): number | null => {
    const realFail = Math.max(0, fail - sutBugs - testGenBugs)
    const denom = pass + realFail
    if (denom === 0) return null
    return Math.round((100 * pass / denom) * 10) / 10
  }

  // R28.1 \u2014 derive per-tool gate status. Pre-R28.1 a tool with ALL SKIP
  // rows (e.g. R21 auth-skip emitting only `automation_tool: playwright,
  // status: SKIP`) rendered \u2713 green because `pwFail===0` \u2192 'pass'. The
  // operator saw "0 pass / all pass \u2713" while the tests didn't actually
  // run. R29.3c adds the 'blocked' state alongside the existing 4. Now:
  //   - hasReal (PASS or FAIL row exists): pass when fail===0, else fail
  //   - blocked > 0 AND no PASS/FAIL: amber 'blocked' (config gap)
  //   - allSkip (only SKIP rows, no PASS/FAIL/BLOCKED): yellow 'concerns'
  //   - !has (no rows at all): grey 'skip' (tool wasn't part of suite)
  // R33.7/R33.8 \u2014 per-tool gate uses effective pass rate against the
  // \u226595% bar. The user's contract: every test type with a judge-able
  // denominator must clear 95%. Below 80% \u2192 'fail' (red); 80-94% \u2192
  // 'concerns' (yellow); \u226595% \u2192 'pass' (green); only-BLOCKED \u2192 'blocked'
  // (amber); no rows at all \u2192 'skip' (grey).
  const PER_TOOL_PASS_PCT = 95
  const PER_TOOL_FAIL_PCT = 80
  const _toolStatus = (
    pass: number, fail: number, skip: number, blocked: number,
    has: boolean, sutBugs: number = 0, testGenBugs: number = 0,
  ): 'pass' | 'fail' | 'concerns' | 'blocked' | 'skip' => {
    if (!has) return 'skip'
    const pct = _effectivePct(pass, fail, sutBugs, testGenBugs)
    if (pct === null) {
      // No judge-able rows. If everything was BLOCKED \u2192 amber; if
      // everything was SKIP \u2192 yellow concerns; otherwise grey skip.
      if (blocked > 0) return 'blocked'
      if (skip > 0) return 'concerns'
      return 'skip'
    }
    if (pct >= PER_TOOL_PASS_PCT) return 'pass'
    if (pct >= PER_TOOL_FAIL_PCT) return 'concerns'
    return 'fail'
  }
  // R71.5 \u2014 translate blocked_reason codes into operator-actionable hints
  // shown on the tile. Pre-R71.5 every blocked tile said "fill env vars"
  // \u2014 wrong for discovery_empty (refresh auth) and
  // no_k6_specs_in_inventory (regenerate by tool).
  const _blockedHintFor = (reason: string | null): string => {
    if (!reason || reason === 'unknown') return 'fill env vars'
    switch (reason) {
      case 'discovery_empty':
      case 'dom_catalog_empty':
        return 'refresh auth \u2014 DOM catalog empty'
      case 'no_k6_specs_in_inventory':
        return 'regenerate k6 \u2014 inventory empty'
      case 'unresolved_vars':
      case 'config_incomplete':
        return 'fill env vars'
      case 'grounding_violation':
        return 'self-heal queued'
      case 'no_api_surface':
        return 'no API surface — recommend UI-only test'
      case 'playwright_grounding_violation':
        return 'PW gen quality — re-gen with stronger hints'
      // R140.B — operator-facing CTA for the two new R140.A violation kinds.
      // These kinds usually appear nested under the parent
      // `playwright_grounding_violation` reason (via R111.E violation_kinds
      // breakdown), but a future R-phase may surface them as top-level
      // reasons; keep the CTAs consistent so operators always see the
      // R45.2 paste action.
      case 'catalog_aria_label_unknown':
      case 'catalog_text_unknown':
        return 'DOM catalog missing UI elements — re-paste fresh session token (R45.2) so R140.C BFS crawl populates more routes'
      case 'spec_drift':
      case 'openapi_placement_drift':
        return 'spec drift \u2014 re-run discovery'
      case 'auth_failed':
      case 'auth_pre_flight_failed':
        return 'refresh auth'
      // R154.C — a spec carried destructive patterns and lacked the opt-in.
      case 'destructive_test_blocked_default_deny':
        return 'destructive test blocked (default-deny) — set up a sandbox namespace + ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1 & SUT_TEST_DATA_NAMESPACE to opt in'
      // R144.D — SSR page-navigation auth-stale skip lane.
      case 'auth_stale_url_redirect':
      case 'auth_stale_unknown':
        return 'auth stale — page navigation landed on login; click Refresh Auth (R45.2 paste)'
      default:
        return reason.replace(/_/g, ' ')
    }
  }
  // R93.C — per-blocked_reason breakdown rows for the QUALITY GATE
  // section. Pre-R93.C this was a single "Configuration completeness:
  // N blocked" tile that aggregated grounding_violation +
  // k6_stub_content + discovery_empty + no_*_specs_in_inventory.
  // Operators saw "143 blocked" + clicked Settings → Variables looking
  // for 143 unfilled vars; found none. R93.C truthfully decomposes the
  // BLOCKED count by reason with per-reason CTAs.
  const _buildBlockedReasonRows = (rows: any[]): Array<{
    name: string; st: 'blocked'; actual: string; expected: string;
  }> => {
    type ReasonInfo = { title: string; cta: string }
    const REASON_COPY: Record<string, ReasonInfo> = {
      grounding_violation: {
        title: 'Newman gen-quality (grounding violations)',
        cta: 'auto-heal in progress (R93.B alternatives hint)',
      },
      no_api_surface: {
        title: 'No API surface for this requirement',
        cta: 'Recommend UI-only test (E2E) — req has no SUT API surface',
      },
      playwright_grounding_violation: {
        title: 'Playwright gen-quality (R102.C dispatch BLOCK)',
        cta: 'Re-gen with stronger hints (R102.B BEFORE/AFTER snippets)',
      },
      // R154.C — destructive-pattern spec blocked by the default-deny gate.
      destructive_test_blocked_default_deny: {
        title: 'Destructive test blocked (R154.C default-deny)',
        cta: 'Read-only by default — opt in with a sandbox namespace + ARTA_R154_ALLOW_DESTRUCTIVE_TESTS=1 & SUT_TEST_DATA_NAMESPACE',
      },
      // R144.D — SSR page-navigation auth-stale skip (distinct from the API lane).
      auth_stale_url_redirect: {
        title: 'Auth stale — SSR page redirected to login (R144.D)',
        cta: 'Click Refresh Auth (R45.2 paste) — the browser navigation cookie is stale',
      },
      // R140.B — operator-facing copy for the new R140.A violation kinds.
      // These kinds USUALLY appear nested under the parent
      // `playwright_grounding_violation` reason via R111.E breakdown (so the
      // entries here are belt-and-suspenders for any future code path that
      // surfaces them as top-level reasons). The kindLabel map below
      // (R140.B kind-label) handles the nested display path.
      catalog_aria_label_unknown: {
        title: 'PW gen — getByLabel not in DOM catalog (R140.A)',
        cta: 'Re-paste fresh session token (R45.2); R140.C BFS crawl populates more routes',
      },
      catalog_text_unknown: {
        title: 'PW gen — getByText not in DOM catalog (R140.A)',
        cta: 'Re-paste fresh session token (R45.2); R140.C BFS crawl populates more routes',
      },
      pytest_grounding_violation: {
        // R113.L — per-tool BLOCKED reason for pytest gen-quality (parallel
        // to playwright_grounding_violation). Pre-R113.L the pytest tile
        // mis-mapped to "Newman gen-quality" via the generic
        // `grounding_violation` key — wrong-tool routing.
        title: 'Pytest gen-quality (R113.L dispatch BLOCK)',
        cta: 'Re-gen with R111.L BEFORE/AFTER hints (assertion_value_drift)',
      },
      tier_filter_excluded: {
        // R114.F.2 — pytest adversarial / tier3 tests excluded by smoke
        // tier filter (intentional design: tier3 stays nightly-only via
        // regression/full). Pre-R114.F.2 these SKIPs had empty metadata
        // → operator perceived adversarial tests as "not executing"
        // even though the filter behavior was intentional.
        title: 'Test deferred (tier filter — operator-configurable)',
        cta: 'Run suite_type=regression or full to include adversarial/tier3 tests',
      },
      analytics_value_drift: {
        // R115.H — analytics recipe-vs-SUT value drift. Pre-R115.H these
        // surfaced as opaque pytest FAILs (13 in run-f45f85). R115.H's
        // `assert_recipe_value` helper now emits pytest.skip with truthful
        // skip_reason so operators distinguish "ARTA recipe outdated"
        // from "real SUT analytics regression".
        title: 'Analytics recipe-vs-SUT drift',
        cta: 'Update recipe at fixtures/analytics/<req>/recipe.json OR file SUT regression',
      },
      k6_stub_content: {
        title: 'k6 inventory (stub content)',
        cta: 'rescue queued (POST /api/admin/rescue-k6-quarantine)',
      },
      discovery_empty: {
        title: 'Playwright (DOM catalog empty)',
        cta: 'refresh auth via Refresh Auth modal',
      },
      dom_catalog_empty: {
        title: 'Playwright (DOM catalog empty)',
        cta: 'refresh auth via Refresh Auth modal',
      },
      no_k6_specs_in_inventory: {
        title: 'k6 inventory empty',
        cta: 'Regenerate by Tool → k6',
      },
      no_pytest_specs_in_inventory: {
        title: 'Pytest inventory empty',
        cta: 'Regenerate by Tool → pytest',
      },
      no_axe_specs_in_inventory: {
        title: 'Axe inventory empty',
        cta: 'Regenerate by Tool → axe',
      },
      no_selenium_specs_in_inventory: {
        title: 'Selenium inventory empty',
        cta: 'Regenerate by Tool → selenium',
      },
      no_cypress_specs_in_inventory: {
        title: 'Cypress inventory empty',
        cta: 'Regenerate by Tool → cypress',
      },
      selenium_dispatcher_not_implemented: {
        title: 'Selenium dispatcher not implemented',
        cta: 'remove from tool selector (roadmap)',
      },
      cypress_dispatcher_not_implemented: {
        title: 'Cypress dispatcher not implemented',
        cta: 'remove from tool selector (roadmap)',
      },
      unresolved_vars: {
        title: 'Configuration completeness (unfilled env vars)',
        cta: 'Settings → Environments → Variables',
      },
      config_incomplete: {
        title: 'Configuration completeness (unfilled env vars)',
        cta: 'Settings → Environments → Variables',
      },
      auth_failed: {
        title: 'Auth pre-flight failed',
        cta: 'refresh auth via Refresh Auth modal',
      },
      auth_pre_flight_failed: {
        title: 'Auth pre-flight failed',
        cta: 'refresh auth via Refresh Auth modal',
      },
      spec_drift: {
        title: 'Spec drift (OpenAPI mismatch)',
        cta: 'refresh discovery; bulk-regen-newman-for-bearer',
      },
      openapi_placement_drift: {
        title: 'OpenAPI parameter placement drift',
        cta: 'refresh discovery; bulk-regen-newman',
      },
      unknown_tool_requested: {
        title: 'Unknown tool requested',
        cta: 'remove from tool selector',
      },
    }
    const counts: Record<string, number> = {}
    // R111.E — per-violation-kind aggregator. When PW spec is BLOCKED by
    // R102.C, metadata.violation_kinds is a {kind: count} map. Surface
    // the dominant violation_kind alongside the parent blocked_reason so
    // the operator knows EXACTLY which gen-quality bug class to re-target.
    const violationKindCounts: Record<string, number> = {}
    for (const r of rows) {
      if (r.status !== 'BLOCKED') continue
      const md = (r as any).metadata || (r as any).metadata_ || {}
      const mdObj = typeof md === 'string'
        ? (() => { try { return JSON.parse(md) } catch { return {} } })()
        : md
      // R111.J — when metadata.blocked_reasons is a list (multi-reason),
      // count EACH reason separately. The legacy single-reason path
      // (blocked_reason field) still works as fallback.
      const reasonsList: any[] = Array.isArray(mdObj?.blocked_reasons)
        ? mdObj.blocked_reasons
        : null
      if (reasonsList && reasonsList.length > 0) {
        for (const entry of reasonsList) {
          const k = String(entry?.kind || 'unknown').toLowerCase()
          counts[k] = (counts[k] || 0) + 1
        }
      } else {
        const reason = (
          (mdObj?.blocked_reason || mdObj?.blockedReason) || 'unknown'
        ).toString().toLowerCase()
        counts[reason] = (counts[reason] || 0) + 1
      }
      // R111.E — accumulate violation_kinds breakdown when present
      const vk = mdObj?.violation_kinds
      if (vk && typeof vk === 'object') {
        for (const [kind, n] of Object.entries(vk as Record<string, number>)) {
          violationKindCounts[kind] = (violationKindCounts[kind] || 0) + Number(n || 0)
        }
      }
    }
    const out: Array<{ name: string; st: 'blocked'; actual: string; expected: string }> = []
    // Sort reasons by count descending so the biggest blocker shows first
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1])
    for (const [reason, count] of sorted) {
      const info = REASON_COPY[reason] || {
        title: `Blocked: ${reason.replace(/_/g, ' ')}`,
        cta: 'investigate via container logs',
      }
      // R111.E — if this row is the PW grounding-violation aggregate, append
      // the violation_kinds breakdown for operator-actionable detail.
      //
      // R140.B kind-label map — make the violation-kind names human-friendly
      // in the breakdown summary. Unmapped kinds fall back to the raw key
      // (with underscores replaced by spaces) so future kinds don't go
      // unrendered.
      const VIOLATION_KIND_LABEL: Record<string, string> = {
        hallucinated_testid: 'unknown testid',
        hallucinated_role_name: 'unknown role+name',
        unknown_endpoint: 'hallucinated endpoint',
        // R140.A — new violation kinds for getByLabel/getByText catalog grounding
        catalog_aria_label_unknown: 'unknown aria-label',
        catalog_text_unknown: 'unknown visible text',
        // R95.3 / R101.C
        bad_playwright_api: 'PW API misuse',
        // R113.I.2 / R114.A
        pw_syntax_error: 'TS syntax error',
        // R123.A / R123.B
        undefined_symbol: 'undefined symbol',
      }
      const _kindLabel = (k: string): string =>
        VIOLATION_KIND_LABEL[k] || k.replace(/_/g, ' ')
      let actualMsg = `${count} blocked — ${info.cta}`
      if (reason === 'playwright_grounding_violation' && Object.keys(violationKindCounts).length > 0) {
        const kindSummary = Object.entries(violationKindCounts)
          .sort((a, b) => b[1] - a[1])
          .slice(0, 4)
          .map(([k, n]) => `${n}× ${_kindLabel(k)}`)
          .join(', ')
        actualMsg = `${count} blocked (${kindSummary}) — ${info.cta}`
      }
      out.push({
        name: info.title,
        st: 'blocked',
        actual: actualMsg,
        expected: '0 blocked',
      })
    }

    // R112.F — count R112.E auth-stale SKIPs and surface as a dashboard row.
    // R112.E injects `test.skip(true, '[ARTA R112.E] Auth state stale...')` after
    // page.goto when the page actually loaded login form. The SKIP row's
    // error_message carries the marker; aggregate count surfaces a per-run
    // banner so operator immediately sees the auth-stale pattern without
    // scrolling through N skipped specs.
    // R144.D — PREFER the backend's AUTHORITATIVE pre-computed auth-stale count
    // (pillar_4.auth_stale_skips / skip_by_cause), which counts by the structured
    // skip-cause marker (auth_stale_url_redirect | auth_stale_unknown). The old
    // client substring scan required BOTH "R112.E" AND "Auth state stale" and so
    // MISSED the auth_stale_url_redirect variant → undercounted. Fall back to a
    // (broadened) substring scan only when the backend field is absent (old runs).
    const _p4 = missionReport?.pillar_4_report_sut_quality
    let authStaleSkipCount = 0
    if (_p4 && typeof _p4.auth_stale_skips === 'number') {
      authStaleSkipCount = _p4.auth_stale_skips
    } else if (_p4?.skip_by_cause && typeof _p4.skip_by_cause === 'object') {
      authStaleSkipCount = (_p4.skip_by_cause['auth_stale_url_redirect'] || 0)
        + (_p4.skip_by_cause['auth_stale_unknown'] || 0)
    } else {
      for (const r of rows) {
        if (r.status !== 'SKIP') continue
        const errMsg = (r as any).error_message || (r as any).errorMessage || ''
        if (typeof errMsg === 'string' && errMsg.includes('R112.E')
            && (errMsg.includes('Auth state stale') || errMsg.includes('auth_stale'))) {
          authStaleSkipCount++
        }
      }
    }
    if (authStaleSkipCount > 0) {
      out.push({
        name: 'Playwright auth-state stale (R112.E)',
        st: 'blocked',
        actual: `${authStaleSkipCount} spec(s) skipped — page.goto landed on login form. Operator: click Refresh Auth in test-explorer banner.`,
        expected: '0 skipped',
      })
    }

    return out
  }

  const _toolActual = (
    pass: number, fail: number, skip: number, blocked: number,
    has: boolean, sutBugs: number = 0, testGenBugs: number = 0,
    dominantBlockedReason: string | null = null,
  ): string => {
    if (!has) return 'not executed'
    const pct = _effectivePct(pass, fail, sutBugs, testGenBugs)
    if (pct === null) {
      if (blocked > 0) return `${blocked} blocked \u2014 ${_blockedHintFor(dominantBlockedReason)}`
      if (skip > 0) return `${skip} skip \u2014 auth blocked`
      return 'no judge-able rows'
    }
    // R33.8 + R36.7 \u2014 three-tier render: effective% (gate-decisive), raw
    // counts (sanity check), projected% (operator's recovery target).
    //
    // Effective: pass / (pass + real_fail), excludes BLOCKED + SUT bugs
    //   + test_gen_bugs. Decides the gate.
    // Raw: pass / (pass + fail), the operator's "did anything actually
    //   work?" sanity check.
    // Projected: assumes operator fills BLOCKED \u2192 PASS, ARTA self-heals
    //   test_gen_bugs \u2192 PASS, SUT bugs stay (real backend issues).
    //   Upper bound on what THIS suite can reach with operator action.
    const real_fail = Math.max(0, fail - sutBugs - testGenBugs)
    const rawTotal = pass + fail
    const rawPct = rawTotal > 0 ? Math.round((100 * pass / rawTotal) * 10) / 10 : null
    // Projected: pass + blocked-after-fill + test_gen_bugs-after-heal
    // divided by all judge-able rows post-recovery. SUT bugs unfixable.
    const projDenom = pass + real_fail + blocked + testGenBugs
    const projNum = pass + blocked + testGenBugs
    const projPct = projDenom > 0 ? Math.round((100 * projNum / projDenom) * 10) / 10 : null
    let tag = ''
    if (sutBugs > 0) tag += ` \u00b7 ${sutBugs} SUT bug${sutBugs > 1 ? 's' : ''}`
    if (testGenBugs > 0) tag += ` \u00b7 ${testGenBugs} self-heal queued`
    if (blocked > 0) tag += ` \u00b7 ${blocked} blocked`
    // Compact format: "EFF% (P/E eff \u00b7 raw R% \u00b7 proj P%)"
    const parts = [`${pct}% eff`]
    if (rawPct !== null && rawPct !== pct) parts.push(`raw ${rawPct}%`)
    if (projPct !== null && projPct !== pct && projPct >= 80) {
      parts.push(`projected ${projPct}%`)
    }
    return `${parts.join(' \u00b7 ')}${tag}`
  }

  const _attrRate = attributablePassRate(missionReport)
  const gateChecks = [
    // B2/B4 \u2014 the TRUTHFUL credibility headline: ARTA-attributable pass rate
    // = PASS / (TOTAL \u2212 sut_regression_span \u2212 BLOCKED). This is the metric the
    // mission hinges on (excludes SUT bugs ARTA correctly found + BLOCKED
    // non-runs). Raw passed/total stays as a clearly-labeled secondary \u2014 never
    // present raw as the truthful rate.
    ...(_attrRate !== null ? [{
      name: 'ARTA-attributable pass rate',
      st: _attrRate >= 90 ? 'pass' as const : (total === 0 ? 'skip' as const : 'fail' as const),
      actual: `${Math.round(_attrRate)}%`,
      expected: '\u226590% (excl. SUT bugs + BLOCKED)',
    }] : []),
    { name: _attrRate !== null ? 'Overall pass rate (raw)' : 'Overall pass rate', st: passRate >= 90 ? 'pass' : total === 0 ? 'skip' : 'fail', actual: `${passRate}%`, expected: '\u226590%' },
    { name: 'P0 pass rate', st: failedCount === 0 ? 'pass' : 'fail', actual: failedCount === 0 ? '100%' : `${Math.round((total - failedCount) / Math.max(total, 1) * 100)}%`, expected: '100%' },
    // R28.1 \u2014 Open P0 defects fetched from DB (was hardcoded '0').
    // R77.4 \u2014 distinguish "loading", "auth failure", and "no defects" so
    // operators don't see a misleading "0" when their session expired.
    // R115.E \u2014 split run-scoped vs all-time P0 counts. Pre-R115.E the
    // tile fell back to all-time when run-scoped returned 0 \u2014 operator
    // confused "did this run create N P0?" when N was historical.
    {
      name: 'P0 defects from THIS run',
      st: openP0Defects === null
        ? (openP0Error ? 'blocked' : 'skip')
        : openP0Defects === 0
        ? 'pass'
        : 'fail',
      actual: openP0Defects === null
        ? (openP0Error
            ? (openP0Error.startsWith('auth_failure') ? 'auth expired \u2014 re-login' : `error: ${openP0Error}`)
            : 'loading...')
        : String(openP0Defects),
      expected: '\u22640',
    },
    // R115.E \u2014 secondary tile: all-time open P0 (project-wide accumulated).
    // Helps operator distinguish run-specific signal from historical backlog.
    ...(allTimeP0Defects !== null && allTimeP0Defects > 0 ? [{
      name: 'All-time open P0 (project)',
      st: 'skip' as const,
      actual: `${allTimeP0Defects} accumulated (\u2260 this run)`,
      expected: 'triage backlog \u2014 see Defects page',
    }] : []),
    // R115.F \u2014 self-heal queue ETA tile. Operator visibility on test-gen-bug
    // regen pipeline throughput. Helps set expectations for "when will my
    // 2049 queued items drain?". Drain rate 20/cycle \u00d7 5min = 4/min.
    ...(regenQueueDepth !== null && regenQueueDepth > 0 ? [{
      name: 'Self-heal regen queue',
      st: regenQueueDepth > 500 ? 'fail' as const
          : regenQueueDepth > 100 ? 'skip' as const
          : 'pass' as const,
      actual: `${regenQueueDepth} queued${regenQueueEta !== null ? ` \u00b7 ETA \u2248 ${regenQueueEta} min` : ''}`,
      expected: 'drain at 20/cycle \u00b7 5min cycles',
    }] : []),
    // R93.C \u2014 per-blocked_reason breakdown. Pre-R93.C this row was a
    // single "Configuration completeness: N blocked" aggregate that
    // mislabeled the underlying root causes (grounding_violation,
    // k6_stub_content, discovery_empty, no_*_specs_in_inventory). Operators
    // searched Settings for unfilled vars that didn't exist. R93.C splits
    // the aggregate into per-reason rows so each row has its own
    // actionable CTA path.
    ...(totalBlocked > 0 ? _buildBlockedReasonRows(results) : []),
    { name: 'E2E UI tests (Playwright)', st: _toolStatus(pwPass, pwFail, pwSkip, pwBlocked, hasPw, pwSutBugs, pwTestGenBugs), actual: _toolActual(pwPass, pwFail, pwSkip, pwBlocked, hasPw, pwSutBugs, pwTestGenBugs, pwBlockedReason), expected: '\u226595% eff' },
    { name: 'API tests (Newman)', st: _toolStatus(apiPass, apiFail, apiSkip, apiBlocked, hasApi, apiSutBugs, apiTestGenBugs), actual: _toolActual(apiPass, apiFail, apiSkip, apiBlocked, hasApi, apiSutBugs, apiTestGenBugs, apiBlockedReason), expected: '\u226595% eff' },
    { name: 'NFR Performance (k6)', st: _toolStatus(perfPass, perfFail, perfSkip, perfBlocked, hasPerf, perfSutBugs, perfTestGenBugs), actual: _toolActual(perfPass, perfFail, perfSkip, perfBlocked, hasPerf, perfSutBugs, perfTestGenBugs, perfBlockedReason), expected: '\u226595% eff' },
    { name: 'NFR Security (ZAP)', st: _toolStatus(secPass, secFail, secSkip, secBlocked, hasSec, secSutBugs, secTestGenBugs), actual: _toolActual(secPass, secFail, secSkip, secBlocked, hasSec, secSutBugs, secTestGenBugs, secBlockedReason), expected: '\u226595% eff' },
    // B3 \u2014 TRUTHFUL a11y verdict from pillar_4.a11y.status: `clean` only when
    // axe actually SCANNED a real authenticated page; `not_assessed` when every
    // axe row was blocked/skipped (login wall / auth-stale) \u2014 NEVER render that
    // as a green pass. The raw axe execution row below keeps the PASS/FAIL/
    // BLOCKED detail. Falls back to the raw row alone for old runs (no a11y).
    ...((() => {
      const v = a11yVerdict(missionReport)
      if (v.status === 'unknown') return []
      return [{
        name: 'Accessibility verdict (WCAG 2.1 AA)',
        st: v.status === 'clean' ? 'pass' as const
            : v.status === 'not_assessed' ? 'skip' as const
            : 'fail' as const,
        actual: v.status === 'clean' ? 'clean \u2014 real scan, 0 violations'
              : v.status === 'not_assessed' ? 'not assessed \u2014 auth/scan blocked'
              : `${v.critical} critical \u00b7 ${v.moderate} moderate`,
        expected: v.status === 'not_assessed' ? 'needs a fresh-auth scan' : '0 critical/serious',
      }]
    })()),
    { name: 'Accessibility (axe)', st: _toolStatus(a11yPass, a11yFail, a11ySkip, a11yBlocked, hasA11y, a11ySutBugs, a11yTestGenBugs), actual: _toolActual(a11yPass, a11yFail, a11ySkip, a11yBlocked, hasA11y, a11ySutBugs, a11yTestGenBugs, a11yBlockedReason), expected: '\u226595% eff' },
    { name: 'Analytics (pytest)', st: _toolStatus(anPass, anFail, anSkip, anBlocked, hasAnalytics, anSutBugs, anTestGenBugs), actual: _toolActual(anPass, anFail, anSkip, anBlocked, hasAnalytics, anSutBugs, anTestGenBugs, anBlockedReason), expected: '\u226595% eff' },
    // R123.E \u2014 Defect-class breakdown row. Pre-R123.E the operator
    // saw "API tests: 2681 FAIL" + needed to drill DB queries to find
    // the 1 sut_regression critical buried under 2681 raw rows. R123.E
    // surfaces the truthful actionable breakdown (sut_regression vs
    // operator_review vs test_gen_bug) at-a-glance, reusing the
    // missionReport.pillar_4_report_sut_quality.by_category data that
    // R115.G already loads. Operator sees "Of N FAILs, X are real SUT
    // bugs (Jira queue), Y are ARTA-fixable (auto-healing), Z need
    // operator review" \u2014 the actionable signal hidden under raw noise.
    ...((() => {
      const byCat = (missionReport?.pillar_4_report_sut_quality?.by_category) || null
      if (!byCat) return []
      // B1/B5 — by_category is NESTED {category:{severity:count}}. Reading flat
      // keys (sut_regression_critical) returned undefined→0 and HID real SUT
      // bugs. Sum ALL severities per category via the shared helpers.
      const sutCrit = severityCount(byCat, 'sut_regression', 'critical')
      const sutHigh = severityCount(byCat, 'sut_regression', 'high')
      const sutReg = sumCategory(byCat, 'sut_regression')
      const opReview = sumCategory(byCat, 'operator_review')
      const testGen = sumCategory(byCat, 'test_gen_bug')
      const sutContract = sumCategory(byCat, 'sut_contract_change')
      const total = sutReg + opReview + testGen + sutContract
      if (total === 0) return []
      return [{
        name: 'Defect breakdown (R123.E)',
        st: sutCrit > 0 ? 'fail' as const : (opReview > 0 ? 'skip' as const : 'pass' as const),
        actual: (
          `${sutCrit > 0 ? sutCrit + ' SUT-critical \u00b7 ' : ''}` +
          `${sutHigh > 0 ? sutHigh + ' SUT-high \u00b7 ' : ''}` +
          `${sutContract > 0 ? sutContract + ' contract-change \u00b7 ' : ''}` +
          `${opReview} operator-review \u00b7 ${testGen} auto-heal`
        ),
        expected: 'actionable signal hidden under raw FAIL noise',
      }]
    })()),
    // R304 \u2014 REPORTING FIDELITY. Surfaces the R259/R260 fidelity block the
    // frontend previously dropped: how ARTA's failures split into confident SUT
    // signal vs ARTA-noise vs assessed-pending-adjudication vs truly-not-assessed,
    // plus the R260 confidence that says whether the SUT verdict is trustworthy.
    // `adjudication_pending` (evidence-backed operator_review, e.g. "SUT ignores
    // query params") is ASSESSED \u2014 distinct from not-assessed (no evidence).
    ...((() => {
      const rf = reportingFidelity(missionReport)
      if (!rf) return []
      const st = rf.confidence === 'high' ? 'pass' as const
        : (rf.confidence === 'low' || rf.confidence === 'none') ? 'skip' as const : 'skip' as const
      return [{
        name: 'Reporting fidelity (R304)',
        st,
        actual: (
          `confidence ${rf.confidence.toUpperCase()} \u00b7 ${rf.sutSignal} SUT-signal \u00b7 ` +
          `${rf.adjudicationPending} adjudication-pending \u00b7 ` +
          `${rf.notAssessed} not-assessed (${Math.round(rf.notAssessedPct * 100)}%) \u00b7 ` +
          `${Math.round(rf.artaDefectRate * 100)}% ARTA-noise` +
          (rf.scoreReason ? ` \u2014 ${rf.scoreReason}` : '')
        ),
        expected: 'high confidence: SUT verdict trustworthy (low not-assessed)',
      }]
    })()),
    // F2/F3 (R218) \u2014 per-REQUIREMENT, RISK-WEIGHTED SUT verdict (the mission's
    // purpose: "did requirement X pass?"). Summarises the by_requirement block
    // into one at-a-glance row: how many requirements are clean vs degraded
    // (high-risk defect) vs blocked (not measured). DEGRADED on ANY requirement
    // makes the row fail \u2014 a critical-requirement defect must not read clean.
    ...((() => {
      const byReq = (missionReport?.pillar_4_report_sut_quality?.by_requirement) || null
      if (!byReq || byReq.length === 0) return []
      const band = (v: string) => (v || '').toUpperCase()
      const clean = byReq.filter((r: any) => band(r.verdict) === 'CLEAN').length
      const degraded = byReq.filter((r: any) => ['DEGRADED', 'FAIL'].includes(band(r.verdict))).length
      const mixed = byReq.filter((r: any) => band(r.verdict) === 'MIXED').length
      const blocked = byReq.filter((r: any) => ['BLOCKED', 'SKIPPED'].includes(band(r.verdict))).length
      return [{
        name: 'Per-requirement SUT verdict (R218 F2)',
        st: degraded > 0 ? 'fail' as const : (blocked > 0 || mixed > 0 ? 'skip' as const : 'pass' as const),
        actual: (
          `${byReq.length} req \u00b7 ${clean} clean` +
          `${mixed > 0 ? ' \u00b7 ' + mixed + ' mixed' : ''}` +
          `${degraded > 0 ? ' \u00b7 ' + degraded + ' degraded (high-risk defect)' : ''}` +
          `${blocked > 0 ? ' \u00b7 ' + blocked + ' not-measured' : ''}`
        ),
        expected: 'every requirement measured + risk-weighted',
      }]
    })()),
    // R123.E (run-level) \u2014 SUT-health-outage row when R123.C's
    // pre-flight detected \u226580% 5xx. Operator sees ONE truthful row
    // instead of 2681 raw FAILs.
    ...((() => {
      const sutHealthRow = results.find((t: any) =>
        t.automation_tool === 'sut_health' &&
        t.metadata?.blocked_reason === 'sut_health_outage'
      )
      if (!sutHealthRow) return []
      const rate = sutHealthRow.metadata?.five_xx_rate ?? 0
      return [{
        name: 'SUT health degraded (R123.C)',
        st: 'fail' as const,
        actual: `${Math.round(rate * 100)}% 5xx on probed endpoints`,
        expected: 'SUT-team queue \u00b7 Newman/PW/k6 noise expected',
      }]
    })()),
    // R33.6 \u2014 when ARTA detected real SUT bugs, surface them as a
    // separate (informational) row so operators see "ARTA detected N
    // backend regressions" alongside test-quality metrics. Detection
    // is not a failure \u2014 the gate doesn't fail because of these.
    ...(totalSutBugs > 0 ? [{
      name: 'SUT bugs detected by ARTA',
      st: 'pass' as const,
      actual: `${totalSutBugs} detected`,
      expected: '(detection metric)',
    }] : []),
    // R34.3 \u2014 surface auto-heal queue depth. Test-gen bugs are queued
    // for regeneration via R30.3 + Tier-1 autofix; not test failures
    // operators need to investigate. Counter, not a quality regression.
    ...(totalTestGenBugs > 0 ? [{
      name: 'Self-heal queue (test-gen bugs)',
      st: 'pass' as const,
      actual: `${totalTestGenBugs} queued`,
      expected: '(auto-heal pipeline)',
    }] : []),
  ]

  const allTypes = ['playwright', 'newman', 'k6', 'zap']

  return (
    <div className="p-5 overflow-y-auto" style={{ maxHeight: 600 }}>
      {/* LIVE indicator */}
      {isLive && (
        <div className="mb-3 flex items-center gap-2">
          <span className="text-[10px] px-2 py-0.5 rounded-full font-semibold animate-pulse"
                style={{ background: 'rgba(16,185,129,0.15)', color: '#10b981', border: '1px solid #10b98130' }}>
            &#x25CF; LIVE
          </span>
          <span className="text-[11px]" style={{ color: '#94a3b8' }}>Polling for updates…</span>
        </div>
      )}

      {/* R115.G — ARTA Mission Outcome consolidated report. Single-view
          operator dashboard for ARTA's 4-pillar autonomous ATDD delivery:
          Pillar 1 (test cases), Pillar 1b (test scripts), Pillar 2
          (execute flawlessly), Pillar 4 (report SUT quality). Pre-R115.G:
          operator ran ad-hoc DB queries to assess per-pillar health.
          R115.G surfaces ALL FOUR on one page with run-specific CTAs. */}
      {missionReport && (() => {
        const SCORE_COLOR: Record<string, { fg: string; bg: string; bd: string }> = {
          CLEAN:       { fg: '#10b981', bg: 'rgba(16,185,129,0.10)', bd: '#10b98140' },
          OPTIMISTIC:  { fg: '#06b6d4', bg: 'rgba(6,182,212,0.10)',  bd: '#06b6d440' },
          MIXED:       { fg: '#f59e0b', bg: 'rgba(245,158,11,0.10)', bd: '#f59e0b40' },
          PESSIMISTIC: { fg: '#fb7185', bg: 'rgba(251,113,133,0.10)', bd: '#fb718540' },
        }
        const PILLARS = [
          { key: 'pillar_1_test_case_quality',   label: 'P1 · Test Cases',   icon: '📝' },
          { key: 'pillar_1b_test_script_quality', label: 'P1b · Test Scripts', icon: '🧪' },
          { key: 'pillar_2_execute_flawlessly',  label: 'P2 · Execution',    icon: '⚙️' },
          { key: 'pillar_4_report_sut_quality',  label: 'P4 · SUT Report',   icon: '📊' },
        ]
        const band = missionReport.outcome_band || 'MIXED'
        const bandColor = SCORE_COLOR[band] || SCORE_COLOR.MIXED
        const ctas = Array.isArray(missionReport.actionable_ctas) ? missionReport.actionable_ctas : []
        return (
          <div className="mb-4 rounded-lg px-4 py-3" style={{
            background: 'rgba(99,102,241,0.05)',
            border: '1px solid rgba(99,102,241,0.20)',
          }}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-3">
                <span className="text-[14px] font-semibold" style={{ color: '#e5e7eb' }}>
                  ARTA Mission Outcome
                </span>
                <span className="text-[10px] px-2 py-0.5 rounded-full font-bold"
                      style={{ color: bandColor.fg, background: bandColor.bg, border: `1px solid ${bandColor.bd}` }}>
                  {band}
                </span>
              </div>
              <span className="text-[10px]" style={{ color: '#64748b' }}>
                {missionReport.wallclock_sec ? `${Math.round(missionReport.wallclock_sec / 60)} min` : ''}
              </span>
            </div>
            <div className="grid grid-cols-4 gap-2 mb-3">
              {PILLARS.map(p => {
                const pdata = (missionReport as any)[p.key] || {}
                const score = pdata.score || 'UNKNOWN'
                const c = SCORE_COLOR[score] || { fg: '#64748b', bg: 'rgba(100,116,139,0.10)', bd: '#64748b40' }
                return (
                  <div key={p.key} className="rounded px-2 py-2 text-center"
                       style={{ background: c.bg, border: `1px solid ${c.bd}` }}>
                    <div className="text-[18px] mb-0.5">{p.icon}</div>
                    <div className="text-[10px] font-semibold" style={{ color: '#94a3b8' }}>{p.label}</div>
                    <div className="text-[11px] font-bold mt-1" style={{ color: c.fg }}>{score}</div>
                  </div>
                )
              })}
            </div>
            {ctas.length > 0 && (
              <div>
                <div className="text-[10px] font-semibold mb-1" style={{ color: '#94a3b8' }}>
                  Actionable Next Steps:
                </div>
                <ul className="space-y-1">
                  {ctas.slice(0, 6).map((cta: string, idx: number) => (
                    <li key={idx} className="text-[11px] flex items-start gap-1.5"
                        style={{ color: '#cbd5e1' }}>
                      <span style={{ color: '#6366f1' }}>›</span>
                      <span>{cta}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )
      })()}

      {/* FE-sync P3 — run-scoped SUT-quality drill-downs (top 5xx / per-requirement /
          auth-scope / evidence package). Self-hides until there's data. */}
      <SutQualityDrilldowns runId={selected?.run_id || selected?.id} />

      {/* R38.8 — Auth pre-flight failed banner. R37.5's probe-side
          liveness check writes `auth_failed.flag` when /api/v1/users/me
          returns 401 or HTML; the orchestrator stamps
          `auth_pre_flight_failed=true` + `pre_run_diagnosis` on the
          run. This banner makes the gap actionable in ONE click —
          before the operator scrolls through 0% tool results trying to
          understand why every test failed. Distinct from R33.11
          (zero-envvars) because pre-flight catches the gap BEFORE the
          probe even completes; both can fire on the same run if auth
          is stale enough. */}
      {(selected.auth_pre_flight_failed === true || selected.auth_pre_flight_failed === 'true') && (() => {
        // R39.6 — reason-specific copy. R39.1 + R37.5 stamp distinct
        // reasons (har_empty, no_json_responses, all_api_calls_failed,
        // cookie_invalid_or_expired, no_auth_credential, spa_login_redirect)
        // — render the right diagnosis + remediation hint per reason
        // instead of the generic "cookie likely expired" stub.
        const REASON_COPY: Record<string, { headline: string; detail: string }> = {
          cookie_invalid_or_expired: {
            headline: 'Auth cookie expired',
            detail: 'The stored cookie returns 401. Paste a fresh cookie from the SUT.',
          },
          spa_login_redirect: {
            headline: 'SPA served the login screen',
            detail:
              'The SPA does not consider this session authenticated. Paste a fresh cookie OR upload a Playwright storage state file.',
          },
          no_auth_credential: {
            headline: 'No auth credential available',
            detail:
              'No cookie + no storage state file. Paste a cookie or upload `.arta/environments/<env>-storage.json` before running discovery.',
          },
          har_empty: {
            headline: 'Discovery probe produced 0 records',
            detail:
              'The probe ran but captured no traffic — likely a network or SUT-reachability issue. Confirm `base_url`/`api_base_url` and re-run.',
          },
          no_json_responses: {
            headline: 'Probe captured 0 authenticated API calls',
            detail:
              'All HAR entries were HTML (likely the login page). Auth state is stale; paste a fresh cookie via Refresh Auth.',
          },
          all_api_calls_failed: {
            headline: 'All authenticated API calls failed',
            detail:
              'JSON responses were captured but every status was 4xx/5xx. Cookie may have wrong scope/tenant; verify role.',
          },
        };
        const reason = (selected.auth_pre_flight_reason as string | undefined) || 'unknown';
        const copy = REASON_COPY[reason] || {
          headline: 'Auth pre-flight failed',
          detail: selected.pre_run_diagnosis
            || `Reason: ${reason}. Refresh auth before running tests.`,
        };
        return (
          <div className="mb-4 rounded-lg px-4 py-3 flex items-center justify-between"
               style={{ background: 'rgba(251,113,133,0.10)',
                        border: '1px solid rgba(251,113,133,0.4)' }}>
            <div className="flex items-center gap-3">
              <span style={{ color: '#fb7185', fontSize: 18 }}>⛔</span>
              <div>
                <div className="text-sm font-semibold" style={{ color: '#fb7185' }}>
                  {copy.headline}
                </div>
                <div className="text-[11px] mt-0.5" style={{ color: '#94a3b8' }}>
                  {copy.detail}
                </div>
              </div>
            </div>
            <a
              href={`/admin?project=${selected.project_id || ''}#auth`}
              className="px-3 py-1.5 rounded-md text-xs font-semibold text-white"
              style={{ background: 'linear-gradient(135deg,#f59e0b,#fb7185)',
                       textDecoration: 'none' }}
            >
              Refresh Auth →
            </a>
          </div>
        );
      })()}

      {/* R33.11 — Discovery-zero-harvest banner. When discovery ran but
          produced 0 env vars (run-80b983 pattern: cookie expired between
          R28.0b storage-state read and probe's first navigation), the
          probe likely got the SPA login screen. Surfaces the diagnostic
          BEFORE the operator wonders why all tests failed. */}
      {(selected.discovery_zero_envvars === true || selected.discovery_zero_envvars === 'true') && (
        <div className="mb-4 rounded-lg px-4 py-3 flex items-center justify-between"
             style={{ background: 'rgba(239,68,68,0.10)', border: '1px solid rgba(239,68,68,0.3)' }}>
          <div className="flex items-center gap-3">
            <span style={{ color: '#ef4444', fontSize: 18 }}>⛔</span>
            <div>
              <div className="text-sm font-semibold" style={{ color: '#fb7185' }}>
                Discovery harvested 0 env vars — auth likely stale
              </div>
              <div className="text-[11px] mt-0.5" style={{ color: '#94a3b8' }}>
                {selected.discovery_diagnosis || 'The discovery probe could not capture authenticated API traffic; likely the SPA served the login screen. Refresh auth (paste a fresh cookie via Refresh Auth modal) and re-run.'}
              </div>
            </div>
          </div>
          <a
            href={`/admin?project=${selected.project_id || ''}#auth`}
            className="px-3 py-1.5 rounded-md text-xs font-medium"
            style={{ background: '#ef4444', color: '#fff', textDecoration: 'none' }}
          >
            Refresh Auth →
          </a>
        </div>
      )}

      {/* Phase K8 — Discovery-not-run banner. Surfaces when cascade-skip
          dominates the run (run-89e80da6: 2,463 of 2,927 step records).
          Operator action: click "Re-run discovery" in the project's
          Discovery panel to harvest path-param env vars. */}
      {(selected.unresolved_path_params?.length || 0) > 5 && (
        <div className="mb-4 rounded-lg px-4 py-3 flex items-center justify-between"
             style={{ background: '#f59e0b18', border: '1px solid #f59e0b44' }}>
          <div className="flex items-center gap-3">
            <span style={{ color: '#f59e0b', fontSize: 18 }}>{'⚠'}</span>
            <div>
              <div className="text-sm font-semibold" style={{ color: '#fbbf24' }}>
                {selected.unresolved_path_params.length} env var{selected.unresolved_path_params.length === 1 ? '' : 's'} unresolved — many tests cascade-skipped
              </div>
              <div className="text-[11px] mt-0.5" style={{ color: '#92711a' }}>
                Missing: {(selected.unresolved_path_params || []).slice(0, 5).join(', ')}
                {selected.unresolved_path_params.length > 5 && ` + ${selected.unresolved_path_params.length - 5} more`}.
                {' '}Click <strong>Re-run Discovery</strong> on the project's Discovery panel to auto-harvest these from a Playwright run.
              </div>
            </div>
          </div>
          <a
            href={`/admin?project=${selected.project_id || ''}#discovery`}
            className="px-3 py-1.5 rounded-md text-xs font-medium transition-colors"
            style={{ background: '#f59e0b', color: '#fff', textDecoration: 'none' }}
          >
            Open Discovery →
          </a>
        </div>
      )}
      {/* Heal toast */}
      {healToast && (
        <div className="mb-3 px-3 py-2 rounded-lg text-xs"
             style={{ background: 'rgba(99,102,241,0.1)', border: '1px solid #6366f130', color: '#a5b4fc' }}>
          {healToast}
        </div>
      )}
      {/* Metadata */}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm mb-5">
        <dt style={{ color: '#94a3b8' }}>Branch</dt>
        <dd style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 12 }}>{selected.branch || '\u2014'}</dd>
        <dt style={{ color: '#94a3b8' }}>Started</dt>
        <dd style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 12 }}>{fmtDate(selected.started_at)}</dd>
        <dt style={{ color: '#94a3b8' }}>Duration</dt>
        <dd style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 12 }}>{fmtDuration(selected.duration_s)}</dd>
        <dt style={{ color: '#94a3b8' }}>Coverage</dt>
        <dd style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 12 }}>{selected.coverage_pct != null ? `${selected.coverage_pct}%` : '\u2014'}</dd>
        <dt style={{ color: '#94a3b8' }}>Environment</dt>
        <dd style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 12 }}>{selected.environment || '\u2014'}</dd>
        <dt style={{ color: '#94a3b8' }}>Trigger</dt>
        <dd style={{ color: '#e2e8f0', fontFamily: 'monospace', fontSize: 12 }}>{selected.trigger || '\u2014'}</dd>
      </dl>

      {/* Error banner */}
      {selected.status === 'failed' && selected.error && (
        <div className="mb-4 px-3 py-2 rounded-lg text-xs" style={{ background: '#2d1215', border: '1px solid #7f1d1d' }}>
          <p className="font-semibold mb-1" style={{ color: '#fca5a5' }}>Execution Failed</p>
          <p style={{ color: '#f87171' }}>{selected.error}</p>
        </div>
      )}

      {/* Quality Gate */}
      {selected.gate_decision && (
        <details className="mb-4" open={selected.gate_decision === 'FAIL' || selected.gate_decision === 'CONCERNS'}>
          <summary className="flex items-center gap-2 cursor-pointer text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>
            <span>Quality Gate:</span>
            <span className="px-2 py-0.5 rounded text-[10px] font-bold" style={{ background: GATE_BG[selected.gate_decision], color: GATE_COLOR[selected.gate_decision] }}>
              {selected.gate_decision}
            </span>
          </summary>
          <div className="space-y-1 ml-1 mb-4">
            {gateChecks.map(c => {
              // R28.1 / R29.3c \u2014 distinguish 5 states:
              //   pass     \u2192 green \u2713
              //   fail     \u2192 red \u2717
              //   concerns \u2192 yellow \u26A0 (e.g. tool dispatched but only SKIP rows)
              //   blocked  \u2192 amber \uD83D\uDD12 (config gap \u2014 operator action needed)
              //   skip     \u2192 grey \u25CB (tool wasn't part of suite)
              const _color = c.st === 'pass' ? '#34d399'
                          : c.st === 'fail' ? '#fb7185'
                          : c.st === 'concerns' ? '#fbbf24'
                          : c.st === 'blocked' ? '#fb923c'
                          : '#94a3b8'
              const _icon = c.st === 'pass' ? '\u2713'
                         : c.st === 'fail' ? '\u2717'
                         : c.st === 'concerns' ? '\u26A0'
                         : c.st === 'blocked' ? '\uD83D\uDD12'
                         : '\u25CB'
              return (
              <div key={c.name} className="flex items-center gap-2 text-[11px] px-2 py-1 rounded" style={{ background: '#0d0d1a' }}>
                <span style={{ color: _color, width: 12, textAlign: 'center' }}>{_icon}</span>
                <span className="flex-1" style={{ color: '#94a3b8' }}>{c.name}</span>
                <span style={{ color: '#64748b', fontFamily: 'monospace', fontSize: 10 }}>{c.actual} / {c.expected}</span>
              </div>
              )
            })}
          </div>
        </details>
      )}

      {/* R28.6.1 — Cross-cutting per-run navigation. Operator clicks
          a run row in /run-history → this panel opens → these three
          links lead to THAT specific run's defects, traceability,
          and test-explorer views. Keeps the "navigate back to a
          prior run" flow coherent across the system. */}
      {(selected.run_id || selected.id) && (
        <div className="mb-4 flex gap-2 flex-wrap">
          <Link
            href={`/defects?run_id=${encodeURIComponent(selected.run_id || selected.id)}`}
            className="px-3 py-1.5 rounded-lg text-xs font-medium transition-opacity hover:opacity-90"
            style={{ background: 'rgba(239,68,68,0.15)', color: '#fb7185', border: '1px solid rgba(239,68,68,0.3)' }}
          >
            🐞 Defects from this run{openP0Defects !== null ? ` (${openP0Defects} P0)` : ''}
          </Link>
          <Link
            href={`/traceability?run_id=${encodeURIComponent(selected.run_id || selected.id)}${selected.project_id ? `&project_id=${encodeURIComponent(selected.project_id)}` : ''}`}
            className="px-3 py-1.5 rounded-lg text-xs font-medium transition-opacity hover:opacity-90"
            style={{ background: 'rgba(99,102,241,0.15)', color: '#818cf8', border: '1px solid rgba(99,102,241,0.3)' }}
          >
            🔗 Traceability for this run
          </Link>
          <Link
            href={`/test-explorer?run_id=${encodeURIComponent(selected.run_id || selected.id)}`}
            className="px-3 py-1.5 rounded-lg text-xs font-medium transition-opacity hover:opacity-90"
            style={{ background: 'rgba(6,182,212,0.15)', color: '#06b6d4', border: '1px solid rgba(6,182,212,0.3)' }}
          >
            🧪 Test Explorer (this run)
          </Link>
        </div>
      )}

      {/* View Report + Artifacts */}
      {selected.report_url && (
        <div className="mb-4 flex gap-2">
          <a href={selected.report_url} target="_blank" rel="noopener noreferrer"
             className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition hover:opacity-90"
             style={{ background: '#6366f1', color: '#fff' }}>
            ↗ Open Full Report
          </a>
          {selected.artifacts_url && (
            <button
              onClick={loadArtifacts}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition hover:opacity-90 cursor-pointer"
              style={{ background: '#1e1e3a', color: '#94a3b8', border: 'none' }}>
              {loadingArtifacts ? 'Loading...' : showArtifacts ? 'Hide Artifacts' : 'Artifacts'}
            </button>
          )}
        </div>
      )}

      {/* Artifacts Panel */}
      {showArtifacts && artifacts.length > 0 && (() => {
        const grouped: Record<string, any[]> = {}
        for (const a of artifacts) {
          const parts = a.name.split('/')
          const dir = parts.length > 1 ? parts[0] : '_root'
          if (!grouped[dir]) grouped[dir] = []
          grouped[dir].push({ ...a, filename: parts[parts.length - 1] })
        }
        const typeIcon: Record<string, string> = { png: '\ud83d\udcf7', zip: '\ud83d\udce6', md: '\ud83d\udcdd', webm: '\ud83c\udfa5' }
        const typeLabel: Record<string, string> = { png: 'Screenshot', zip: 'Trace', md: 'Error Context', webm: 'Video' }
        return (
          <div className="mb-4 rounded-lg overflow-hidden" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
            <div className="px-3 py-2 text-[10px] font-bold uppercase tracking-wider" style={{ color: '#64748b', background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}>
              Test Artifacts ({artifacts.length} files)
            </div>
            <div style={{ maxHeight: 300, overflowY: 'auto' }}>
              {Object.entries(grouped).map(([dir, files]) => (
                <details key={dir} className="group">
                  <summary className="px-3 py-1.5 cursor-pointer text-[11px] font-medium hover:bg-white/5 flex items-center gap-2" style={{ color: '#94a3b8' }}>
                    <span style={{ color: '#6366f1' }}>{'\ud83d\udcc1'}</span>
                    <span className="truncate flex-1">{dir === '_root' ? 'Root' : dir.replace(/^req_bt_\d+-/, '').replace(/-/g, ' ').slice(0, 60)}</span>
                    <span className="text-[9px] px-1.5 py-0.5 rounded" style={{ background: '#1e1e3a', color: '#64748b' }}>{files.length}</span>
                  </summary>
                  <div className="pl-6 pr-3 pb-2 space-y-1">
                    {files.map((f: any, i: number) => (
                      <a key={i} href={f.url} target="_blank" rel="noopener noreferrer"
                         className="flex items-center gap-2 px-2 py-1 rounded text-[10px] hover:bg-white/5 transition"
                         style={{ color: '#cbd5e1' }}>
                        <span>{typeIcon[f.type] || '\ud83d\udcc4'}</span>
                        <span className="flex-1 truncate">{typeLabel[f.type] || f.type}: {f.filename}</span>
                        <span className="text-[9px]" style={{ color: '#94a3b8' }}>{f.size_kb}KB</span>
                      </a>
                    ))}
                  </div>
                </details>
              ))}
            </div>
          </div>
        )
      })()}

      {showArtifacts && artifacts.length === 0 && !loadingArtifacts && (
        <div className="mb-4 px-3 py-3 rounded-lg text-xs text-center" style={{ background: '#0a0a14', color: '#94a3b8' }}>
          No artifacts found for this run.
        </div>
      )}

      {/* Running indicator */}
      {(selected.status === 'running' || selected.status === 'queued') && total === 0 && (
        <div className="mb-4 px-3 py-3 rounded-lg text-xs text-center" style={{ background: '#0d0d1a' }}>
          <div className="animate-pulse mb-2" style={{ color: '#6366f1' }}>Execution in progress...</div>
          <p style={{ color: '#64748b' }}>Use the Watch Live button above to stream results.</p>
        </div>
      )}

      {/* Browser breakdown */}
      {browserEntries.length > 0 && (
        <div className="mb-5">
          <p className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>Browser Breakdown</p>
          <div className="grid grid-cols-3 gap-2">
            {browserEntries.map(([browser, counts]) => (
              <div key={browser} className="px-3 py-2 rounded-lg text-center" style={{ background: '#0d0d1a' }}>
                <p className="text-xs font-medium capitalize" style={{ color: '#a5b4fc' }}>{browser}</p>
                <p className="text-sm font-mono mt-1">
                  <span style={{ color: '#34d399' }}>{counts.passed}</span>
                  <span style={{ color: '#94a3b8' }}>/</span>
                  <span style={{ color: '#fb7185' }}>{counts.failed}</span>
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Test type summary cards */}
      <div className="grid grid-cols-4 gap-2 mb-4">
        {allTypes.map(tool => {
          const tests = grouped[tool] || []
          const p = tests.filter(t => t.status === 'PASS').length
          return (
            <div key={tool} className="px-2 py-2 rounded-lg text-center" style={{ background: '#0d0d1a', borderLeft: `2px solid ${TOOL_COLORS[tool]}` }}>
              <p className="text-[9px] font-semibold uppercase" style={{ color: TOOL_COLORS[tool] }}>{TOOL_LABELS[tool]}</p>
              <p className="text-sm font-mono mt-0.5" style={{ color: tests.length > 0 ? '#e2e8f0' : '#94a3b8' }}>
                {tests.length > 0 ? `${p}/${tests.length}` : '\u2014'}
              </p>
            </div>
          )
        })}
      </div>

      {/* Results header */}
      <p className="text-xs font-semibold uppercase tracking-wider mb-3" style={{ color: '#94a3b8' }}>
        Test Results ({total})
        {total > 0 && (
          <span className="ml-2 normal-case" style={{ color: '#94a3b8' }}>
            <span style={{ color: '#34d399' }}>{passedCount} pass</span>
            {' \u00b7 '}
            <span style={{ color: '#fb7185' }}>{failedCount} fail</span>
            {/* R306.B \u2014 surface BLOCKED + SKIP so the header accounts for all
                `total` rows (executed + blocked + skipped), not just P/F. */}
            {results.filter(t => t.status === 'BLOCKED').length > 0 && <>
              {' \u00b7 '}
              <span style={{ color: '#c084fc' }}>{results.filter(t => t.status === 'BLOCKED').length} blocked</span>
            </>}
            {results.filter(t => t.status === 'SKIP').length > 0 && <>
              {' \u00b7 '}
              <span style={{ color: '#fbbf24' }}>{results.filter(t => t.status === 'SKIP').length} skip</span>
            </>}
          </span>
        )}
      </p>

      {/* Empty results */}
      {total === 0 && selected.status !== 'running' && selected.status !== 'queued' && (
        <div className="px-3 py-4 rounded-lg text-xs text-center" style={{ background: '#0d0d1a', color: '#64748b' }}>
          No test results recorded for this run.
        </div>
      )}

      {/* Grouped results by tool */}
      <div className="space-y-3">
        {Object.entries(grouped).map(([tool, tests]) => (
          <div key={tool}>
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-[9px] px-1.5 py-0.5 rounded font-semibold uppercase" style={{ background: `${TOOL_COLORS[tool]}20`, color: TOOL_COLORS[tool] }}>
                {TOOL_LABELS[tool] || tool}
              </span>
              <span className="text-[10px]" style={{ color: '#64748b' }}>
                {(tests as any[]).filter(t => t.status === 'PASS').length} passed, {(tests as any[]).filter(t => t.status === 'FAIL').length} failed
              </span>
            </div>
            <div className="space-y-1">
              {(tests as any[]).map((t, idx) => (
                <details key={t.test_id || `${tool}-${idx}`}>
                  <summary className="flex items-center gap-3 px-3 py-2 rounded-lg text-xs cursor-pointer list-none" style={{ background: '#0d0d1a' }}>
                    <span className="font-bold w-8 flex-shrink-0" style={{ color: STATUS_COLOR[t.status] || '#94a3b8', fontFamily: 'monospace' }}>
                      {t.status}
                    </span>
                    <span className="flex-1 truncate" style={{ color: '#94a3b8' }}>
                      {t.title}
                      {t.test_id && (
                        <a href={`/test-explorer?test_id=${t.test_id}`}
                           onClick={e => e.stopPropagation()}
                           title="Open in Test Explorer"
                           className="ml-1 inline-block opacity-40 hover:opacity-100 transition-opacity"
                           style={{ color: '#6366f1', fontSize: 9 }}>{'\u2197'}</a>
                      )}
                    </span>
                    <span style={{ color: '#94a3b8', fontFamily: 'monospace', flexShrink: 0 }}>
                      {t.duration_ms > 0 ? (t.duration_ms > 999 ? `${(t.duration_ms / 1000).toFixed(1)}s` : `${t.duration_ms}ms`) : '\u2014'}
                    </span>
                    {t.error && <span style={{ color: '#fb7185', fontSize: 10 }}>{'\u25BE'}</span>}
                  </summary>
                  {(t.error || t.screenshot_url || t.trace_url) && (
                    <div className="mx-3 mt-1 mb-2 space-y-1">
                      {t.failure_class && (
                        <div className="flex items-center gap-2 px-3 py-1 flex-wrap">
                          <span className="text-[9px] px-1.5 py-0.5 rounded font-semibold"
                                style={{ background: `${t.failure_class.color}18`, color: t.failure_class.color }}>
                            {t.failure_class.label}
                          </span>
                          <span className="text-[10px]" style={{ color: '#64748b' }}>
                            {t.failure_class.hint}
                          </span>
                          {t.failure_class.type === 'script_bug' && (
                            <button
                              className="text-[9px] px-1.5 py-0.5 rounded ml-auto cursor-pointer"
                              disabled={healingTestId === t.test_id}
                              style={{ background: '#6366f118', color: '#6366f1', border: '1px solid #6366f130' }}
                              onClick={async (e) => {
                                e.stopPropagation()
                                setHealingTestId(t.test_id)
                                try {
                                  await requestHeal(t.test_id || 'unknown', t.error || '', t.failure_class?.type, selected?.project_id, t.title)
                                  setHealToast('Healing proposal created')
                                  setTimeout(() => setHealToast(null), 3000)
                                  router.push('/healing')
                                } catch {
                                  setHealToast('Proposal created (mock)')
                                  setTimeout(() => setHealToast(null), 3000)
                                }
                                setHealingTestId(null)
                              }}
                            >
                              {healingTestId === t.test_id ? 'Requesting...' : 'Request Heal'}
                            </button>
                          )}
                          {t.failure_class.type === 'product_defect' && (
                            <a href={`/defects?from_test=${t.test_id || ''}`}
                               onClick={(e) => e.stopPropagation()}
                               className="text-[9px] px-1.5 py-0.5 rounded ml-auto no-underline"
                               style={{ background: '#fb718518', color: '#fb7185', border: '1px solid #fb718530' }}>
                              Report Defect
                            </a>
                          )}
                          {t.failure_class.type === 'environment' && (
                            <a href="/settings"
                               onClick={(e) => e.stopPropagation()}
                               className="text-[9px] px-1.5 py-0.5 rounded ml-auto no-underline"
                               style={{ background: '#64748b18', color: '#64748b', border: '1px solid #64748b30' }}>
                              Check Env
                            </a>
                          )}
                          {t.failure_class.type === 'test_design' && (
                            <a href={`/test-explorer?test_id=${t.test_id || ''}`}
                               onClick={(e) => e.stopPropagation()}
                               className="text-[9px] px-1.5 py-0.5 rounded ml-auto no-underline"
                               style={{ background: '#a78bfa18', color: '#a78bfa', border: '1px solid #a78bfa30' }}>
                              View Test
                            </a>
                          )}
                        </div>
                      )}
                      {t.error && (
                        <div className="px-3 py-2 rounded text-[11px]" style={{ background: '#1a0a0e', color: '#fca5a5', fontFamily: 'JetBrains Mono, monospace' }}>
                          {t.error}
                        </div>
                      )}
                      {(t.screenshot_url || t.trace_url) && (
                        <div className="flex gap-3 px-3 py-1">
                          {t.screenshot_url && (
                            <a href={t.screenshot_url} target="_blank" rel="noopener noreferrer"
                               className="text-[10px] flex items-center gap-1 hover:underline" style={{ color: '#06b6d4' }}>
                              Screenshot
                            </a>
                          )}
                          {t.trace_url && (
                            <a href={t.trace_url} target="_blank" rel="noopener noreferrer"
                               className="text-[10px] flex items-center gap-1 hover:underline" style={{ color: '#a78bfa' }}>
                              Trace
                            </a>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </details>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* Phase J3 — call-sequence timeline. Renders Phase E1+E3 step data
          (per-step bars, cascade markers, per-endpoint p95). Shows an
          empty-state for older runs that don't carry step records. */}
      {(selected.run_id || selected.id) && (
        <div className="mt-6">
          <CallSequenceTimeline runId={String(selected.run_id || selected.id)} />
        </div>
      )}
    </div>
  )
}
