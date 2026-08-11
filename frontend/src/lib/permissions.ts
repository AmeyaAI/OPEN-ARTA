'use client'

import { useAuth } from './auth-context'
import { useProject } from './project-context'

// Mirrors the backend hierarchy (src/api/dependencies.py _ROLE_ORDER).
const ROLE_ORDER: Record<string, number> = { viewer: 0, tester: 1, qa_lead: 2, admin: 3 }

export interface Permissions {
  role: string | undefined      // the user's role on the current project (undefined = none)
  isAdmin: boolean
  canRead: boolean              // any role, or admin
  canWrite: boolean             // tester+ (create/edit/run/generate) — the backend gate
  canManage: boolean            // qa_lead+ (settings / integrations / delete)
}

/**
 * The current user's permissions on a project. Mirrors the backend RBAC so the UI can
 * hide/disable write actions a viewer would only get a 403 on. The backend remains the
 * real gate — this is purely UX. Platform admins can do everything.
 *
 * @param projectId  Project to evaluate against. Defaults to the ACTIVE project; pass an
 *                   explicit id for components that operate on a specific project (e.g. a
 *                   modal receiving `projectId` as a prop).
 */
export function usePermissions(projectId?: string | null): Permissions {
  const { user, roles } = useAuth()
  const { currentProjectId } = useProject()
  const pid = projectId ?? currentProjectId
  const isAdmin = !!user?.is_admin
  const role = pid ? roles[pid] : undefined
  const level = role ? (ROLE_ORDER[role] ?? -1) : -1
  return {
    role,
    isAdmin,
    canRead: isAdmin || level >= 0,
    canWrite: isAdmin || level >= ROLE_ORDER.tester,
    canManage: isAdmin || level >= ROLE_ORDER.qa_lead,
  }
}
