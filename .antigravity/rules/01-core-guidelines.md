# Core Operational Guidelines

## Scope & Containment
- Restrict all file reads, modifications, and shell executions strictly to the current workspace root.
- Do NOT introduce unrequested third-party packages or refactor working code modules.
- NEVER delete or alter pre-existing test suites or evaluation scripts.

## Verification Workflow
- Always verify changes locally using test runners (`pytest`, `curl`, or Python scripts) before reporting completion.
- Maintain a clean git status; make targeted, atomic commits for each bug or feature component.
