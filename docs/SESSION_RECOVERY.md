# Session Recovery — beginner-friendly guide

How to get back into a safe Claude Code session on Loki from anywhere
(e.g. the Chromebook). No secrets in this file.

## 1. SSH into dex247

Open the Linux terminal (Chromebook: Launcher → Terminal) and run:

```bash
ssh dex247
```

If `ssh dex247` isn't configured on this device, use the Tailscale IP:

```bash
ssh g2k247@100.68.187.69
```

You're in the right place when the prompt says `g2k247@dex247`.

## 2. Enter the Loki repository

```bash
cd /home/g2k247/loki-bot
```

## 3. Check Git status (read-only, always safe)

```bash
git status
git log --oneline -5
```

Normal state: branch `master`, and `ha_integration.py` may show as modified —
**that change is running in production; do not discard it.**

## 4. Check Loki is alive (read-only)

```bash
systemctl status loki        # want: Active: active (running)
journalctl -u loki -n 30     # recent log lines
tail -30 loki_bot.log        # bot's own log file
```

## 5. Start Claude Code

```bash
claude
```

It reads `CLAUDE.md` in this directory automatically and knows the safety
rules. State what you want in plain language.

## 6. End a session

- Type `/exit` (or press Ctrl+C twice) to leave Claude Code.
- Type `exit` to close the SSH connection.
- Loki keeps running — it's a systemd service, independent of your session.

## 7. Resume later

Just repeat steps 1–5. Claude Code sessions can also be resumed with
`claude --resume` (pick the previous conversation from the list).

## Things to AVOID typing

| Never run casually | Why |
|---|---|
| `git reset --hard`, `git checkout -- .`, `git clean` | Destroys uncommitted production code |
| `git push --force`, `git commit`, `git merge`, `git pull` | Only with explicit approval |
| `sudo systemctl restart loki` / `stop loki` | Restarts/kills the live bot |
| `docker restart …`, `docker compose up/down` | Touches production containers |
| `rm` anything in `~/loki-bot` or `~/skillkit` | No deletions without approval |
| `/model` inside Claude Code | Don't switch models unless you mean to |
| `cat .env` (or any `.env.bak*`) | Prints live secrets to screen |

## Safe read-only helpers

```bash
~/skillkit/bin/skillkit list          # what skills exist
~/skillkit/bin/skillkit incidents     # recent incident records
~/skillkit/bin/skillkit approvals     # anything waiting for approval
sqlite3 "file:jobsite.db?mode=ro" "SELECT COUNT(*) FROM work_sessions;"
```

## If Loki seems down

1. `systemctl status loki` — if not running, note the error.
2. `journalctl -u loki -n 100` — read the crash reason.
3. Ask Claude Code to diagnose (read-only first). Restarting
   (`sudo systemctl restart loki`) is the fix of last resort and is safe to
   run only when you understand why it stopped.
