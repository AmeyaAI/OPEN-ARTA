/**
 * R29.4 — typed client for env-var listing + bulk update.
 *
 * Backend routes (added in src/api/routers/projects.py):
 *   GET  /api/projects/{id}/environments/{env}/variables
 *   PUT  /api/projects/{id}/environments/{env}/variables
 */

const API_BASE = process.env.NEXT_PUBLIC_ARTA_API_URL ?? ''

function buildHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  }
  // Same token-attachment pattern as api-client.ts.
  if (typeof window !== 'undefined') {
    const tok = window.localStorage.getItem('arta_token')
    if (tok) headers['Authorization'] = `Bearer ${tok}`
  }
  const apiKey = process.env.NEXT_PUBLIC_ARTA_API_KEY
  if (apiKey) headers['X-API-Key'] = apiKey
  return headers
}

export interface EnvVariable {
  name: string
  value: string
  is_placeholder: boolean
  is_sensitive: boolean
}

export interface EnvVariablesResponse {
  project_id: string
  env_name: string
  variables: EnvVariable[]
  filled_count: number
  total_count: number
  needs_attention: string[]
}

export interface UpdateVariablesResponse {
  project_id: string
  env_name: string
  saved: string[]
  filled_count: number
  total_count: number
}

export async function fetchEnvVariables(
  projectId: string,
  envName: string,
): Promise<EnvVariablesResponse> {
  const url = `${API_BASE}/api/projects/${encodeURIComponent(projectId)}/environments/${encodeURIComponent(envName)}/variables`
  const res = await fetch(url, { headers: buildHeaders() })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`)
  }
  return res.json()
}

export async function updateEnvVariables(
  projectId: string,
  envName: string,
  updates: Record<string, string>,
): Promise<UpdateVariablesResponse> {
  const url = `${API_BASE}/api/projects/${encodeURIComponent(projectId)}/environments/${encodeURIComponent(envName)}/variables`
  const res = await fetch(url, {
    method: 'PUT',
    headers: buildHeaders(),
    body: JSON.stringify({ updates }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    // 400 with { error, rejected } payload — surface the rejected map.
    const detail = body.detail
    if (detail && typeof detail === 'object' && detail.rejected) {
      const names = Object.keys(detail.rejected).join(', ')
      throw new Error(`Rejected: ${names} (values still look like placeholders)`)
    }
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`)
  }
  return res.json()
}
