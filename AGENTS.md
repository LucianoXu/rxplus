# Repository Guidelines

- Repo: https://github.com/LucianoXu/rxplus.git
- GitHub issues/comments/PR comments: use literal multiline strings or `-F - <<'EOF'` (or $'...') for real newlines; never embed "\\n".


## Project Structure & Module Organization
- Source code: `rxplus/`.
- Docs: `docs/`.
- Tasks that can be started by `python -m ...` are in `tasks/`.
- Tests: `tests/`.
- Information for Agents: `ai-instru/`. It includes Agent skills, remote server information, Grafana stacks and running projects info.


## Build, Test, and Development Commands
- Pre-commit hooks: `prek install` (runs same checks as CI)
- Test with coverage: `pytest --cov=rxplus --cov-report=term-missing`


## Coding Style & Naming Conventions
- Formatting/linting using `mypy`: `conda run -n turbo mypy`.
- Avoid ambiguous typing (e.g., `Any`) whenever possible. Don't abuse `# type: ignore`. Try to build clear and informative typing notations.
- Follow the reactive programming paradigm.
- Aim to keep files under ~700 LOC; guideline only (not a hard guardrail). Split/refactor when it improves clarity or testability.
- Add brief code comments for tricky or non-obvious logic.
- **Logging**: Use OpenTelemetry via `rxplus/telemetry.py` (`OTelLogger`), never `print` or `logging` directly. Refer to `ai-instru` for the Grafana endpoint of dhproto.
- **Documentation**: After implementing any new component or instance, add a doc in `docs/` covering: introduction, arguments, reactive components, dataflow graph, sink/stream data types, log records, potential vulnerabilities.


## Commit & Pull Request Guidelines
- You should also include the design proposal/specification files in the commit.
- After pushing the commit, you should use `gh` to watch the CI result and make sure it passes.
- When required to use `PR` mode, or the development work is heavy, you should work in a `git` worktree in a separated branch, and contribute to the code by making pull requests.

## Agent-Specific Notes
- When adding a new `AGENTS.md` anywhere in the repo, also add a `CLAUDE.md` symlink pointing to it (example: `ln -s AGENTS.md CLAUDE.md`).
- When working on a GitHub Issue or PR, print the full URL at the end of the task.
- When answering questions, respond with high-confidence answers only: verify in code; do not guess.
- Lint/format churn:
  - If staged+unstaged diffs are formatting-only, auto-resolve without asking.
  - If commit/push already requested, auto-stage and include formatting-only follow-ups in the same commit (or a tiny follow-up commit if needed), no extra confirmation.
  - Only ask when changes are semantic (logic/data/behavior).
- **NEVER** change `devlop.md`. This is for human notes.
- **NEVER** commit any real ip-address, api-keys or other security related information. Use obviously fake placeholders in the documentations.
