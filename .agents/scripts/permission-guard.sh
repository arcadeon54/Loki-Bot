#!/usr/bin/env bash
# PreToolUse guard for Antigravity (agy) on dex247.
#
# Reads the tool call as JSON on stdin and emits {"decision": ...} on stdout.
# Decisions: allow | ask | force_ask | deny  (see the agy hooks contract).
#
# This is a BACKSTOP for the policy in .agents/rules/production-safety.md, not a
# substitute for the agent behaving correctly. It is deliberately conservative:
# anything it cannot classify falls through to agy's normal prompting, so a
# parsing failure can never silently widen permissions.
set -uo pipefail

payload="$(cat)"

# Pull the command line out of the tool call without requiring jq.
cmd="$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
args = (d.get("toolCall") or {}).get("args") or {}
for k in ("CommandLine", "commandLine", "command", "Command"):
    v = args.get(k)
    if isinstance(v, str) and v.strip():
        print(v); break
else:
    print("")
' 2>/dev/null)"

# Nothing parseable: defer to agy rather than guessing.
if [ -z "$cmd" ]; then
    printf '{"decision":"allow"}\n'
    exit 0
fi

deny() { printf '{"decision":"deny","reason":"%s"}\n' "$1"; exit 0; }
ask()  { printf '{"decision":"ask","reason":"%s"}\n'  "$1"; exit 0; }

# ── DENY ────────────────────────────────────────────────────────────────────
case "$cmd" in
    *"git push"*|*"git "*" push"*)
        deny "git push is Boss-approved only. See .agents/rules/git-policy.md." ;;
    *"git push --force"*|*"push -f"*)
        deny "Force push is never permitted." ;;
    *"--no-verify"*)
        deny "Skipping git hooks is not permitted." ;;
    *"rm -rf /"*|*"rm -rf ~"*|*"rm -fr /"*)
        deny "Recursive destructive delete of a root or home path is blocked." ;;
    *"docker system prune"*|*"docker volume prune"*|*"docker image prune"*|*"docker container prune"*)
        deny "Docker prune destroys rollback targets and is never permitted." ;;
    *"docker volume rm"*)
        deny "Volume deletion runs only inside an approval-gated decommission." ;;
    *"sshpass"*)
        deny "sshpass is never permitted; use key auth." ;;
    *"StrictHostKeyChecking=no"*|*"StrictHostKeyChecking no"*)
        deny "Host-key verification must not be disabled." ;;
    *"ssh root@"*|*"@"*" -l root"*)
        deny "Root SSH is not permitted." ;;
    *"authorized_keys"*|*".ssh/config"*|*".ssh/id_"*)
        deny "Modifying SSH key material or config is not permitted." ;;
    *".env"*)
        case "$cmd" in
            *".env.example"*) : ;;                      # names-only reference is fine
            *cat*|*less*|*more*|*head*|*tail*|*grep*|*strings*|*cp*|*scp*|*curl*|*base64*)
                deny "Reading or copying .env is blocked; use variable names only." ;;
        esac ;;
    *"usermod"*"docker"*|*"gpasswd"*"docker"*)
        deny "Adding an account to the docker group is root-equivalent." ;;
    *"chmod 777"*|*"chmod -R 777"*)
        deny "World-writable permissions are not permitted." ;;
esac

# ── ASK ─────────────────────────────────────────────────────────────────────
case "$cmd" in
    *"systemctl restart"*|*"systemctl stop"*|*"systemctl start"*|\
    *"systemctl disable"*|*"systemctl enable"*|*"systemctl mask"*)
        ask "Service state change needs Boss approval." ;;
    *"docker restart"*|*"docker stop"*|*"docker rm"*|*"docker compose up"*|\
    *"docker compose down"*|*"docker pull"*)
        ask "Container mutation needs Boss approval." ;;
    *"git commit"*|*"git merge"*|*"git rebase"*|*"git checkout"*|*"git reset"*|\
    *"git stash"*|*"git pull"*|*"git switch"*)
        ask "Git state change needs Boss approval; discarding work may delete live production code." ;;
    *"pip install"*|*"apt install"*|*"apt-get install"*|*"npm install"*)
        ask "Package installation needs Boss approval." ;;
    *"ssh nas-maint"*|*"loki-nas-maint"*)
        ask "NAS dispatcher action — confirm it is read-only." ;;
    *"ssh razr"*)
        ask "Remote action on razr needs confirmation." ;;
    "sudo "*|*" sudo "*)
        ask "Privileged command — confirm scope." ;;
    *"rm -rf"*|*"rm -r "*)
        ask "Recursive delete — confirm the target." ;;
esac

printf '{"decision":"allow"}\n'
