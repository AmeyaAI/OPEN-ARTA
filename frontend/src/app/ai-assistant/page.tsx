'use client'

import { useState, useRef, useEffect, Suspense } from 'react'
import { useSearchParams } from 'next/navigation'
import { sendCommand, fetchRequirements, fetchTests, commitUpload } from '@/lib/api-client'
import { useProject } from '@/lib/project-context'
import { useTasks } from '@/lib/task-context'
import RefineWorkspace from '@/components/RefineWorkspace'

const COMMANDS = [
  { cmd: '/generate-tests', label: 'Generate Tests', desc: 'Generate ATDD tests from a requirement' },
  { cmd: '/analyze-risk', label: 'Analyze Risk', desc: 'Run TEA risk scoring' },
  { cmd: '/generate-edge-cases', label: 'Edge Cases', desc: 'Generate adversarial scenarios' },
  { cmd: '/check-coverage', label: 'Coverage', desc: 'Check test coverage gaps' },
  { cmd: '/explain-failure', label: 'Explain Failure', desc: 'Root cause analysis' },
  { cmd: '/check-gate', label: 'Quality Gate', desc: 'Evaluate release readiness' },
  { cmd: '/run-atdd', label: 'Run ATDD', desc: 'Full ATDD pipeline' },
  { cmd: '/heal-tests', label: 'Heal Tests', desc: 'Self-healing for broken tests' },
]

const TEA_STEPS = [
  { id: 1, name: 'Requirement Analysis', command: '/check-coverage', icon: '\uD83D\uDCCB' },
  { id: 2, name: 'Risk Scoring', command: '/analyze-risk', icon: '\u26A1' },
  { id: 3, name: 'Test Design', command: '/generate-tests', icon: '\u270F\uFE0F' },
  { id: 4, name: 'Automation Gen', command: '/generate-edge-cases', icon: '\u2699\uFE0F' },
  { id: 5, name: 'Execution', command: '/run-atdd', icon: '\u25B6\uFE0F' },
  { id: 6, name: 'Quality Gate', command: '/check-gate', icon: '\u25C6' },
]

// Map commands to workflow steps
const COMMAND_TO_STEP: Record<string, number> = {
  '/check-coverage': 1,
  '/analyze-risk': 2,
  '/generate-tests': 3,
  '/generate-edge-cases': 4,
  '/run-atdd': 5,
  '/check-gate': 6,
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  data?: Record<string, unknown>
}

// R320 — route to the test-scoped Refinement Copilot when a test_id is present
// (deep-linked from Test Explorer's "Refine with AI"); otherwise the generic
// command console. useSearchParams requires a Suspense boundary (Next 14).
export default function AIAssistantPage() {
  return (
    <Suspense fallback={null}>
      <AIAssistantRouter />
    </Suspense>
  )
}

function AIAssistantRouter() {
  const sp = useSearchParams()
  if (sp.get('test_id')) return <RefineWorkspace />
  return <AIConsole />
}

