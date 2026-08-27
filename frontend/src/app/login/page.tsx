'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { useAuth } from '@/lib/auth-context'

export default function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [showDemo, setShowDemo] = useState(false)
  const { login } = useAuth()
  const router = useRouter()

  // SEC: the demo-credentials panel is fail-closed — it renders ONLY when
  // explicitly enabled via NEXT_PUBLIC_ARTA_DEMO_MODE=1 (local demos, alongside
  // the backend ARTA_DEMO_MODE). It never shows on a normal deployment, so real
  // hosts never advertise demo access.
  const demoLoginEnabled = process.env.NEXT_PUBLIC_ARTA_DEMO_MODE === '1'

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await login(email, password)
      router.push('/')
    } catch (err: any) {
      setError(err.message || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  const fillDemo = () => {
    setEmail('admin@arta.dev')
    setPassword('demo1234')
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-14 h-14 rounded-2xl flex items-center justify-center text-2xl font-bold mb-3"
               style={{ background: 'linear-gradient(135deg,#6366f1,#8b5cf6)', color: '#fff' }}>
            A
          </div>
          <h1 className="text-xl font-bold text-white">ARTA Platform</h1>
          <p className="text-xs mt-1" style={{ color: '#64748b' }}>AI Requirements & Test Architect</p>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="rounded-xl p-6 space-y-4"
              style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
          {error && (
            <div className="px-3 py-2 rounded text-sm"
                 style={{ background: '#1a0a0a', border: '1px solid #7f1d1d', color: '#fca5a5' }}>
              {error}
            </div>
          )}

          <div>
            <label className="block text-xs mb-1.5" style={{ color: '#94a3b8' }}>Email</label>
            <input
              type="email" value={email} onChange={e => setEmail(e.target.value)}
              placeholder="you@company.com"
              className="w-full px-3 py-2 rounded-lg text-sm text-white outline-none focus:ring-1"
              style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}
              required
            />
          </div>

          <div>
            <label className="block text-xs mb-1.5" style={{ color: '#94a3b8' }}>Password</label>
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                value={password} onChange={e => setPassword(e.target.value)}
                placeholder="Enter your password"
                className="w-full px-3 py-2 pr-10 rounded-lg text-sm text-white outline-none focus:ring-1"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}
                required
              />
              <button type="button"
                      onClick={() => setShowPassword(!showPassword)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-xs"
                      style={{ color: '#94a3b8' }}>
                {showPassword ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>

          <button type="submit" disabled={loading}
                  className="w-full py-2.5 rounded-lg text-sm font-medium text-white transition"
                  style={{ background: loading ? '#4338ca' : '#6366f1' }}>
            {loading ? 'Signing in\u2026' : 'Sign In'}
          </button>

          {/* Demo credentials - collapsible, fail-closed (see demoLoginEnabled) */}
          {demoLoginEnabled && (
          <div className="pt-1">
            <button type="button"
                    onClick={() => setShowDemo(!showDemo)}
                    className="text-[10px] w-full text-center transition-colors"
                    style={{ color: '#94a3b8' }}>
              {showDemo ? 'Hide demo access' : 'Demo access available'}
            </button>
            {showDemo && (
              <div className="mt-2 px-3 py-2 rounded-lg text-[11px] flex items-center justify-between"
                   style={{ background: '#0a0a14', border: '1px solid #1e1e3a' }}>
                <div>
                  <span style={{ color: '#64748b' }}>admin@arta.dev</span>
                  <span className="mx-1.5" style={{ color: '#1e1e3a' }}>/</span>
                  <span style={{ color: '#64748b' }}>demo1234</span>
                </div>
                <button type="button" onClick={fillDemo}
                        className="px-2 py-0.5 rounded text-[10px] font-medium"
                        style={{ background: '#6366f1', color: '#fff' }}>
                  Use
                </button>
              </div>
            )}
          </div>
          )}

          {/* OAuth Divider */}
          <div className="flex items-center gap-3 pt-1">
            <div className="flex-1 h-px" style={{ background: '#1e1e3a' }} />
            <span className="text-[10px]" style={{ color: '#94a3b8' }}>or continue with</span>
            <div className="flex-1 h-px" style={{ background: '#1e1e3a' }} />
          </div>

          {/* OAuth Buttons */}
          <div className="flex gap-2">
            <button type="button" disabled
                    className="flex-1 flex items-center justify-center gap-2 py-2 rounded-lg text-sm transition opacity-50 cursor-not-allowed"
                    style={{ background: '#1e1e3a', border: '1px solid #2d2d4a', color: '#64748b' }}
                    title="Coming soon">
              <svg viewBox="0 0 24 24" className="w-4 h-4" fill="currentColor">
                <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/>
                <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
                <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
                <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
              </svg>
              Google
            </button>
            <button type="button" disabled
                    className="flex-1 flex items-center justify-center gap-2 py-2 rounded-lg text-sm transition opacity-50 cursor-not-allowed"
                    style={{ background: '#1e1e3a', border: '1px solid #2d2d4a', color: '#64748b' }}
                    title="Coming soon">
              <svg viewBox="0 0 24 24" className="w-4 h-4" fill="currentColor">
                <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/>
              </svg>
              GitHub
            </button>
          </div>
          <p className="text-center text-[9px]" style={{ color: '#374151' }}>
            OAuth integrations coming soon
          </p>
        </form>
      </div>
    </div>
  )
}
