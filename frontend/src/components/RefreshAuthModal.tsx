/**
 * R15c — RefreshAuthModal
 *
 * Pre-run modal that opens when useRunGuard's pre-flight detects an
 * expired SUT cookie. Walks the operator through:
 *
 *   1. Open the SUT login URL in a new tab → browser handles OAuth on
 *      SUT's domain (uses SUT's own registered redirect URI; ARTA never
 *      participates in the OAuth dance).
 *   2. On the post-login SUT page, click the ARTA bookmarklet → it
 *      reads document.cookie + localStorage.refresh-token + window.location.host
 *      and writes a JSON blob to the clipboard.
 *   3. Switch back to the dashboard, click "Paste from clipboard" → modal
 *      validates JWT shape + cookie name + host.
 *   4. Click "Refresh & Run" → POSTs to /api/projects/{id}/auth-state →
 *      backend writes the storage state atomically.
 *   5. Modal closes; the queued run auto-triggers via onSuccess().
 *
 * Same-origin / cross-origin handling:
 *   - The bookmarklet runs in the SUT's tab (same-origin to the SUT) so
 *     it can read cookies + localStorage.
 *   - Clipboard write/read works on HTTPS or localhost (the modal lives
 *     on localhost in dev or under HTTPS in deployed environments).
 *   - ARTA frontend never reads cookies cross-origin — only via the
 *     operator-mediated clipboard paste.
 */
'use client'

import { useEffect, useState } from 'react'
import { postAuthState, decodeJwtPayload, type AuthState } from '@/lib/auth-state'

// R15-V4 — single-source-of-truth bookmarklet payload. Operator drags
// this into bookmarks bar ONCE per ARTA install. Works for any SUT
// whose session cookie name matches `*token*` (case-insensitive).
const BOOKMARKLET_JS = `javascript:(()=>{const m=document.cookie.match(/(?:^|;\\s*)([\\w-]*token[\\w-]*)=([^;]+)/i);if(!m){alert('No *-token cookie found. Are you logged in?');return;}const refresh=localStorage.getItem('refresh-token')||localStorage.getItem('refresh_token')||null;const blob=JSON.stringify({cookie:m[2],refresh:refresh,name:m[1],host:window.location.host});navigator.clipboard.writeText(blob).then(()=>alert('ARTA: copied. Switch to ARTA dashboard and click Paste.'),e=>alert('Clipboard write blocked: '+e.message+'\\n\\nManually copy:\\n'+blob));})()`

interface ClipboardPayload {
  cookie: string
  refresh: string | null
  name: string
  host: string
}

interface Props {
  open: boolean
  projectId: string | null
  environment: string | null
  initialState: AuthState | null
  onClose: () => void
  onSuccess: () => Promise<void> | void
}

type Phase = 'review' | 'paste' | 'submitting' | 'done' | 'error'

