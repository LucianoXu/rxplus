# rxplus

`rxplus` is a Python library that provides a collection of extensions for the `reactivex` library.

## Build & Development

- Python: 3.13
- Environment: `conda activate rxplus`
- Pre-commit hooks: `pre-commit install`
- Test: `pytest --cov=rxplus --cov-report=term-missing`
- Lint: `ruff check .`
- Format: `ruff format .`
- Type check: `mypy rxplus`

## Coding Style

- Formatting/linting: ruff + mypy
- Avoid `Any` types; use explicit type annotations
- Keep files under ~700 LOC; split when it improves clarity
- Add brief comments for non-obvious logic

## Commit Guidelines

- Write concise, descriptive commit messages
- Run `pre-commit` before committing
- Keep PRs focused on a single concern
