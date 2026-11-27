import { BaseSequencer, type TestSpecification } from 'vitest/node'
export default class DurationSequencer extends BaseSequencer {
  sort(files: TestSpecification[]): Promise<TestSpecification[]>
}
