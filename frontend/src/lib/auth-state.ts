/**
 * R15d — Typed wrappers for the SUT-auth-state pre-flight + ingest routes.
 *
 * Used by:
 *   - useRunGuard hook (R15e) — pre-run pre-flight check
 *   - RefreshAuthModal component (R15c) — paste-flow submission
 *
 * Mirrors api-client.ts conventions: returns parsed JSON; throws Error
 * with `.status` + `.detail` on non-2xx so callers can branch.
 */

const API_BASE = process.env.NEXT_PUBLIC_ARTA_API_URL ?? ''

export type AuthStateStatus =
  | 'valid'           // JWT exp is in the future
  | 'expired'         // JWT exp is past or within leeway
  | 'missing'         // no storage-state file exists
  | 'unreadable'      // file exists but corrupt
  | 'missing_cookie'  // file exists but no session cookie inside
  | 'opaque'          // session cookie isn't a JWT (can't auto-detect expiry)

export interface AuthState {
  status: AuthStateStatus
  needs_refresh: boolean
  cookie_name: string | null
  base_url: string | null
  refresh_url: string | null
  env_name: string | null
  expires_at?: string | null
  expired_at?: string | null
  message?: string
}

export interface AuthStateUpdateResult {
  status: 'ok'
  storage_path: string
  expires_at: string | null
  cookie_name: string | null
  env_name: string | null
  refresh_token_updated: boolean
}

export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (typeof window !== 'undefined') {
    const tok = localStorage.getItem('arta_token')
    if (tok) headers['Authorization'] = `Bearer ${tok}`
  }
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY
  if (apiKey) headers['X-API-Key'] = apiKey
  return headers
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    // R41.3 — match api-client.ts behaviour: a 401 with reason=jwt_expired
    // (or no_credential_provided when localStorage cleared) means the
    // session is invalid. Clear the stale token + send the operator to
    // /login so they can re-auth and come back to the same page.
    // Pre-fix RefreshAuthModal would show a misleading "Invalid or
    // missing API key" toast and stay open, trapping the operator.
    if (res.status === 401 && typeof window !== 'undefined') {
      const detail = body?.detail
      const reason = (detail && typeof detail === 'object')
        ? (detail as { reason?: string }).reason
        : undefined
      if (reason === 'jwt_expired'
          || reason === 'query_jwt_expired'
          || reason === 'no_credential_provided') {
        try {
          localStorage.removeItem('arta_token')
          const next = encodeURIComponent(window.location.pathname + window.location.search)
          window.location.href = `/login?next=${next}`
        } catch { /* best-effort */ }
      }
    }
    const message = typeof body.detail === 'string'
      ? body.detail
      : (body.detail?.message ?? body.detail?.reason ?? `HTTP ${res.status}`)
    const err = new Error(message) as Error & { status: number; detail?: unknown }
    err.status = res.status
    err.detail = body.detail
    throw err
  }
  return res.json()
}

/**
 * R15a — pre-flight status check. Returns an AuthState describing whether
 * the project's stored SUT cookie is valid, expired, or missing. Cheap
 * (no SUT network call); safe to call before every run trigger.
 */
export async function fetchAuthState(
  projectId: string,
  environment: string,
): Promise<AuthState> {
  const url = `${API_BASE}/api/projects/${encodeURIComponent(projectId)}/auth-state` +
              `?environment=${encodeURIComponent(environment)}`
  const res = await fetch(url, { headers: authHeaders() })
  return jsonOrThrow<AuthState>(res)
}

/**
 * R15b — submit a fresh cookie from the modal paste flow. Backend writes
 * `.arta/environments/<env>-storage.json` atomically (with `.pre-refresh.bak`
 * backup) and returns the new expiry derived from the JWT exp claim.
 *
 * The R15-V1 host-mismatch + cookie-name-mismatch + already-expired-paste
 * checks fire here as 400 errors with human-readable detail strings.
 */
export async function postAuthState(
  projectId: string,
  body: {
    environment: string
    cookie_value: string
    refresh_token?: string | null
    cookie_name?: string | null
    source_host?: string | null
    // R82.2 — operator-supplied additional localStorage entries
    localStorage_entries?: Record<string, string>
    // R87.2 — sessionStorage entries (separate API from localStorage)
    sessionStorage_entries?: Record<string, string>
    additional_cookies?: Array<{
      name: string
      value: string
      domain?: string
      path?: string
      httpOnly?: boolean
      secure?: boolean
      sameSite?: string
      expires?: number
    }>
  },
): Promise<AuthStateUpdateResult> {
  const url = `${API_BASE}/api/projects/${encodeURIComponent(projectId)}/auth-state`
  const res = await fetch(url, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  })
  return jsonOrThrow<AuthStateUpdateResult>(res)
}

/**
 * Decode a JWT payload client-side WITHOUT verifying signature. Used by the
 * modal to show "Cookie valid until <X>" before the operator clicks submit.
 *
 * Returns null on any parse failure — callers must handle the opaque case.
 */
export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const parts = (token || '').split('.')
  if (parts.length !== 3) return null
  try {
    // base64url → base64 (browser atob doesn't accept url-safe alphabet)
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const pad = '='.repeat((4 - (b64.length % 4)) % 4)
    return JSON.parse(atob(b64 + pad))
  } catch {
    return null
  }
}
