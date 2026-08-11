'use client'

import { useEffect, useRef, useState } from 'react'
import { streamRunResults, type StreamEvent } from '@/lib/api-client'

const STATUS_COLORS: Record<string, { bg: string; text: string }> = {
  PASS: { bg: 'rgba(52,211,153,0.15)', text: '#34d399' },
  FAIL: { bg: 'rgba(251,113,133,0.15)', text: '#fb7185' },
  SKIP: { bg: 'rgba(251,191,36,0.15)', text: '#fbbf24' },
}

const TEA_STAGES = [
  { id: 1, label: 'Requirements', short: 'REQ' },
  { id: 2, label: 'Risk Scoring', short: 'RSK' },
  { id: 3, label: 'ATDD Design', short: 'ATD' },
  { id: 4, label: 'Automation', short: 'AUT' },
  { id: 5, label: 'Execution', short: 'EXE' },
  { id: 6, label: 'Evidence', short: 'EVD' },
  { id: 7, label: 'Traceability', short: 'TRC' },
  { id: 8, label: 'NFR', short: 'NFR' },
  { id: 9, label: 'Gate', short: 'GTD' },
]

export default function LiveExecutionFeed({
  runId,
  onClose,
  onComplete,
}: {
  runId: string
  onClose: () => void
  onComplete?: () => void
}) {
  const [events, setEvents] = useState<StreamEvent[]>([])
  const [isConnected, setIsConnected] = useState(true)
  const [total, setTotal] = useState(0)
  const [elapsed, setElapsed] = useState(0)
  const [currentStage, setCurrentStage] = useState(1) // Start from Requirements
  const scrollRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<ReturnType<typeof setInterval>>()
  const stageTimerRef = useRef<ReturnType<typeof setInterval>>()

  // Animate pre-execution stages (1-4) in the first seconds
  useEffect(() => {
    let stage = 1
    stageTimerRef.current = setInterval(() => {
      stage++
      if (stage <= 4) {
        setCurrentStage(stage)
      } else {
        setCurrentStage(5) // Arrive at Execution
        if (stageTimerRef.current) clearInterval(stageTimerRef.current)
      }
    }, 600)
    return () => { if (stageTimerRef.current) clearInterval(stageTimerRef.current) }
  }, [runId])

  useEffect(() => {
    setIsConnected(true)
    timerRef.current = setInterval(() => setElapsed(s => s + 1), 1000)
    const cleanup = streamRunResults(
      runId,
      (event) => {
        if (event.type === 'run_started' && event.total) {
          setTotal(event.total)
        } else if (event.type === 'run_completed') {
          setEvents((prev) => [...prev, event])
          setIsConnected(false)
          setCurrentStage(9) // Move to gate
          if (timerRef.current) clearInterval(timerRef.current)
          onComplete?.()
        } else {
          setEvents((prev) => [...prev, event])
        }
      },
      () => {
        setIsConnected(false)
        if (timerRef.current) clearInterval(timerRef.current)
      },
    )
    return () => {
      cleanup()
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [runId])

  // Advance stages 6-8 based on test completion percentage
  useEffect(() => {
    if (!isConnected) return // Already completed
    const testCount = events.filter(e => e.type === 'test_result').length
    if (total <= 0) return
    const pct = (testCount / total) * 100
    if (pct >= 90 && currentStage < 8) setCurrentStage(8) // NFR
    else if (pct >= 70 && currentStage < 7) setCurrentStage(7) // Traceability
    else if (pct >= 40 && currentStage < 6) setCurrentStage(6) // Evidence
  }, [events, total, isConnected, currentStage])

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [events])

  const testEvents = events.filter((e) => e.type === 'test_result')
  const completed = events.find((e) => e.type === 'run_completed')
  const progress = total > 0 ? Math.round((testEvents.length / total) * 100) : 0
  const fmtElapsed = `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, '0')}`

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: '#12121f', border: '1px solid #1e1e3a' }}>
      {/* Header */}
      <div
        className="px-4 py-3 flex items-center justify-between"
        style={{ background: '#0d0d1a', borderBottom: '1px solid #1e1e3a' }}
      >
        <div className="flex items-center gap-3">
          {isConnected && (
            <span className="w-2 h-2 rounded-full animate-pulse" style={{ background: '#6366f1' }} />
          )}
          <span className="text-xs font-semibold" style={{ color: isConnected ? '#a5b4fc' : '#64748b' }}>
            {isConnected ? 'Live' : 'Complete'} — {runId}
          </span>
          <span className="text-xs font-mono" style={{ color: '#94a3b8' }}>{fmtElapsed}</span>
        </div>
        <button
          onClick={onClose}
          className="text-xs px-2 py-1 rounded hover:opacity-80 transition"
          style={{ color: '#64748b', background: '#1e1e3a' }}
        >
          Close
        </button>
      </div>

      {/* TEA Pipeline Stage Progress */}
      <div className="px-4 py-2 flex items-center gap-1" style={{ background: '#0a0a14' }}>
        {TEA_STAGES.map((stage, _i) => {
          const isComplete = stage.id < currentStage
          const isCurrent = stage.id === currentStage
          return (
            <div key={stage.id} className="flex items-center gap-1 flex-1">
              <div className="flex flex-col items-center flex-1">
                <div className="w-full h-1 rounded-full mb-1" style={{
                  background: isComplete ? '#6366f1' : isCurrent ? 'linear-gradient(90deg, #6366f1, #1e1e3a)' : '#1e1e3a',
                  transition: 'background 0.4s ease',
                }} />
                <span className="text-[8px] font-mono" style={{
                  color: isComplete ? '#818cf8' : isCurrent ? '#a5b4fc' : '#94a3b8',
                  fontWeight: isCurrent ? 700 : 400,
                }}>
                  {stage.short}
                </span>
              </div>
            </div>
          )
        })}
      </div>

      {/* Progress bar */}
      <div className="h-1" style={{ background: '#0d0d1a' }}>
        <div
          className="h-full transition-all duration-300"
          style={{ width: `${progress}%`, background: 'linear-gradient(90deg, #6366f1, #8b5cf6)' }}
        />
      </div>

      {/* Test progress counter */}
      <div className="px-4 py-1.5 flex items-center justify-between text-[10px]"
           style={{ background: '#0a0a14', borderBottom: '1px solid #1e1e3a', color: '#94a3b8' }}>
        <span>{testEvents.length} / {total || '?'} tests completed</span>
        <span>{progress}%</span>
      </div>

      {/* Event list */}
      <div ref={scrollRef} className="p-3 space-y-1 overflow-y-auto" style={{ maxHeight: 320 }}>
        {testEvents.map((e, i) => {
          const colors = STATUS_COLORS[e.status || ''] || STATUS_COLORS.SKIP
          return (
            <div
              key={i}
              className="flex items-center gap-3 px-3 py-2 rounded-lg text-xs"
              style={{ background: '#0a0a14' }}
            >
              <span
                className="font-bold w-8 flex-shrink-0 text-center px-1 py-0.5 rounded"
                style={{ background: colors.bg, color: colors.text, fontFamily: 'monospace' }}
              >
                {e.status}
              </span>
              <span className="flex-1 truncate" style={{ color: '#94a3b8' }}>
                {e.title}
              </span>
              <span style={{ color: '#94a3b8', fontFamily: 'monospace', flexShrink: 0 }}>
                {e.duration_ms
                  ? e.duration_ms > 999
                    ? `${(e.duration_ms / 1000).toFixed(1)}s`
                    : `${e.duration_ms}ms`
                  : ''}
              </span>
              <span style={{ color: '#94a3b8', fontFamily: 'monospace', fontSize: 10 }}>{e.progress}</span>
            </div>
          )
        })}

        {testEvents.length === 0 && isConnected && (
          <div className="text-center py-6 text-xs" style={{ color: '#94a3b8' }}>
            Waiting for results...
          </div>
        )}
      </div>

      {/* Summary */}
      {completed && (
        <div
          className="px-4 py-3 flex items-center gap-4 text-xs"
          style={{ background: '#0d0d1a', borderTop: '1px solid #1e1e3a' }}
        >
          <span style={{ color: '#34d399', fontFamily: 'monospace' }}>{completed.passed} passed</span>
          <span style={{ color: '#fb7185', fontFamily: 'monospace' }}>{completed.failed} failed</span>
          <span style={{ color: '#64748b', fontFamily: 'monospace' }}>{completed.total} total</span>
          <span className="ml-auto" style={{ color: '#94a3b8', fontFamily: 'monospace' }}>
            Completed in {fmtElapsed}
          </span>
        </div>
      )}
    </div>
  )
}
