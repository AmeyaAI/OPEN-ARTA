'use client'

// R320 — Test-Case Refinement Copilot workspace. Anchored to a specific
// generated test (deep-linked from Test Explorer). The tester states a
// correction; ARTA CLASSIFIES it against its own accessible SUT knowledge
// (arta_knew = an upstream gen/grounding defect vs human_knowledge = durable
// grounding), then on Apply persists the grounding fact + runs a surgical regen
// with the correction as the prompt hint. Corrections are listed + revertible.

import { useEffect, useState, useCallback } from 'react'
import { useSearchParams } from 'next/navigation'
import Link from 'next/link'
import {
  refineTest, listCorrections, revertCorrection,
  correctionsAnalytics, verifyCorrection, reconcileVerify,
  fetchTest, fetchTestScript, fetchTestLatestResult,
  type RefineResult, type TestCorrection, type CorrectionsAnalytics, type LatestResult,
} from '@/lib/api-client'
import { useProject } from '@/lib/project-context'

const KINDS = [
  { key: 'endpoint', label: 'Wrong / missing endpoint', hint: 'ARTA called the wrong API path' },
  { key: 'field_value', label: 'Wrong field or value', hint: 'e.g. name → displayName, region value' },
  { key: 'shape', label: 'Wrong response shape', hint: 'e.g. array vs { clusters: [...] }' },
  { key: 'intent', label: 'Wrong test intent', hint: 'e.g. this is a negative test, expect 404' },
]

