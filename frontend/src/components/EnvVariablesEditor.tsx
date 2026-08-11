'use client'

/**
 * R29.4 — Operator-facing env-var bulk editor.
 *
 * Pre-R29.4 operators had to hand-edit `.arta/projects.json` to fill
 * the 22+ unfilled vars (REPLACE_ME / ***) blocking Newman/k6/ZAP
 * dispatch. After R29.3a these unfilled vars surface as BLOCKED rows
 * in the dashboard with a clear count, but operators STILL had no
 * dashboard path to fix them.
 *
 * This editor:
 *   - Lists all declared env-vars with `is_placeholder` status
 *   - Groups into "Needs attention" (placeholder) + "Filled" sections
 *   - Sensitive values (token/secret/password/key/cookie/auth) render
 *     as `<input type="password">` with show/hide toggle
 *   - Bulk save via PUT /api/projects/{id}/environments/{env}/variables
 *   - On success, refetches and updates the badge counts
 *
 * Mounted from the Settings → Environments tab, scoped to the active
 * project + selected environment.
 */

import * as React from 'react'
import { useEffect, useState, useCallback } from 'react'
import { fetchEnvVariables, updateEnvVariables, type EnvVariable } from '@/lib/env-variables'

interface Props {
  projectId: string
  envName: string
}

