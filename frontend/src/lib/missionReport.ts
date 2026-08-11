// Single source of truth for reading the backend's mission-report shape
// (GET /api/execution/runs/{run_id}/mission-report) — the 4-pillar SUT-quality
// report. Consumed by the Dashboard SUT-health hero, the SUT Quality page, and
// the run-detail Mission Outcome so the truthful values are read identically
// everywhere.
//
// CRITICAL shape note: `pillar_4_report_sut_quality.by_category` is NESTED
// `{category: {severity: count}}` (backend execution.py defect_summary). Reading
// flat keys like `by_category.sut_regression_critical` returns undefined → 0,
// which silently HIDES real SUT bugs. Always go through sumCategory/severityCount.

export interface MissionReport {
  outcome_band?: string
  arta_attributable_pass_rate?: number
  actionable_ctas?: string[]
  pillar_1_test_case_quality?: Record<string, any>
  pillar_1b_test_script_quality?: Record<string, any>
  pillar_2_execute_flawlessly?: {
    arta_attributable_pass_rate?: number
    axe_pass?: number
    zap_pass?: number
    [k: string]: any
  }
  pillar_4_report_sut_quality?: {
    score?: string
    // R260 — how much to trust `score`, and why. Rendered by the fidelity tile.
    confidence?: 'none' | 'low' | 'high' | string
    score_reason?: string
    defects_total?: number
    sut_regression_critical?: number
    by_category?: Record<string, Record<string, number>>
    // R259/R304 — reporting-fidelity: how ARTA's failures split ARTA-noise vs
    // confident SUT signal vs assessed-pending-adjudication vs truly-not-assessed.
    reporting_fidelity?: ReportingFidelity
    // F2 (R218) — per-requirement, RISK-WEIGHTED SUT verdict (the mission's
    // purpose: "did requirement X pass, weighted by its risk?").
    by_requirement?: RequirementVerdict[]
    a11y?: A11ySubReport
    skip_cascade_ratio?: number
    // R144.D — the backend's AUTHORITATIVE, pre-computed skip attribution. The
    // frontend must READ these rather than re-derive a count client-side by
    // substring-matching each row's error_message (which misses the
    // `auth_stale_url_redirect` marker). `auth_stale_skips` = total auth-stale
    // SKIPs; `skip_by_cause` = {cause: count} (auth_stale_url_redirect,
    // auth_stale_unknown, framework_limit_or_implicit, …).
    auth_stale_skips?: number
    skip_by_cause?: Record<string, number>
    [k: string]: any
  }
  [k: string]: any
}

/** R259/R304 — ARTA's reporting fidelity for a run. `adjudication_pending`
 * (evidence-backed operator_review, e.g. "SUT ignores query params") is ASSESSED
 * and distinct from `not_assessed` (no evidence). All optional (old runs omit). */
export interface ReportingFidelity {
  triaged?: number
  arta_attributed?: number
  sut_signal_count?: number
  unknown?: number
  adjudication_pending?: number
  arta_defect_rate?: number
  not_assessed_pct?: number
  adjudication_pending_pct?: number
  noise_signal_ratio?: number
}

/** F2 (R218) — one requirement's risk-weighted SUT verdict. */
export interface RequirementVerdict {
  requirement_id: string
  priority?: string | null
  risk_score?: number | null
  tests: number
  pass: number
  fail: number
  blocked: number
  skipped: number
  verdict: string  // CLEAN | MIXED | DEGRADED | FAIL | BLOCKED | SKIPPED
}

/** F2 (R218) — Tailwind colour class for a requirement verdict band. */
export function requirementVerdictColor(verdict: string): string {
  switch ((verdict || '').toUpperCase()) {
    case 'CLEAN':    return 'text-emerald-400'
    case 'MIXED':    return 'text-amber-400'
    case 'DEGRADED': return 'text-red-400'
    case 'FAIL':     return 'text-red-500'
    case 'BLOCKED':  return 'text-violet-400'
    default:         return 'text-slate-400'  // SKIPPED / unknown
  }
}

export interface A11ySubReport {
  status?: 'clean' | 'not_assessed' | 'violations_found' | string
  scanned_rows?: number
  violations_critical?: number
  violations_moderate?: number
  violations_minor?: number
  axe_blocked?: number
  axe_skipped?: number
}

/** by_category is NESTED {category:{severity:count}} — sum ALL severities. */
export function sumCategory(
  byCategory: Record<string, Record<string, number>> | undefined | null,
  category: string,
): number {
  const cat = byCategory?.[category]
  if (!cat || typeof cat !== 'object') return 0
  return Object.values(cat).reduce((a, b) => a + (Number(b) || 0), 0)
}

/** A specific {category}.{severity} count (defensive nested read). */
export function severityCount(
  byCategory: Record<string, Record<string, number>> | undefined | null,
  category: string,
  severity: string,
): number {
  return Number(byCategory?.[category]?.[severity] ?? 0) || 0
}

/** The ARTA-attributable pass rate — the verdict's CREDIBILITY metric
 * (PASS / (TOTAL − sut_regression_span − BLOCKED)). Null when absent (old runs). */