function AIConsole() {
  const [messages, setMessages] = useState<Message[]>([
    { role: 'assistant', content: 'I\'m ARTA \u2014 your AI Test Architect. Use the slash commands below or describe what you need.' },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [workflowStep, setWorkflowStep] = useState(0)
  const [importModalOpen, setImportModalOpen] = useState(false)
  const [importTab, setImportTab] = useState<'upload' | 'url' | 'paste'>('upload')
  const [importFile, setImportFile] = useState<File | null>(null)
  const [importUrl, setImportUrl] = useState('')
  const [importText, setImportText] = useState('')
  const [importLoading, setImportLoading] = useState(false)
  const [importSuccess, setImportSuccess] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const { currentProjectId, currentProject } = useProject()
  const [contextReq, setContextReq] = useState<any>(null)
  const [contextTests, setContextTests] = useState<any[]>([])
  // R320 S3 — real import result (replaces the mock "12 requirements parsed")
  const [importCount, setImportCount] = useState<number | null>(null)
  const [importError, setImportError] = useState('')

  // Restore chat state from localStorage on mount
  useEffect(() => {
    try {
      const savedMsgs = localStorage.getItem('arta_chat_messages')
      const savedStep = localStorage.getItem('arta_workflow_step')
      if (savedMsgs) setMessages(JSON.parse(savedMsgs))
      if (savedStep) setWorkflowStep(parseInt(savedStep, 10) || 0)
    } catch {}
  }, [])

  // Persist chat state to localStorage on change
  useEffect(() => {
    if (messages.length > 1) localStorage.setItem('arta_chat_messages', JSON.stringify(messages))
  }, [messages])

  useEffect(() => {
    localStorage.setItem('arta_workflow_step', String(workflowStep))
  }, [workflowStep])

  // Fetch project context when project changes
  useEffect(() => {
    if (currentProjectId) {
      fetchRequirements({ project_id: currentProjectId })
        .then(d => { if (d.requirements?.length) setContextReq(d.requirements[0]) })
        .catch(() => {})
      fetchTests({ project_id: currentProjectId })
        .then(d => setContextTests(d.tests || []))
        .catch(() => setContextTests([]))
    }
  }, [currentProjectId])

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  const { addTask, completeTask, failTask } = useTasks()

  const handleSend = async (overrideInput?: string) => {
    const userMsg = (overrideInput || input).trim()
    if (!userMsg || loading) return
    setInput('')
    setMessages(prev => [...prev, { role: 'user', content: userMsg }])
    setLoading(true)

    const cmd = userMsg.startsWith('/') ? userMsg.split(' ')[0] : '/generate-tests'
    const tid = `ai-cmd-${Date.now()}`
    addTask({ id: tid, type: 'ai_command', label: `AI: ${cmd}`, result_url: '/ai-assistant' })

    try {
      if (userMsg.startsWith('/')) {
        const parts = userMsg.split(' ')
        const cmdName = parts[0]
        const args = parts.slice(1).join(' ')

        const step = COMMAND_TO_STEP[cmdName]
        if (step && step > workflowStep) {
          setWorkflowStep(step)
        }

        const result = await sendCommand(cmdName, args, currentProjectId ?? undefined)
        completeTask(tid, { detail: (result as any).message || 'Done' })
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: (result as any).message || JSON.stringify(result, null, 2),
          data: result,
        }])
      } else {
        const result = await sendCommand('/generate-tests', userMsg, currentProjectId ?? undefined)
        if (workflowStep < 3) setWorkflowStep(3)
        completeTask(tid, { detail: (result as any).message || 'Done' })
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: (result as any).message || 'Processing complete.',
          data: result,
        }])
      }
    } catch (err: any) {
      failTask(tid, err.message || 'Command failed')
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `Error: ${err.message}`,
      }])
    } finally {
      setLoading(false)
    }
  }

  // Determine if message might contain actionable content
  function hasActionableContent(content: string): boolean {
    const keywords = ['test', 'risk', 'coverage', 'requirement', 'scenario', 'generate', 'analysis']
    const lower = content.toLowerCase()
    return keywords.some(k => lower.includes(k))
  }

  // R320 S3 — REAL requirement import (replaces the setTimeout mock). The Upload
  // tab commits the file to the project via the canonical /api/requirements/upload
  // endpoint and shows the ACTUAL parsed count. URL/Paste import isn't wired on
  // this secondary console — honestly point to the Requirements page rather than
  // fake a success.
  async function handleImport() {
    setImportLoading(true); setImportSuccess(false); setImportError(''); setImportCount(null)
    try {
      if (importTab === 'upload') {
        if (!importFile) { setImportError('Choose a file to import first.'); setImportLoading(false); return }
        const res: any = await commitUpload(importFile, currentProjectId || undefined)
        const n = Array.isArray(res) ? res.length : (res?.requirements?.length ?? res?.count ?? 0)
        setImportCount(n); setImportSuccess(true)
        if (currentProjectId) {
          fetchRequirements({ project_id: currentProjectId })
            .then(d => { if (d.requirements?.length) setContextReq(d.requirements[0]) }).catch(() => {})
        }
        setTimeout(() => {
          setImportSuccess(false); setImportModalOpen(false); setImportFile(null)
        }, 2500)
      } else {
        setImportError('URL / paste import isn’t available here — use the Requirements import on the Test Architecture page.')
      }
    } catch (e: any) {
      setImportError(e?.message || 'Import failed')
    }
    setImportLoading(false)
  }

  return (
    <div className="flex flex-col h-screen">
      {/* Header */}
      <div className="px-6 py-4 border-b flex-shrink-0" style={{ borderColor: '#1e1e3a' }}>
        <h1 className="text-xl font-bold">AI Assistant</h1>
        <p className="text-sm" style={{ color: '#64748b' }}>TEA-powered test generation, risk analysis, and debugging</p>
      </div>

      {/* Command palette */}
      <div className="px-6 py-2.5 flex gap-2 flex-wrap border-b flex-shrink-0" style={{ borderColor: '#1e1e3a', background: '#0a0a14' }}>
        {COMMANDS.map(c => (
          <button key={c.cmd}
                  onClick={() => setInput(c.cmd + ' ')}
                  className="px-2.5 py-1 rounded-md text-[11px] font-mono transition hover:ring-1"
                  style={{ background: '#12121f', border: '1px solid #1e1e3a', color: '#a5b4fc' }}
                  title={c.desc}>
            {c.cmd}
          </button>
        ))}
      </div>

      {/* 3-Column Layout */}
      <div className="flex flex-1 overflow-hidden">

        {/* Left: TEA Workflow Panel */}
        <div className="hidden md:flex flex-col flex-shrink-0 py-5 px-4" style={{ width: 220, background: '#0d0d18', borderRight: '1px solid #1e1e3a' }}>
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
              TEA Workflow
            </h3>
            {workflowStep > 0 && (
              <button
                onClick={() => {
                  setWorkflowStep(0)
                  setMessages([{ role: 'assistant', content: "I'm ARTA — your AI Test Architect. Use the slash commands above or describe a requirement to get started.\n\nWorkflow has been reset. Ready for a new ATDD cycle." }])
                  localStorage.removeItem('arta_chat_messages')
                  localStorage.removeItem('arta_workflow_step')
                }}
                className="text-[11px] px-3 py-1 rounded-md font-medium transition-all hover:opacity-90"
                style={{ color: '#fff', background: workflowStep >= 5 ? '#6366f1' : '#334155' }}
                title="Reset workflow to start a new ATDD cycle"
              >
                {workflowStep >= 5 ? 'Reset ↻' : `${workflowStep}/6 ↻`}
              </button>
            )}
          </div>
          <div className="space-y-0">
            {TEA_STEPS.map((step, idx) => {
              const isCompleted = step.id < workflowStep || (step.id === workflowStep && workflowStep > 0)
              const isActive = step.id === workflowStep + 1

              return (
                <div key={step.id} className="flex items-start gap-3">
                  {/* Step indicator + connecting line */}
                  <div className="flex flex-col items-center" style={{ width: 24 }}>
                    <button
                      onClick={() => handleSend(step.command + ' ')}
                      className="w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-bold transition flex-shrink-0 relative"
                      style={{
                        background: isCompleted ? '#10b981' : isActive ? '#6366f1' : 'transparent',
                        border: `2px solid ${isCompleted ? '#10b981' : isActive ? '#6366f1' : '#334155'}`,
                        color: isCompleted || isActive ? '#fff' : '#94a3b8',
                        cursor: 'pointer',
                      }}
                      title={`Run ${step.command}`}
                    >
                      {isCompleted ? '\u2713' : step.id}
                      {isActive && (
                        <span
                          className="absolute inset-0 rounded-full animate-ping"
                          style={{ border: '2px solid #6366f1', opacity: 0.4 }}
                        />
                      )}
                    </button>
                    {idx < TEA_STEPS.length - 1 && (
                      <div
                        className="flex-1"
                        style={{
                          width: 2,
                          minHeight: 28,
                          background: isCompleted ? '#10b981' : '#1e1e3a',
                        }}
                      />
                    )}
                  </div>

                  {/* Step label */}
                  <button
                    onClick={() => handleSend(step.command + ' ')}
                    className="pt-1 text-left"
                    style={{ cursor: 'pointer' }}
                  >
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs">{step.icon}</span>
                      <span
                        className="text-[11px] font-medium"
                        style={{
                          color: isCompleted ? '#10b981' : isActive ? '#a5b4fc' : '#94a3b8',
                        }}
                      >
                        {step.name}
                      </span>
                    </div>
                    <span className="text-[9px] font-mono" style={{ color: '#334155' }}>
                      {step.command}
                    </span>
                  </button>
                </div>
              )
            })}
          </div>
        </div>

        {/* Center: Chat Interface */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
            {messages.map((msg, i) => (
              <div key={i}>
                <div className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <div className="max-w-2xl rounded-xl px-4 py-3 text-sm"
                       style={{
                         background: msg.role === 'user' ? '#6366f1' : '#12121f',
                         border: msg.role === 'assistant' ? '1px solid #1e1e3a' : 'none',
                         color: msg.role === 'user' ? '#fff' : '#e2e8f0',
                       }}>
                    <p className="whitespace-pre-wrap">{msg.content}</p>
                    {msg.data && (
                      <details className="mt-2">
                        <summary className="text-[10px] cursor-pointer" style={{ color: '#64748b' }}>
                          View raw response
                        </summary>
                        <pre className="mt-1 text-[10px] p-2 rounded overflow-x-auto"
                             style={{ background: '#0a0a14', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}>
                          {JSON.stringify(msg.data, null, 2)}
                        </pre>
                      </details>
                    )}
                  </div>
                </div>
                {/* Action Buttons for assistant messages */}
                {msg.role === 'assistant' && i > 0 && hasActionableContent(msg.content) && (
                  <div className="flex gap-2 mt-2 ml-0">
                    <button
                      onClick={() => handleSend('/generate-tests ')}
                      className="px-3 py-1.5 rounded-lg text-[11px] font-medium transition"
                      style={{ background: 'transparent', border: '1px solid #1e1e3a', color: '#6366f1' }}
                      onMouseEnter={e => { e.currentTarget.style.background = '#6366f1'; e.currentTarget.style.color = '#fff' }}
                      onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = '#6366f1' }}
                    >
                      Generate Tests
                    </button>
                    <button
                      onClick={() => handleSend('/analyze-risk ')}
                      className="px-3 py-1.5 rounded-lg text-[11px] font-medium transition"
                      style={{ background: 'transparent', border: '1px solid #1e1e3a', color: '#6366f1' }}
                      onMouseEnter={e => { e.currentTarget.style.background = '#6366f1'; e.currentTarget.style.color = '#fff' }}
                      onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = '#6366f1' }}
                    >
                      Analyze Risk
                    </button>
                    <button
                      onClick={() => handleSend('/check-coverage ')}
                      className="px-3 py-1.5 rounded-lg text-[11px] font-medium transition"
                      style={{ background: 'transparent', border: '1px solid #1e1e3a', color: '#6366f1' }}
                      onMouseEnter={e => { e.currentTarget.style.background = '#6366f1'; e.currentTarget.style.color = '#fff' }}
                      onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = '#6366f1' }}
                    >
                      Check Coverage
                    </button>
                  </div>
                )}
              </div>
            ))}
            {loading && (
              <div className="flex justify-start">
                <div className="rounded-xl px-4 py-3 text-sm" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
                  <span className="animate-pulse" style={{ color: '#6366f1' }}>Thinking...</span>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* Input */}
          <div className="px-6 py-3 border-t flex-shrink-0" style={{ borderColor: '#1e1e3a', background: '#0a0a14' }}>
            <div className="flex gap-3">
              <input
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleSend()}
                placeholder="Type a command or describe a requirement..."
                className="flex-1 px-4 py-2.5 rounded-lg text-sm outline-none"
                style={{ background: '#12121f', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
              />
              <button onClick={() => handleSend()} disabled={loading}
                      className="px-5 py-2.5 rounded-lg text-sm font-medium"
                      style={{ background: '#6366f1', color: '#fff' }}>
                Send
              </button>
            </div>
          </div>
        </div>

        {/* Right: Context Panel */}
        <div className="hidden md:flex flex-col flex-shrink-0 py-5 px-4 gap-3 overflow-y-auto" style={{ width: 260, background: '#0d0d18', borderLeft: '1px solid #1e1e3a' }}>
          <h3 className="text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
            Context
          </h3>

          {/* Active Requirement Card */}
          <div className="rounded-lg p-3" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <span className="text-[9px] font-semibold uppercase tracking-wider" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
              Active Requirement
            </span>
            <p className="text-sm font-medium mt-1" style={{ color: '#e2e8f0' }}>
              {contextReq?.req_id || 'REQ-017'}
            </p>
            <p className="text-[11px]" style={{ color: '#94a3b8' }}>
              {contextReq?.title || 'Checkout Payment'}
            </p>
            <span className="inline-block mt-1.5 text-[9px] px-2 py-0.5 rounded-full" style={{ background: '#6366f118', color: '#a5b4fc', border: '1px solid #6366f144' }}>
              {currentProject?.name || 'Document'}
            </span>
          </div>

          {/* Risk Score Card */}
          <div className="rounded-lg p-3" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <span className="text-[9px] font-semibold uppercase tracking-wider" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
              Risk Score
            </span>
            <div className="flex items-center gap-2 mt-1">
              <span className="text-lg font-bold" style={{ color: (contextReq?.risk_score ?? 9.4) >= 8 ? '#fb7185' : '#f59e0b' }}>
                {contextReq?.risk_score ?? '9.4'}
              </span>
              <span className="text-[10px]" style={{ color: '#64748b' }}>/ 10</span>
              <span className="text-[9px] px-1.5 py-0.5 rounded-full font-bold ml-auto" style={{ background: '#ef444422', color: '#ef4444' }}>
                {contextReq?.priority || 'P0'}
              </span>
            </div>
            <div className="mt-2 h-1.5 rounded-full overflow-hidden" style={{ background: '#1e1e3a' }}>
              <div className="h-full rounded-full" style={{ width: `${((contextReq?.risk_score ?? 9.4) * 10)}%`, background: 'linear-gradient(90deg, #ef4444, #f59e0b)' }} />
            </div>
          </div>

          {/* Generated Tests Card */}
          <div className="rounded-lg p-3" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <span className="text-[9px] font-semibold uppercase tracking-wider" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
              Generated
            </span>
            <p className="text-sm font-medium mt-1" style={{ color: '#e2e8f0' }}>
              {contextTests.length > 0 ? contextTests.length : 8} scenarios
            </p>
            <p className="text-[11px]" style={{ color: '#94a3b8' }}>
              {contextTests.length > 0
                ? `${new Set(contextTests.map(t => t.test_type)).size} types`
                : '4 types'}
            </p>
          </div>

          {/* Coverage Card */}
          <div className="rounded-lg p-3" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            <span className="text-[9px] font-semibold uppercase tracking-wider" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
              Coverage
            </span>
            <p className="text-lg font-bold mt-1" style={{ color: '#10b981' }}>
              {Math.min(100, contextReq?.coverage_pct ?? (contextTests.length > 0 ? Math.round(contextTests.length / Math.max(contextTests.length, contextReq?.acceptance_criteria?.length || 1) * 100) : 0))}%
            </p>
            <div className="mt-1.5 h-1.5 rounded-full overflow-hidden" style={{ background: '#1e1e3a' }}>
              <div className="h-full rounded-full" style={{ width: `${Math.min(100, contextReq?.coverage_pct ?? (contextTests.length > 0 ? Math.round(contextTests.length / Math.max(contextTests.length, contextReq?.acceptance_criteria?.length || 1) * 100) : 0))}%`, background: '#10b981' }} />
            </div>
          </div>

          {/* Import Requirements Button */}
          <button
            onClick={() => { setImportModalOpen(true); setImportTab('upload'); setImportSuccess(false) }}
            className="w-full py-2.5 rounded-lg text-xs font-medium transition mt-auto"
            style={{ background: '#6366f1', color: '#fff' }}
          >
            Import Requirements
          </button>
        </div>
      </div>

      {/* Import Requirements Modal */}
      {importModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: 'rgba(0,0,0,0.7)' }}>
          <div className="w-full max-w-lg rounded-xl p-6" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
            {/* Modal Header */}
            <div className="flex items-center justify-between mb-5">
              <h2 className="text-base font-semibold">Import Requirements</h2>
              <button
                onClick={() => setImportModalOpen(false)}
                className="w-7 h-7 flex items-center justify-center rounded-lg"
                style={{ background: '#1e1e3a', color: '#94a3b8' }}
              >
                {'\u2715'}
              </button>
            </div>

            {/* Tabs */}
            <div className="flex gap-0 mb-5 rounded-lg overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
              {(['upload', 'url', 'paste'] as const).map(tab => (
                <button
                  key={tab}
                  onClick={() => setImportTab(tab)}
                  className="flex-1 px-3 py-2 text-xs font-medium transition"
                  style={{
                    background: importTab === tab ? '#6366f1' : 'transparent',
                    color: importTab === tab ? '#fff' : '#64748b',
                  }}
                >
                  {tab === 'upload' ? 'Upload' : tab === 'url' ? 'URL' : 'Paste'}
                </button>
              ))}
            </div>

            {/* Upload Tab */}
            {importTab === 'upload' && (
              <div>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".docx,.xlsx,.pdf,.md,.txt,.json,.yaml,.yml"
                  className="hidden"
                  onChange={e => {
                    const f = e.target.files?.[0]
                    if (f) setImportFile(f)
                  }}
                />
                <div
                  onClick={() => fileInputRef.current?.click()}
                  onDragOver={e => e.preventDefault()}
                  onDrop={e => {
                    e.preventDefault()
                    const f = e.dataTransfer.files?.[0]
                    if (f) setImportFile(f)
                  }}
                  className="rounded-lg p-8 text-center cursor-pointer transition"
                  style={{
                    border: '2px dashed #1e1e3a',
                    background: '#0a0a14',
                  }}
                >
                  {importFile ? (
                    <div>
                      <p className="text-sm font-medium" style={{ color: '#a5b4fc' }}>{importFile.name}</p>
                      <p className="text-[11px] mt-1" style={{ color: '#64748b' }}>
                        {(importFile.size / 1024).toFixed(1)} KB
                      </p>
                    </div>
                  ) : (
                    <div>
                      <p className="text-sm" style={{ color: '#64748b' }}>
                        Drop files here or click to browse
                      </p>
                      <p className="text-[10px] mt-2" style={{ color: '#94a3b8' }}>
                        .docx, .xlsx, .pdf, .md, .txt, .json, .yaml
                      </p>
                    </div>
                  )}
                </div>
                <button
                  onClick={handleImport}
                  disabled={!importFile || importLoading}
                  className="w-full mt-4 py-2.5 rounded-lg text-xs font-medium transition disabled:opacity-50"
                  style={{ background: importSuccess ? '#10b981' : '#6366f1', color: '#fff' }}
                >
                  {importLoading ? 'Importing...' : importSuccess ? `${importCount ?? 0} requirements imported` : 'Import'}
                </button>
              </div>
            )}

            {/* URL Tab */}
            {importTab === 'url' && (
              <div>
                <input
                  value={importUrl}
                  onChange={e => setImportUrl(e.target.value)}
                  placeholder="https://jira.example.com/browse/PROJ-123 or Confluence URL"
                  className="w-full px-4 py-2.5 rounded-lg text-sm outline-none mb-4"
                  style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                />
                <button
                  onClick={handleImport}
                  disabled={!importUrl.trim() || importLoading}
                  className="w-full py-2.5 rounded-lg text-xs font-medium transition disabled:opacity-50"
                  style={{ background: importSuccess ? '#10b981' : '#6366f1', color: '#fff' }}
                >
                  {importLoading ? 'Importing...' : importSuccess ? `${importCount ?? 0} requirements imported` : 'Import'}
                </button>
              </div>
            )}

            {/* Paste Tab */}
            {importTab === 'paste' && (
              <div>
                <textarea
                  value={importText}
                  onChange={e => setImportText(e.target.value)}
                  placeholder="Paste requirement text here..."
                  className="w-full px-4 py-3 rounded-lg text-sm outline-none min-h-[160px] resize-y mb-4"
                  style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                />
                <button
                  onClick={handleImport}
                  disabled={!importText.trim() || importLoading}
                  className="w-full py-2.5 rounded-lg text-xs font-medium transition disabled:opacity-50"
                  style={{ background: importSuccess ? '#10b981' : '#6366f1', color: '#fff' }}
                >
                  {importLoading ? 'Importing...' : importSuccess ? `${importCount ?? 0} requirements imported` : 'Import'}
                </button>
              </div>
            )}
            {importError && (
              <p className="text-[11px] mt-3" style={{ color: '#fb7185' }}>{importError}</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