export default function RefreshAuthModal({
  open, projectId, environment, initialState, onClose, onSuccess,
}: Props) {
  const [phase, setPhase] = useState<Phase>('review')
  const [parsed, setParsed] = useState<ClipboardPayload | null>(null)
  const [parsedExpiry, setParsedExpiry] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [manualText, setManualText] = useState('')
  // R15-V7 — direct two-field mode bypasses the bookmarklet/clipboard
  // path entirely. Operator pastes the cookie value + refresh-token in
  // separate fields. Works on HTTP, in Incognito, on locked-down browsers
  // — anywhere the operator can copy text from DevTools.
  const [directCookie, setDirectCookie] = useState('')
  const [directRefresh, setDirectRefresh] = useState('')
  // R87.3 — Full Capture textarea. Operator pastes the JSON produced
  // by the DevTools console snippet (Object.fromEntries(localStorage)
  // + sessionStorage + document.cookie). Parsed on submit; the fields
  // route to postAuthState's localStorage_entries / sessionStorage_entries
  // / additional_cookies channels. The cookie field is ALWAYS provided
  // via the existing direct/parsed flow — Full Capture only fills the
  // peripheral state SPAs may need beyond the main session cookie.
  const [fullCaptureJSON, setFullCaptureJSON] = useState('')
  const [fullCaptureError, setFullCaptureError] = useState<string | null>(null)

  // Reset internal state when modal opens
  useEffect(() => {
    if (!open) return
    setPhase('review')
    setParsed(null)
    setParsedExpiry(null)
    setError(null)
    setManualText('')
    setDirectCookie('')
    setDirectRefresh('')
    setFullCaptureJSON('')
    setFullCaptureError(null)
  }, [open])

  // Escape to close
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const expectedCookieName = initialState?.cookie_name ?? null
  const expectedHost = initialState?.base_url
    ? new URL(initialState.base_url).host
    : null
  const refreshUrl = initialState?.refresh_url ?? '#'

  function validatePayload(blob: ClipboardPayload): string | null {
    if (!blob.cookie || typeof blob.cookie !== 'string' || blob.cookie.length < 20) {
      return 'Cookie value missing or too short. Did the bookmarklet run on the SUT login page?'
    }
    if (expectedCookieName && blob.name !== expectedCookieName) {
      return `Cookie name mismatch: project expects '${expectedCookieName}', clipboard has '${blob.name}'. Re-copy from the right SUT tab.`
    }
    if (expectedHost && blob.host && !(blob.host === expectedHost
        || blob.host.endsWith('.' + expectedHost)
        || expectedHost.endsWith('.' + blob.host))) {
      return `Cookie copied from '${blob.host}' but project expects '${expectedHost}'. Wrong tab?`
    }
    // Optional client-side expiry preview (catches paste of an old cookie
    // before the round-trip to the backend).
    const payload = decodeJwtPayload(blob.cookie)
    if (payload && typeof payload.exp === 'number') {
      const exp = payload.exp * 1000
      if (exp < Date.now()) {
        return `Pasted cookie is already expired (${new Date(exp).toISOString()}). Re-login first.`
      }
      setParsedExpiry(new Date(exp).toISOString())
    } else {
      setParsedExpiry(null)  // opaque token — no client-side preview
    }
    return null
  }

  async function handlePasteFromClipboard() {
    setError(null)
    let raw: string
    try {
      raw = await navigator.clipboard.readText()
    } catch (e) {
      setError(`Clipboard read blocked: ${(e as Error).message}. Use the manual paste box below.`)
      return
    }
    handleRawText(raw)
  }

  function handleRawText(raw: string) {
    setError(null)
    let blob: ClipboardPayload
    try {
      blob = JSON.parse(raw.trim())
    } catch {
      setError('Clipboard does not contain valid JSON. Click the bookmarklet on the SUT page first.')
      return
    }
    const err = validatePayload(blob)
    if (err) {
      setError(err)
      return
    }
    setParsed(blob)
    setPhase('paste')
  }

  /**
   * R15-V7 — build a ClipboardPayload from the direct cookie/refresh
   * fields (skipping the JSON wrapper the bookmarklet produces).
   * Returns null when the cookie field is empty.
   */
  function buildDirectPayload(): ClipboardPayload | null {
    const cookie = directCookie.trim()
    if (!cookie) return null
    const cleanedCookie = cookie.replace(/^"+|"+$/g, '').trim()
    let cleanedRefresh: string | null = directRefresh.trim()
    if (cleanedRefresh) {
      cleanedRefresh = cleanedRefresh.replace(/^"+|"+$/g, '').trim()
    } else {
      cleanedRefresh = null
    }
    return {
      cookie: cleanedCookie,
      refresh: cleanedRefresh,
      name: expectedCookieName ?? '',
      host: expectedHost ?? '',
    }
  }

  function handleDirectFieldSubmit() {
    setError(null)
    const blob = buildDirectPayload()
    if (!blob) {
      setError('Cookie value is required.')
      return
    }
    const err = validatePayload(blob)
    if (err) {
      setError(err)
      return
    }
    setParsed(blob)
    setPhase('paste')
  }

  async function handleSubmit() {
    if (!projectId || !environment) return
    // R15-V8 — single-click submit. If `parsed` isn't set yet but the
    // direct fields have content, validate + submit in one go. Removes
    // the confusing two-step flow where operators had to click "Use
    // these values" before "Refresh & Run".
    let payload = parsed
    if (!payload) {
      const blob = buildDirectPayload()
      if (!blob) {
        setError('Paste the cookie value (and optionally the refresh token) before clicking Refresh & Run.')
        return
      }
      const err = validatePayload(blob)
      if (err) {
        setError(err)
        return
      }
      setParsed(blob)
      payload = blob
    }
    setPhase('submitting')
    setError(null)
    // R87.3 — parse Full Capture JSON if provided. Maps the DevTools
    // snippet's output ({ localStorage, sessionStorage, cookies }) onto
    // postAuthState's optional channels. Cookie value still comes from
    // the main paste field — Full Capture only adds PERIPHERAL state.
    let fullCapture: {
      localStorage_entries?: Record<string, string>
      sessionStorage_entries?: Record<string, string>
      additional_cookies?: Array<{ name: string; value: string }>
    } = {}
    if (fullCaptureJSON.trim()) {
      try {
        const parsed_fc = JSON.parse(fullCaptureJSON)
        if (parsed_fc && typeof parsed_fc === 'object') {
          if (parsed_fc.localStorage && typeof parsed_fc.localStorage === 'object') {
            const ls: Record<string, string> = {}
            for (const [k, v] of Object.entries(parsed_fc.localStorage)) {
              if (typeof k === 'string' && typeof v === 'string') ls[k] = v
            }
            if (Object.keys(ls).length > 0) fullCapture.localStorage_entries = ls
          }
          if (parsed_fc.sessionStorage && typeof parsed_fc.sessionStorage === 'object') {
            const ss: Record<string, string> = {}
            for (const [k, v] of Object.entries(parsed_fc.sessionStorage)) {
              if (typeof k === 'string' && typeof v === 'string') ss[k] = v
            }
            if (Object.keys(ss).length > 0) fullCapture.sessionStorage_entries = ss
          }
          // `document.cookie` returns "k1=v1; k2=v2" — split + filter
          // out the main session cookie (already in payload.cookie).
          if (typeof parsed_fc.cookies === 'string' && parsed_fc.cookies) {
            const additional: Array<{ name: string; value: string }> = []
            for (const pair of parsed_fc.cookies.split(';')) {
              const trimmed = pair.trim()
              if (!trimmed) continue
              const eq = trimmed.indexOf('=')
              if (eq < 1) continue
              const ck_name = trimmed.slice(0, eq).trim()
              const ck_val = trimmed.slice(eq + 1).trim()
              if (ck_name === payload.name) continue   // skip main cookie
              additional.push({ name: ck_name, value: ck_val })
            }
            if (additional.length > 0) fullCapture.additional_cookies = additional
          }
        }
      } catch (parseErr) {
        setFullCaptureError(
          'Could not parse Full Capture JSON. It must be a JSON object with localStorage/sessionStorage/cookies keys. Ignoring this field.',
        )
        // Don't fail the submit; the main cookie path still works.
        fullCapture = {}
      }
    }
    try {
      await postAuthState(projectId, {
        environment,
        cookie_value: payload.cookie,
        refresh_token: payload.refresh ?? undefined,
        cookie_name: payload.name,
        source_host: payload.host,
        ...fullCapture,
      })
      setPhase('done')
      // Close + fire onSuccess (which dequeues the original triggerRun).
      // Small delay so operator sees the "Refreshed" confirmation.
      setTimeout(() => {
        onClose()
        // Fire-and-forget; onSuccess may itself open another modal.
        Promise.resolve(onSuccess()).catch(err => {
          // eslint-disable-next-line no-console
          console.error('R15: onSuccess threw:', err)
        })
      }, 600)
    } catch (e) {
      const err = e as Error & { status?: number; detail?: unknown }
      setPhase('paste')
      setError(err.message ?? 'Refresh failed. Check the dashboard logs.')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center"
         role="dialog" aria-modal="true" aria-labelledby="refresh-auth-modal-title">
      <button
        type="button"
        aria-label="Close dialog"
        onClick={onClose}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-default"
      />

      <div
        className="relative w-full max-w-xl mx-4 rounded-2xl p-6 shadow-2xl"
        style={{ background: '#12121f', border: '1px solid #1e1e3a' }}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 id="refresh-auth-modal-title" className="text-lg font-bold"
              style={{ color: '#e2e8f0', fontFamily: 'DM Sans, sans-serif' }}>
            SUT Auth Refresh Required
          </h2>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-sm hover:opacity-80 transition-opacity"
            style={{ background: '#1e1e3a', color: '#94a3b8' }}
            aria-label="Close"
          >
            &#x2715;
          </button>
        </div>

        {/* Status header — the WHY */}
        <div className="rounded-lg p-3 mb-4 text-xs"
             style={{ background: '#1e0a0a', border: '1px solid #4a1414', color: '#fca5a5' }}>
          {initialState?.status === 'expired' && initialState.expired_at && (
            <span>Cookie <code>{expectedCookieName}</code> expired{' '}
              {new Date(initialState.expired_at).toLocaleString()} (
              {Math.floor((Date.now() - new Date(initialState.expired_at).getTime()) / 86400000)} days ago).
              Tests will cascade-skip until refreshed.</span>
          )}
          {initialState?.status === 'missing' && (
            <span>No storage-state file yet (first-run setup). Login to capture session.</span>
          )}
          {initialState?.status === 'unreadable' && (
            <span>Storage-state file is corrupt: {initialState.message}</span>
          )}
          {initialState?.status === 'missing_cookie' && (
            <span>{initialState.message}</span>
          )}
        </div>

        {/* Step 1 — open SUT login */}
        <div className="space-y-3 text-sm" style={{ color: '#cbd5e1' }}>
          <div className="rounded-lg p-3" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
            <div className="text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>
              Step 1: Login to {expectedHost ?? 'SUT'}
            </div>
            <a
              href={refreshUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center px-3 py-2 rounded-lg text-xs font-medium hover:opacity-80 transition-opacity"
              style={{ background: '#6366f1', color: '#fff' }}
            >
              Open {refreshUrl} →
            </a>
            <span className="ml-3 text-xs" style={{ color: '#64748b' }}>
              Opens in a new tab. Sign in normally; OAuth flow happens on the SUT's domain.
            </span>
          </div>

          {/* Step 2 — bookmarklet (V4 affordance) */}
          <div className="rounded-lg p-3" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
            <div className="text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>
              Step 2: Click the ARTA bookmarklet on the SUT page
            </div>
            <a
              href={BOOKMARKLET_JS}
              draggable={true}
              onClick={(e) => e.preventDefault()}
              className="inline-flex items-center px-3 py-2 rounded-lg text-xs font-medium"
              style={{ background: '#1e1e3a', color: '#e2e8f0', border: '1px dashed #6366f1' }}
            >
              📋 ARTA Copy Auth
            </a>
            <span className="ml-3 text-xs" style={{ color: '#64748b' }}>
              First time? Drag this to your bookmarks bar (or right-click → "Bookmark this link").
            </span>
            <div className="text-[10px] mt-2" style={{ color: '#64748b' }}>
              On the SUT post-login page, click this bookmark. It copies the cookie + refresh-token
              to your clipboard.
            </div>
          </div>

          {/* Step 3 — paste (V7: direct-fields path is now the default) */}
          <div className="rounded-lg p-3" style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
            <div className="text-xs uppercase tracking-wider mb-2" style={{ color: '#94a3b8' }}>
              Step 3: Paste cookie + refresh-token
            </div>
            {expectedCookieName && (
              <div className="text-[11px] mb-2" style={{ color: '#94a3b8' }}>
                Project expects cookie <code>{expectedCookieName}</code>{' '}
                {expectedHost ? <>from <code>{expectedHost}</code></> : null}
              </div>
            )}

            {/* V7 — direct two-field mode. Operator opens DevTools on the
                SUT post-login page (Application → Cookies → &lt;your session cookie&gt;,
                Local Storage → refresh-token), copies each value, pastes
                here. Works on HTTP, Incognito, restricted browsers — no
                bookmarklet, no clipboard API. */}
            <label className="block text-[11px] mt-1" style={{ color: '#94a3b8' }}>
              Cookie value <code className="text-[10px]">{expectedCookieName ?? 'session'}</code>{' '}
              <span className="text-[10px]" style={{ color: '#64748b' }}>
                (DevTools → Application → Cookies → {expectedCookieName ?? '<cookie>'} → copy "Value")
              </span>
            </label>
            <textarea
              value={directCookie}
              onChange={e => setDirectCookie(e.target.value)}
              placeholder="<paste JWT here>"
              rows={2}
              className="w-full mt-1 px-3 py-2 rounded-lg text-xs font-mono"
              style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
            />

            <label className="block text-[11px] mt-3" style={{ color: '#94a3b8' }}>
              Refresh token <span className="text-[10px]" style={{ color: '#64748b' }}>(optional: Local Storage → refresh-token)</span>
            </label>
            <textarea
              value={directRefresh}
              onChange={e => setDirectRefresh(e.target.value)}
              placeholder='1//0g... (paste with or without surrounding quotes)'
              rows={2}
              className="w-full mt-1 px-3 py-2 rounded-lg text-xs font-mono"
              style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
            />

            <button
              type="button"
              onClick={handleDirectFieldSubmit}
              disabled={!directCookie.trim() || phase === 'submitting'}
              className="mt-2 px-3 py-2 rounded-lg text-xs font-medium hover:opacity-80 transition-opacity disabled:opacity-40 disabled:cursor-not-allowed"
              style={{ background: '#06b6d4', color: '#0a0a14' }}
            >
              Use these values
            </button>

            {/* Bookmarklet/clipboard path — kept as a "speed option" for
                operators who installed the bookmark and have HTTPS clipboard. */}
            <details className="mt-3">
              <summary className="text-[10px] cursor-pointer" style={{ color: '#64748b' }}>
                Or paste from clipboard (bookmarklet path)
              </summary>
              <button
                type="button"
                onClick={handlePasteFromClipboard}
                disabled={phase === 'submitting'}
                className="mt-2 px-3 py-2 rounded-lg text-xs font-medium hover:opacity-80 transition-opacity"
                style={{ background: '#1e1e3a', color: '#06b6d4' }}
              >
                Paste from clipboard
              </button>
              <textarea
                value={manualText}
                onChange={e => setManualText(e.target.value)}
                placeholder='{"cookie":"eyJ...", "refresh":"1//0g...", "name":"session-token", "host":"app.example.internal"}'
                rows={3}
                className="w-full mt-2 px-3 py-2 rounded-lg text-xs font-mono"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
              <button
                type="button"
                onClick={() => handleRawText(manualText)}
                disabled={!manualText.trim() || phase === 'submitting'}
                className="mt-1 px-2 py-1 rounded text-[10px]"
                style={{ background: '#1e1e3a', color: '#94a3b8' }}
              >
                Use manual JSON
              </button>
            </details>

            {/* R87.3 — Full Capture section. For SPAs that need more
               than the main session cookie (sessionStorage entries,
               auxiliary cookies, additional localStorage keys), the
               operator can paste a comprehensive snapshot captured by
               running the DevTools console snippet below. */}
            <details className="mt-3">
              <summary className="cursor-pointer text-xs"
                       style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
                Advanced: Full Capture (optional, fixes SPA login redirects)
              </summary>
              <div className="mt-2 rounded-lg p-3 text-[11px]"
                   style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#94a3b8' }}>
                <p className="mb-2" style={{ color: '#e2e8f0' }}>
                  If the SPA still serves the login screen after refresh, it
                  may depend on sessionStorage or auxiliary cookies. Open
                  DevTools on the SUT tab, paste this into Console, then
                  paste the clipboard result below:
                </p>
                <pre className="rounded p-2 text-[10px] overflow-x-auto"
                     style={{ background: '#000', color: '#86efac', fontFamily: 'JetBrains Mono, monospace' }}>
{`copy(JSON.stringify({
  localStorage: Object.fromEntries(Object.entries(localStorage)),
  sessionStorage: Object.fromEntries(Object.entries(sessionStorage)),
  cookies: document.cookie,
}, null, 2));`}
                </pre>
                <p className="mt-2 mb-1" style={{ color: '#fbbf24' }}>
                  ⚠ HttpOnly cookies are NOT visible to JS. Inspect
                  Application → Cookies tab manually for those (rare).
                </p>
              </div>
              <textarea
                value={fullCaptureJSON}
                onChange={e => {
                  setFullCaptureJSON(e.target.value)
                  setFullCaptureError(null)
                }}
                placeholder='{"localStorage":{"UserName":"..."},"sessionStorage":{},"cookies":"k1=v1; k2=v2"}'
                rows={4}
                className="w-full mt-2 px-3 py-2 rounded-lg text-xs font-mono"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
              {fullCaptureError && (
                <div className="mt-1 text-[10px]" style={{ color: '#fca5a5' }}>
                  {fullCaptureError}
                </div>
              )}
              {fullCaptureJSON.trim() && !fullCaptureError && (
                <div className="mt-1 text-[10px]" style={{ color: '#86efac' }}>
                  ✓ Full Capture queued. It will submit with the cookie below
                </div>
              )}
            </details>
          </div>

          {/* Parsed-payload preview (only shown after successful parse) */}
          {parsed && phase === 'paste' && (
            <div className="rounded-lg p-3 text-xs"
                 style={{ background: '#062114', border: '1px solid #14532d', color: '#86efac' }}>
              ✓ Parsed: <code>{parsed.name}</code> from <code>{parsed.host}</code>
              {parsedExpiry && <> · valid until <code>{parsedExpiry}</code></>}
              {!parsedExpiry && <> · opaque token (server will accept)</>}
              {parsed.refresh && <> · refresh-token included</>}
            </div>
          )}

          {error && (
            <div className="rounded-lg p-3 text-xs"
                 style={{ background: '#1e0a0a', border: '1px solid #4a1414', color: '#fca5a5' }}>
              {error}
            </div>
          )}

          {phase === 'done' && (
            <div className="rounded-lg p-3 text-xs"
                 style={{ background: '#062114', border: '1px solid #14532d', color: '#86efac' }}>
              ✓ Storage state refreshed. Running suite now…
            </div>
          )}

          {/* Submit row */}
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-2 rounded-lg text-xs"
              style={{ background: '#1e1e3a', color: '#94a3b8' }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSubmit}
              /* R15-V8 — enable when EITHER `parsed` is set OR the direct
                 fields have content. handleSubmit will validate inline
                 if parsed is null, removing the prior two-step flow. */
              disabled={
                (!parsed && !directCookie.trim())
                || phase === 'submitting'
                || phase === 'done'
              }
              className="px-4 py-2 rounded-lg text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-80 transition-opacity"
              style={{ background: '#8b5cf6', color: '#fff' }}
            >
              {phase === 'submitting' ? 'Refreshing…' : phase === 'done' ? '✓ Done' : 'Refresh & Run'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
