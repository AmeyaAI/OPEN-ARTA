'use client'

import { useEffect, useState } from 'react'
import { fetchProjects, inviteUser, type InviteResponse, type Project } from '@/lib/api-client'
import { useToast } from '@/components/ui/Toast'

const PROJECT_ROLES = [
  { value: '', label: 'No project assignment' },
  { value: 'admin', label: 'Admin' },
  { value: 'test_architect', label: 'Test Architect' },
  { value: 'qa_lead', label: 'QA Lead' },
  { value: 'tester', label: 'Tester' },
  { value: 'developer', label: 'Developer' },
  { value: 'viewer', label: 'Viewer' },
]

interface InviteUserModalProps {
  open: boolean
  onClose: () => void
  onInvited?: (response: InviteResponse) => void
}

export default function InviteUserModal({ open, onClose, onInvited }: InviteUserModalProps) {
  const toast = useToast()
  const [email, setEmail] = useState('')
  const [fullName, setFullName] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [projectId, setProjectId] = useState('')
  const [projectRole, setProjectRole] = useState('')
  const [projects, setProjects] = useState<Project[]>([])
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<InviteResponse | null>(null)
  const [copiedField, setCopiedField] = useState<'link' | 'email' | null>(null)

  // Escape closes the modal — keyboard parity with the backdrop click.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') handleClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // Load projects once when the modal opens — used by the optional project dropdown.
  useEffect(() => {
    if (!open) return
    fetchProjects()
      .then(res => setProjects(res.projects || []))
      .catch(() => setProjects([]))
  }, [open])

  function handleClose() {
    // Resetting state on close so the next open is a clean form.
    setEmail('')
    setFullName('')
    setIsAdmin(false)
    setProjectId('')
    setProjectRole('')
    setResult(null)
    setCopiedField(null)
    onClose()
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!email.trim() || !fullName.trim()) {
      toast.error('Email and full name are required')
      return
    }
    if ((projectId && !projectRole) || (projectRole && !projectId)) {
      toast.error('Pick both a project and a role, or neither')
      return
    }
    setLoading(true)
    try {
      const res = await inviteUser({
        email: email.trim(),
        full_name: fullName.trim(),
        is_admin: isAdmin,
        project_id: projectId || null,
        project_role: projectRole || null,
      })
      setResult(res)
      toast.success(res.email_sent ? 'Invite emailed!' : 'Invite created. Copy the link')
      onInvited?.(res)
    } catch (err: any) {
      toast.error(`Invite failed: ${err.message || 'unknown error'}`)
    } finally {
      setLoading(false)
    }
  }

  async function copyToClipboard(text: string, field: 'link' | 'email') {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedField(field)
      setTimeout(() => setCopiedField((prev: typeof field | null) => (prev === field ? null : prev)), 2000)
    } catch {
      toast.error('Could not copy. Please select the text manually')
    }
  }

  function buildMailtoHref(): string {
    if (!result) return '#'
    const subject = encodeURIComponent(result.email_subject)
    const body = encodeURIComponent(result.email_body)
    return `mailto:${encodeURIComponent(result.email)}?subject=${subject}&body=${body}`
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center"
         role="dialog" aria-modal="true" aria-labelledby="invite-user-modal-title">
      <button
        type="button"
        aria-label="Close dialog"
        onClick={handleClose}
        className="absolute inset-0 bg-black/60 backdrop-blur-sm cursor-default"
      />

      <div
        className="relative w-full max-w-lg mx-4 rounded-2xl p-6 shadow-2xl"
        style={{ background: '#12121f', border: '1px solid #1e1e3a', backdropFilter: 'blur(20px)' }}
      >
        <div className="flex items-center justify-between mb-6">
          <h2 id="invite-user-modal-title" className="text-lg font-bold"
              style={{ color: '#e2e8f0', fontFamily: 'DM Sans, sans-serif' }}>
            {result ? 'Invite created' : 'Invite User'}
          </h2>
          <button
            onClick={handleClose}
            className="w-8 h-8 rounded-lg flex items-center justify-center text-sm hover:opacity-80 transition-opacity"
            style={{ background: '#1e1e3a', color: '#94a3b8' }}
          >
            &#x2715;
          </button>
        </div>

        {!result ? (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs uppercase tracking-wider mb-1" style={{ color: '#94a3b8' }}>
                Email
              </label>
              <input
                type="email"
                required
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="alice@example.com"
                className="w-full px-3 py-2 rounded-lg text-sm"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
            </div>

            <div>
              <label className="block text-xs uppercase tracking-wider mb-1" style={{ color: '#94a3b8' }}>
                Full Name
              </label>
              <input
                type="text"
                required
                value={fullName}
                onChange={e => setFullName(e.target.value)}
                placeholder="Alice Smith"
                className="w-full px-3 py-2 rounded-lg text-sm"
                style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
            </div>

            <label className="flex items-center gap-2 cursor-pointer text-sm" style={{ color: '#cbd5e1' }}>
              <input
                type="checkbox"
                checked={isAdmin}
                onChange={e => setIsAdmin(e.target.checked)}
                className="cursor-pointer"
              />
              Platform admin (can invite other users)
            </label>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs uppercase tracking-wider mb-1" style={{ color: '#94a3b8' }}>
                  Project (optional)
                </label>
                <select
                  value={projectId}
                  onChange={e => setProjectId(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg text-sm"
                  style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                >
                  <option value="">No project</option>
                  {projects.map(p => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs uppercase tracking-wider mb-1" style={{ color: '#94a3b8' }}>
                  Project role
                </label>
                <select
                  value={projectRole}
                  onChange={e => setProjectRole(e.target.value)}
                  disabled={!projectId}
                  className="w-full px-3 py-2 rounded-lg text-sm"
                  style={{
                    background: '#0a0a14',
                    border: '1px solid #1e1e3a',
                    color: projectId ? '#e2e8f0' : '#475569',
                  }}
                >
                  {PROJECT_ROLES.map(r => (
                    <option key={r.value} value={r.value}>{r.label}</option>
                  ))}
                </select>
              </div>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={handleClose}
                disabled={loading}
                className="px-4 py-2 rounded-lg text-sm"
                style={{ background: '#1e1e3a', color: '#94a3b8' }}
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={loading}
                className="px-4 py-2 rounded-lg text-sm font-medium"
                style={{ background: '#6366f1', color: '#fff', opacity: loading ? 0.6 : 1 }}
              >
                {loading ? 'Sending…' : 'Send Invite'}
              </button>
            </div>
          </form>
        ) : (
          <div className="space-y-4">
            <div className="text-sm" style={{ color: '#cbd5e1' }}>
              {result.email_sent ? (
                <>An invite email was sent to <strong>{result.email}</strong>. They have <strong>72 hours</strong> to accept.</>
              ) : (
                <>SMTP is not configured. Copy this link and share it with <strong>{result.email}</strong> directly. Expires in <strong>72 hours</strong>.</>
              )}
            </div>

            <div>
              <label className="block text-xs uppercase tracking-wider mb-1" style={{ color: '#94a3b8' }}>
                Invite link
              </label>
              <div className="flex gap-2">
                <input
                  type="text"
                  readOnly
                  value={result.invite_url}
                  onFocus={e => e.target.select()}
                  className="flex-1 px-3 py-2 rounded-lg text-xs font-mono"
                  style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                />
                <button
                  type="button"
                  onClick={() => copyToClipboard(result.invite_url, 'link')}
                  className="px-3 py-2 rounded-lg text-xs font-medium"
                  style={{ background: copiedField === 'link' ? '#10b981' : '#6366f1', color: '#fff' }}
                >
                  {copiedField === 'link' ? 'Copied!' : 'Copy'}
                </button>
              </div>
            </div>

            {!result.email_sent && (
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="block text-xs uppercase tracking-wider" style={{ color: '#94a3b8' }}>
                    Email body
                  </label>
                  <a
                    href={buildMailtoHref()}
                    className="text-[10px] uppercase tracking-wider px-2 py-1 rounded"
                    style={{
                      background: 'rgba(99,102,241,0.15)',
                      color: '#a5b4fc',
                      border: '1px solid rgba(99,102,241,0.35)',
                    }}
                  >
                    Open in mail app
                  </a>
                </div>
                <div className="text-[11px] mb-1" style={{ color: '#64748b' }}>
                  Subject: <span style={{ color: '#cbd5e1' }}>{result.email_subject}</span>
                </div>
                <div className="flex gap-2">
                  <textarea
                    readOnly
                    value={result.email_body}
                    onFocus={e => e.currentTarget.select()}
                    rows={6}
                    className="flex-1 px-3 py-2 rounded-lg text-xs font-mono resize-y"
                    style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                  />
                  <button
                    type="button"
                    onClick={() => copyToClipboard(`Subject: ${result.email_subject}\n\n${result.email_body}`, 'email')}
                    className="self-start px-3 py-2 rounded-lg text-xs font-medium"
                    style={{ background: copiedField === 'email' ? '#10b981' : '#6366f1', color: '#fff' }}
                  >
                    {copiedField === 'email' ? 'Copied!' : 'Copy email'}
                  </button>
                </div>
              </div>
            )}

            {result.email_sent && (
              <div className="text-[10px] font-bold inline-block px-2.5 py-1 rounded"
                   style={{ background: 'rgba(16,185,129,0.12)', color: '#10b981' }}>
                EMAIL SENT
              </div>
            )}

            <div className="flex justify-end pt-2">
              <button
                type="button"
                onClick={handleClose}
                className="px-4 py-2 rounded-lg text-sm font-medium"
                style={{ background: '#6366f1', color: '#fff' }}
              >
                Done
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
