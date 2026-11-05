# Contributing

Baler is AGPLv3 (see [LICENSE](LICENSE)): you're always free to
fork it and self-host, no agreement needed. Contributing changes back
upstream is a separate thing and requires signing the
[CLA](CLA.md) first — it lets the project (Jacob Nash, its maintainer)
offer hosting/SLA support on top of the same code without splitting the
project into a separate "open" and "paid" fork.

## Before you open a PR

1. Sign the CLA — comment on your first PR and the CLA bot will walk you
   through it (or read [CLA.md](CLA.md) and email a signed copy per its
   instructions).
2. `pip install -e ".[dev,validate]"`
3. `python -m pytest -m "not integration"` — see [README.md](README.md#tests)
   for the full test commands, including the integration suite.
4. `ruff check src/ tests/` and `mypy src/baler` — both block CI
   (`.github/workflows/ci.yml`), zero findings, no loosening the gate. Scoped
   `# noqa` with a reason is fine; blanket suppression isn't.
5. Touching `gateway/njs/`? Run `node gateway/njs/test_jwt.mjs && node
   gateway/njs/test_policy.mjs` too — see CLAUDE.md for the njs gotchas
   (ES-module-only, no destructuring/spread/classes at nginx's njs 1.0.0).

Optional: `pip install pre-commit && pre-commit install` runs `ruff --fix`
on commit.

## PRs

Keep them scoped to one change. If it adds a feature, it needs a docs
page or API doc too (see `docs-site/`) — undocumented features aren't
considered done.
