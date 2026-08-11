'use client'

import { useState, useEffect, useCallback } from 'react'
import {
  fetchRuns,
  requestHeal,
  fetchHealingQueue,
} from './api-client'
import { useProject } from './project-context'

// ── Types ────────────────────────────────────────────────────────────────────

export interface RunResult {
  test_id: string
  title: string
  status: 'PASS' | 'FAIL' | 'SKIP'
  error?: string
  error_message?: string
  failure_class?: { type: string; label: string; color: string; hint: string }
  duration_ms?: number
  requirement_id?: string
  ac_id?: string
  automation_tool?: string
}

interface ExecutionHealingState {
  /** Latest run results indexed by test_id */
  resultsByTestId: Record<string, RunResult>
  /** Active healing proposals indexed by test_id */
  proposalsByTestId: Record<string, any>
  /** Latest run metadata */
  latestRun: { id: string; status: string; passed: number; failed: number; total: number } | null
  /** Loading state */
  loading: boolean
  /** Which test is currently being healed */
  healingTestId: string | null
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export function useExecutionAndHealing() {
  const { currentProjectId } = useProject()

  const [state, setState] = useState<ExecutionHealingState>({
    resultsByTestId: {},
    proposalsByTestId: {},
    latestRun: null,
    loading: true,
    healingTestId: null,
  })

  // Fetch latest run results + healing proposals
  const refresh = useCallback(async () => {
    if (!currentProjectId) return
    setState(s => ({ ...s, loading: true }))

    try {
      // 1. Get latest run
      const runsData = await fetchRuns({ project_id: currentProjectId })
      const runs = runsData?.runs || []
      const completed = runs.filter((r: any) => r.status === 'completed' || r.status === 'failed')
      const latest = completed[0] // Already sorted newest first

      let resultsByTestId: Record<string, RunResult> = {}
      let latestRun = null

      if (latest) {
        latestRun = {
          id: latest.id || latest.run_id,
          status: latest.status,
          passed: latest.passed || 0,
          failed: latest.failed || 0,
          total: latest.total || latest.total_tests || 0,
        }

        // Fetch per-test results from run detail
        try {
          const resp = await fetch(`/api/execution/runs/${latestRun.id}`)
          if (resp.ok) {
            const detail = await resp.json()
            const results: RunResult[] = detail.results || []
            for (const r of results) {
              const tid = r.test_id || r.title?.replace(/\s+/g, '-').toLowerCase() || ''
              if (tid) {
                resultsByTestId[tid] = r
              }
              // Also index by title for fuzzy matching
              if (r.title) {
                resultsByTestId[`title:${r.title}`] = r
              }
            }
          }
        } catch { /* run detail fetch failed — use aggregate data only */ }
      }

      // 2. Get healing proposals
      let proposalsByTestId: Record<string, any> = {}
      try {
        const healingData = await fetchHealingQueue(undefined, currentProjectId)
        const proposals = healingData?.proposals || []
        for (const p of proposals) {
          if (p.test_id) {
            proposalsByTestId[p.test_id] = p
          }
        }
      } catch { /* healing queue not available */ }

      setState(s => ({
        ...s,
        resultsByTestId,
        proposalsByTestId,
        latestRun,
        loading: false,
      }))
    } catch {
      setState(s => ({ ...s, loading: false }))
    }
  }, [currentProjectId])

  useEffect(() => {
    refresh()
  }, [refresh])

  // ── Lookup helpers ─────────────────────────────────────────────────────

  const getResultForTest = useCallback((testId: string): RunResult | undefined => {
    return state.resultsByTestId[testId]
  }, [state.resultsByTestId])

  const getResultForTitle = useCallback((title: string): RunResult | undefined => {
    return state.resultsByTestId[`title:${title}`]
  }, [state.resultsByTestId])

  const getProposalForTest = useCallback((testId: string): any | undefined => {
    return state.proposalsByTestId[testId]
  }, [state.proposalsByTestId])

  // ── Heal action ────────────────────────────────────────────────────────

  const handleRequestHeal = useCallback(async (
    testId: string,
    errorMessage: string = '',
    failureClass?: string,
    testTitle?: string,
  ) => {
    setState(s => ({ ...s, healingTestId: testId }))
    try {
      await requestHeal(testId, errorMessage, failureClass, currentProjectId || undefined, testTitle)
      // Refresh proposals after requesting
      await refresh()
    } catch {
      // Error handled by caller
    } finally {
      setState(s => ({ ...s, healingTestId: null }))
    }
  }, [currentProjectId, refresh])

  return {
    ...state,
    getResultForTest,
    getResultForTitle,
    getProposalForTest,
    handleRequestHeal,
    refresh,
  }
}
