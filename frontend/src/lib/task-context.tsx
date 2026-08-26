'use client'

import { createContext, useContext, useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { useNotifications } from './notification-context'

// ── Task Types ────────────────────────────────────────────────────────────────

export type TaskType =
  | 'test_execution'
  | 'test_generation'
  | 'ai_command'
  | 'requirement_discovery'
  | 'jira_sync'
  | 'document_parse'
  | 'integration_call'
  | 'healing_apply'
  | 'integration_test'
  | 'config_save'
  | 'data_refresh'

export type TaskStatus = 'running' | 'completed' | 'failed'

export interface Task {
  id: string
  type: TaskType
  label: string
  status: TaskStatus
  progress?: number        // 0-100
  detail?: string          // "12/24 tests passed"
  started_at: string
  finished_at?: string
  result_url?: string      // navigate on click
}

// Exported so consumers (and the type system) keep the icon→type map alive.
export const TASK_ICONS: Record<TaskType, string> = {
  test_execution: '▶',
  test_generation: '⚙',
  ai_command: '◆',
  requirement_discovery: '⬡',
  jira_sync: '↻',
  document_parse: '⬡',
  integration_call: '↗',
  healing_apply: '⚕',
  integration_test: '⚡',
  config_save: '✓',
  data_refresh: '↻',
}

// ── Context Interface ─────────────────────────────────────────────────────────

interface TaskState {
  tasks: Task[]
  activeTasks: Task[]
  recentTasks: Task[]
  addTask: (task: Omit<Task, 'started_at' | 'status'>) => string
  updateTask: (id: string, updates: Partial<Pick<Task, 'progress' | 'detail' | 'status'>>) => void
  completeTask: (id: string, opts?: { detail?: string; result_url?: string }) => void
  failTask: (id: string, detail?: string) => void
}

const TaskContext = createContext<TaskState>({
  tasks: [],
  activeTasks: [],
  recentTasks: [],
  addTask: () => '',
  updateTask: () => {},
  completeTask: () => {},
  failTask: () => {},
})

const STORAGE_KEY = 'arta_tasks'
const MAX_RECENT = 20

// ── Provider ──────────────────────────────────────────────────────────────────

export function TaskProvider({ children }: { children: ReactNode }) {
  const [tasks, setTasks] = useState<Task[]>([])
  const { addNotification } = useNotifications()
  const notifyRef = useRef(addNotification)
  notifyRef.current = addNotification

  // Load from localStorage on mount
  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY)
      if (stored) {
        const parsed: Task[] = JSON.parse(stored)
        // Mark any "running" tasks as failed (stale from previous session)
        setTasks(parsed.map(t => t.status === 'running' ? { ...t, status: 'failed' as const, detail: 'Interrupted (app restarted)' } : t))
      }
    } catch {}
  }, [])

  // Persist on change
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks.slice(0, MAX_RECENT)))
    } catch {}
  }, [tasks])

  const activeTasks = tasks.filter(t => t.status === 'running')
  const recentTasks = tasks.filter(t => t.status !== 'running').slice(0, 10)

  const addTask = useCallback((task: Omit<Task, 'started_at' | 'status'>): string => {
    const newTask: Task = {
      ...task,
      status: 'running',
      started_at: new Date().toISOString(),
    }
    setTasks(prev => [newTask, ...prev])
    return task.id
  }, [])

  const updateTask = useCallback((id: string, updates: Partial<Pick<Task, 'progress' | 'detail' | 'status'>>) => {
    setTasks(prev => prev.map(t => t.id === id ? { ...t, ...updates } : t))
  }, [])

  const completeTask = useCallback((id: string, opts?: { detail?: string; result_url?: string }) => {
    setTasks(prev => prev.map(t => {
      if (t.id !== id) return t
      const updated = {
        ...t,
        status: 'completed' as const,
        progress: 100,
        finished_at: new Date().toISOString(),
        ...(opts?.detail ? { detail: opts.detail } : {}),
        ...(opts?.result_url ? { result_url: opts.result_url } : {}),
      }
      // Fire notification
      notifyRef.current({
        icon: '✓',
        message: `${updated.label}: ${opts?.detail || 'completed'}`,
        timestamp: updated.finished_at!,
      })
      return updated
    }))
  }, [])

  const failTask = useCallback((id: string, detail?: string) => {
    setTasks(prev => prev.map(t => {
      if (t.id !== id) return t
      const updated = {
        ...t,
        status: 'failed' as const,
        finished_at: new Date().toISOString(),
        ...(detail ? { detail } : {}),
      }
      notifyRef.current({
        icon: '✗',
        message: `${updated.label}: ${detail || 'failed'}`,
        timestamp: updated.finished_at!,
      })
      return updated
    }))
  }, [])

  return (
    <TaskContext.Provider value={{ tasks, activeTasks, recentTasks, addTask, updateTask, completeTask, failTask }}>
      {children}
    </TaskContext.Provider>
  )
}

export function useTasks() {
  return useContext(TaskContext)
}

// ── Helper: wrap an async operation with task tracking ────────────────────────

export function useTrackedOperation() {
  const { addTask, updateTask, completeTask, failTask } = useTasks()

  return useCallback(async <T,>(opts: {
    id?: string
    type: TaskType
    label: string
    result_url?: string
    execute: (onProgress: (progress: number, detail?: string) => void) => Promise<T>
  }): Promise<T> => {
    const taskId = opts.id || `task-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    addTask({ id: taskId, type: opts.type, label: opts.label, result_url: opts.result_url })

    const onProgress = (progress: number, detail?: string) => {
      updateTask(taskId, { progress, ...(detail ? { detail } : {}) })
    }

    try {
      const result = await opts.execute(onProgress)
      completeTask(taskId, { detail: 'Success', result_url: opts.result_url })
      return result
    } catch (err) {
      failTask(taskId, err instanceof Error ? err.message : 'Unknown error')
      throw err
    }
  }, [addTask, updateTask, completeTask, failTask])
}
