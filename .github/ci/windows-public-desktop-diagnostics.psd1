@{
    Isolated = @{
        'diagnose-index' = @{
            file = 'src/main/index.test.ts'
            cases = @(
                'a native folder selection belongs to its conversation and cannot authorize another conversation'
                'a delivered artifact opens a fixed current snapshot only in its owning active conversation'
            )
            sources = @('desktop/src/main/index.ts', 'desktop/src/main/python-service.ts', 'tests/desktop_fixture.py')
        }
        'diagnose-lifecycle' = @{
            file = 'src/main/lifecycle.test.ts'
            cases = @(
                'real hidden desktop closes, reopens and waits for owned work; blocked save=false'
                'real hidden desktop closes, reopens and waits for owned work; blocked save=true'
            )
            sources = @('desktop/tests/lifecycle.electron.mjs', 'desktop/tests/reopen.electron.mjs', 'desktop/tests/desktop-process.ts',
                'desktop/tests/process-observation.mjs', 'tests/desktop_fixture.py')
        }
        'diagnose-exit-acceptance' = @{
            file = 'src/main/exit-acceptance.integration.test.ts'
            cases = @(
                'real desktop exits and reopens without rerunning owned work: running'
                'real desktop exits and reopens without rerunning owned work: approval'
                'real desktop exits and reopens without rerunning owned work: timeout'
            )
            sources = @('desktop/tests/exit-acceptance.electron.mjs', 'desktop/tests/desktop-process.ts',
                'desktop/tests/process-observation.mjs', 'tests/exit_acceptance_fixture.py', 'tests/support/observed_check_runner.py')
        }
    }
    PythonService = @{
        file = 'src/renderer/App.integration.test.tsx'
        sources = @('desktop/src/renderer/App.tsx', 'desktop/src/main/python-service.ts',
            'tests/desktop_fixture.py', 'myrk/desktop_service.py')
        cases = @(
            'a new chat inherits saved default approval settings after configuring a temporary connection'
            'desktop keeps saved chat choices apart from the global default and reopens them without a password'
            'desktop saves its request approval default without a service password and keeps it after restarting the service'
            'the desktop screen authorizes a real folder, shows sourced results, and reopens its evidence (long material: false)'
            'the desktop screen authorizes a real folder, shows sourced results, and reopens its evidence (long material: true)'
            'the real desktop routes concurrent file work, acknowledges supplements, queues work and stops only its conversation'
            'the real desktop shows on-demand skill selection delegation and main artifact verification'
            'the user trusts a folder, edits real text, calculates, and accepts work after the selected check actually ran'
        )
    }
}
