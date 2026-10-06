# Contributing

Thanks for contributing to `AlpacaPlatform`.

## Ground Rules

- Prefer small, low-risk pull requests.
- Keep refactors separate from behavior changes.
- Add or update tests when changing runtime behavior.
- Do not use deployment or scheduled workflows as a substitute for local verification.
- Changes touching paper/live admission, credentials, risk-gate decisions, receipt stores, or any future broker-facing adapter must be verified with the existing contract tests first; a passing CI run does not by itself authorize enabling a new execution path.

## Branching and Pull Requests

- Create a topic branch for each change.
- Open a pull request with a short summary and a concrete test plan.
- Wait for CI to pass before merging.

## Local Verification

Run the checks CI uses before opening a pull request:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```
