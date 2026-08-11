'use client'

import { usePermissions } from '@/lib/permissions'

/**
 * Shown when the current user has a read-only (viewer) role on the active project, so they
 * understand up front why create/edit/run actions are unavailable — instead of hitting 403s.
 * Hidden for admins, writers, and when there's no role on the current project.
 */
export default function ReadOnlyBanner() {
  const { canWrite, role, isAdmin } = usePermissions()
  if (isAdmin || canWrite || !role) return null
  return (
    <div
      role="status"
      className="px-4 py-2 text-sm flex items-center gap-2"
      style={{ background: '#241a09', borderBottom: '1px solid #5a4410', color: '#f0b429' }}
    >
      <span aria-hidden>🔒</span>
      <span>
        You have <b>{role}</b> access to this project — it&apos;s read-only. Ask a project
        admin for <b>tester</b> access to create, edit, run, or generate.
      </span>
    </div>
  )
}
