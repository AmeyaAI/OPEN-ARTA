/**
 * R15e — Pre-run gate hook. Wraps any "trigger a run" action with an
 * auth-state pre-flight: if the SUT cookie is expired, opens the
 * RefreshAuthModal and queues the run for after the operator pastes a
 * fresh cookie.
 *
 * Usage at every Run Suite click site:
 *
 *   const { needsRefresh, runOrPromptRefresh, onRefreshSuccess, dismissRefresh, authState } =
 *     useRunGuard(projectId, environment)
 *
 *   async function handleRunSuite() {
 *     await runOrPromptRefresh(() => triggerRun({ suite_type, environment }))
 *   }
 *
 *   return (<>
 *     ...
 *     <RefreshAuthModal
 *       open={needsRefresh}
 *       projectId={projectId}
 *       environment={environment}
 *       initialState={authState}
 *       onClose={dismissRefresh}
 *       onSuccess={onRefreshSuccess}
 *     />
 *   </>)
 *
 * Falls open (lets the run proceed) if the pre-flight itself errors —
 * the legacy behavior is no worse off than before R15.
 */
'use client'

import { useState, useCallback } from 'react'
import { fetchAuthState, type AuthState } from './auth-state'

export interface UseRunGuardResult {
  needsRefresh: boolean
  authState: AuthState | null
  runOrPromptRefresh: (triggerFn: () => Promise<unknown>) => Promise<void>
  onRefreshSuccess: () => Promise<void>
  dismissRefresh: () => void
}

export function useRunGuard(projectId: string | null | undefined, environment: string | null | undefined): UseRunGuardResult {
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const [authState, setAuthState] = useState<AuthState | null>(null)
  const [pendingRun, setPendingRun] = useState<(() => Promise<unknown>) | null>(null)

  const runOrPromptRefresh = useCallback(async (triggerFn: () => Promise<unknown>) => {
    // No project/env context yet (component still loading) — skip pre-flight.
    if (!projectId || !environment) {
      await triggerFn()
      return
    }
    let state: AuthState | null = null
    try {
      state = await fetchAuthState(projectId, environment)
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('R15: auth-state pre-flight failed, allowing run:', err)
      await triggerFn()
      return
    }
    if (state && state.needs_refresh) {
      setAuthState(state)
      // Wrap in a function to avoid React's setState eager-call when fn type
      setPendingRun(() => () => triggerFn())
      setNeedsRefresh(true)
      return
    }
    setAuthState(state)
    await triggerFn()
  }, [projectId, environment])

  const onRefreshSuccess = useCallback(async () => {
    setNeedsRefresh(false)
    if (pendingRun) {
      try {
        await pendingRun()
      } finally {
        setPendingRun(null)
      }
    }
  }, [pendingRun])

  const dismissRefresh = useCallback(() => {
    setNeedsRefresh(false)
    setPendingRun(null)
  }, [])

  return { needsRefresh, authState, runOrPromptRefresh, onRefreshSuccess, dismissRefresh }
}