export default function EnvVariablesEditor({ projectId, envName }: Props) {
  const [items, setItems] = useState<EnvVariable[]>([])
  const [, setFilledCount] = useState<number>(0)
  const [totalCount, setTotalCount] = useState<number>(0)
  const [resolvedEnv, setResolvedEnv] = useState<string>(envName)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [edited, setEdited] = useState<Record<string, string>>({})
  const [revealed, setRevealed] = useState<Set<string>>(new Set())
  const [savedFlash, setSavedFlash] = useState<string | null>(null)
  // R45.5 — `?highlight=name1,name2,...` deep-links from the toast.
  // Read once on mount; pre-populate a Set the row renderer reads.
  const [highlighted] = useState<Set<string>>(() => {
    if (typeof window === 'undefined') return new Set()
    try {
      const params = new URLSearchParams(
        // The toast uses `#env=staging&highlight=...` form; also
        // accept plain `?highlight=...` for direct links.
        (window.location.hash.startsWith('#') ? window.location.hash.slice(1) : '')
        + '&'
        + window.location.search.replace(/^\?/, ''),
      )
      const raw = params.get('highlight')
      if (!raw) return new Set()
      return new Set(
        raw.split(',').map(s => s.trim()).filter(Boolean),
      )
    } catch { return new Set() }
  })

  const refresh = useCallback(async () => {
    if (!projectId || !envName) return
    setLoading(true)
    setError(null)
    try {
      const r = await fetchEnvVariables(projectId, envName)
      setItems(r.variables)
      setFilledCount(r.filled_count)
      setTotalCount(r.total_count)
      setResolvedEnv(r.env_name)
      setEdited({})
    } catch (e: any) {
      setError(e?.message || 'Failed to load environment variables')
    } finally {
      setLoading(false)
    }
  }, [projectId, envName])

  useEffect(() => {
    refresh()
  }, [refresh])

  const dirtyCount = Object.keys(edited).length
  const handleSaveAll = async () => {
    if (dirtyCount === 0) return
    setSaving(true)
    setError(null)
    try {
      const r = await updateEnvVariables(projectId, envName, edited)
      setSavedFlash(`Saved ${r.saved.length} variable${r.saved.length === 1 ? '' : 's'}`)
      setTimeout(() => setSavedFlash(null), 2500)
      await refresh()
    } catch (e: any) {
      setError(e?.message || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const updateValue = (name: string, val: string) => {
    setEdited(prev => ({ ...prev, [name]: val }))
  }

  const toggleReveal = (name: string) => {
    setRevealed(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  if (loading) {
    return (
      <div className="text-xs" style={{ color: '#64748b' }}>
        Loading environment variables…
      </div>
    )
  }
  if (totalCount === 0) {
    return (
      <div className="text-xs" style={{ color: '#64748b' }}>
        No env-vars declared for environment <code>{resolvedEnv}</code>.
        Use <strong>Custom Variables</strong> above to add names; their
        values appear here once added.
      </div>
    )
  }

  const placeholders = items.filter(i => i.is_placeholder)
  const filled = items.filter(i => !i.is_placeholder)

  return (
    <div className="space-y-4">
      {/* Status banner */}
      <div className="flex items-center justify-between px-3 py-2 rounded-lg"
           style={{
             background: placeholders.length > 0 ? 'rgba(251,146,60,0.15)' : 'rgba(52,211,153,0.10)',
             border: placeholders.length > 0 ? '1px solid rgba(251,146,60,0.3)' : '1px solid rgba(52,211,153,0.2)',
           }}>
        <div className="text-xs" style={{ color: placeholders.length > 0 ? '#fb923c' : '#34d399' }}>
          {placeholders.length > 0
            ? <><strong>{placeholders.length}</strong> of {totalCount} variable{totalCount === 1 ? '' : 's'} need{placeholders.length === 1 ? 's' : ''} a value — tests using these will be marked <strong>BLOCKED</strong> until filled.</>
            : <>All {totalCount} declared variable{totalCount === 1 ? '' : 's'} have values for <code>{resolvedEnv}</code>.</>}
        </div>
        {dirtyCount > 0 && (
          <button onClick={handleSaveAll} disabled={saving}
                  className="px-3 py-1.5 rounded text-xs font-medium"
                  style={{ background: '#6366f1', color: '#fff', opacity: saving ? 0.6 : 1 }}>
            {saving ? 'Saving…' : `Save ${dirtyCount}`}
          </button>
        )}
      </div>

      {error && (
        <div className="px-3 py-2 rounded-lg text-xs"
             style={{ background: 'rgba(251,113,133,0.15)', color: '#fb7185',
                      border: '1px solid rgba(251,113,133,0.2)' }}>
          {error}
        </div>
      )}

      {savedFlash && (
        <div className="px-3 py-2 rounded-lg text-xs"
             style={{ background: 'rgba(52,211,153,0.15)', color: '#34d399',
                      border: '1px solid rgba(52,211,153,0.2)' }}>
          {savedFlash}
        </div>
      )}

      {/* Needs attention */}
      {placeholders.length > 0 && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wider mb-2"
               style={{ color: '#fb923c' }}>
            Needs attention ({placeholders.length})
          </div>
          <div className="space-y-1.5">
            {placeholders.map(v => (
              <VariableRow key={v.name}
                           variable={v}
                           value={edited[v.name] ?? ''}
                           onChange={val => updateValue(v.name, val)}
                           revealed={revealed.has(v.name)}
                           onToggleReveal={() => toggleReveal(v.name)}
                           placeholder
                           highlighted={highlighted.has(v.name)} />
            ))}
          </div>
        </div>
      )}

      {/* Filled */}
      {filled.length > 0 && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-wider mb-2"
               style={{ color: '#34d399' }}>
            Filled ({filled.length})
          </div>
          <div className="space-y-1.5">
            {filled.map(v => (
              <VariableRow key={v.name}
                           variable={v}
                           value={edited[v.name] ?? v.value}
                           onChange={val => updateValue(v.name, val)}
                           highlighted={highlighted.has(v.name)}
                           revealed={revealed.has(v.name)}
                           onToggleReveal={() => toggleReveal(v.name)}
                           placeholder={false} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function VariableRow({
  variable, value, onChange, revealed, onToggleReveal, placeholder, highlighted,
}: {
  variable: EnvVariable
  value: string
  onChange: (v: string) => void
  revealed: boolean
  onToggleReveal: () => void
  placeholder: boolean
  highlighted?: boolean
}) {
  const inputType = variable.is_sensitive && !revealed ? 'password' : 'text'
  // R45.5 — when highlighted (deep-link from toast), scroll the row
  // into view + apply a yellow sidebar marker. Only first highlighted
  // row in render order scrolls; others are still styled.
  const ref = React.useRef<HTMLDivElement | null>(null)
  React.useEffect(() => {
    if (highlighted && ref.current) {
      try {
        ref.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
      } catch { /* older browsers */ }
    }
  }, [highlighted])
  return (
    <div ref={ref}
         className="flex items-center gap-2 px-2 py-1.5 rounded"
         style={{
           background: highlighted ? 'rgba(251,191,36,0.08)' : '#0a0a14',
           border: highlighted ? '1px solid rgba(251,191,36,0.5)' : '1px solid #1e1e3a',
           boxShadow: highlighted ? 'inset 3px 0 0 #fbbf24' : undefined,
         }}>
      <code className="text-[11px] font-mono shrink-0"
            style={{
              width: 200,
              color: placeholder ? '#fb923c' : '#94a3b8',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
            title={variable.name}>
        {variable.name}
      </code>
      <input
        type={inputType}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder ? 'Fill in a real value…' : ''}
        className="flex-1 px-2 py-1 rounded text-xs outline-none font-mono"
        style={{
          background: '#12121f',
          border: placeholder ? '1px solid rgba(251,146,60,0.4)' : '1px solid #1e1e3a',
          color: '#e2e8f0',
        }}
      />
      {variable.is_sensitive && (
        <button onClick={onToggleReveal}
                className="px-2 text-xs"
                style={{ color: '#64748b' }}
                title={revealed ? 'Hide value' : 'Show value'}>
          {revealed ? '🙈' : '👁'}
        </button>
      )}
    </div>
  )
}
