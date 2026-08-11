'use client'

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { fetchProjects, type Project } from './api-client'

interface ProjectState {
  projects: Project[]
  currentProjectId: string | null
  currentProject: Project | null
  isLoading: boolean
  switchProject: (id: string) => void
}

const ProjectContext = createContext<ProjectState>({
  projects: [],
  currentProjectId: null,
  currentProject: null,
  isLoading: true,
  switchProject: () => {},
})

const STORAGE_KEY = 'arta_project'

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([])
  const [currentProjectId, setCurrentProjectId] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    fetchProjects()
      .then(data => {
        const list: Project[] = data.projects || data
        setProjects(list)
        // Restore from localStorage or default to first project
        const stored = localStorage.getItem(STORAGE_KEY)
        const valid = list.find(p => p.id === stored)
        setCurrentProjectId(valid ? stored : list[0]?.id ?? null)
      })
      .catch(() => {})
      .finally(() => setIsLoading(false))
  }, [])

  const switchProject = (id: string) => {
    setCurrentProjectId(id)
    localStorage.setItem(STORAGE_KEY, id)
  }

  const currentProject = projects.find(p => p.id === currentProjectId) ?? null

  return (
    <ProjectContext.Provider value={{ projects, currentProjectId, currentProject, isLoading, switchProject }}>
      {children}
    </ProjectContext.Provider>
  )
}

export function useProject() {
  return useContext(ProjectContext)
}
