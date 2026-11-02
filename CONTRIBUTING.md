# Contributing

Thank you for helping improve the Concert Operate Demo Reservation Board.

## Table of contents

1. [Local development setup](#1-local-development-setup)
2. [Branch and pull-request workflow](#2-branch-and-pull-request-workflow)
3. [Commit message format](#3-commit-message-format)
4. [Versioning and changelog](#4-versioning-and-changelog)
5. [Code style guidelines](#5-code-style-guidelines)
6. [Reporting bugs and requesting features](#6-reporting-bugs-and-requesting-features)

---

## 1. Local development setup

**Requirements:** Python 3.10+ and `git`. No other dependencies — the app uses the standard library only.

```bash
# Clone
git clone https://github.com/raghuhulgundi/concert-demo-reservations-cloud.git
cd concert-demo-reservations-cloud

# Create a local config (edit it with your own URLs; sign-in can stay off for local work)
cp config.example.json config.json

# Run
python3 server.py
# Open http://localhost:8080
```

The server picks up `config.json` automatically. Cloud Object Storage is optional for local work —
without `COS_BUCKET`, `COS_ENDPOINT`, and `COS_APIKEY` set, the database lives in `data/reservations.db`
in the repo folder and is lost when you delete it.

**Syntax check (run before every commit):**

```bash
python3 -m py_compile server.py && echo "OK"
```

---

## 2. Branch and pull-request workflow

`main` is always deployable. **Never push directly to `main`.**

```bash
# 1. Start from the latest main
git checkout main
git pull

# 2. Create a short-lived branch
git checkout -b fix/cos-retry-logging   # or feature/..., docs/..., chore/...

# 3. Make your changes, then commit (see section 3 for message format)
git add .
git commit -m "fix: include retry delay in COS save-failed log line"

# 4. Push the branch
git push origin fix/cos-retry-logging

# 5. Open a Pull Request on GitHub and request a review
# 6. After approval, merge with "Squash and merge" to keep main history clean
# 7. Delete the branch after merging
```

**Branch naming:**

| Prefix | Use for |
|---|---|
| `feature/` | New user-visible functionality |
| `fix/` | Bug fix |
| `docs/` | Documentation only |
| `refactor/` | Code change with no behaviour change |
| `chore/` | Build, CI, dependency, or config updates |

---

## 3. Commit message format

This repo follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short summary>

<optional body — explain what changed and why, not how>

<optional footer — e.g. Closes #42>
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `chore`, `test`

**Examples:**

```
feat(auth): support PKCE on providers that require it
fix(cos): cap retry backoff at 5 minutes
docs: document JWT signature decision in Notes section
chore: add CI workflow for syntax checks
```

Keep the summary under 72 characters. Use the body for anything that needs explaining.

---

## 4. Versioning and changelog

This project follows [Semantic Versioning](https://semver.org/):

- **PATCH** (`1.1.x`) — bug fixes and documentation
- **MINOR** (`1.x.0`) — new features, backwards-compatible
- **MAJOR** (`x.0.0`) — breaking changes

**Before opening a PR that introduces a user-visible change:**

1. Add a bullet under `## Unreleased` in `CHANGELOG.md`.
2. If you are cutting a release, rename `## Unreleased` to `## vX.Y.Z (YYYY-MM-DD)` and
   update the `VERSION` file and the `__version__` string in `server.py`.
3. Tag the release after merging: `git tag -a vX.Y.Z -m "Release vX.Y.Z" && git push origin vX.Y.Z`

---

## 5. Code style guidelines

- **Standard library only** — do not add third-party dependencies.
- Follow existing style: 4-space indent, single quotes for strings, `%`-formatting for log messages.
- Use `def` instead of `lambda` for named helpers.
- Add docstrings to new public functions; add inline comments for non-obvious decisions.
- All user input must go through `clean_text()`, `clean_multiline()`, or `clean_links()`.
- All mutating database operations must hold `WRITE_LOCK`.
- All secret comparisons must use `secrets.compare_digest()` or `hmac.compare_digest()`.

---

## 6. Reporting bugs and requesting features

Use the GitHub Issues templates:

- **Bug:** describe what you did, what you expected, and what happened instead. Include the
  `/healthz` output and any relevant log lines from `ibmcloud ce app logs --name reservations`.
- **Feature request:** describe the problem you are trying to solve, not just the solution.

For security issues, **do not open a public issue**. Email the repo owner directly.
