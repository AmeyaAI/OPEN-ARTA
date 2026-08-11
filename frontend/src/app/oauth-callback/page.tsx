'use client'

import { useEffect, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { setToken } from '@/lib/api-client'

export default function OAuthCallbackPage() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const token = searchParams.get('token')
    const userB64 = searchParams.get('user')

    if (!token || !userB64) {
      setError('Missing authentication data — please try again.')
      return
    }

    try {
      // Decode base64url user JSON
      const userJson = atob(userB64.replace(/-/g, '+').replace(/_/g, '/'))
      const user = JSON.parse(userJson)

      // Store token and user
      setToken(token)
      localStorage.setItem('arta_user', JSON.stringify(user))

      // Redirect to dashboard
      router.push('/')
    } catch {
      setError('Failed to process authentication response.')
    }
  }, [searchParams, router])

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center px-4">
        <div className="text-center">
          <p className="text-red-400 mb-4">{error}</p>
          <button
            onClick={() => router.push('/login')}
            className="px-4 py-2 rounded-lg text-sm text-white"
            style={{ background: '#6366f1' }}
          >
            Back to Login
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="text-center">
        <div className="w-8 h-8 border-2 border-indigo-500 border-t-transparent rounded-full animate-spin mx-auto mb-3" />
        <p className="text-sm" style={{ color: '#94a3b8' }}>Completing sign-in...</p>
      </div>
    </div>
  )
}
