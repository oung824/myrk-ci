export interface TestRun {
  directory: string
  output: string
  fixtures: string
}
export function createTestRun(source: string, environment?: NodeJS.ProcessEnv): Promise<TestRun & { source: string; environment: NodeJS.ProcessEnv }>
export function testRun(environment?: NodeJS.ProcessEnv): TestRun
export function builtMain(environment?: NodeJS.ProcessEnv): string
export function testDirectory(prefix: string): Promise<string>
