# ForgeAgent

ForgeAgent is a small, inspectable local coding agent implemented from scratch. It communicates
with an OpenAI-compatible model, lets the model call locally executed structured tools, and runs
an explicit model → action → observation loop until the task is completed or a deterministic
limit is reached.

No agent framework or server-hosted file/code-execution service is used. The agent loop, model
response parsing, tool registry, workspace safety, context budgeting, compaction, termination,
error handling, persistence, and evidence-based completion are implemented in this repository.

## Features

- Provider-neutral model interface with OpenAI-compatible tool calling.
- Workspace-scoped, line-numbered file reading, ignored-path-aware search, and repository outline.
- Atomic writes, SHA-256 conflict checks, persistent snapshots, unified diff, and cross-restart undo.
- Independent local command execution with timeout, process-tree cleanup, exit codes, and bounded
  output. Interactive programs and long-running services are refused instead of hanging.
- Plan mode that advertises only read-only tools and can continue in Build mode after approval.
- Explainable command risk classification and interactive approval, including session-scoped
  remembered approvals.
- Four-layer context: stable instructions, project context, compacted memory, and recent verbatim
  history.
- Deterministic token estimation and structured context compaction.
- SQLite WAL storage for messages, events, compactions, edit transactions, claims, and evidence.
- Session resume, no-side-effect trajectory replay with optional `--speed`, and redacted JSONL
  export.
- Pre-publication scanning for secrets, identity paths, databases, environment files, and PDFs.
- Evidence-backed completion: verification results are valid only for the latest workspace
  version. Failed verification becomes a structured debug hypothesis and is retired after two
  identical experiments.
- Automatic verification after edits when a preferred or inferred command is available.
- Local React chat UI (shadcn-style) served by FastAPI, with live WebSocket events, unified diff,
  verification evidence, context compaction, session resume, cancellation, and non-blocking approval.
- Offline `forge eval` with FakeModel and AgentLoop sample cases.
- Deterministic `FakeModel` for offline tests.

## Requirements

- Python 3.12 or newer.
- An OpenAI-compatible chat-completions endpoint that supports native function/tool calling.
- PowerShell on Windows or a POSIX shell on Linux/macOS.
- Git is optional; `git_diff` and `git_status` report a normal tool error when Git is unavailable.

## Installation

Create and activate an isolated virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Set credentials through environment variables. Never place real values in `.env.example`, source
code, logs, screenshots, or commits.

```powershell
$env:FORGE_API_KEY = "..."
$env:FORGE_BASE_URL = "https://your-compatible-endpoint/v1"
$env:FORGE_MODEL = "your-model"
```

`FORGE_BASE_URL` is optional when using the client's default endpoint.

## Usage

### Graphical interface

Start the dashboard:

```powershell
forge-gui
```

Open `http://127.0.0.1:8080` if the browser does not open automatically. The server binds only to
the loopback interface. The API key remains in the process environment and is never placed in a
form or browser state.

The left sidebar lists sessions. The center column is a chat: your task, tool steps, and the
model's final answer. The right panels show unified diffs, version-matched verification evidence,
and context compaction. Medium-risk operations pause without blocking the web server until Allow
or Reject is selected. Stop cancels the active model call or command and cleans up its process tree.

To rebuild the frontend after changing files under `web/`:

```powershell
cd web
npm install
npm run build
```

Then restart `forge-gui`. Frontend development can use `npm run dev` in `web/` while `forge-gui`
is already running on port 8080.

Demo mode is enabled by default and redacts the workspace path and credential-like values from
event details. It changes presentation only and never substitutes fake model results.

### Command-line interface

Run in a target repository:

```powershell
forge run "Fix the failing unit test and verify the change."
```

Specify another workspace and preferred verification command:

```powershell
forge run `
  --workspace D:\demo\sample-project `
  --verify "python -m pytest -q" `
  "Add input validation and tests."
