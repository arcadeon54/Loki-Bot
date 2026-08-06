# Git Policy

## Push

**Never `git push` without explicit Boss permission for that specific push.**
Permission for one push is not a standing grant. The repository is deliberately
46 commits ahead of `origin/master`; that is a choice, not a backlog.

`git push` is in the DENY tier and is blocked by `.agents/hooks.json`.

## Staging

- Stage specific files by name. **Never `git add -A` or `git add .`.**
- Check `git status` and `git diff` before every commit.
- Keep commits atomic — one logical change per commit.

## Preserve unrelated work

Several dirty or untracked files are live production state. Do not commit them
incidentally and do not revert them:

- `config/homelab_lifecycle.yml` — a generated mirror of the DB table, rewritten
  on every lifecycle change. It goes dirty during normal operation **by
  design**. The DB is authoritative. Never hand-edit it.
- `*.db`, `*.db-wal`, `*.db-shm`, `*_state.json`, `tool_calls.jsonl`,
  `loki_bot.log` — runtime state, gitignored.

If you find unexpected modifications you did not make, stop and report them
rather than absorbing them into your commit.

## Commit messages

Explain **why**, not what — the diff already shows what. State the problem the
change solves and any constraint a future reader would otherwise re-litigate.

## Forbidden

- Force-pushing `master`.
- `--no-verify` to skip hooks.
- Amending published commits.
- Switching branches or merging without approval.
- `git checkout` / `git reset` / `git stash` used to discard uncommitted work.

## Branching

Work happens on `master` in this repository, matching existing practice. Do not
create branches without asking.

## After committing

Report the short hash and the files changed. Do not offer to push; wait to be
asked.
