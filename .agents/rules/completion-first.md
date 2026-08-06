# Completion-First

One requested user-facing objective is active at a time. It stays active until
it is **DONE** or the Boss explicitly **parks** it.

## While an objective is active

- Fix implementation defects you hit **in the requested path**, run focused
  validation, and continue toward the original goal. A defect is a step, not a
  stopping point.
- Do not endlessly audit.
- Do not restart completed investigations.
- Do not branch into unrelated tasks because they look easy or interesting.
- Do not stop after proposing an architecture. Build it.
- Do not stop after creating directories or writing documentation. Those are
  not the deliverable unless the Boss said they were.

## Completion is not "tests pass"

Passing unit tests is evidence, not completion. Completion is the stated DONE
condition met and verified against live behaviour. This project has repeatedly
had green tests over broken behaviour.

State the DONE condition **before** you start, record it in
`docs/agent-context/TASK_LEDGER.md`, and hold to it.

## When to interrupt the Boss

Only for:

- credentials or authentication
- unavoidable privileged bootstrap
- physical action
- destructive approval
- security-sensitive approval
- a genuinely ambiguous high-risk decision

Everything else, decide and proceed. If you must interrupt, do the work that
does **not** depend on the answer first, then ask one precise question with the
exact action required.

## Reporting

Report faithfully. If a step was skipped, say so. If something failed, show the
output. If the system contradicts what you were told happened, say that plainly
with evidence — a claimed success the system contradicts is worse than a
reported failure.

When work is genuinely done and verified, say so plainly without hedging.

## Scope

The requested scope is the deliverable. Do not quietly narrow it, widen it, or
transform it. If part of the scope is blocked, finish everything else in full
and say explicitly what was left out and why — scaling work down is the Boss's
call, not yours.
