'use client'

import { useState, useEffect, Suspense } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import Link from 'next/link'
import { acceptInvite } from '@/lib/api-client'

function AcceptInviteForm() {
  const router = useRouter()
  const search = useSearchParams()
  const token = search.get('token') || ''

  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Surface a clear error before the user fills the form when the link is broken.
  useEffect(() => {
    if (!token) setError('Missing invite token. The link you used appears to be incomplete.')
  }, [token])

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!token) return
    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    setSubmitting(true)
    try {
      await acceptInvite(token, password)
      router.push('/admin')
    } catch (err: any) {
      setError(err?.message || 'Could not accept invite. The link may have expired.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4"
         style={{ background: '#05050a', color: '#e2e8f0' }}>
      <div className="w-full max-w-md">
        <div className="flex items-center gap-3 mb-8 justify-center">
          <div className="w-9 h-9 rounded-lg flex items-center justify-center text-lg font-bold"
               style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)' }}>A</div>
          <span className="text-xl font-bold">ARTA</span>
        </div>

        <div className="rounded-2xl p-6"
             style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
          <h1 className="text-lg font-bold mb-1" style={{ color: '#e2e8f0' }}>Accept your invite</h1>
          <p className="text-xs mb-6" style={{ color: '#94a3b8' }}>
            Set a password to activate your account. After this you can sign in any time at the login page.
          </p>

          {error && (
            <div className="mb-4 px-3 py-2 rounded-lg text-xs"
                 style={{ background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)', color: '#f87171' }}>
              {error}
            </div>
          )}

          <form onSubmit={onSubmit} className="space-y-4">
            <div>
              <label className="block text-xs uppercase tracking-wider mb-1" style={{ color: '#94a3b8' }}>
                New password
              </label>
              <input
                type="password"
                required
                minLength={8}
                disabled={!token || submitting}
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="At least 8 characters"
                className="w-full px-3 py-2 rounded-lg text-sm"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
            </div>
            <div>
              <label className="block text-xs uppercase tracking-wider mb-1" style={{ color: '#94a3b8' }}>
                Confirm password
              </label>
              <input
                type="password"
                required
                minLength={8}
                disabled={!token || submitting}
                value={confirm}
                onChange={e => setConfirm(e.target.value)}
                className="w-full px-3 py-2 rounded-lg text-sm"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
            </div>

            <button
              type="submit"
              disabled={!token || submitting}
              className="w-full px-4 py-2.5 rounded-lg text-sm font-medium"
              style={{
                background: '#6366f1',
                color: '#fff',
                opacity: (!token || submitting) ? 0.6 : 1,
              }}
            >
              {submitting ? 'Activating…' : 'Accept invite & sign in'}
            </button>
          </form>

          <div className="mt-6 text-center text-xs">
            <Link href="/login" style={{ color: '#94a3b8' }}>Back to sign-in</Link>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function AcceptInvitePage() {
  return (
    <Suspense fallback={null}>
      <AcceptInviteForm />
    </Suspense>
  )
}