export default function RefineWorkspace() {
  const sp = useSearchParams()
  const { currentProjectId } = useProject()

  const testId = sp.get('test_id') || ''
  const requirementId = sp.get('requirement_id') || ''
  const tool = sp.get('tool') || ''
  const acId = sp.get('ac_id') || ''
  const title = sp.get('title') || ''

  const [kind, setKind] = useState('endpoint')
  const [method, setMethod] = useState('GET')
  const [path, setPath] = useState('')
  const [field, setField] = useState('')
  const [fromValue, setFromValue] = useState('')
  const [toValue, setToValue] = useState('')
  const [text, setText] = useState('')

  const [draft, setDraft] = useState<RefineResult | null>(null)
  const [applied, setApplied] = useState<RefineResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const [corrections, setCorrections] = useState<TestCorrection[]>([])
  const [analytics, setAnalytics] = useState<CorrectionsAnalytics | null>(null)

  // Artifact + latest failure (what the tester is correcting)
  const [gherkin, setGherkin] = useState('')
  const [script, setScript] = useState('')
  const [artifactTab, setArtifactTab] = useState<'gherkin' | 'script'>('gherkin')
  const [showArtifact, setShowArtifact] = useState(false)
  const [failure, setFailure] = useState<LatestResult | null>(null)

  const loadCorrections = useCallback(() => {
    listCorrections(currentProjectId || undefined)
      .then(d => setCorrections((d.corrections || []).filter(c => !testId || c.test_id === testId)))
      .catch(() => {})
    correctionsAnalytics(currentProjectId || undefined).then(setAnalytics).catch(() => {})
  }, [currentProjectId, testId])

  useEffect(() => { loadCorrections() }, [loadCorrections])

  // Load the corrected test's artifact + latest failure so the tester sees
  // exactly what is wrong (no more correcting blind).
  useEffect(() => {
    if (!testId) return
    fetchTest(testId, currentProjectId || undefined).then((t: any) => {
      setGherkin(t?.gherkin || (Array.isArray(t?.gherkin_scenarios) ? t.gherkin_scenarios.join('\n\n') : '') || t?.scenario || '')
      if (t?.script_content) setScript(t.script_content)
    }).catch(() => {})
    fetchTestScript(testId).then(s => { if (s?.content) setScript(prev => prev || s.content) }).catch(() => {})
    fetchTestLatestResult(testId).then(setFailure).catch(() => {})
  }, [testId, currentProjectId])

  const verify = async (id: string) => {
    try {
      await verifyCorrection(id); loadCorrections()
      // Poll reconcile so verify_status resolves (passed|failed) without a manual
      // reload — a scoped run takes a few minutes; cap at ~2min.
      let tries = 0
      const iv = setInterval(async () => {
        tries += 1
        try {
          const r = await reconcileVerify(id)
          if (r.verify_status === 'passed' || r.verify_status === 'failed' || tries >= 20) {
            clearInterval(iv); loadCorrections()
          }
        } catch { if (tries >= 20) clearInterval(iv) }
      }, 6000)
    } catch {}
  }

  const payload = (confirm: boolean) => ({
    project_id: currentProjectId || undefined,
    test_id: testId || undefined,
    requirement_id: requirementId || undefined,
    ac_id: acId || undefined,
    tool: tool || undefined,
    kind, method,
    path: path || undefined,
    field: field || undefined,
    from_value: fromValue || undefined,
    to_value: toValue || undefined,
    correction_text: text,
    confirm,
  })

  const analyze = async () => {
    setError(''); setApplied(null); setBusy(true)
    try { setDraft(await refineTest(payload(false))) }
    catch (e: any) { setError(String(e?.message || e)) }
    setBusy(false)
  }

  const apply = async () => {
    setError(''); setBusy(true)
    try {
      const res = await refineTest(payload(true))
      setApplied(res); setDraft(null)
      loadCorrections()
    } catch (e: any) { setError(String(e?.message || e)) }
    setBusy(false)
  }

  const doRevert = async (id: string) => {
    try { await revertCorrection(id); loadCorrections() } catch {}
  }

  const needsEndpoint = kind === 'field_value' || kind === 'shape'
  const canAnalyze = text.trim().length > 0 &&
    (kind === 'intent' || (kind === 'endpoint' ? toValue.trim() : path.trim()))

  const input = 'w-full px-3 py-2 rounded-lg text-sm text-white outline-none'
  const inputStyle = { background: '#0a0a14', border: '1px solid #1e1e3a' }

  return (
    <div className="p-8 max-w-4xl">
      <div className="flex items-center gap-3 mb-1">
        <h1 className="text-xl font-bold text-white">Refine Test with AI</h1>
        <span className="px-2 py-0.5 rounded text-[10px]" style={{ background: 'rgba(99,102,241,0.15)', color: '#818cf8' }}>
          {tool || 'test'}
        </span>
      </div>
      <p className="text-sm mb-1" style={{ color: '#64748b' }}>
        Correct the AI-generated test — ARTA grounds on your fix so future generations improve.
      </p>
      <div className="text-xs mb-6" style={{ color: '#94a3b8' }}>
        <span style={{ color: '#a5b4fc', fontFamily: 'monospace' }}>{testId || '—'}</span>
        {title && <> · {title}</>}{requirementId && <> · {requirementId}</>}{acId && <> · {acId}</>}
      </div>

      {/* Latest failure — what the tester is correcting (no more correcting blind) */}
      {failure && failure.status && failure.status !== 'PASS' && (
        <div className="glass-card p-3 mb-4" style={{ border: '1px solid rgba(251,113,133,0.3)' }}>
          <div className="flex items-center gap-2 mb-1">
            <span className="px-2 py-0.5 rounded text-[10px] font-semibold" style={{ background: 'rgba(251,113,133,0.15)', color: '#fb7185' }}>{failure.status}</span>
            {failure.triage_category && <span className="text-[10px]" style={{ color: '#94a3b8' }}>triage: {failure.triage_category}</span>}
            {failure.run_id && <span className="text-[10px]" style={{ color: '#64748b', fontFamily: 'monospace' }}>{failure.run_id}</span>}
          </div>
          {failure.error_message && (
            <pre className="text-[11px] whitespace-pre-wrap overflow-x-auto" style={{ color: '#cbd5e1', fontFamily: 'monospace', maxHeight: 120 }}>
              {failure.error_message.replace(/\[[0-9;]*m/g, '').slice(0, 600)}
            </pre>
          )}
        </div>
      )}

      {/* Generated artifact (Gherkin + script) — collapsible */}
      {(gherkin || script) && (
        <div className="glass-card mb-4 overflow-hidden">
          <button onClick={() => setShowArtifact(v => !v)} className="w-full px-4 py-2 text-left text-xs flex items-center justify-between" style={{ color: '#94a3b8' }}>
            <span>Generated artifact</span><span>{showArtifact ? '▾' : '▸'}</span>
          </button>
          {showArtifact && (
            <div className="px-4 pb-4">
              <div className="flex gap-2 mb-2">
                {(['gherkin', 'script'] as const).map(t => (
                  <button key={t} onClick={() => setArtifactTab(t)} className="px-2 py-1 rounded text-[10px] capitalize"
                    style={{ background: artifactTab === t ? '#6366f1' : '#12121f', border: '1px solid #1e1e3a', color: '#e2e8f0' }}>{t}</button>
                ))}
              </div>
              <pre className="text-[11px] whitespace-pre-wrap overflow-auto" style={{ color: '#cbd5e1', fontFamily: 'monospace', maxHeight: 300, background: '#0a0a14', padding: 10, borderRadius: 8 }}>
                {(artifactTab === 'gherkin' ? gherkin : script) || '(empty)'}
              </pre>
            </div>
          )}
        </div>
      )}

      {/* R320 S2 — corrections analytics: surfaces ARTA's systematic gaps.
          arta_defect_rate = how often gen produced something ARTA ALREADY had
          (should have grounded on) → the signal to fix ARTA's gen/discovery. */}
      {analytics && analytics.total > 0 && (
        <div className="glass-card p-4 mb-4">
          <div className="flex items-center gap-6 flex-wrap">
            <div>
              <div className="text-2xl font-bold" style={{ color: analytics.arta_defect_rate > 0.5 ? '#fb7185' : '#f59e0b' }}>
                {Math.round(analytics.arta_defect_rate * 100)}%
              </div>
              <div className="text-[10px]" style={{ color: '#64748b' }}>ARTA-should-have-known</div>
            </div>
            <div><div className="text-lg font-bold" style={{ color: '#fb7185' }}>{analytics.arta_knew}</div><div className="text-[10px]" style={{ color: '#64748b' }}>ARTA defects</div></div>
            <div><div className="text-lg font-bold" style={{ color: '#10b981' }}>{analytics.human_knowledge}</div><div className="text-[10px]" style={{ color: '#64748b' }}>human grounding</div></div>
            <div><div className="text-lg font-bold" style={{ color: '#a5b4fc' }}>{analytics.total}</div><div className="text-[10px]" style={{ color: '#64748b' }}>total corrections</div></div>
            {analytics.top_corrected_endpoints.length > 0 && (
              <div className="flex-1 min-w-[200px]">
                <div className="text-[10px] mb-1" style={{ color: '#64748b' }}>MOST-CORRECTED (fix ARTA discovery here)</div>
                {analytics.top_corrected_endpoints.slice(0, 3).map(t => (
                  <div key={t.fact} className="text-[11px] truncate" style={{ color: '#94a3b8', fontFamily: 'monospace' }}>{t.count}× {t.fact}</div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Structured correction form */}
      <div className="glass-card p-4 mb-4 space-y-3">
        <div>
          <label className="block text-xs mb-1" style={{ color: '#94a3b8' }}>What is wrong?</label>
          <div className="flex flex-wrap gap-2">
            {KINDS.map(k => (
              <button key={k.key} onClick={() => { setKind(k.key); setDraft(null) }} title={k.hint}
                className="px-3 py-1.5 rounded-lg text-xs"
                style={{ background: kind === k.key ? '#6366f1' : '#12121f', border: '1px solid #1e1e3a', color: '#e2e8f0' }}>
                {k.label}
              </button>
            ))}
          </div>
        </div>

        {kind === 'endpoint' && (
          <div className="flex gap-3">
            <select value={method} onChange={e => setMethod(e.target.value)} className={input + ' w-28'} style={inputStyle}>
              {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map(m => <option key={m}>{m}</option>)}
            </select>
            <input value={toValue} onChange={e => setToValue(e.target.value)} className={input} style={inputStyle}
              placeholder="Correct endpoint path, e.g. /v1/regions/us-texas-1/…/clusters/{id}" />
          </div>
        )}
        {needsEndpoint && (
          <div className="flex gap-3">
            <select value={method} onChange={e => setMethod(e.target.value)} className={input + ' w-28'} style={inputStyle}>
              {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map(m => <option key={m}>{m}</option>)}
            </select>
            <input value={path} onChange={e => setPath(e.target.value)} className={input} style={inputStyle}
              placeholder="Endpoint this applies to, e.g. /v1/…/servers" />
          </div>
        )}
        {kind === 'field_value' && (
          <div className="flex gap-3">
            <input value={field} onChange={e => setField(e.target.value)} className={input + ' w-1/3'} style={inputStyle}
              placeholder="Field, e.g. region" />
            <input value={fromValue} onChange={e => setFromValue(e.target.value)} className={input} style={inputStyle}
              placeholder="Wrong value (from)" />
            <input value={toValue} onChange={e => setToValue(e.target.value)} className={input} style={inputStyle}
              placeholder="Correct value (to)" />
          </div>
        )}

        <div>
          <label className="block text-xs mb-1" style={{ color: '#94a3b8' }}>Explain the correction (fed to regeneration)</label>
          <textarea value={text} onChange={e => setText(e.target.value)} rows={2} className={input + ' resize-none'} style={inputStyle}
            placeholder="e.g. The cluster-detail route is /v1/regions/us-texas-1/projects/tc-main/compute/clusters/{id}, not the global path." />
        </div>

        <div className="flex gap-2">
          <button onClick={analyze} disabled={!canAnalyze || busy}
            className="px-4 py-2 rounded-lg text-sm font-medium"
            style={{ background: canAnalyze && !busy ? '#6366f1' : '#312e81', color: '#fff' }}>
            {busy && !applied ? 'Analyzing…' : 'Analyze correction'}
          </button>
        </div>
      </div>

      {error && <div className="glass-card p-3 mb-4 text-xs" style={{ color: '#fb7185' }}>{error}</div>}

      {/* Draft verdict — the keystone: source fix vs durable human grounding */}
      {draft && (
        <div className="glass-card p-4 mb-4" style={{ border: `1px solid ${draft.verdict === 'arta_knew' ? 'rgba(251,113,133,0.4)' : 'rgba(16,185,129,0.4)'}` }}>
          <div className="flex items-center gap-2 mb-2">
            <span className="px-2 py-0.5 rounded text-[10px] font-semibold"
              style={{ background: draft.verdict === 'arta_knew' ? 'rgba(251,113,133,0.15)' : 'rgba(16,185,129,0.15)', color: draft.verdict === 'arta_knew' ? '#fb7185' : '#10b981' }}>
              {draft.verdict === 'arta_knew' ? 'ARTA DEFECT (should have known)' : 'HUMAN KNOWLEDGE (durable grounding)'}
            </span>
            {draft.matched_source && <span className="text-[10px]" style={{ color: '#94a3b8' }}>matched source: {draft.matched_source}</span>}
          </div>
          <p className="text-xs mb-3" style={{ color: '#cbd5e1' }}>{draft.explain}</p>
          <button onClick={apply} disabled={busy}
            className="px-4 py-2 rounded-lg text-sm font-medium"
            style={{ background: busy ? '#4338ca' : '#10b981', color: '#fff' }}>
            {busy ? 'Applying…' : 'Apply correction + regenerate'}
          </button>
        </div>
      )}

      {/* Applied result */}
      {applied && (
        <div className="glass-card p-4 mb-4" style={{ border: '1px solid rgba(16,185,129,0.4)' }}>
          <div className="text-sm font-semibold mb-1" style={{ color: '#10b981' }}>Correction applied ✓</div>
          <div className="text-xs space-y-1" style={{ color: '#cbd5e1' }}>
            <div>Verdict: <b>{applied.verdict}</b>{applied.matched_source ? ` (source: ${applied.matched_source})` : ''}</div>
            {applied.grounding_written
              ? <div>Durable grounding saved: <span style={{ fontFamily: 'monospace', color: '#a5b4fc' }}>{applied.grounding_fact_ref}</span> — future generations for this SUT will use it.</div>
              : <div>No new grounding fact (intent-only correction — applied as a regeneration hint).</div>}
            <div>Regeneration: <b>{String((applied.regen as any)?.status ?? 'triggered')}</b></div>
          </div>
          <Link href={`/test-explorer?test_id=${encodeURIComponent(testId)}`}
            className="inline-block mt-3 px-3 py-1.5 rounded-lg text-xs" style={{ background: '#1e1e3a', color: '#06b6d4' }}>
            View regenerated test in Test Explorer →
          </Link>
        </div>
      )}

      {/* Corrections for this test — provenance + revert */}
      {corrections.length > 0 && (
        <div className="mt-6">
          <div className="text-xs font-medium mb-2" style={{ color: '#64748b' }}>CORRECTIONS ({corrections.length})</div>
          <div className="space-y-2">
            {corrections.map(c => (
              <div key={c.correction_id} className="glass-card p-3 flex items-start gap-3">
                <span className="px-2 py-0.5 rounded text-[10px] mt-0.5"
                  style={{ background: c.verdict === 'arta_knew' ? 'rgba(251,113,133,0.15)' : 'rgba(16,185,129,0.15)', color: c.verdict === 'arta_knew' ? '#fb7185' : '#10b981' }}>
                  {c.verdict || '—'}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white truncate">{c.correction_text}</div>
                  <div className="text-[10px] mt-0.5" style={{ color: '#64748b', fontFamily: 'monospace' }}>
                    {c.kind}{c.grounding_fact_ref ? ` · ${c.grounding_fact_ref}` : ''}
                    {c.verify_status && c.verify_status !== 'not_run' && (
                      <span style={{ color: c.verify_status === 'passed' ? '#10b981' : c.verify_status === 'failed' ? '#fb7185' : '#f59e0b' }}> · verify: {c.verify_status}</span>
                    )}
                  </div>
                </div>
                {c.requirement_id && c.tool && (
                  <button onClick={() => verify(c.correction_id)} className="px-2 py-1 rounded text-[10px]"
                    style={{ background: '#1e1e3a', color: '#06b6d4' }} title="Run the corrected test against the SUT">Verify</button>
                )}
                <button onClick={() => doRevert(c.correction_id)} className="px-2 py-1 rounded text-[10px]"
                  style={{ background: '#1e1e3a', color: '#94a3b8' }}>Revert</button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
