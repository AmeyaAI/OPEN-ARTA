'use client'

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { type UserPublic, getToken, setToken, login as apiLogin, logout as apiLogout,
  fetchMyProjectRoles } from './api-client'

interface AuthState {
  user: UserPublic | null
  /** project_id → role for the current user (empty until loaded); used to gate write UI. */
  roles: Record<string, string>
  isLoading: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => void
  setUserFromOAuth: (user: UserPublic, token: string) => void
}

const AuthContext = createContext<AuthState>({
  user: null,
  roles: {},
  isLoading: true,
  login: async () => {},
  logout: () => {},
  setUserFromOAuth: () => {},
})

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserPublic | null>(null)
  const [roles, setRoles] = useState<Record<string, string>>({})
  const [isLoading, setIsLoading] = useState(true)

  // Load the user's per-project roles whenever the user changes (for write-action gating).
  useEffect(() => {
    let cancelled = false
    if (!user?.id) { setRoles({}); return }
    fetchMyProjectRoles(user.id)
      .then(list => { if (!cancelled) setRoles(Object.fromEntries((list || []).map(r => [r.project_id, r.role]))) })
      .catch(() => { if (!cancelled) setRoles({}) })
    return () => { cancelled = true }
  }, [user?.id])

  useEffect(() => {
    // Check for existing token on mount
    const token = getToken()
    if (token) {
      // Try to get stored user from localStorage
      const storedUser = localStorage.getItem('arta_user')
      if (storedUser) {
        try {
          setUser(JSON.parse(storedUser))
        } catch {
          setToken(null)
        }
      }
    }
    setIsLoading(false)
  }, [])

  const login = async (email: string, password: string) => {
    const result = await apiLogin(email, password)
    setUser(result.user)
    localStorage.setItem('arta_user', JSON.stringify(result.user))
  }

  const logout = () => {
    apiLogout()
    setUser(null)
    localStorage.removeItem('arta_user')
  }

  const setUserFromOAuth = (oauthUser: UserPublic, token: string) => {
    setToken(token)
    setUser(oauthUser)
    localStorage.setItem('arta_user', JSON.stringify(oauthUser))
  }

  return (
    <AuthContext.Provider value={{ user, roles, isLoading, login, logout, setUserFromOAuth }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