```

Start in read-only plan mode:

```powershell
forge run --mode plan "Inspect the authentication flow and propose a refactoring plan."
```

Ask for approval and continue the same session in Build mode:

```powershell
forge run --mode plan --build-after-plan "Plan and implement input validation."
```

Medium-risk operations request approval by default. `--auto-approve` removes those prompts for
controlled demonstrations, but high-risk operations remain denied.

Inspect locally persisted trajectories:

```powershell
forge sessions
forge inspect SESSION_ID
forge resume SESSION_ID
forge replay SESSION_ID
forge replay SESSION_ID --speed 4
forge rollback SESSION_ID
```

Run the offline FakeModel evaluation suite without network access:

```powershell
forge eval
forge eval --output eval-report.json
```

Check the local environment without sending an API request:

```powershell
forge doctor
```

Export a redacted trajectory and scan before publication:

```powershell
forge export-events SESSION_ID run.jsonl
forge release-check .
```

## Built-in tools

- `read_file`: read a bounded line range with line numbers, binary detection, and SHA-256.
- `list_files`: list visible workspace entries while respecting `.gitignore`.
- `search_text`: use ripgrep when available and a bounded Python fallback otherwise.
- `repo_outline`: summarize languages, key paths, tests, configuration, and Python symbols.
- `replace_in_file`: replace exactly one matching block atomically.
- `write_file`: create or replace a file atomically with optional hash precondition.
- `undo_last_edit`: restore the latest agent edit when no concurrent change is detected.
- `run_command`: run a local command with timeout and bounded output.
- `verify_changes`: run a test/lint/type/build command and record versioned evidence. The command
  is optional when the project already has an inferred verification command.
- `git_diff`: display staged or unstaged Git changes.
- `git_status`: display porcelain status, untracked files, and insertion/deletion counts.
- `rollback_changes`: restore the current unverified edit group.

Every tool has a Pydantic argument model that becomes its JSON Schema. Unknown tools, invalid
arguments, timeouts, path violations, and command failures are returned to the model as structured
observations instead of crashing the process.

## Architecture

```text
CLI / local React UI
     |
     v
SessionService -- EventBus / ApprovalBroker
     |
     v
AgentLoop ------ ContextBudget / RuntimeContext
     |                    |
     v                    v
ModelClient          structured compaction
     |
 tool_calls
     v
PolicyToolRuntime -- ToolRegistry -- local tools
     |
 observations
     v
SQLite events + frontend renderer + completion evidence
```

The durable session history and model-visible context are deliberately separate. Compaction
shortens only the active context; persisted records are not deleted.

## Safety model

ForgeAgent uses defense in depth:

- All file paths are resolved against the workspace.
- Parent traversal, absolute paths, and resolved symlink escapes are rejected.
- Plan mode disables mutating tools.
- Medium-risk operations require approval.
- Destructive commands, direct `.git` writes, Git pushes, and history rewriting are denied.
- Commands have time and output limits.
- Logs and exported data can be passed through credential/path redaction helpers.

These controls reduce risk but are not an operating-system sandbox. Run untrusted repositories in
a container or restricted account. Review diffs before committing, and never allow the agent to
publish changes automatically.

## Verification

Run the test and quality suite:

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy src
```

The integration suite uses `FakeModel` to deterministically reproduce:

1. file inspection;
2. a precise edit;
3. local verification;
4. evidence-backed completion.

Tests do not require a real API key or network connection.

## Current limitations

- Only OpenAI-compatible chat completions are implemented.
- Commands run on the host; container execution is not yet included.
- The repository map is intentionally lightweight and does not implement a full AST reference
  graph or PageRank.
- Command classification is conservative pattern-based risk reduction, not full shell analysis.
- Text files are currently expected to be UTF-8.
- Long-running servers are refused rather than managed as background jobs.

## Development principles

- Keep the core loop explicit and small.
- Prefer deterministic runtime checks over prompt-only promises.
- Never treat model assertions as verification evidence.
- Preserve original history even when compacting active context.
- Fail closed on workspace boundaries and destructive actions.
- Keep externally visible side effects under human control.

## License

MIT