export function attributablePassRate(mr: MissionReport | null | undefined): number | null {
  const v = mr?.arta_attributable_pass_rate ?? mr?.pillar_2_execute_flawlessly?.arta_attributable_pass_rate
  return typeof v === 'number' ? v : null
}

export type Tone = 'good' | 'warn' | 'bad' | 'muted'

/** The TRUTHFUL a11y verdict — `clean` only when axe actually scanned a real
 * page; `not_assessed` when blocked/skipped (NEVER render that as green). */
export function a11yVerdict(mr: MissionReport | null | undefined): {
  status: 'clean' | 'not_assessed' | 'violations_found' | 'unknown'
  critical: number
  moderate: number
  label: string
  tone: Tone
} {
  const a = mr?.pillar_4_report_sut_quality?.a11y
  const critical = Number(a?.violations_critical ?? 0)
  const moderate = Number(a?.violations_moderate ?? 0)
  switch (a?.status) {
    case 'clean':
      return { status: 'clean', critical, moderate, label: 'a11y clean (WCAG 2.1 AA)', tone: 'good' }
    case 'not_assessed':
      return { status: 'not_assessed', critical, moderate, label: 'a11y not assessed (scan blocked)', tone: 'warn' }
    case 'violations_found':
      return { status: 'violations_found', critical, moderate, label: `a11y: ${critical} crit · ${moderate} mod`, tone: 'bad' }
    default:
      return { status: 'unknown', critical, moderate, label: 'a11y n/a', tone: 'muted' }
  }
}

/** Headline SUT-quality verdict for the Dashboard hero + SUT Quality page. */
export function sutVerdict(mr: MissionReport | null | undefined): {
  band: string
  score: string
  sutCritical: number
  operatorReview: number
  autoHeal: number
  tone: Tone
} {
  const p4 = mr?.pillar_4_report_sut_quality
  const byCat = p4?.by_category
  const sutCritical = severityCount(byCat, 'sut_regression', 'critical')
  const operatorReview = sumCategory(byCat, 'operator_review')
  const autoHeal = sumCategory(byCat, 'test_gen_bug')
  const band = (mr?.outcome_band || 'UNKNOWN').toUpperCase()
  // SUT is "degraded" when there are real SUT-critical regressions; otherwise
  // the band drives the tone (CLEAN/OPTIMISTIC = good, PESSIMISTIC = bad).
  let tone: Tone = 'muted'
  if (sutCritical > 0) tone = 'bad'
  else if (band === 'PESSIMISTIC') tone = 'bad'
  else if (band === 'MIXED') tone = 'warn'
  else if (band === 'OPTIMISTIC' || band === 'CLEAN') tone = 'good'
  return {
    band,
    score: p4?.score || 'N/A',
    sutCritical,
    operatorReview,
    autoHeal,
    tone,
  }
}

export function actionableCtas(mr: MissionReport | null | undefined): string[] {
  return Array.isArray(mr?.actionable_ctas) ? (mr!.actionable_ctas as string[]) : []
}

/** R259/R304 — the reporting-fidelity block + R260 confidence, read FLAT (NOT
 * via the nested sumCategory helper). Returns null when the run predates R259
 * (no fidelity to show). `adjudication_pending` is a first-class outcome —
 * ARTA assessed the failure and routed it to a product decision, which is
 * categorically different from `not_assessed` (no evidence). */
export function reportingFidelity(mr: MissionReport | null | undefined): {
  triaged: number
  artaAttributed: number
  sutSignal: number
  adjudicationPending: number
  notAssessed: number
  artaDefectRate: number
  notAssessedPct: number
  adjudicationPendingPct: number
  confidence: string
  scoreReason: string
  tone: Tone
} | null {
  const rf = mr?.pillar_4_report_sut_quality?.reporting_fidelity
  if (!rf || typeof rf !== 'object' || !(Number(rf.triaged) > 0)) return null
  const confidence = (mr?.pillar_4_report_sut_quality?.confidence || '').toLowerCase()
  // Confidence drives tone: high = good (ARTA can stand behind the verdict),
  // low/none = warn (too much unattributable / ARTA noise to grade the SUT).
  const tone: Tone = confidence === 'high' ? 'good'
    : (confidence === 'low' || confidence === 'none') ? 'warn' : 'muted'
  return {
    triaged: Number(rf.triaged) || 0,
    artaAttributed: Number(rf.arta_attributed) || 0,
    sutSignal: Number(rf.sut_signal_count) || 0,
    adjudicationPending: Number(rf.adjudication_pending) || 0,
    notAssessed: Number(rf.unknown) || 0,
    artaDefectRate: Number(rf.arta_defect_rate) || 0,
    notAssessedPct: Number(rf.not_assessed_pct) || 0,
    adjudicationPendingPct: Number(rf.adjudication_pending_pct) || 0,
    confidence: confidence || 'unknown',
    scoreReason: mr?.pillar_4_report_sut_quality?.score_reason || '',
    tone,
  }
}
