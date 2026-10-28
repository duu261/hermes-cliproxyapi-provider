## What and why

<!-- One or two sentences. Link the issue: Fixes #123 -->

## How it was tested

<!-- Commands you ran and their result. Live tests: describe them, never paste keys, emails or hostnames. -->

## Checklist

- [ ] Tests pass: `python -m unittest discover -s tests` and `python -m unittest discover -s cpa-status/tests`
- [ ] `cmp cpa.py cpa-status/cpa.py` is clean (change both copies together)
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] No new management endpoint, background polling or retry after a 401 (see CONTRIBUTING.md, Safety rules)
- [ ] No keys, tokens, emails, hostnames or IPs in code, tests, docs or this description
- [ ] `AGENTS.md` is not changed (pull requests that change it are closed automatically)
- [ ] The title is a Conventional Commit, 50 characters or fewer (`fix(cpa-status): ...`)
