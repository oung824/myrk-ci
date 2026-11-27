import type { ChildProcess } from 'node:child_process'

export interface ProcessIdentity {
  pid: number
  created_filetime: string | null
  image: string | null
}
export interface Observation {
  pid: number
  state: 'running' | 'exited' | 'unknown'
  ownership: 'owned' | 'excluded' | 'unknown'
  identity: ProcessIdentity | null
  completed: boolean | null
  exit_code: number | null
  reasons: string[]
  errors: { operation: string; code: number; parameters: unknown[] }[]
  expected: ProcessIdentity | null
  direct_spawn: { identity: ProcessIdentity; launcher_pid: number; argv: string[]; cwd: string; source: 'caller_spawn' | 'fixture_receipt' } | null
  parent: { identity: ProcessIdentity; reported_parent_pid: number } | null
}
export interface ObservationReport { complete: boolean; processes: Observation[] }
export class ProcessObservation {
  static start(directory: string): Promise<ProcessObservation>
  report: ObservationReport
  capture(child: ChildProcess, argv: string[], cwd: string): Promise<Observation>
  captureReceipts(directory: string): Promise<ObservationReport>
  status(): Promise<ObservationReport>
  wait(timeoutMs: number): Promise<ObservationReport>
  close(): Promise<ObservationReport>
}
