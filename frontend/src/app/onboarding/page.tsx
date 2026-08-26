'use client'

import { useReducer, useEffect, useRef, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { createProject, updateProject, testIntegration } from '@/lib/api-client'
import { useToast } from '@/components/ui/Toast'

// ── Constants ────────────────────────────────────────────────────────────────

// Fix HHH — added 'Connect to SUT' step (5) between Repository (4) and Deploy & CI/CD (6).
const STEP_LABELS = ['Basics', 'Type & Scope', 'Requirements', 'Repository', 'Connect to SUT', 'Deploy & CI/CD', 'Channels', 'Automate']
const OPTIONAL_STEPS = [3, 4, 5, 6, 7]
const TOTAL_STEPS = STEP_LABELS.length

const ICONS = ['🚀', '💼', '🛒', '📱', '🔧', '📊', '🎮', '🏥', '🏦', '📚', '🎵', '🍕', '✈️', '🏠', '⚡', '🌐']

const COLORS = [
  { value: '#6366f1', label: 'Indigo' },
  { value: '#8b5cf6', label: 'Violet' },
  { value: '#06b6d4', label: 'Cyan' },
  { value: '#10b981', label: 'Emerald' },
  { value: '#f59e0b', label: 'Amber' },
  { value: '#ef4444', label: 'Rose' },
  { value: '#ec4899', label: 'Pink' },
  { value: '#14b8a6', label: 'Teal' },
]

type ProjectType = 'web_app' | 'mobile_app' | 'api_service' | 'data_pipeline' | 'desktop_app'

const PROJECT_TYPES: { id: ProjectType; icon: string; label: string; desc: string }[] = [
  { id: 'web_app', icon: '🌐', label: 'Web App', desc: 'React, Next.js, Angular, Vue' },
  { id: 'mobile_app', icon: '📱', label: 'Mobile App', desc: 'iOS, Android, React Native, Flutter' },
  { id: 'api_service', icon: '⚡', label: 'API Service', desc: 'REST, GraphQL, Microservices' },
  { id: 'data_pipeline', icon: '📊', label: 'Data Pipeline', desc: 'ETL, ML pipelines, data processing' },
  { id: 'desktop_app', icon: '💻', label: 'Desktop App', desc: 'Electron, WPF, native desktop' },
]

const STACK_CHIPS: Record<ProjectType, { label: string; kind: 'tool' | 'threshold' }[]> = {
  web_app: [
    { label: 'Playwright (UI)', kind: 'tool' },
    { label: 'Newman (API)', kind: 'tool' },
    { label: 'k6 (Performance)', kind: 'tool' },
    { label: 'Axe (Accessibility)', kind: 'tool' },
    { label: 'P0: 100%', kind: 'threshold' },
    { label: 'P1: 90%', kind: 'threshold' },
  ],
  mobile_app: [
    { label: 'Appium', kind: 'tool' },
    { label: 'Newman (API)', kind: 'tool' },
    { label: 'k6 (Performance)', kind: 'tool' },
    { label: 'P0: 100%', kind: 'threshold' },
    { label: 'P1: 85%', kind: 'threshold' },
  ],
  api_service: [
    { label: 'Newman (API)', kind: 'tool' },
    { label: 'k6 (Performance)', kind: 'tool' },
    { label: 'ZAP (Security)', kind: 'tool' },
    { label: 'P0: 100%', kind: 'threshold' },
    { label: 'P1: 95%', kind: 'threshold' },
  ],
  data_pipeline: [
    { label: 'Custom Fixtures', kind: 'tool' },
    { label: 'LLM Judge', kind: 'tool' },
    { label: 'P0: 100%', kind: 'threshold' },
    { label: 'P1: 80%', kind: 'threshold' },
  ],
  desktop_app: [
    { label: 'Playwright', kind: 'tool' },
    { label: 'k6 (Performance)', kind: 'tool' },
    { label: 'P0: 100%', kind: 'threshold' },
    { label: 'P1: 85%', kind: 'threshold' },
  ],
}

const LLM_PROVIDERS = [
  { value: 'anthropic', label: 'Anthropic', defaultModel: 'claude-sonnet-4-6' },
  { value: 'openai', label: 'OpenAI', defaultModel: 'gpt-4o' },
  { value: 'gemini', label: 'Google Gemini', defaultModel: 'gemini-1.5-pro' },
  { value: 'ollama', label: 'Ollama', defaultModel: 'llama3' },
  { value: 'azure', label: 'Azure OpenAI', defaultModel: 'gpt-4o' },
]

type ThresholdPreset = 'strict' | 'standard' | 'lenient'

const THRESHOLD_PRESETS: Record<ThresholdPreset, { p0: number; p1: number; p2: number; overall: number }> = {
  strict: { p0: 100, p1: 95, p2: 80, overall: 85 },
  standard: { p0: 100, p1: 90, p2: 75, overall: 80 },
  lenient: { p0: 95, p1: 80, p2: 60, overall: 70 },
}

const AUTOMATION_TOOLS = ['Playwright', 'Newman', 'k6', 'Jest', 'Vitest', 'Cypress', 'Selenium', 'OWASP ZAP', 'Axe', 'Appium']

type RepoProvider = 'github' | 'gitlab' | 'bitbucket'
type CICDProvider = 'github_actions' | 'jenkins' | 'circleci' | 'gitlab_ci' | 'azure_devops'
type EngagementModel = 'tea_solo' | 'tea_lite' | 'integrated'

// ── State Type ───────────────────────────────────────────────────────────────

type WizardState = {
  step: number
  // Step 1
  projectName: string
  description: string
  icon: string
  color: string
  // Step 2
  projectType: ProjectType | null
  engagementModel: EngagementModel
  isGreenfield: boolean
  // Step 3
  jiraConfig: { serverUrl: string; email: string; apiToken: string; projectKey: string }
  confluenceConfig: { serverUrl: string; email: string; apiToken: string }
  uploadedFiles: string[]
  requirementsSources: { jira: boolean; confluence: boolean; documents: boolean }
  // Step 4
  repoProvider: RepoProvider
  repositories: { name: string; repo: string; branch: string }[]
  repoPat: string
  useSameRepo: boolean
  automationRepoUrl: string
  automationRepoPat: string
  testDirectory: string
  detectedStack: { language: string; framework: string; test_framework: string; package_manager: string } | null
  confirmedTools: string[]
  // Step 5 — Fix HHH: Connect to SUT
  sutConnect: {
    ranOnboarding: boolean
    autoResolved: number
    probed: Array<{ field: string; best: string; first_id?: string; status?: string }>
    needsOperator: Array<{ field: string; reason: string; suggestion?: string; alternatives?: string[]; value?: string }>
    error?: string
  }
  // Step 6
  environments: { name: string; baseUrl: string; envVars: { key: string; value: string }[] }[]
  secrets: { key: string; value: string }[]
  cicdProvider: CICDProvider | null
  cicdConfig: string | null
  cicdFilename: string | null
  triggers: string[]
  branchPatterns: string[]
  // Step 6
  slackWebhook: string
  slackChannel: string
  teamsWebhook: string
  emailAddresses: string
  // Step 7
  llmProvider: string
  llmModel: string
  llmTemperature: number
  llmApiKey: string
  thresholdPreset: ThresholdPreset
  // Internal
  projectId: string | null
  scaffoldingDone: boolean
  generationDone: boolean
  firstRunDone: boolean
  // Connection states
  jiraConnected: boolean
  confluenceConnected: boolean
  repoConnected: boolean
}

const INITIAL_STATE: WizardState = {
  step: 1,
  projectName: '',
  description: '',
  icon: '🚀',
  color: '#6366f1',
  projectType: null,
  engagementModel: 'tea_solo',
  isGreenfield: true,
  jiraConfig: { serverUrl: '', email: '', apiToken: '', projectKey: '' },
  confluenceConfig: { serverUrl: '', email: '', apiToken: '' },
  uploadedFiles: [],
  requirementsSources: { jira: false, confluence: false, documents: false },
  repoProvider: 'github',
  repositories: [],
  repoPat: '',
  useSameRepo: true,
  automationRepoUrl: '',
  automationRepoPat: '',
  testDirectory: 'tests/',
  detectedStack: null,
  confirmedTools: [],
  sutConnect: { ranOnboarding: false, autoResolved: 0, probed: [], needsOperator: [] },
  environments: [
    { name: 'dev', baseUrl: '', envVars: [] },
    { name: 'qa', baseUrl: '', envVars: [] },
    { name: 'staging', baseUrl: '', envVars: [] },
    { name: 'prod', baseUrl: '', envVars: [] },
  ],
  secrets: [],
  cicdProvider: null,
  cicdConfig: null,
  cicdFilename: null,
  triggers: ['push_main', 'pull_requests'],
  branchPatterns: ['main', 'develop', 'release/*'],
  slackWebhook: '',
  slackChannel: '',
  teamsWebhook: '',
  emailAddresses: '',
  llmProvider: 'anthropic',
  llmModel: 'claude-sonnet-4-6',
  llmTemperature: 0.3,
  llmApiKey: '',
  thresholdPreset: 'standard',
  projectId: null,
  scaffoldingDone: false,
  generationDone: false,
  firstRunDone: false,
  jiraConnected: false,
  confluenceConnected: false,
  repoConnected: false,
}

// ── Reducer ──────────────────────────────────────────────────────────────────

type Action =
  | { type: 'SET_FIELD'; field: keyof WizardState; value: any }
  | { type: 'SET_STEP'; step: number }
  | { type: 'NEXT_STEP' }
  | { type: 'PREV_STEP' }
  | { type: 'SET_JIRA_FIELD'; field: string; value: string }
  | { type: 'SET_CONFLUENCE_FIELD'; field: string; value: string }
  | { type: 'ADD_FILE'; filename: string }
  | { type: 'REMOVE_FILE'; filename: string }
  | { type: 'ADD_ENVIRONMENT' }
  | { type: 'REMOVE_ENVIRONMENT'; index: number }
  | { type: 'UPDATE_ENVIRONMENT'; index: number; field: string; value: string }
  | { type: 'ADD_ENV_VAR'; envIndex: number }
  | { type: 'REMOVE_ENV_VAR'; envIndex: number; varIndex: number }
  | { type: 'UPDATE_ENV_VAR'; envIndex: number; varIndex: number; field: 'key' | 'value'; value: string }
  | { type: 'ADD_SECRET' }
  | { type: 'REMOVE_SECRET'; index: number }
  | { type: 'UPDATE_SECRET'; index: number; field: 'key' | 'value'; value: string }
  | { type: 'TOGGLE_TOOL'; tool: string }
  | { type: 'TOGGLE_TRIGGER'; trigger: string }
  | { type: 'SET_CICD'; provider: CICDProvider }
  | { type: 'RESTORE'; state: WizardState }

function reducer(state: WizardState, action: Action): WizardState {
  switch (action.type) {
    case 'SET_FIELD':
      return { ...state, [action.field]: action.value }
    case 'SET_STEP':
      return { ...state, step: action.step }
    case 'NEXT_STEP':
      return { ...state, step: Math.min(state.step + 1, TOTAL_STEPS) }
    case 'PREV_STEP':
      return { ...state, step: Math.max(state.step - 1, 1) }
    case 'SET_JIRA_FIELD':
      return { ...state, jiraConfig: { ...state.jiraConfig, [action.field]: action.value } }
    case 'SET_CONFLUENCE_FIELD':
      return { ...state, confluenceConfig: { ...state.confluenceConfig, [action.field]: action.value } }
    case 'ADD_FILE':
      return { ...state, uploadedFiles: [...state.uploadedFiles, action.filename] }
    case 'REMOVE_FILE':
      return { ...state, uploadedFiles: state.uploadedFiles.filter(f => f !== action.filename) }
    case 'ADD_ENVIRONMENT':
      return { ...state, environments: [...state.environments, { name: '', baseUrl: '', envVars: [] }] }
    case 'REMOVE_ENVIRONMENT':
      return { ...state, environments: state.environments.filter((_, i) => i !== action.index) }
    case 'UPDATE_ENVIRONMENT': {
      const envs = [...state.environments]
      envs[action.index] = { ...envs[action.index], [action.field]: action.value }
      return { ...state, environments: envs }
    }
    case 'ADD_ENV_VAR': {
      const envs = [...state.environments]
      envs[action.envIndex] = { ...envs[action.envIndex], envVars: [...envs[action.envIndex].envVars, { key: '', value: '' }] }
      return { ...state, environments: envs }
    }
    case 'REMOVE_ENV_VAR': {
      const envs = [...state.environments]
      envs[action.envIndex] = { ...envs[action.envIndex], envVars: envs[action.envIndex].envVars.filter((_, i) => i !== action.varIndex) }
      return { ...state, environments: envs }
    }
    case 'UPDATE_ENV_VAR': {
      const envs = [...state.environments]
      const vars = [...envs[action.envIndex].envVars]
      vars[action.varIndex] = { ...vars[action.varIndex], [action.field]: action.value }
      envs[action.envIndex] = { ...envs[action.envIndex], envVars: vars }
      return { ...state, environments: envs }
    }
    case 'ADD_SECRET':
      return { ...state, secrets: [...state.secrets, { key: '', value: '' }] }
    case 'REMOVE_SECRET':
      return { ...state, secrets: state.secrets.filter((_, i) => i !== action.index) }
    case 'UPDATE_SECRET': {
      const secrets = [...state.secrets]
      secrets[action.index] = { ...secrets[action.index], [action.field]: action.value }
      return { ...state, secrets }
    }
    case 'TOGGLE_TOOL': {
      const tools = state.confirmedTools.includes(action.tool)
        ? state.confirmedTools.filter(t => t !== action.tool)
        : [...state.confirmedTools, action.tool]
      return { ...state, confirmedTools: tools }
    }
    case 'TOGGLE_TRIGGER': {
      const triggers = state.triggers.includes(action.trigger)
        ? state.triggers.filter(t => t !== action.trigger)
        : [...state.triggers, action.trigger]
      return { ...state, triggers }
    }
    case 'SET_CICD': {
      const configs = generateCICDConfig(action.provider)
      return { ...state, cicdProvider: action.provider, cicdConfig: configs.config, cicdFilename: configs.filename }
    }
    case 'RESTORE':
      return action.state
    default:
      return state
  }
}

// ── CI/CD Config Generator ──────────────────────────────────────────────────

function generateCICDConfig(provider: CICDProvider): { config: string; filename: string } {
  switch (provider) {
    case 'github_actions':
      return {
        filename: '.github/workflows/arta-quality.yml',
        config: `name: ARTA Quality Pipeline
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]
  workflow_dispatch:

jobs:
  arta-quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # Stage 1: Requirements Analysis
      - name: Analyze Requirements
        run: arta run --stage requirements

      # Stage 2: Risk Scoring
      - name: Risk Assessment
        run: arta run --stage risk-scoring

      # Stage 3: ATDD Design
      - name: Generate ATDD Scenarios
        run: arta run --stage atdd-design

      # Stage 4: Automation
      - name: Generate Test Scripts
        run: arta run --stage automation

      # Stage 5: Execution
      - name: Execute Test Suite
        run: arta run --stage execution

      # Stage 6: Evidence Collection
      - name: Collect Evidence
        run: arta run --stage evidence

      # Stage 7: Traceability
      - name: Update Traceability
        run: arta run --stage traceability

      # Stage 8: Quality Gate
      - name: Quality Gate Decision
        run: arta run --stage gate
    env:
      ARTA_API_URL: \${{ secrets.ARTA_API_URL }}
      ARTA_API_KEY: \${{ secrets.ARTA_API_KEY }}
      ARTA_PROJECT_ID: \${{ secrets.ARTA_PROJECT_ID }}`,
      }
    case 'jenkins':
      return {
        filename: 'Jenkinsfile',
        config: `pipeline {
    agent any
    environment {
        ARTA_API_URL = credentials('arta-api-url')
        ARTA_API_KEY = credentials('arta-api-key')
        ARTA_PROJECT_ID = credentials('arta-project-id')
    }
    stages {
        stage('Requirements')  { steps { sh 'arta run --stage requirements' } }
        stage('Risk Scoring')  { steps { sh 'arta run --stage risk-scoring' } }
        stage('ATDD Design')   { steps { sh 'arta run --stage atdd-design' } }
        stage('Automation')    { steps { sh 'arta run --stage automation' } }
        stage('Execution')     { steps { sh 'arta run --stage execution' } }
        stage('Evidence')      { steps { sh 'arta run --stage evidence' } }
        stage('Traceability')  { steps { sh 'arta run --stage traceability' } }
        stage('Quality Gate')  { steps { sh 'arta run --stage gate' } }
    }
}`,
      }
    case 'circleci':
      return {
        filename: '.circleci/config.yml',
        config: `version: 2.1
jobs:
  arta-quality:
    docker:
      - image: cimg/base:stable
    steps:
      - checkout
      - run: arta run --stage requirements
      - run: arta run --stage risk-scoring
      - run: arta run --stage atdd-design
      - run: arta run --stage automation
      - run: arta run --stage execution
      - run: arta run --stage evidence
      - run: arta run --stage traceability
      - run: arta run --stage gate
workflows:
  quality:
    jobs:
      - arta-quality`,
      }
    case 'gitlab_ci':
      return {
        filename: '.gitlab-ci.yml',
        config: `stages:
  - requirements
  - risk-scoring
  - atdd-design
  - automation
  - execution
  - evidence
  - traceability
  - gate

requirements:
  stage: requirements
  script: arta run --stage requirements

risk-scoring:
  stage: risk-scoring
  script: arta run --stage risk-scoring

atdd-design:
  stage: atdd-design
  script: arta run --stage atdd-design

automation:
  stage: automation
  script: arta run --stage automation

execution:
  stage: execution
  script: arta run --stage execution

evidence:
  stage: evidence
  script: arta run --stage evidence

traceability:
  stage: traceability
  script: arta run --stage traceability

gate:
  stage: gate
  script: arta run --stage gate`,
      }
    case 'azure_devops':
      return {
        filename: 'azure-pipelines.yml',
        config: `trigger:
  branches:
    include: [main, develop]

pool:
  vmImage: 'ubuntu-latest'

steps:
  - script: arta run --stage requirements
    displayName: 'Stage 1: Requirements'
  - script: arta run --stage risk-scoring
    displayName: 'Stage 2: Risk Scoring'
  - script: arta run --stage atdd-design
    displayName: 'Stage 3: ATDD Design'
  - script: arta run --stage automation
    displayName: 'Stage 4: Automation'
  - script: arta run --stage execution
    displayName: 'Stage 5: Execution'
  - script: arta run --stage evidence
    displayName: 'Stage 6: Evidence'
  - script: arta run --stage traceability
    displayName: 'Stage 7: Traceability'
  - script: arta run --stage gate
    displayName: 'Stage 8: Quality Gate'`,
      }
  }
}

// ── Shared Styles ────────────────────────────────────────────────────────────

const inputStyle: React.CSSProperties = {
  background: '#0a0a14',
  border: '1px solid #1e1e3a',
  color: '#e2e8f0',
}

const cardStyle: React.CSSProperties = {
  background: '#12121f',
  border: '1px solid #1e1e3a',
}

// ── Step Indicator ───────────────────────────────────────────────────────────

function StepIndicator({ currentStep }: { currentStep: number }) {
  return (
    <div className="mb-10">
      {/* Desktop */}
      <div className="hidden sm:flex items-center justify-between relative">
        {STEP_LABELS.map((label, i) => {
          const stepNum = i + 1
          const isCompleted = stepNum < currentStep
          const isActive = stepNum === currentStep
          return (
            <div key={stepNum} className="flex flex-col items-center relative z-10" style={{ flex: 1 }}>
              {/* Connector line */}
              {i > 0 && (
                <div
                  className="absolute top-4 right-1/2 h-0.5"
                  style={{
                    width: '100%',
                    background: stepNum <= currentStep ? '#10b981' : '#334155',
                  }}
                />
              )}
              {/* Circle */}
              <div
                className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold relative z-10 transition-all"
                style={{
                  background: isCompleted ? '#10b981' : isActive ? '#6366f1' : 'transparent',
                  border: isCompleted ? '2px solid #10b981' : isActive ? '2px solid #6366f1' : '2px solid #334155',
                  color: isCompleted || isActive ? '#fff' : '#64748b',
                  boxShadow: isActive ? '0 0 16px rgba(99,102,241,0.4)' : 'none',
                  fontFamily: 'Space Mono, monospace',
                }}
              >
                {isCompleted ? '\u2713' : stepNum}
              </div>
              {/* Label */}
              <span
                className="mt-2 text-[10px] font-medium"
                style={{
                  color: isActive ? '#c7d2fe' : isCompleted ? '#10b981' : '#94a3b8',
                  fontFamily: 'Space Mono, monospace',
                }}
              >
                {label}
              </span>
            </div>
          )
        })}
      </div>
      {/* Mobile */}
      <div className="sm:hidden text-center">
        <span className="text-xs font-medium" style={{ color: '#6366f1', fontFamily: 'Space Mono, monospace' }}>
          Step {currentStep} of 7
        </span>
        <div className="text-sm font-semibold mt-1" style={{ color: '#c7d2fe' }}>
          {STEP_LABELS[currentStep - 1]}
        </div>
        <div className="mt-2 h-1 rounded-full overflow-hidden" style={{ background: '#1e1e3a' }}>
          <div
            className="h-full rounded-full transition-all duration-300"
            style={{ width: `${(currentStep / 7) * 100}%`, background: 'linear-gradient(90deg, #6366f1, #8b5cf6)' }}
          />
        </div>
      </div>
    </div>
  )
}

// ── Collapsible Card ─────────────────────────────────────────────────────────

function CollapsibleCard({
  title,
  icon,
  badge,
  badgeColor,
  open,
  onToggle,
  children,
}: {
  title: string
  icon: string
  badge?: string
  badgeColor?: string
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}) {
  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-5 py-4 text-left transition-colors"
        style={{ background: open ? '#12121f' : '#0d0d18' }}
      >
        <span className="text-lg">{icon}</span>
        <span className="text-sm font-medium flex-1" style={{ color: open ? '#c7d2fe' : '#94a3b8' }}>
          {title}
        </span>
        {badge && (
          <span
            className="text-[10px] font-medium px-2 py-0.5 rounded-full"
            style={{
              background: badgeColor === 'green' ? 'rgba(16,185,129,0.15)' : 'rgba(100,116,139,0.15)',
              color: badgeColor === 'green' ? '#10b981' : '#64748b',
              border: `1px solid ${badgeColor === 'green' ? 'rgba(16,185,129,0.3)' : 'rgba(100,116,139,0.3)'}`,
            }}
          >
            {badge}
          </span>
        )}
        <span
          className="text-xs transition-transform"
          style={{
            color: '#6366f1',
            transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
            display: 'inline-block',
          }}
        >
          {'\u25B6'}
        </span>
      </button>
      <div
        className="overflow-hidden transition-all duration-300"
        style={{
          maxHeight: open ? 1200 : 0,
          opacity: open ? 1 : 0,
        }}
      >
        <div className="px-5 py-5" style={{ background: '#12121f', borderTop: '1px solid #1e1e3a' }}>
          {children}
        </div>
      </div>
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function OnboardingPage() {
  const router = useRouter()
  const toast = useToast()
  const nameRef = useRef<HTMLInputElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [state, dispatch] = useReducer(reducer, INITIAL_STATE)
  const [loading, setLoading] = useState(false)
  const [expandedSources, setExpandedSources] = useState<Record<string, boolean>>({ jira: false, confluence: false, documents: false })
  const [expandedEnvVars, setExpandedEnvVars] = useState<Record<number, boolean>>({})
  // Previously tracked showScaffolding / showCICDManual state — removed when panels were
  // inlined. Keep state scaffolding noted here so future panel work can reintroduce them.
  const [showLLMCustomize, setShowLLMCustomize] = useState(false)

  // Scaffolding animation state
  const [scaffoldSteps, setScaffoldSteps] = useState<string[]>([])
  // Generation state
  const [generationProgress, setGenerationProgress] = useState(0)
  const [generatedTests, setGeneratedTests] = useState<{ name: string; type: string }[] | null>(null)
  // First run state
  const [runResults, setRunResults] = useState<{ title: string; status: string }[]>([])
  const [runSummary, setRunSummary] = useState<{ passed: number; failed: number; skipped: number; coverage: number; grade: string } | null>(null)
  const [runningSmoke, setRunningSmoke] = useState(false)
  const [pipelineStage, setPipelineStage] = useState(0)

  // Restore from sessionStorage on mount
  useEffect(() => {
    try {
      const saved = sessionStorage.getItem('arta_wizard_state')
      if (saved) {
        const parsed = JSON.parse(saved)
        dispatch({ type: 'RESTORE', state: { ...INITIAL_STATE, ...parsed } })
      }
    } catch { /* ignore */ }
  }, [])

  // Persist to sessionStorage on every state change
  useEffect(() => {
    try {
      sessionStorage.setItem('arta_wizard_state', JSON.stringify(state))
    } catch { /* ignore */ }
  }, [state])

  // Auto-focus name input on step 1
  useEffect(() => {
    if (state.step === 1) {
      setTimeout(() => nameRef.current?.focus(), 100)
    }
  }, [state.step])

  // Auto-suggest model when LLM provider changes
  useEffect(() => {
    const prov = LLM_PROVIDERS.find(p => p.value === state.llmProvider)
    if (prov) dispatch({ type: 'SET_FIELD', field: 'llmModel', value: prov.defaultModel })
  }, [state.llmProvider])

  // Run scaffolding animation when entering step 7
  useEffect(() => {
    if (state.step === TOTAL_STEPS && !state.scaffoldingDone) {
      setScaffoldSteps([])
      const steps = [
        'Creating directory structure...',
        'Generating playwright.config.ts...',
        'Generating k6.config.js...',
        'Scaffolding complete',
      ]
      steps.forEach((s, i) => {
        setTimeout(() => {
          setScaffoldSteps(prev => [...prev, s])
          if (i === steps.length - 1) {
            dispatch({ type: 'SET_FIELD', field: 'scaffoldingDone', value: true })
          }
        }, (i + 1) * 500)
      })
    }
  }, [state.step, state.scaffoldingDone])

  const setField = useCallback((field: keyof WizardState, value: any) => {
    dispatch({ type: 'SET_FIELD', field, value })
  }, [])

  // ── Validation ──────────────────────────────────────────────────────────────

  const canProceed = (): boolean => {
    switch (state.step) {
      case 1: return state.projectName.trim().length > 0
      case 2: return state.projectType !== null
      default: return true
    }
  }

  // ── Handlers ────────────────────────────────────────────────────────────────

  const handleNext = async () => {
    if (state.step === 1) {
      // Create project
      setLoading(true)
      try {
        const result = await createProject({
          name: state.projectName.trim(),
          description: state.description,
          icon: state.icon,
          color: state.color,
        })
        setField('projectId', result.id)
        toast.success('Project created!')
        dispatch({ type: 'NEXT_STEP' })
      } catch (err: any) {
        toast.error(err.message || 'Failed to create project')
      } finally {
        setLoading(false)
      }
      return
    }

    if (state.step === 2 && state.projectId) {
      // Update project with type/scope
      setLoading(true)
      try {
        await updateProject(state.projectId, {
          project_type: state.projectType || undefined,
          engagement_model: state.engagementModel,
          is_greenfield: state.isGreenfield,
        } as any)
        dispatch({ type: 'NEXT_STEP' })
      } catch (err: any) {
        toast.error(err.message || 'Failed to update project')
      } finally {
        setLoading(false)
      }
      return
    }

    if (state.step === 4 && state.projectId) {
      // Save repositories to project
      if (state.repositories.length > 0 || state.repoPat) {
        setLoading(true)
        try {
          await updateProject(state.projectId, {
            integrations: {
              github_repo: state.repositories[0]?.repo || '',
              github_token: state.repoPat,
              repositories: state.repositories.map(r => ({ ...r, provider: state.repoProvider })),
            },
          } as any)
        } catch (err: any) {
          toast.error(err.message || 'Failed to save repositories')
        } finally {
          setLoading(false)
        }
      }
      dispatch({ type: 'NEXT_STEP' })
      return
    }

    dispatch({ type: 'NEXT_STEP' })
  }

  const handleBack = () => dispatch({ type: 'PREV_STEP' })
  const handleSkip = () => dispatch({ type: 'NEXT_STEP' })

  const handleTestJira = async () => {
    if (!state.projectId) return
    setLoading(true)
    try {
      await testIntegration(state.projectId, 'jira')
      setField('jiraConnected', true)
      toast.success('Jira connected successfully!')
    } catch (err: any) {
      toast.error(err.message || 'Jira connection failed')
    } finally {
      setLoading(false)
    }
  }

  const handleTestConfluence = async () => {
    if (!state.projectId) return
    setLoading(true)
    try {
      await testIntegration(state.projectId, 'confluence')
      setField('confluenceConnected', true)
      toast.success('Confluence connected successfully!')
    } catch (err: any) {
      toast.error(err.message || 'Confluence connection failed')
    } finally {
      setLoading(false)
    }
  }

  const handleTestRepo = async () => {
    setLoading(true)
    // Mock detection — in future calls /api/projects/{id}/detect-stack
    await new Promise(r => setTimeout(r, 1500))
    setField('repoConnected', true)
    setField('detectedStack', {
      language: 'TypeScript',
      framework: 'Next.js 14',
      test_framework: 'Playwright',
      package_manager: 'npm',
    })
    setField('confirmedTools', ['Playwright', 'Newman', 'k6'])
    toast.success('Repository connected. Stack detected!')
    setLoading(false)
  }

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files) return
    for (let i = 0; i < files.length; i++) {
      dispatch({ type: 'ADD_FILE', filename: files[i].name })
    }
    e.target.value = ''
  }

  const handleCopyConfig = () => {
    if (state.cicdConfig) {
      navigator.clipboard.writeText(state.cicdConfig)
      toast.success('Config copied to clipboard!')
    }
  }

  const handleCommitConfig = () => {
    toast.success('PR created successfully')
  }

  const handleTestSlack = () => {
    toast.success('Test message sent!')
  }

  const handleTestTeams = () => {
    toast.success('Test message sent!')
  }

  const handleGenerateTests = () => {
    setGenerationProgress(0)
    const interval = setInterval(() => {
      setGenerationProgress(prev => {
        if (prev >= 100) {
          clearInterval(interval)
          setGeneratedTests([
            { name: 'User Login Flow', type: 'UI' },
            { name: 'Product Search & Filter', type: 'UI' },
            { name: 'Checkout Process', type: 'UI' },
            { name: 'Payment API Validation', type: 'API' },
            { name: 'Inventory API CRUD', type: 'API' },
            { name: 'User Profile Endpoints', type: 'API' },
            { name: 'Homepage Load Performance', type: 'Performance' },
            { name: 'Auth Endpoint Security Scan', type: 'Security' },
          ])
          dispatch({ type: 'SET_FIELD', field: 'generationDone', value: true })
          return 100
        }
        return prev + 3
      })
    }, 80)
  }

  const handleRunSmoke = async () => {
    setRunningSmoke(true)
    setPipelineStage(1)
    const mockResults = [
      { title: 'Login with valid credentials', status: 'PASS' },
      { title: 'Login with invalid password', status: 'PASS' },
      { title: 'Product search returns results', status: 'PASS' },
      { title: 'Add item to cart', status: 'PASS' },
      { title: 'Checkout flow completes', status: 'FAIL' },
      { title: 'Payment API returns 200', status: 'PASS' },
      { title: 'User profile loads', status: 'PASS' },
      { title: 'Homepage loads under 3s', status: 'PASS' },
    ]
    // Animate pipeline stages
    for (let i = 1; i <= 9; i++) {
      await new Promise(r => setTimeout(r, 300))
      setPipelineStage(i)
    }
    // Stream results
    for (let i = 0; i < mockResults.length; i++) {
      await new Promise(r => setTimeout(r, 400))
      setRunResults(prev => [...prev, mockResults[i]])
    }
    await new Promise(r => setTimeout(r, 500))
    setRunSummary({ passed: 7, failed: 1, skipped: 0, coverage: 87, grade: 'B' })
    dispatch({ type: 'SET_FIELD', field: 'firstRunDone', value: true })
    setRunningSmoke(false)
  }

  const handleFinish = () => {
    sessionStorage.removeItem('arta_wizard_state')
    router.push('/')
  }

  // ── Render Steps ────────────────────────────────────────────────────────────

  const renderStep1 = () => (
    <div className="space-y-8">
      <div>
        <label className="text-xs font-medium mb-2 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          PROJECT NAME *
        </label>
        <input
          ref={nameRef}
          type="text"
          value={state.projectName}
          onChange={e => setField('projectName', e.target.value)}
          placeholder="My E-Commerce App"
          className="w-full px-5 py-4 rounded-xl text-lg outline-none transition-all focus:ring-2"
          style={{ ...inputStyle, fontFamily: 'DM Sans, sans-serif', ['--tw-ring-color' as any]: '#6366f1' }}
        />
      </div>

      <div>
        <label className="text-xs font-medium mb-2 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          DESCRIPTION
        </label>
        <textarea
          value={state.description}
          onChange={e => setField('description', e.target.value)}
          placeholder="Brief description of your project..."
          rows={3}
          className="w-full px-4 py-3 rounded-xl text-sm outline-none resize-none focus:ring-2"
          style={{ ...inputStyle, fontFamily: 'DM Sans, sans-serif', ['--tw-ring-color' as any]: '#6366f1' }}
        />
      </div>

      <div>
        <label className="text-xs font-medium mb-3 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          ICON
        </label>
        <div className="grid grid-cols-8 gap-2 mb-3">
          {ICONS.map(icon => (
            <button
              key={icon}
              onClick={() => setField('icon', icon)}
              className="w-10 h-10 rounded-lg flex items-center justify-center text-lg transition-all"
              style={{
                background: state.icon === icon ? 'rgba(99,102,241,0.15)' : '#0a0a14',
                border: `1px solid ${state.icon === icon ? '#6366f1' : '#1e1e3a'}`,
              }}
            >
              {icon}
            </button>
          ))}
        </div>
        <input
          type="text"
          value={state.icon}
          onChange={e => setField('icon', e.target.value)}
          placeholder="Or type custom emoji/text"
          className="w-48 px-3 py-1.5 rounded-lg text-xs outline-none"
          style={inputStyle}
        />
      </div>

      <div>
        <label className="text-xs font-medium mb-3 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          COLOR
        </label>
        <div className="flex gap-3">
          {COLORS.map(c => (
            <button
              key={c.value}
              onClick={() => setField('color', c.value)}
              className="w-8 h-8 rounded-full transition-all"
              style={{
                background: c.value,
                boxShadow: state.color === c.value ? `0 0 0 3px #05050a, 0 0 0 5px ${c.value}` : 'none',
                transform: state.color === c.value ? 'scale(1.15)' : 'scale(1)',
              }}
              title={c.label}
            />
          ))}
        </div>
      </div>
    </div>
  )

  const renderStep2 = () => (
    <div className="space-y-8">
      <div>
        <label className="text-xs font-medium mb-3 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          PROJECT TYPE *
        </label>
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
          {PROJECT_TYPES.map(pt => {
            const selected = state.projectType === pt.id
            return (
              <button
                key={pt.id}
                onClick={() => setField('projectType', pt.id)}
                className="rounded-xl px-3 py-4 text-center transition-all"
                style={{
                  background: selected ? 'rgba(99,102,241,0.1)' : '#12121f',
                  border: `1px solid ${selected ? '#6366f1' : '#1e1e3a'}`,
                  boxShadow: selected ? '0 0 20px rgba(99,102,241,0.15)' : 'none',
                }}
              >
                <div className="text-2xl mb-2">{pt.icon}</div>
                <div className="text-xs font-semibold mb-1" style={{ color: selected ? '#c7d2fe' : '#94a3b8' }}>
                  {pt.label}
                </div>
                <div className="text-[10px] leading-tight" style={{ color: '#94a3b8' }}>
                  {pt.desc}
                </div>
              </button>
            )
          })}
        </div>
      </div>

      {/* Auto-configured chips */}
      {state.projectType && (
        <div className="flex flex-wrap gap-2">
          {STACK_CHIPS[state.projectType].map(chip => (
            <span
              key={chip.label}
              className="px-3 py-1 rounded-full text-xs font-medium"
              style={{
                background: chip.kind === 'tool' ? 'rgba(6,182,212,0.12)' : 'rgba(139,92,246,0.12)',
                color: chip.kind === 'tool' ? '#06b6d4' : '#8b5cf6',
                border: `1px solid ${chip.kind === 'tool' ? 'rgba(6,182,212,0.25)' : 'rgba(139,92,246,0.25)'}`,
                fontFamily: 'Space Mono, monospace',
              }}
            >
              {chip.label}
            </span>
          ))}
        </div>
      )}

      <div>
        <label className="text-xs font-medium mb-3 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          ENGAGEMENT MODEL
        </label>
        <div className="flex gap-3">
          {([
            { id: 'tea_solo' as EngagementModel, label: 'TEA Solo', desc: 'Fully autonomous' },
            { id: 'tea_lite' as EngagementModel, label: 'TEA Lite', desc: 'AI-assisted' },
            { id: 'integrated' as EngagementModel, label: 'Integrated', desc: 'Human-in-the-loop' },
          ]).map(em => {
            const selected = state.engagementModel === em.id
            return (
              <button
                key={em.id}
                onClick={() => setField('engagementModel', em.id)}
                className="flex-1 rounded-xl px-4 py-3 text-left transition-all"
                style={{
                  background: selected ? 'rgba(99,102,241,0.08)' : '#0a0a14',
                  border: `1px solid ${selected ? '#6366f1' : '#1e1e3a'}`,
                }}
              >
                <div className="flex items-center gap-2 mb-1">
                  <div
                    className="w-3.5 h-3.5 rounded-full border-2 flex items-center justify-center"
                    style={{ borderColor: selected ? '#6366f1' : '#334155' }}
                  >
                    {selected && <div className="w-1.5 h-1.5 rounded-full" style={{ background: '#6366f1' }} />}
                  </div>
                  <span className="text-xs font-semibold" style={{ color: selected ? '#c7d2fe' : '#94a3b8' }}>
                    {em.label}
                  </span>
                </div>
                <div className="text-[10px] ml-5" style={{ color: '#94a3b8' }}>{em.desc}</div>
              </button>
            )
          })}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={() => setField('isGreenfield', !state.isGreenfield)}
          className="w-10 h-5 rounded-full relative transition-colors"
          style={{ background: state.isGreenfield ? '#6366f1' : '#334155' }}
        >
          <div
            className="w-4 h-4 rounded-full absolute top-0.5 transition-all"
            style={{
              background: '#fff',
              left: state.isGreenfield ? 22 : 2,
            }}
          />
        </button>
        <span className="text-sm" style={{ color: '#94a3b8' }}>Greenfield project</span>
        <span className="text-[10px]" style={{ color: '#94a3b8' }}>(no existing tests)</span>
      </div>
    </div>
  )

  const renderStep3 = () => (
    <div className="space-y-4">
      <p className="text-sm mb-4" style={{ color: '#64748b' }}>
        Connect your requirements sources. Click a card to expand.
      </p>

      {/* Jira */}
      <CollapsibleCard
        title="Jira"
        icon="\uD83D\uDCCB"
        badge={state.jiraConnected ? 'Connected' : 'Not configured'}
        badgeColor={state.jiraConnected ? 'green' : 'gray'}
        open={expandedSources.jira}
        onToggle={() => setExpandedSources(prev => ({ ...prev, jira: !prev.jira }))}
      >
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Server URL</label>
              <input
                type="text"
                value={state.jiraConfig.serverUrl}
                onChange={e => dispatch({ type: 'SET_JIRA_FIELD', field: 'serverUrl', value: e.target.value })}
                placeholder="https://yourteam.atlassian.net"
                className="w-full px-3 py-2 rounded-lg text-xs outline-none"
                style={inputStyle}
              />
            </div>
            <div>
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Email</label>
              <input
                type="email"
                value={state.jiraConfig.email}
                onChange={e => dispatch({ type: 'SET_JIRA_FIELD', field: 'email', value: e.target.value })}
                placeholder="you@company.com"
                className="w-full px-3 py-2 rounded-lg text-xs outline-none"
                style={inputStyle}
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>API Token</label>
              <input
                type="password"
                value={state.jiraConfig.apiToken}
                onChange={e => dispatch({ type: 'SET_JIRA_FIELD', field: 'apiToken', value: e.target.value })}
                placeholder="ATATT..."
                className="w-full px-3 py-2 rounded-lg text-xs outline-none"
                style={inputStyle}
              />
            </div>
            <div>
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Project Key</label>
              <input
                type="text"
                value={state.jiraConfig.projectKey}
                onChange={e => dispatch({ type: 'SET_JIRA_FIELD', field: 'projectKey', value: e.target.value })}
                placeholder="PROJ"
                className="w-full px-3 py-2 rounded-lg text-xs outline-none"
                style={inputStyle}
              />
            </div>
          </div>
          <button
            onClick={handleTestJira}
            disabled={loading}
            className="px-4 py-2 rounded-lg text-xs font-medium transition-all"
            style={{
              background: 'transparent',
              color: loading ? '#94a3b8' : '#06b6d4',
              border: '1px solid #1e1e3a',
            }}
          >
            {loading ? 'Testing...' : 'Test Connection'}
          </button>
        </div>
      </CollapsibleCard>

      {/* Documents */}
      <CollapsibleCard
        title="Documents"
        icon="\uD83D\uDCC4"
        badge={state.uploadedFiles.length > 0 ? `${state.uploadedFiles.length} file(s)` : 'No files'}
        badgeColor={state.uploadedFiles.length > 0 ? 'green' : 'gray'}
        open={expandedSources.documents}
        onToggle={() => setExpandedSources(prev => ({ ...prev, documents: !prev.documents }))}
      >
        <div>
          <div
            onClick={() => fileInputRef.current?.click()}
            onDragOver={e => { e.preventDefault(); e.stopPropagation() }}
            onDrop={e => {
              e.preventDefault()
              e.stopPropagation()
              const files = e.dataTransfer.files
              for (let i = 0; i < files.length; i++) {
                dispatch({ type: 'ADD_FILE', filename: files[i].name })
              }
            }}
            className="rounded-xl p-8 text-center cursor-pointer transition-colors hover:border-indigo-500"
            style={{
              border: '2px dashed #1e1e3a',
              background: '#0a0a14',
            }}
          >
            <div className="text-2xl mb-2">📁</div>
            <p className="text-xs" style={{ color: '#64748b' }}>
              Drop requirement files here or click to browse
            </p>
            <p className="text-[10px] mt-1" style={{ color: '#94a3b8' }}>
              .docx, .xlsx, .pdf, .md, .txt, .json, .yaml
            </p>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            accept=".docx,.xlsx,.pdf,.md,.txt,.json,.yaml,.yml"
            multiple
            onChange={handleFileSelect}
          />
          {state.uploadedFiles.length > 0 && (
            <div className="mt-3 space-y-1">
              {state.uploadedFiles.map(f => (
                <div key={f} className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs" style={{ background: '#0a0a14' }}>
                  <span style={{ color: '#94a3b8' }} className="flex-1 truncate">{f}</span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full" style={{ background: 'rgba(16,185,129,0.15)', color: '#10b981' }}>
                    Uploaded
                  </span>
                  <button
                    onClick={() => dispatch({ type: 'REMOVE_FILE', filename: f })}
                    className="text-xs hover:opacity-70"
                    style={{ color: '#94a3b8' }}
                  >
                    {'\u2715'}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </CollapsibleCard>

      {/* Confluence */}
      <CollapsibleCard
        title="Confluence"
        icon="\uD83D\uDCD8"
        badge={state.confluenceConnected ? 'Connected' : 'Not configured'}
        badgeColor={state.confluenceConnected ? 'green' : 'gray'}
        open={expandedSources.confluence}
        onToggle={() => setExpandedSources(prev => ({ ...prev, confluence: !prev.confluence }))}
      >
        <div className="space-y-3">
          <div>
            <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Server URL</label>
            <input
              type="text"
              value={state.confluenceConfig.serverUrl}
              onChange={e => dispatch({ type: 'SET_CONFLUENCE_FIELD', field: 'serverUrl', value: e.target.value })}
              placeholder="https://yourteam.atlassian.net/wiki"
              className="w-full px-3 py-2 rounded-lg text-xs outline-none"
              style={inputStyle}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Email</label>
              <input
                type="email"
                value={state.confluenceConfig.email}
                onChange={e => dispatch({ type: 'SET_CONFLUENCE_FIELD', field: 'email', value: e.target.value })}
                placeholder="you@company.com"
                className="w-full px-3 py-2 rounded-lg text-xs outline-none"
                style={inputStyle}
              />
            </div>
            <div>
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>API Token</label>
              <input
                type="password"
                value={state.confluenceConfig.apiToken}
                onChange={e => dispatch({ type: 'SET_CONFLUENCE_FIELD', field: 'apiToken', value: e.target.value })}
                placeholder="ATATT..."
                className="w-full px-3 py-2 rounded-lg text-xs outline-none"
                style={inputStyle}
              />
            </div>
          </div>
          <button
            onClick={handleTestConfluence}
            disabled={loading}
            className="px-4 py-2 rounded-lg text-xs font-medium transition-all"
            style={{
              background: 'transparent',
              color: loading ? '#94a3b8' : '#06b6d4',
              border: '1px solid #1e1e3a',
            }}
          >
            {loading ? 'Testing...' : 'Test Connection'}
          </button>
        </div>
      </CollapsibleCard>
    </div>
  )

  const [newRepo, setNewRepo] = useState({ name: '', repo: '', branch: 'main' })

  const handleAddRepo = () => {
    if (!newRepo.name.trim() || !newRepo.repo.trim()) return
    setField('repositories', [...state.repositories, { ...newRepo }])
    setNewRepo({ name: '', repo: '', branch: 'main' })
  }

  const handleRemoveRepo = (idx: number) => {
    setField('repositories', state.repositories.filter((_: any, i: number) => i !== idx))
  }

  const renderStep4 = () => {
    const repoPlaceholders: Record<RepoProvider, string> = {
      github: 'https://github.com/org/repo',
      gitlab: 'https://gitlab.com/org/repo',
      bitbucket: 'https://bitbucket.org/org/repo',
    }
    return (
      <div className="space-y-8">
        {/* Provider tabs */}
        <div className="flex border-b" style={{ borderColor: '#1e1e3a' }}>
          {(['github', 'gitlab', 'bitbucket'] as RepoProvider[]).map(p => {
            const active = state.repoProvider === p
            return (
              <button
                key={p}
                onClick={() => setField('repoProvider', p)}
                className="px-6 py-3 text-sm font-medium capitalize transition-all"
                style={{
                  color: active ? '#c7d2fe' : '#94a3b8',
                  borderBottom: active ? '2px solid #6366f1' : '2px solid transparent',
                }}
              >
                {p === 'github' ? 'GitHub' : p === 'gitlab' ? 'GitLab' : 'Bitbucket'}
              </button>
            )
          })}
        </div>

        {/* Personal Access Token */}
        <div className="space-y-3">
          <div>
            <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Personal Access Token</label>
            <input
              type="password"
              value={state.repoPat}
              onChange={e => setField('repoPat', e.target.value)}
              placeholder={state.repoProvider === 'github' ? 'ghp_...' : 'glpat-...'}
              className="w-full px-3 py-2 rounded-lg text-sm outline-none"
              style={inputStyle}
            />
            <p className="text-[10px] mt-1" style={{ color: '#94a3b8' }}>
              Shared across all repositories for this provider
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleTestRepo}
              disabled={loading}
              className="px-4 py-2 rounded-lg text-xs font-medium transition-all"
              style={{
                background: 'transparent',
                color: loading ? '#94a3b8' : '#06b6d4',
                border: '1px solid #1e1e3a',
              }}
            >
              {loading ? 'Testing...' : 'Test Connection'}
            </button>
            {state.repoConnected && (
              <span className="text-[10px] px-2 py-0.5 rounded-full" style={{ background: 'rgba(16,185,129,0.15)', color: '#10b981', border: '1px solid rgba(16,185,129,0.3)' }}>
                Connected
              </span>
            )}
          </div>
        </div>

        {/* Repositories table */}
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
          <div className="px-4 py-3 flex items-center justify-between" style={{ background: '#0d0d18', borderBottom: '1px solid #1e1e3a' }}>
            <h4 className="text-xs font-semibold" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
              REPOSITORIES ({state.repositories.length})
            </h4>
          </div>

          {/* Existing repos */}
          {state.repositories.length > 0 && (
            <div style={{ maxHeight: 320, overflowY: 'auto' }}>
              <table className="w-full text-xs" style={{ color: '#94a3b8' }}>
                <thead>
                  <tr style={{ background: '#0a0a14', borderBottom: '1px solid #1e1e3a' }}>
                    <th className="text-left px-4 py-2 font-medium" style={{ color: '#94a3b8' }}>Name</th>
                    <th className="text-left px-4 py-2 font-medium" style={{ color: '#94a3b8' }}>Repository</th>
                    <th className="text-left px-4 py-2 font-medium" style={{ color: '#94a3b8' }}>Branch</th>
                    <th className="px-4 py-2 w-10" />
                  </tr>
                </thead>
                <tbody>
                  {state.repositories.map((r: { name: string; repo: string; branch: string }, idx: number) => (
                    <tr key={idx} style={{ borderBottom: '1px solid #1e1e3a' }}>
                      <td className="px-4 py-2" style={{ color: '#c7d2fe' }}>{r.name}</td>
                      <td className="px-4 py-2" style={{ fontFamily: 'JetBrains Mono, monospace', color: '#818cf8' }}>{r.repo}</td>
                      <td className="px-4 py-2">
                        <span className="px-2 py-0.5 rounded text-[10px]" style={{ background: 'rgba(99,102,241,0.12)', color: '#a5b4fc', border: '1px solid rgba(99,102,241,0.25)' }}>
                          {r.branch}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-center">
                        <button
                          onClick={() => handleRemoveRepo(idx)}
                          className="text-xs transition-colors"
                          style={{ color: '#94a3b8', background: 'none', border: 'none', cursor: 'pointer' }}
                          onMouseEnter={e => (e.currentTarget.style.color = '#ef4444')}
                          onMouseLeave={e => (e.currentTarget.style.color = '#94a3b8')}
                        >
                          {'\u2715'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Add repo row */}
          <div className="px-4 py-3 flex gap-2 items-end" style={{ background: '#0a0a14', borderTop: state.repositories.length > 0 ? '1px solid #1e1e3a' : 'none' }}>
            <div className="flex-1">
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Name</label>
              <input
                type="text"
                value={newRepo.name}
                onChange={e => setNewRepo(r => ({ ...r, name: e.target.value }))}
                placeholder="e.g., billing services"
                className="w-full px-2 py-1.5 rounded text-xs outline-none"
                style={inputStyle}
              />
            </div>
            <div className="flex-1">
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Repo name</label>
              <input
                type="text"
                value={newRepo.repo}
                onChange={e => setNewRepo(r => ({ ...r, repo: e.target.value }))}
                placeholder="e.g., owner/repo"
                className="w-full px-2 py-1.5 rounded text-xs outline-none"
                style={inputStyle}
                onKeyDown={e => { if (e.key === 'Enter') handleAddRepo() }}
              />
            </div>
            <div style={{ width: 100 }}>
              <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Branch</label>
              <input
                type="text"
                value={newRepo.branch}
                onChange={e => setNewRepo(r => ({ ...r, branch: e.target.value }))}
                placeholder="main"
                className="w-full px-2 py-1.5 rounded text-xs outline-none"
                style={inputStyle}
                onKeyDown={e => { if (e.key === 'Enter') handleAddRepo() }}
              />
            </div>
            <button
              onClick={handleAddRepo}
              disabled={!newRepo.name.trim() || !newRepo.repo.trim()}
              className="px-3 py-1.5 rounded text-xs font-medium transition-all whitespace-nowrap"
              style={{
                background: newRepo.name.trim() && newRepo.repo.trim() ? '#6366f1' : '#1e1e3a',
                color: newRepo.name.trim() && newRepo.repo.trim() ? '#fff' : '#94a3b8',
                cursor: newRepo.name.trim() && newRepo.repo.trim() ? 'pointer' : 'not-allowed',
              }}
            >
              + Add
            </button>
          </div>
        </div>

        {/* Automation scripts section */}
        <div className="rounded-xl p-5" style={cardStyle}>
          <div className="flex items-center gap-3 mb-4">
            <button
              onClick={() => setField('useSameRepo', !state.useSameRepo)}
              className="w-10 h-5 rounded-full relative transition-colors"
              style={{ background: state.useSameRepo ? '#6366f1' : '#334155' }}
            >
              <div
                className="w-4 h-4 rounded-full absolute top-0.5 transition-all"
                style={{ background: '#fff', left: state.useSameRepo ? 22 : 2 }}
              />
            </button>
            <span className="text-sm" style={{ color: '#94a3b8' }}>Use same repository for test scripts</span>
          </div>

          {!state.useSameRepo && (
            <div className="space-y-3 mb-4">
              <div>
                <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Automation Repo URL</label>
                <input
                  type="text"
                  value={state.automationRepoUrl}
                  onChange={e => setField('automationRepoUrl', e.target.value)}
                  placeholder={repoPlaceholders[state.repoProvider]}
                  className="w-full px-3 py-2 rounded-lg text-sm outline-none"
                  style={inputStyle}
                />
              </div>
              <div>
                <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Access Token</label>
                <input
                  type="password"
                  value={state.automationRepoPat}
                  onChange={e => setField('automationRepoPat', e.target.value)}
                  placeholder="ghp_..."
                  className="w-full px-3 py-2 rounded-lg text-sm outline-none"
                  style={inputStyle}
                />
              </div>
            </div>
          )}

          <div>
            <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Target test directory</label>
            <input
              type="text"
              value={state.testDirectory}
              onChange={e => setField('testDirectory', e.target.value)}
              placeholder="e.g., tests/ or e2e/"
              className="w-full px-3 py-2 rounded-lg text-sm outline-none"
              style={inputStyle}
            />
          </div>
        </div>

        {/* Detected stack */}
        {state.detectedStack && (
          <div className="rounded-xl p-5" style={cardStyle}>
            <h4 className="text-sm font-semibold mb-3" style={{ color: '#c7d2fe' }}>Detected Technology Stack</h4>
            <div className="flex flex-wrap gap-2 mb-4">
              {Object.entries(state.detectedStack).map(([key, val]) => (
                <span key={key} className="text-[10px] px-3 py-1 rounded-full" style={{ background: 'rgba(99,102,241,0.12)', color: '#a5b4fc', border: '1px solid rgba(99,102,241,0.25)' }}>
                  {key.replace('_', ' ')}: {val}
                </span>
              ))}
            </div>
            <h5 className="text-xs font-medium mb-2" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
              SUGGESTED AUTOMATION TOOLS
            </h5>
            <div className="flex flex-wrap gap-2">
              {AUTOMATION_TOOLS.map(tool => {
                const selected = state.confirmedTools.includes(tool)
                return (
                  <button
                    key={tool}
                    onClick={() => dispatch({ type: 'TOGGLE_TOOL', tool })}
                    className="text-xs px-3 py-1.5 rounded-full transition-all"
                    style={{
                      background: selected ? 'rgba(6,182,212,0.15)' : '#0a0a14',
                      color: selected ? '#06b6d4' : '#94a3b8',
                      border: `1px solid ${selected ? 'rgba(6,182,212,0.3)' : '#1e1e3a'}`,
                    }}
                  >
                    {tool}
                  </button>
                )
              })}
            </div>
          </div>
        )}

        {!state.detectedStack && state.projectType && (
          <div className="rounded-xl p-5" style={cardStyle}>
            <h4 className="text-sm font-semibold mb-3" style={{ color: '#c7d2fe' }}>We recommend based on your project type:</h4>
            <div className="flex flex-wrap gap-2">
              {AUTOMATION_TOOLS.map(tool => {
                const selected = state.confirmedTools.includes(tool)
                return (
                  <button
                    key={tool}
                    onClick={() => dispatch({ type: 'TOGGLE_TOOL', tool })}
                    className="text-xs px-3 py-1.5 rounded-full transition-all"
                    style={{
                      background: selected ? 'rgba(6,182,212,0.15)' : '#0a0a14',
                      color: selected ? '#06b6d4' : '#94a3b8',
                      border: `1px solid ${selected ? 'rgba(6,182,212,0.3)' : '#1e1e3a'}`,
                    }}
                  >
                    {tool}
                  </button>
                )
              })}
            </div>
          </div>
        )}

        {/* Scaffolding preview */}
        <details className="rounded-xl overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
          <summary className="px-5 py-3 text-sm font-medium cursor-pointer" style={{ background: '#0d0d18', color: '#94a3b8' }}>
            Preview scaffolding
          </summary>
          <pre
            className="px-5 py-4 text-xs overflow-x-auto"
            style={{ background: '#0a0a14', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace' }}
          >
{`tests/
├── e2e/
│   ├── checkout.spec.ts
│   └── auth.spec.ts
├── api/
│   └── payment.test.ts
├── perf/
│   └── load-test.js
├── config/
│   ├── playwright.config.ts
│   └── k6.config.js
└── README.md`}
          </pre>
        </details>
      </div>
    )
  }

  // Fix HHH — Step 5: Connect to SUT (auto-discovers auth + list endpoints + frontend routes).
  const renderStep5_ConnectSUT = () => {
    const sc = state.sutConnect
    const updateNeed = (idx: number, value: string) => {
      const updated = sc.needsOperator.map((row, i) => i === idx ? { ...row, value } : row)
      dispatch({ type: 'SET_FIELD', field: 'sutConnect', value: { ...sc, needsOperator: updated } })
    }
    const acceptSuggestion = (idx: number) => {
      const row = sc.needsOperator[idx]
      updateNeed(idx, row.suggestion || '')
    }
    const acceptAll = () => {
      const updated = sc.needsOperator.map(row => ({ ...row, value: row.value || row.suggestion || '' }))
      dispatch({ type: 'SET_FIELD', field: 'sutConnect', value: { ...sc, needsOperator: updated } })
    }
    const runOnboard = async () => {
      if (!state.projectId) {
        toast.error('Save the project first')
        return
      }
      setLoading(true)
      try {
        const { runSutOnboarding } = await import('@/lib/api-client')
        const res = await runSutOnboarding(state.projectId)
        const cfg = res.onboarding_config || {}
        const probed: WizardState['sutConnect']['probed'] = []
        const listEps = cfg.list_endpoints || {}
        Object.entries(listEps).forEach(([field, info]: [string, any]) => {
          probed.push({
            field,
            best: info?.best || '',
            first_id: info?.probed_first_id,
            status: info?.probe_status,
          })
        })
        dispatch({
          type: 'SET_FIELD',
          field: 'sutConnect',
          value: {
            ranOnboarding: true,
            autoResolved: res.auto_resolved_count || Object.keys(cfg.harvested_claims || {}).length,
            probed,
            needsOperator: (res.needs_operator || []).map((n: any) => ({ ...n, value: '' })),
          } as WizardState['sutConnect'],
        })
        toast.success(`Onboarding complete · ${probed.length} probed · ${(res.needs_operator || []).length} need input`)
      } catch (e: any) {
        const msg = e?.message || String(e)
        dispatch({ type: 'SET_FIELD', field: 'sutConnect', value: { ...sc, error: msg } })
        toast.error(`Onboarding failed: ${msg}`)
      } finally {
        setLoading(false)
      }
    }

    const submitAccepted = async () => {
      if (!state.projectId) return
      const accepted: Record<string, string> = {}
      sc.needsOperator.forEach(row => {
        const v = (row.value && row.value.trim()) || ''
        if (v) accepted[row.field] = v
      })
      if (Object.keys(accepted).length === 0) return
      setLoading(true)
      try {
        const { bulkAddEnvironmentVariables } = await import('@/lib/api-client')
        await bulkAddEnvironmentVariables(state.projectId, 'staging', accepted)
        toast.success(`Saved ${Object.keys(accepted).length} variable(s)`)
      } catch (e: any) {
        toast.error(`Persist failed: ${e?.message || String(e)}`)
      } finally {
        setLoading(false)
      }
    }

    return (
      <div className="space-y-6">
        <p className="text-sm" style={{ color: '#64748b' }}>
          ARTA inspects your SUT&apos;s OpenAPI + auth state to auto-fill path-param values.
          Accept the suggestions to skip 15 minutes of manual env-var editing.
        </p>

        {!sc.ranOnboarding && (
          <button
            onClick={runOnboard}
            disabled={loading || !state.projectId}
            className="px-5 py-2.5 rounded-xl text-sm font-semibold transition-all"
            style={{ background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff' }}
          >
            {loading ? 'Discovering…' : 'Run SUT discovery'}
          </button>
        )}

        {sc.ranOnboarding && (
          <>
            {/* Auto-discovered */}
            <div className="p-4 rounded-xl" style={{ background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.25)' }}>
              <div className="text-xs font-medium mb-2" style={{ color: '#34d399', fontFamily: 'Space Mono, monospace' }}>
                AUTO-DISCOVERED · {sc.autoResolved} field(s)
              </div>
              <div className="text-xs" style={{ color: '#94a3b8' }}>
                JWT claims harvested + base URL reachable. No action needed.
              </div>
            </div>

            {/* Probed */}
            {sc.probed.length > 0 && (
              <div className="p-4 rounded-xl" style={{ background: 'rgba(6,182,212,0.06)', border: '1px solid rgba(6,182,212,0.25)' }}>
                <div className="text-xs font-medium mb-3" style={{ color: '#22d3ee', fontFamily: 'Space Mono, monospace' }}>
                  PROBED · {sc.probed.length} list endpoint(s)
                </div>
                <div className="space-y-2">
                  {sc.probed.map((p, i) => (
                    <div key={i} className="flex items-center justify-between text-xs">
                      <code style={{ color: '#cbd5e1' }}>{p.field}</code>
                      <span style={{ color: p.first_id ? '#34d399' : '#f59e0b' }}>
                        {p.first_id ? `→ ${String(p.first_id).slice(0, 14)}…` : (p.status || 'unprobed')}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Needs operator */}
            {sc.needsOperator.length > 0 && (
              <div className="p-4 rounded-xl" style={{ background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.3)' }}>
                <div className="flex items-center justify-between mb-3">
                  <div className="text-xs font-medium" style={{ color: '#fbbf24', fontFamily: 'Space Mono, monospace' }}>
                    NEEDS YOUR INPUT · {sc.needsOperator.length} field(s)
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={acceptAll}
                      className="text-xs px-3 py-1 rounded-lg"
                      style={{ background: 'rgba(245,158,11,0.18)', color: '#fbbf24', border: '1px solid rgba(245,158,11,0.4)' }}
                    >
                      Accept all suggestions
                    </button>
                    <button
                      onClick={submitAccepted}
                      disabled={loading}
                      className="text-xs px-3 py-1 rounded-lg"
                      style={{ background: '#10b981', color: '#0a0a14', border: 'none' }}
                    >
                      Save to env
                    </button>
                  </div>
                </div>
                <div className="space-y-2">
                  {sc.needsOperator.map((row, i) => (
                    <div key={i} className="grid grid-cols-12 gap-2 items-center text-xs">
                      <code className="col-span-3 truncate" style={{ color: '#cbd5e1' }}>{row.field}</code>
                      <span className="col-span-3 truncate" style={{ color: '#94a3b8' }}>{row.reason}</span>
                      <input
                        className="col-span-4 px-2 py-1 rounded"
                        style={{ background: '#0a0a14', border: '1px solid #1e1e3a', color: '#e2e8f0' }}
                        placeholder={row.suggestion || ''}
                        value={row.value || ''}
                        onChange={e => updateNeed(i, e.target.value)}
                      />
                      <button
                        onClick={() => acceptSuggestion(i)}
                        className="col-span-2 text-xs px-2 py-1 rounded"
                        style={{ background: 'rgba(99,102,241,0.18)', color: '#a5b4fc', border: '1px solid rgba(99,102,241,0.3)' }}
                      >
                        {row.suggestion ? 'Accept' : 'Skip'}
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <button
              onClick={runOnboard}
              disabled={loading}
              className="text-xs px-3 py-1 rounded"
              style={{ color: '#94a3b8', background: 'transparent', border: '1px solid #1e1e3a' }}
            >
              Re-run discovery
            </button>
          </>
        )}

        {sc.error && (
          <div className="text-xs" style={{ color: '#ef4444' }}>Error: {sc.error}</div>
        )}
      </div>
    )
  }

  const renderStep6 = () => (
    <div className="space-y-8">
      {/* Environments */}
      <div>
        <label className="text-xs font-medium mb-3 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          ENVIRONMENTS
        </label>
        <div className="space-y-2">
          {state.environments.map((env, i) => (
            <div key={i} className="rounded-xl overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
              <div className="flex items-center gap-2 px-4 py-2" style={{ background: '#0d0d18' }}>
                <input
                  type="text"
                  value={env.name}
                  onChange={e => dispatch({ type: 'UPDATE_ENVIRONMENT', index: i, field: 'name', value: e.target.value })}
                  placeholder="Environment name"
                  className="w-28 px-2 py-1 rounded text-xs outline-none"
                  style={inputStyle}
                />
                <input
                  type="text"
                  value={env.baseUrl}
                  onChange={e => dispatch({ type: 'UPDATE_ENVIRONMENT', index: i, field: 'baseUrl', value: e.target.value })}
                  placeholder="https://dev.myapp.com"
                  className="flex-1 px-2 py-1 rounded text-xs outline-none"
                  style={inputStyle}
                />
                <button
                  onClick={() => setExpandedEnvVars(prev => ({ ...prev, [i]: !prev[i] }))}
                  className="text-[10px] px-2 py-1 rounded"
                  style={{ color: '#6366f1', border: '1px solid #1e1e3a' }}
                >
                  Vars
                </button>
                <button
                  onClick={() => dispatch({ type: 'REMOVE_ENVIRONMENT', index: i })}
                  className="text-xs hover:opacity-70"
                  style={{ color: '#94a3b8' }}
                >
                  {'\u2715'}
                </button>
              </div>
              {expandedEnvVars[i] && (
                <div className="px-4 py-3 space-y-2" style={{ background: '#0a0a14', borderTop: '1px solid #1e1e3a' }}>
                  {env.envVars.map((v, vi) => (
                    <div key={vi} className="flex gap-2">
                      <input
                        type="text"
                        value={v.key}
                        onChange={e => dispatch({ type: 'UPDATE_ENV_VAR', envIndex: i, varIndex: vi, field: 'key', value: e.target.value })}
                        placeholder="KEY"
                        className="w-1/3 px-2 py-1 rounded text-xs outline-none font-mono"
                        style={inputStyle}
                      />
                      <input
                        type="text"
                        value={v.value}
                        onChange={e => dispatch({ type: 'UPDATE_ENV_VAR', envIndex: i, varIndex: vi, field: 'value', value: e.target.value })}
                        placeholder="value"
                        className="flex-1 px-2 py-1 rounded text-xs outline-none"
                        style={inputStyle}
                      />
                      <button onClick={() => dispatch({ type: 'REMOVE_ENV_VAR', envIndex: i, varIndex: vi })} className="text-xs" style={{ color: '#94a3b8' }}>{'\u2715'}</button>
                    </div>
                  ))}
                  <button
                    onClick={() => dispatch({ type: 'ADD_ENV_VAR', envIndex: i })}
                    className="text-[10px] px-2 py-1 rounded"
                    style={{ color: '#6366f1', border: '1px solid #1e1e3a' }}
                  >
                    + Add Variable
                  </button>
                </div>
              )}
            </div>
          ))}
          <button
            onClick={() => dispatch({ type: 'ADD_ENVIRONMENT' })}
            className="text-xs px-4 py-2 rounded-lg transition-all"
            style={{ color: '#6366f1', border: '1px solid #1e1e3a', background: '#0a0a14' }}
          >
            + Add Environment
          </button>
        </div>
      </div>

      {/* Secrets */}
      <div>
        <label className="text-xs font-medium mb-3 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          SECRETS
        </label>
        <div className="space-y-2">
          {state.secrets.map((s, i) => (
            <div key={i} className="flex gap-2">
              <input
                type="text"
                value={s.key}
                onChange={e => dispatch({ type: 'UPDATE_SECRET', index: i, field: 'key', value: e.target.value })}
                placeholder="SECRET_KEY"
                className="w-1/3 px-3 py-2 rounded-lg text-xs outline-none font-mono"
                style={inputStyle}
              />
              <input
                type="password"
                value={s.value}
                onChange={e => dispatch({ type: 'UPDATE_SECRET', index: i, field: 'value', value: e.target.value })}
                placeholder="value"
                className="flex-1 px-3 py-2 rounded-lg text-xs outline-none"
                style={inputStyle}
              />
              <button onClick={() => dispatch({ type: 'REMOVE_SECRET', index: i })} className="text-xs px-2" style={{ color: '#94a3b8' }}>{'\u2715'}</button>
            </div>
          ))}
          <button
            onClick={() => dispatch({ type: 'ADD_SECRET' })}
            className="text-xs px-4 py-2 rounded-lg"
            style={{ color: '#6366f1', border: '1px solid #1e1e3a', background: '#0a0a14' }}
          >
            + Add Secret
          </button>
        </div>
      </div>

      {/* CI/CD Provider */}
      <div>
        <label className="text-xs font-medium mb-3 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          CI/CD PROVIDER
        </label>
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
          {([
            { id: 'github_actions' as CICDProvider, label: 'GitHub Actions' },
            { id: 'jenkins' as CICDProvider, label: 'Jenkins' },
            { id: 'circleci' as CICDProvider, label: 'CircleCI' },
            { id: 'gitlab_ci' as CICDProvider, label: 'GitLab CI' },
            { id: 'azure_devops' as CICDProvider, label: 'Azure DevOps' },
          ]).map(p => {
            const selected = state.cicdProvider === p.id
            return (
              <button
                key={p.id}
                onClick={() => dispatch({ type: 'SET_CICD', provider: p.id })}
                className="rounded-xl px-3 py-3 text-center text-xs font-medium transition-all"
                style={{
                  background: selected ? 'rgba(99,102,241,0.1)' : '#0a0a14',
                  border: `1px solid ${selected ? '#6366f1' : '#1e1e3a'}`,
                  color: selected ? '#c7d2fe' : '#94a3b8',
                  boxShadow: selected ? '0 0 16px rgba(99,102,241,0.15)' : 'none',
                }}
              >
                {p.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* CI/CD Config Preview */}
      {state.cicdConfig && (
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
          <div className="px-4 py-2 flex items-center justify-between" style={{ background: '#0d0d18', borderBottom: '1px solid #1e1e3a' }}>
            <span
              className="text-[10px] px-2 py-0.5 rounded-full"
              style={{ background: 'rgba(99,102,241,0.12)', color: '#a5b4fc', fontFamily: 'JetBrains Mono, monospace' }}
            >
              {state.cicdFilename}
            </span>
            <div className="flex gap-2">
              <button
                onClick={handleCopyConfig}
                className="text-[10px] px-3 py-1 rounded-lg transition-all"
                style={{ color: '#06b6d4', border: '1px solid #1e1e3a' }}
              >
                Copy to Clipboard
              </button>
              <button
                onClick={handleCommitConfig}
                className="text-[10px] px-3 py-1 rounded-lg transition-all"
                style={{ color: '#10b981', border: '1px solid #1e1e3a' }}
              >
                Commit to Repo
              </button>
            </div>
          </div>
          <pre
            className="px-4 py-4 text-[11px] overflow-x-auto"
            style={{ background: '#0a0a14', color: '#94a3b8', fontFamily: 'JetBrains Mono, monospace', maxHeight: 400 }}
          >
            {state.cicdConfig}
          </pre>
        </div>
      )}

      {/* Manual steps */}
      <details className="rounded-xl overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
        <summary className="px-5 py-3 text-sm font-medium cursor-pointer" style={{ background: '#0d0d18', color: '#94a3b8' }}>
          If you prefer manual setup...
        </summary>
        <div className="px-5 py-4 space-y-3" style={{ background: '#0a0a14' }}>
          <ol className="list-decimal list-inside space-y-2 text-xs" style={{ color: '#94a3b8' }}>
            <li>Copy the config above</li>
            <li>Save as <code className="px-1 py-0.5 rounded" style={{ background: '#1e1e3a', fontFamily: 'JetBrains Mono, monospace', color: '#a5b4fc' }}>{state.cicdFilename || '[filename]'}</code> in your repository</li>
            <li>Add these secrets to your CI/CD settings</li>
          </ol>
          <table className="w-full text-xs mt-3" style={{ color: '#94a3b8' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #1e1e3a' }}>
                <th className="text-left py-1 font-medium" style={{ color: '#64748b' }}>Secret</th>
                <th className="text-left py-1 font-medium" style={{ color: '#64748b' }}>Description</th>
              </tr>
            </thead>
            <tbody>
              <tr style={{ borderBottom: '1px solid #1e1e3a' }}>
                <td className="py-1.5 font-mono" style={{ color: '#a5b4fc' }}>ARTA_API_URL</td>
                <td className="py-1.5">Your ARTA platform API endpoint</td>
              </tr>
              <tr style={{ borderBottom: '1px solid #1e1e3a' }}>
                <td className="py-1.5 font-mono" style={{ color: '#a5b4fc' }}>ARTA_API_KEY</td>
                <td className="py-1.5">API key for authentication</td>
              </tr>
              <tr>
                <td className="py-1.5 font-mono" style={{ color: '#a5b4fc' }}>ARTA_PROJECT_ID</td>
                <td className="py-1.5">Your project identifier</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>

      {/* Triggers */}
      <div>
        <label className="text-xs font-medium mb-3 block" style={{ color: '#64748b', fontFamily: 'Space Mono, monospace' }}>
          TRIGGERS
        </label>
        <div className="flex flex-wrap gap-3">
          {[
            { id: 'push_main', label: 'Push to main' },
            { id: 'pull_requests', label: 'Pull requests' },
            { id: 'manual_dispatch', label: 'Manual dispatch' },
            { id: 'scheduled', label: 'Scheduled' },
          ].map(t => {
            const checked = state.triggers.includes(t.id)
            return (
              <label key={t.id} className="flex items-center gap-2 cursor-pointer text-xs" style={{ color: '#94a3b8' }}>
                <div
                  className="w-4 h-4 rounded border flex items-center justify-center"
                  style={{
                    borderColor: checked ? '#6366f1' : '#334155',
                    background: checked ? '#6366f1' : 'transparent',
                  }}
                  onClick={() => dispatch({ type: 'TOGGLE_TRIGGER', trigger: t.id })}
                >
                  {checked && <span className="text-[10px] text-white">{'\u2713'}</span>}
                </div>
                {t.label}
              </label>
            )
          })}
        </div>
      </div>

      {/* Branch patterns */}
      <div>
        <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Branch Patterns (comma-separated)</label>
        <input
          type="text"
          value={state.branchPatterns.join(', ')}
          onChange={e => setField('branchPatterns', e.target.value.split(',').map(s => s.trim()).filter(Boolean))}
          placeholder="main, develop, release/*"
          className="w-full px-3 py-2 rounded-lg text-xs outline-none"
          style={inputStyle}
        />
      </div>
    </div>
  )

  const renderStep7 = () => (
    <div className="space-y-4">
      <p className="text-sm mb-2" style={{ color: '#64748b' }}>
        Configure where you want to receive notifications.
      </p>

      {/* Slack */}
      <CollapsibleCard
        title="Slack"
        icon="\uD83D\uDCAC"
        open={expandedSources.slack ?? false}
        onToggle={() => setExpandedSources(prev => ({ ...prev, slack: !prev.slack }))}
      >
        <div className="space-y-3">
          <div>
            <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Webhook URL</label>
            <input
              type="text"
              value={state.slackWebhook}
              onChange={e => setField('slackWebhook', e.target.value)}
              placeholder="https://hooks.slack.com/services/..."
              className="w-full px-3 py-2 rounded-lg text-xs outline-none"
              style={inputStyle}
            />
          </div>
          <div>
            <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Channel Name</label>
            <input
              type="text"
              value={state.slackChannel}
              onChange={e => setField('slackChannel', e.target.value)}
              placeholder="#arta-notifications"
              className="w-full px-3 py-2 rounded-lg text-xs outline-none"
              style={inputStyle}
            />
          </div>
          <button
            onClick={handleTestSlack}
            className="px-4 py-2 rounded-lg text-xs font-medium transition-all"
            style={{ background: 'transparent', color: '#06b6d4', border: '1px solid #1e1e3a' }}
          >
            Send Test Message
          </button>
        </div>
      </CollapsibleCard>

      {/* Microsoft Teams */}
      <CollapsibleCard
        title="Microsoft Teams"
        icon="\uD83D\uDCE2"
        open={expandedSources.teams ?? false}
        onToggle={() => setExpandedSources(prev => ({ ...prev, teams: !prev.teams }))}
      >
        <div className="space-y-3">
          <div>
            <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Webhook URL</label>
            <input
              type="text"
              value={state.teamsWebhook}
              onChange={e => setField('teamsWebhook', e.target.value)}
              placeholder="https://outlook.office.com/webhook/..."
              className="w-full px-3 py-2 rounded-lg text-xs outline-none"
              style={inputStyle}
            />
          </div>
          <button
            onClick={handleTestTeams}
            className="px-4 py-2 rounded-lg text-xs font-medium transition-all"
            style={{ background: 'transparent', color: '#06b6d4', border: '1px solid #1e1e3a' }}
          >
            Send Test Message
          </button>
        </div>
      </CollapsibleCard>

      {/* Email */}
      <CollapsibleCard
        title="Email"
        icon="\uD83D\uDCE7"
        open={expandedSources.email ?? false}
        onToggle={() => setExpandedSources(prev => ({ ...prev, email: !prev.email }))}
      >
        <div>
          <label className="text-[10px] mb-1 block" style={{ color: '#94a3b8' }}>Notification Email Addresses (comma-separated)</label>
          <textarea
            value={state.emailAddresses}
            onChange={e => setField('emailAddresses', e.target.value)}
            placeholder="team@company.com, qa-lead@company.com"
            rows={3}
            className="w-full px-3 py-2 rounded-lg text-xs outline-none resize-none"
            style={inputStyle}
          />
        </div>
      </CollapsibleCard>
    </div>
  )

  const renderStep8 = () => {
    const TEA_STAGES_MINI = ['REQ', 'RSK', 'ATD', 'AUT', 'EXE', 'EVD', 'TRC', 'NFR', 'GTD']
    const thresholds = THRESHOLD_PRESETS[state.thresholdPreset]
    const typeColors: Record<string, { bg: string; color: string }> = {
      UI: { bg: 'rgba(99,102,241,0.15)', color: '#818cf8' },
      API: { bg: 'rgba(6,182,212,0.15)', color: '#06b6d4' },
      Performance: { bg: 'rgba(245,158,11,0.15)', color: '#f59e0b' },
      Security: { bg: 'rgba(239,68,68,0.15)', color: '#ef4444' },
    }

    return (
      <div className="space-y-8">
        {/* LLM Config Summary */}
        <div className="rounded-xl p-5" style={cardStyle}>
          <div className="flex items-center justify-between mb-3">
            <h4 className="text-sm font-semibold" style={{ color: '#c7d2fe' }}>LLM Configuration</h4>
            <button
              onClick={() => setShowLLMCustomize(!showLLMCustomize)}
              className="text-[10px] px-3 py-1 rounded-lg"
              style={{ color: '#6366f1', border: '1px solid #1e1e3a' }}
            >
              {showLLMCustomize ? 'Collapse' : 'Customize'}
            </button>
          </div>
          <div className="flex gap-3 mb-3">
            <span className="text-[10px] px-2 py-0.5 rounded-full" style={{ background: 'rgba(99,102,241,0.12)', color: '#a5b4fc' }}>
              {LLM_PROVIDERS.find(p => p.value === state.llmProvider)?.label || state.llmProvider}
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded-full" style={{ background: 'rgba(139,92,246,0.12)', color: '#c084fc' }}>
              {state.llmModel}
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded-full" style={{ background: 'rgba(6,182,212,0.12)', color: '#06b6d4' }}>
              temp: {state.llmTemperature.toFixed(1)}
            </span>
          </div>
          {showLLMCustomize && (
            <div className="space-y-3 mt-4 pt-4" style={{ borderTop: '1px solid #1e1e3a' }}>
              <div>
                <label className="text-xs mb-1.5 block" style={{ color: '#64748b' }}>Provider</label>
                <select
                  value={state.llmProvider}
                  onChange={e => setField('llmProvider', e.target.value)}
                  className="w-full px-3 py-2 rounded-lg text-sm outline-none appearance-none"
                  style={inputStyle}
                >
                  {LLM_PROVIDERS.map(p => (
                    <option key={p.value} value={p.value}>{p.label}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-xs mb-1.5 block" style={{ color: '#64748b' }}>Model Name</label>
                <input
                  type="text"
                  value={state.llmModel}
                  onChange={e => setField('llmModel', e.target.value)}
                  className="w-full px-3 py-2 rounded-lg text-sm outline-none"
                  style={inputStyle}
                />
              </div>
              <div>
                <label className="text-xs mb-1.5 block" style={{ color: '#64748b' }}>
                  Temperature: <span style={{ color: '#c7d2fe', fontFamily: 'Space Mono, monospace' }}>{state.llmTemperature.toFixed(1)}</span>
                </label>
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.1"
                  value={state.llmTemperature}
                  onChange={e => setField('llmTemperature', parseFloat(e.target.value))}
                  className="w-full accent-indigo-500"
                />
                <div className="flex justify-between text-[10px] mt-1" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>
                  <span>0.0</span><span>1.0</span>
                </div>
              </div>
              <div>
                <label className="text-xs mb-1.5 block" style={{ color: '#64748b' }}>API Key</label>
                <input
                  type="password"
                  value={state.llmApiKey}
                  onChange={e => setField('llmApiKey', e.target.value)}
                  placeholder="sk-..."
                  className="w-full px-3 py-2 rounded-lg text-sm outline-none"
                  style={inputStyle}
                />
              </div>
            </div>
          )}
        </div>

        {/* Threshold Presets */}
        <div className="rounded-xl p-5" style={cardStyle}>
          <h4 className="text-sm font-semibold mb-3" style={{ color: '#c7d2fe' }}>Threshold Presets</h4>
          <div className="flex gap-2 mb-4">
            {(['strict', 'standard', 'lenient'] as ThresholdPreset[]).map(preset => {
              const selected = state.thresholdPreset === preset
              return (
                <button
                  key={preset}
                  onClick={() => setField('thresholdPreset', preset)}
                  className="px-4 py-2 rounded-lg text-xs font-medium capitalize transition-all"
                  style={{
                    background: selected ? '#6366f1' : '#0a0a14',
                    color: selected ? '#fff' : '#64748b',
                    border: `1px solid ${selected ? '#6366f1' : '#1e1e3a'}`,
                  }}
                >
                  {preset}
                </button>
              )
            })}
          </div>
          <div className="grid grid-cols-4 gap-3">
            {[
              { label: 'P0', value: thresholds.p0 },
              { label: 'P1', value: thresholds.p1 },
              { label: 'P2', value: thresholds.p2 },
              { label: 'Overall', value: thresholds.overall },
            ].map(t => (
              <div key={t.label} className="rounded-lg p-3 text-center" style={{ background: '#0a0a14' }}>
                <div className="text-[10px] mb-1" style={{ color: '#94a3b8', fontFamily: 'Space Mono, monospace' }}>{t.label}</div>
                <div className="text-lg font-bold" style={{ color: '#c7d2fe', fontFamily: 'Space Mono, monospace' }}>{t.value}%</div>
              </div>
            ))}
          </div>
        </div>

        {/* Scaffolding Progress */}
        <div className="rounded-xl p-5" style={cardStyle}>
          <h4 className="text-sm font-semibold mb-3" style={{ color: '#c7d2fe' }}>Scaffolding Progress</h4>
          <div className="space-y-2">
            {scaffoldSteps.map((step, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span style={{ color: '#10b981' }}>{'\u2713'}</span>
                <span style={{ color: '#94a3b8' }}>{step}</span>
              </div>
            ))}
            {scaffoldSteps.length === 0 && (
              <div className="text-xs" style={{ color: '#94a3b8' }}>Preparing scaffolding...</div>
            )}
          </div>
        </div>

        {/* Test Generation */}
        {state.scaffoldingDone && !state.generationDone && (
          <div className="rounded-xl p-5" style={cardStyle}>
            <h4 className="text-sm font-semibold mb-3" style={{ color: '#c7d2fe' }}>Test Generation</h4>
            {generationProgress === 0 ? (
              <button
                onClick={handleGenerateTests}
                className="w-full py-3 rounded-xl text-sm font-semibold transition-all"
                style={{
                  background: 'linear-gradient(135deg, #6366f1, #8b5cf6)',
                  color: '#fff',
                }}
                onMouseEnter={e => (e.currentTarget.style.filter = 'brightness(1.15)')}
                onMouseLeave={e => (e.currentTarget.style.filter = 'brightness(1)')}
              >
                Generate Test Suite from Requirements
              </button>
            ) : (
              <div>
                <div className="flex items-center justify-between text-xs mb-2">
                  <span style={{ color: '#94a3b8' }}>Processing requirements...</span>
                  <span style={{ color: '#6366f1', fontFamily: 'Space Mono, monospace' }}>{generationProgress}%</span>
                </div>
                <div className="h-2 rounded-full overflow-hidden" style={{ background: '#1e1e3a' }}>
                  <div
                    className="h-full rounded-full transition-all duration-150"
                    style={{ width: `${generationProgress}%`, background: 'linear-gradient(90deg, #6366f1, #8b5cf6)' }}
                  />
                </div>
              </div>
            )}
          </div>
        )}

        {/* Generated tests summary */}
        {generatedTests && (
          <div className="rounded-xl p-5" style={cardStyle}>
            <h4 className="text-sm font-semibold mb-1" style={{ color: '#c7d2fe' }}>Generated Test Suite</h4>
            <p className="text-xs mb-4" style={{ color: '#64748b' }}>
              {generatedTests.length} test scenarios generated
            </p>
            <div className="grid grid-cols-2 gap-2">
              {generatedTests.map((t, i) => {
                const tc = typeColors[t.type] || { bg: 'rgba(100,116,139,0.15)', color: '#94a3b8' }
                return (
                  <div key={i} className="flex items-center gap-2 px-3 py-2 rounded-lg" style={{ background: '#0a0a14' }}>
                    <span className="flex-1 text-xs truncate" style={{ color: '#94a3b8' }}>{t.name}</span>
                    <span className="text-[10px] px-2 py-0.5 rounded-full flex-shrink-0" style={{ background: tc.bg, color: tc.color }}>
                      {t.type}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* First Run */}
        {state.generationDone && !state.firstRunDone && !runningSmoke && runResults.length === 0 && (
          <div className="rounded-xl p-5" style={cardStyle}>
            <button
              onClick={handleRunSmoke}
              className="w-full py-3 rounded-xl text-sm font-semibold transition-all"
              style={{
                background: 'linear-gradient(135deg, #10b981, #06b6d4)',
                color: '#fff',
              }}
              onMouseEnter={e => (e.currentTarget.style.filter = 'brightness(1.15)')}
              onMouseLeave={e => (e.currentTarget.style.filter = 'brightness(1)')}
            >
              Run Smoke Suite (P0)
            </button>
          </div>
        )}

        {/* Running / Results */}
        {(runningSmoke || runResults.length > 0) && (
          <div className="rounded-xl overflow-hidden" style={{ border: '1px solid #1e1e3a' }}>
            {/* Mini pipeline */}
            <div className="px-4 py-2 flex items-center gap-1" style={{ background: '#0a0a14' }}>
              {TEA_STAGES_MINI.map((stage, i) => {
                const stageNum = i + 1
                const isComplete = stageNum <= pipelineStage
                return (
                  <div key={stage} className="flex-1 flex flex-col items-center">
                    <div className="w-full h-1 rounded-full mb-1" style={{
                      background: isComplete ? '#6366f1' : '#1e1e3a',
                    }} />
                    <span className="text-[8px] font-mono" style={{
                      color: isComplete ? '#818cf8' : '#94a3b8',
                    }}>
                      {stage}
                    </span>
                  </div>
                )
              })}
            </div>

            {/* Test results */}
            <div className="p-3 space-y-1" style={{ background: '#12121f' }}>
              {runResults.map((r, i) => (
                <div key={i} className="flex items-center gap-3 px-3 py-2 rounded-lg text-xs" style={{ background: '#0a0a14' }}>
                  <span
                    className="font-bold w-10 text-center px-1 py-0.5 rounded flex-shrink-0"
                    style={{
                      background: r.status === 'PASS' ? 'rgba(52,211,153,0.15)' : 'rgba(251,113,133,0.15)',
                      color: r.status === 'PASS' ? '#34d399' : '#fb7185',
                      fontFamily: 'monospace',
                    }}
                  >
                    {r.status}
                  </span>
                  <span className="flex-1 truncate" style={{ color: '#94a3b8' }}>{r.title}</span>
                </div>
              ))}
              {runningSmoke && runResults.length === 0 && (
                <div className="text-center py-4 text-xs" style={{ color: '#94a3b8' }}>
                  Running pipeline...
                </div>
              )}
            </div>

            {/* Summary */}
            {runSummary && (
              <div className="px-4 py-3 flex items-center gap-4 text-xs" style={{ background: '#0d0d18', borderTop: '1px solid #1e1e3a' }}>
                <span style={{ color: '#34d399', fontFamily: 'monospace' }}>{runSummary.passed} passed</span>
                <span style={{ color: '#fb7185', fontFamily: 'monospace' }}>{runSummary.failed} failed</span>
                <span style={{ color: '#fbbf24', fontFamily: 'monospace' }}>{runSummary.skipped} skipped</span>
                <span style={{ color: '#a5b4fc', fontFamily: 'monospace' }}>{runSummary.coverage}% coverage</span>
                <span className="ml-auto font-bold" style={{
                  color: runSummary.grade === 'A' ? '#34d399' : runSummary.grade === 'B' ? '#a5b4fc' : '#fbbf24',
                  fontFamily: 'Space Mono, monospace',
                }}>
                  Grade {runSummary.grade}
                </span>
              </div>
            )}
          </div>
        )}

        {/* Completion actions */}
        {state.firstRunDone && (
          <div className="space-y-3">
            {state.cicdProvider && (
              <div className="text-center">
                <span className="text-[10px] px-3 py-1 rounded-full" style={{ background: 'rgba(16,185,129,0.15)', color: '#10b981', border: '1px solid rgba(16,185,129,0.3)' }}>
                  CI/CD pipeline committed. Tests will run on every push
                </span>
              </div>
            )}
            <button
              onClick={handleFinish}
              className="w-full py-4 rounded-xl text-sm font-semibold transition-all"
              style={{
                background: 'linear-gradient(135deg, #6366f1, #8b5cf6)',
                color: '#fff',
              }}
              onMouseEnter={e => (e.currentTarget.style.filter = 'brightness(1.15)')}
              onMouseLeave={e => (e.currentTarget.style.filter = 'brightness(1)')}
            >
              Go to Dashboard
            </button>
            <div className="text-center">
              <button
                onClick={() => router.push('/test-explorer')}
                className="text-xs transition-colors"
                style={{ color: '#64748b', background: 'none', border: 'none', cursor: 'pointer' }}
                onMouseEnter={e => (e.currentTarget.style.color = '#94a3b8')}
                onMouseLeave={e => (e.currentTarget.style.color = '#64748b')}
              >
                View Test Explorer
              </button>
            </div>
          </div>
        )}
      </div>
    )
  }

  const stepRenderers: Record<number, () => React.ReactNode> = {
    1: renderStep1,
    2: renderStep2,
    3: renderStep3,
    4: renderStep4,
    5: renderStep5_ConnectSUT,
    6: renderStep6,
    7: renderStep7,
    8: renderStep8,
  }

  // ── Main Render ─────────────────────────────────────────────────────────────

  const isOptionalStep = OPTIONAL_STEPS.includes(state.step)
  const nextLabel = state.step === 1 ? 'Create Project' : state.step === TOTAL_STEPS ? 'Finish' : 'Next'

  return (
    <div className="min-h-screen flex flex-col" style={{ background: '#05050a' }}>
      {/* Header */}
      <div className="py-6 px-4 text-center">
        <button
          onClick={() => router.push('/')}
          className="inline-flex items-center gap-2 text-lg font-bold transition-opacity hover:opacity-80"
          style={{ color: '#e2e8f0', fontFamily: 'DM Sans, sans-serif' }}
        >
          <span className="text-2xl" style={{ color: '#6366f1' }}>{'\u25C8'}</span>
          ARTA
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 px-4 pb-32">
        <div className="max-w-3xl mx-auto">
          <StepIndicator currentStep={state.step} />

          {/* Step title */}
          <div className="mb-8">
            <h2 className="text-xl font-bold mb-1" style={{ color: '#e2e8f0', fontFamily: 'DM Sans, sans-serif' }}>
              {STEP_LABELS[state.step - 1]}
            </h2>
            <p className="text-sm" style={{ color: '#64748b' }}>
              {state.step === 1 && 'Set up your project basics'}
              {state.step === 2 && 'Choose your project type and engagement model'}
              {state.step === 3 && 'Connect your requirements sources'}
              {state.step === 4 && 'Link your source code repository'}
              {state.step === 5 && 'Auto-discover SUT auth, list endpoints, and routes'}
              {state.step === 6 && 'Configure deployment environments and CI/CD'}
              {state.step === 7 && 'Set up notification channels'}
              {state.step === 8 && 'Generate tests and run your first pipeline'}
            </p>
          </div>

          {/* Step content */}
          {stepRenderers[state.step]?.()}
        </div>
      </div>

      {/* Navigation (fixed bottom) */}
      <div className="fixed bottom-0 left-0 right-0 px-4 py-4" style={{ background: 'linear-gradient(transparent, #05050a 30%)', zIndex: 50 }}>
        <div className="max-w-3xl mx-auto flex items-center justify-between">
          <div>
            {state.step > 1 && (
              <button
                onClick={handleBack}
                className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium transition-all"
                style={{ color: '#94a3b8', border: '1px solid #1e1e3a', background: '#0a0a14' }}
                onMouseEnter={e => (e.currentTarget.style.borderColor = '#334155')}
                onMouseLeave={e => (e.currentTarget.style.borderColor = '#1e1e3a')}
              >
                {'\u2190'} Back
              </button>
            )}
          </div>

          <div className="flex items-center gap-4">
            {isOptionalStep && (
              <button
                onClick={handleSkip}
                className="text-xs transition-colors"
                style={{ color: '#94a3b8', background: 'none', border: 'none', cursor: 'pointer' }}
                onMouseEnter={e => (e.currentTarget.style.color = '#94a3b8')}
                onMouseLeave={e => (e.currentTarget.style.color = '#94a3b8')}
              >
                Skip this step
              </button>
            )}

            {/* Hide next/finish on step 7 when first run is done (show Go to Dashboard in content) */}
            {!(state.step === TOTAL_STEPS && state.firstRunDone) && (
              <button
                onClick={state.step === TOTAL_STEPS ? handleFinish : handleNext}
                disabled={!canProceed() || loading}
                className="flex items-center gap-2 px-6 py-2.5 rounded-xl text-sm font-semibold transition-all"
                style={{
                  background: canProceed() && !loading ? 'linear-gradient(135deg, #6366f1, #8b5cf6)' : '#1e1e3a',
                  color: canProceed() && !loading ? '#fff' : '#94a3b8',
                  cursor: canProceed() && !loading ? 'pointer' : 'not-allowed',
                  opacity: loading ? 0.7 : 1,
                }}
                onMouseEnter={e => {
                  if (canProceed() && !loading) e.currentTarget.style.filter = 'brightness(1.15)'
                }}
                onMouseLeave={e => { e.currentTarget.style.filter = 'brightness(1)' }}
              >
                {loading ? 'Working...' : nextLabel} {state.step < TOTAL_STEPS && '\u2192'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
