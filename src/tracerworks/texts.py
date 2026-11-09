"""Every sentence tracerworks says to a human, in one place (#850).

English, so that the factory can serve others one day; another language plugs
in here without searching the code. Each text is a `str.format` template; its
fields are named where it is used.

What stays French, and is not here: the workers' instructions (the primer,
`primer_rules`, the pause and stop requests — `cli.RESUME_TEXT`,
`cli.WIP_REQUEST` —, the reasons of a resume — `parking.RESUME_RED`,
`order.SET_ASIDE_RESUME`) and what tracerworks writes on GitHub, which the recorder
reads back (the usage comment, the closing comment of a spec).
"""

from __future__ import annotations

# --- the command line ---

REFUSED = "tracerworks: refused — {text}"
FAILED = "tracerworks: {text}"
NO_WINDOW = "no window {window}: the worker is not running"
DEAD_WINDOW = "{window} is dead (no provider in the window)"
NOT_RECEIVED = (
    "message NOT received by {window} ({marker}) — {trace}."
    " Look at the window's `tmux capture-pane` before sending again."
)
RECEIVED = "message received by {window} ({marker}) — {trace}"
EMPTY_MESSAGE = "empty message, or not UTF-8 ({source})"
PAUSED = "{window} interrupted (Escape); to resume: tracerworks resume"
NO_NEW_WORKER = "no new worker: {reasons}"
NO_SUCH_ISSUE = "#{n} does not exist on GitHub"
INSTALL_FAILED = (
    "#{n}: {path} not installed (exit {code}: {cause}) — the worker starts"
    " without it; the next launch, resume or push tries again"
)
INSTALL_NO_FOLDER = "the command made no folder"
INSTALL_NO_OUTPUT = "no output"
ISSUE_CLOSED = "#{n} is closed"
WINDOW_EXISTS = "{window} already exists ({state})"
PREPARED = "#{n}: worktree {path} on {branch} ({how}), primer {primer}"
RESUMED = "resumed"
CREATED_FROM = "created from {base}"
#: `history.py` reads this line back from the journal (its French form too).
LAUNCHED = "{window} launched ({provider}; {level})"
#: a resumed worker's model and effort, read from its spec's labels (#2251)
RESUMED_AT_LEVEL = "#{n}: resumed with {level}"
LEVEL_NOT_RECORDED = "#{n}: its model and effort NOT recorded for status: {error}"
STOPPED_WITH_WORKTREE = "stopped (worktree present): resume it with an explicit start"
HAS_WORKER = "already has a worker ({state})"
SKIPPED = "skipped — {reason}"
NO_CANDIDATE = "no spec {ready} + {launch} with a free head ticket"

# --- the factory's order (#1823; the lanes before it, #1261) ---

ROLE_DEVELOPMENT = "in development"
ROLE_CI = "light check"  # the words of its state (#1729)
ROLE_SET_ASIDE = "parked (set aside)"
ORDER_LINE = "{rank} #{n} {state}"
ORDER_FULL = "{n} in flight, the limit is {limit} — waits for a place"
SESSIONS_FULL = "{n}/{max} sessions busy: {held}"
ORDER_UNREADABLE = "the order unreadable (history base): {error}"
ENTERED = "#{n} entered the factory{how}"
ENTERED_AUTO = " (it had no entry)"
ENTRY_NOT_WRITTEN = "#{n}: its entry NOT recorded in the history base: {error}"
LEFT = "#{n} left the factory's order"
ENTRY_NOT_FORGOTTEN = "#{n}: its entry NOT forgotten (history base): {error}"
RANK = "{rank} in the order"
RED_DUE = "#{n}: PR #{pull} needs its worker"
ASIDE_DUE = "#{n} set aside, a session free"
RESUME_REFUSED = "{what} — waits, before any new spec: {reason}"
RED_WAITS_ASIDE = "{why} — #{red}'s red waits"
NOT_SET_ASIDE = "#{n} NOT set aside: {why}"
ASIDE_DEAD = "its window is dead: someone has to look at it"
ASIDE_NOT_CLAUDE = "its worker is {provider}'s: only a Claude session is resumed"
ASIDE_NO_SESSION = "no Claude session found for its worktree"
ASIDE_NOT_WRITTEN = (
    "#{n}: set aside, but NOT recorded in the history base — resume it by hand"
    " (claude --resume {session}): {error}"
)
SET_ASIDE_BY = "#{n} set aside (session {session}) by {who}"
SET_ASIDE_FOR = "#{n} set aside (session {session}) for #{red}'s red, better ranked"
ASIDE_RESUMED = "#{n}: a session free — {window} resumed (claude --resume {session})"
ASIDE_NOT_FORGOTTEN = (
    "#{n}: its set-aside session NOT forgotten (history base): {error}"
)
ASIDE_WAITS = "#{n} set aside — waits: {sessions}"
ASIDE_STALE = "#{n} has its window again: no longer set aside"
RED_WAITS_SESSION = (
    "#{n}: PR #{pull} needs its worker — waits: {sessions}, none ranked after it"
    " at a safe point yet"
)
HEAD_TICKET = "#{n} {title} — head ticket {head}"
# a spec whose `start` was refused for want of a place (#2118)
PLACE_WAITS = "{reason} — #{n} waits for a place — it comes back before any new spec"
#: `ORDER_FULL`'s own end, said once by `PLACE_WAITS`
WAITS_FOR_A_PLACE = " — waits for a place"
PLACE_WAIT_NOT_WRITTEN = (
    "#{n}: its wait for a place NOT recorded in the history base — it will not"
    " come back by itself: {error}"
)
PLACE_WAIT_NOT_FORGOTTEN = (
    "#{n}: its wait for a place NOT forgotten (history base): {error}"
)
PLACE_WAIT_HELD = "#{n} waits for a place ({rank}) — {reason}"
PLACE_WAIT_DUE = "#{n} waited for a place since {since}"
PLACE_WAIT_CLOSED = "#{n} no longer waits for a place: {why}"
STOPPED_BY = "stopped by {who}"
PLACE_WAIT_LINE = "waits for a place: {rank} #{n} since {since}"

USAGE_LINE = (
    "{label}: {total} in all — input {input}, cache written {cache_write},"
    " cache read {cache_read}, output {output} ({messages} replies)"
)
NO_CLAUDE_SESSION = "no Claude session for {worktree}"
USAGE_OWNER = "owner"
USAGE_SUBAGENT = '  subagent "{name}"'
USAGE_SUBAGENTS = "subagents"
USAGE_TOTAL = "whole spec"
USAGE_DURATION = "duration: {hours} h {minutes:02d} (first → last activity)"

KEPT_RESUMED = (
    "#{n} kept — resumed for PR #{pull} {minutes} min ago, its worker is on it"
)
KEPT_SHELL = "#{n} kept — PR #{pull} open, but a shell is still running"
KEPT_BACKGROUND = (
    "#{n} kept — PR #{pull} open, its worker waits on its background job"
    " ({minutes} min, parked past {cap} min)"
)
PARKED_PAST_BACKGROUND = (
    "#{n}: its background job runs for {minutes} min, past {cap} min — not waited for"
)
WOULD_PARK = "#{n}: PR #{pull}, would park {window} and give its slot back"
PARKED_OPEN = (
    "#{n}: PR #{pull} open, {window} closed — parked (session {session}), slot"
    " given back; resumed by start-next if its CI goes red or it conflicts"
)
PARKED_MERGED = (
    "#{n}: PR #{pull} merged, its checks still run — {window} parked"
    " (session {session}), slot given back"
)
NO_SESSION = "unknown: will not be resumed"
PARK_NOT_CLOSED = "#{n}: {window} NOT closed, parked all the same: {error}"
PARK_NOT_WRITTEN = "#{n}: parked session NOT recorded in the history base: {error}"
PARKED_UNREADABLE = "parked sessions unreadable (history base): {error}"
RESUME_PENDING = "#{n} kept — PR #{pull} red after the merge, its parked worker resumes"
RESUMED_PARKED = (
    "#{n}: PR #{pull} needs its worker — {window} resumed (claude --resume {session})"
)
RESUMED_AGAIN = (
    "#{n}: PR #{pull} red, its resume left unanswered (no push, no comment) —"
    " {window} resumed once more (claude --resume {session})"
)
RESUMED_AFTER_RERUN = (
    "#{n}: PR #{pull} red again after its rerun — {window} resumed"
    " (claude --resume {session})"
)
RERUN_DONE = (
    "#{n}: PR #{pull} red judged unrelated by its worker — failed runs of {sha}"
    " rerun once ({runs})"
)
RERUN_NONE = "no failed run"
RERUN_WAITS = (
    "#{n}: PR #{pull} red judged unrelated by its worker — rerun once its run is over"
)
RERUN_FAILED = "#{n}: PR #{pull} NOT rerun: {error}"
RERUN_REFUSED = (
    "#{n}: PR #{pull} rerun refused by GitHub, recorded as tried — its worker is"
    " resumed if still red: {error}"
)
RERUN_NOT_RECORDED = "#{n}: rerun, but its rerun NOT recorded: {error}"
RESUME_FAILED = "#{n}: parked worker NOT resumed: {error}"
RESUME_NOT_MARKED = "#{n}: resumed, but its resume NOT recorded: {error}"
FORGET_FAILED = "#{n}: its parked session NOT forgotten (history base): {error}"
RESUME_WAITS_CLAUDE = (
    "{what}, but its session is Claude's and Claude is not the provider now:"
    " the resume waits, before any new spec"
)
ASKING_STATE = "blocked — {request}"
# One state per chantier, the same words everywhere (#1729).
PHASE_WORDS = {
    "queued": "queued",
    "development": "in development",
    "light_check": "light check",
    "merge_queue": "in the merge queue",
    "human": "waits for a human",
    "blocked": "blocked",
    "parked": "parked",
    "done": "done",
}
PHASE_WITH_REASON = "{phase} ({why})"
PHASE_SINCE = "since {ago}"
WHY_MERGED = "PR #{pull} merged"
WHY_QUEUE_TESTING = "its full suite runs"
WHY_QUEUE_WAITING = "waiting its turn"
WHY_DEAD = "its provider is gone from its window"
WHY_NEEDS_HUMAN = "asked on #{issue}"
WHY_SET_ASIDE_BY_HAND = "set aside by hand"
WHY_SET_ASIDE_FOR = "set aside for #{spec}"
WHY_PARKED_CI = "PR #{pull}, its session closed"
WHY_PARKED_RED = "PR #{pull} red, not resumed yet"
WHY_PARKED_CONFLICT = "PR #{pull} in conflict, not resumed yet"
WHY_CI = "PR #{pull}"
WHY_AT_REST = "at rest"
WHY_FIELD_TAKEN = 'at rest, text waiting in its field: "{text}"'
WHY_STOPPED = "stopped"
WHY_MEMORY = "waits for memory"
HISTORY_NONE = "#{spec}: no state recorded yet (serve records them)"
HISTORY_STEP = "{when} — {state}{lane} — {cause}"
HISTORY_REFUSED = "{when} — REFUSED: {state}{lane} — {cause}"
HISTORY_LANE = " — lane {lane}"  # the history before #1823
HISTORY_UNREADABLE = "history base unreadable: {error}"
CAUSE_CONFIRMED = "{cause}, confirmed: refused, then lasting"
TRANSITION_REFUSED = (
    "#{spec}: transition to '{state}' refused by the table; its last state kept"
)
# What a worker stopped for someone waits for (#984, #1728).
WAITS_PERMISSION = "permission: {request}"
WAITS_QUESTION = "question: {question}"
WAITS_LOGIN = "login expired — /login in the worker's window"
WAITS_LIMIT = "limit reached: {kind}"
PARKED_STATE = "parked — CI (PR #{pull}, session {session})"
NO_WORKTREE = "no spec worktree"
MERGE_UNREADABLE = "merge into {base} unreadable"
NOT_MERGED = "not merged into {base} yet"
FAILED_KEPT = "tracerworks: #{n}: {error}; kept"
WINDOW_OPEN_NO_PR = "window {window} open, no merged PR"
KEPT = "#{n} kept — {why}"
WOULD_CLOSE = "#{n}: PR #{pull} merged, would close {window}"
CLOSED = "#{n}: PR #{pull} merged, {window} closed"
NOTHING_TO_POST = "no Claude session, nothing to post"
USAGE_ALREADY_POSTED = "usage already posted"
WOULD_POST = "would post the usage on #{n}"
POSTED = "usage posted on #{n}"
USAGE_NOT_POSTED = "tracerworks: #{n}: usage NOT posted ({error}); worktree kept"
WOULD_REMOVE = "#{n}: {measure}, then would remove {path} ({size}) (--dry-run)"
REMOVE_FAILED = "tracerworks: #{n}: {measure}; {error}"
NO_LONGER_A_WORKTREE = (
    "#{n}: {measure}; {path} was no longer a git worktree — git's list pruned{left}"
)
FOLDER_LEFT = "; its folder is still there: look, then remove it by hand"
REMOVED = (
    "#{n}: {measure}; worktree {path} removed ({size}) — branch and Claude logs kept"
)
CHECKS_RUNNING = "PR #{pull} merged, its checks still run: {names}"
RED_TOLD = (
    "#{n}: PR #{pull} merged but red after the merge ({names}) — {window} told, kept"
)
WOULD_TELL_RED = (
    "#{n}: PR #{pull} merged but red after the merge ({names}) — would tell {window}"
)
RED_NOT_TOLD = (
    "#{n} kept — PR #{pull} red after the merge ({names}), {window} not told: {error}"
)
SPEC_KEPT_RED = (
    "#{n}: spec kept open — PR #{pull} red after the merge ({names}),"
    " no worker left to tell"
)
SPEC_CLOSED = "#{n}: spec closed (delivered by #{pull})"
SPEC_KEPT_TICKETS = (
    "#{n}: spec kept open — PR #{pull} merged, tickets still open: {tickets};"
    " no next tranche before the operator puts `{launch}` back"
)
WOULD_KEEP_SPEC = (
    "#{n}: would keep the spec open — tickets still open: {tickets}"
    " (their progress label would come off)"
)
SPEC_NOT_CLOSED = "tracerworks: #{n}: spec NOT closed ({error})"
SPEC_CLOSED_GONE = (
    "#{n}: spec closed (delivered by #{pull}), its worktree already gone — {measure}"
)
WOULD_CLOSE_GONE = (
    "#{n}: would close the spec (delivered by #{pull}), its worktree already"
    " gone — {measure} (--dry-run)"
)
WORKTREE_VANISHED = "#{n}: worktree vanished, not removed by clean, last seen {seen}"
VANISHED_NOT_WRITTEN = "tracerworks: worktrees seen NOT written ({error})"
ALERT_VANISHED = (
    "the worktree of #{spec} vanished outside tracerworks clean, last seen"
    " {seen} UTC — {project}"
)
FREED = "{verb}: {size}{unknown} ({count} worktree(s))"
WOULD_FREE = "would free"
FREED_VERB = "freed"
UNKNOWN_SIZES = " + unknown sizes"
HISTORY_NOT_WRITTEN = "tracerworks: #{n}: history NOT written ({error})"

SQL_OR_SCHEMA = "an SQL statement, or --schema"
TEST_ALERT = "🔴 test alert — tracerworks alert-test"
TEST_ALERT_SENT = "test alert sent"
TEST_RESOLVED = "✅ resolved: test alert — tracerworks alert-test"
RESOLVED_SENT = '"resolved" sent: both messages should be in Telegram'
BACKUP_TAKEN = "copy: {copy}; kept {days} days in {folder}"
PROVIDER_SET = "provider {name} for the NEXT workers; those running keep theirs"
QUOTA_RANGE = "the quota is a percentage, from 0 to 100"
QUEUE_PAUSED = (
    "queue paused (settings): start-next starts nothing, the workers running"
    " go on; tracerworks start <n> still starts one"
)
QUEUE_PAUSED_LINE = "QUEUE PAUSED (settings): start-next starts nothing"
CAP_REACHED = (
    "cap reached: no new Claude worker by start-next; the running workers go on"
)
NO_WORKER_FOR = "no worker for #{n}"
ALREADY_STOPPED = "#{n} is already stopped; {saved}"
UNSAVED = "#{n}: {saved}"
DEAD_UNSAVED = (
    "{reason}; the worker is dead, it cannot commit by itself."
    " Commit by hand in the worktree, or --force after the operator's"
    " confirmation (the worktree is kept)"
)
WIP_ASKED = "wip: commit asked of the worker"
WIP_NOT_RECEIVED = "wip: request NOT received ({error})"
STOP_AGAIN = "{reason}; {asked}. Run stop again once pushed"
WORKTREE_KEPT = "; worktree kept: {path}"
NOT_SAVED = "; NOT saved (--force): {saved}"
STOPPED = "{window} stopped{kept}"
# a stop with work left: `set-aside` brings it back before any new spec (#2118)
STOP_SET_ASIDE = "to come back before any new spec, `tracerworks set-aside {n}`"
DASHBOARD_ON = "dashboard on http://{host}:{port}/"
SERVICE_FAILED = (
    "tracerworks installed ({current}), but the service {unit} could not be"
    " updated: {error}"
)
NOTHING = "nothing"
UPDATED = "tracerworks {before} → {current} ({base}), launcher {launcher}"
SERVICE_UNIT = "service {unit}: unit {how} ({path}), restarted if it was running"
UNIT_WRITTEN = "written"
UNIT_UNCHANGED = "unchanged"

# --- status ---

JUST_NOW = "just now"
MINUTES_AGO = "{m} min ago"
HOURS_AGO = "{h} h {m:02d} ago"
DAYS_AGO = "{d} d ago"
NO_WORKTREE_SAVED = "no worktree"
GIT_UNREADABLE = "git state unreadable"
ALL_PUSHED = "everything is pushed"
NEVER_PUSHED = " (branch never pushed)"
UNPUSHED = "{n} unpushed commit(s){suffix}"
UNCOMMITTED = "{n} uncommitted file(s)"
QUOTA = "Claude quota {used}/{cap}%"
MEMORY_UNREADABLE = "memory unreadable"
MEMORY = "memory {gb:.1f} GB free"
DETACHED = "detached HEAD"
PROVIDER_UNKNOWN = "provider ?"
DISK = "disk {size}"
ACTIVITY = "activity {ago}"
PROVIDER = "provider {name}"
NO_WORKER = "no worker"
RUNNER_UNKNOWN = "runner {name} unknown"
RUNNER_STATUS = "runner {name} {status}"
RUNNER_ONLINE = "runner {name} online, {busy}"
BUSY = "busy"
FREE = "free"

# --- admission: guards, routing, the queue ---

TOO_MANY = "{count} workers already running ({names}), maximum {max}"
RUNNER_UNREADABLE = "runner {name} unreadable on GitHub: busy or not, nobody knows"
FREE_MEMORY_UNREADABLE = "available memory unreadable"
RUNNER_BUSY = " (runner {name} busy: CI running)"
LOW_MEMORY = "available memory {mb} MB < threshold {floor} MB{why}"
CODEX_CLOSED = "the Codex route is not open (codex.available: false)"
MEASURED = " (measured)"
DECLARED = " (declared)"
PROVIDER_CODEX = "provider codex"
CLAUDE_NO_QUOTA = "provider claude (quota neither measured nor declared)"
CLAUDE_UNDER_CAP = "provider claude, quota {used}/{cap}%{source}"
OVER_CAP = "quota {used}/{cap}%{source}: cap exceeded by explicit decision"
TO_CODEX = "Claude quota {used}/{cap}%{source}: cap reached, switching to Codex"
CAP_AND_CLOSED = "Claude quota {used}/{cap}%{source}: cap reached and {closed}"
SKIPPED_SPEC = "#{n}: {why}"
NO_FREE_TICKET = "#{n}: no unblocked, free ticket"
NOT_A_WORKTREE = "{path} exists but is not a worktree of {repo}: to look at by hand"
SEVERAL_BRANCHES = (
    "several branches for #{n} ({names}): which one to resume is a human decision"
)
NOT_WIRED = (
    "launching a {provider} worker is not wired yet in this version of tracerworks"
)
NO_LIVE_PROVIDER = "no live provider in {window}"

# --- sending a message to a worker ---

STAYED_BUSY = "{window} stayed busy {seconds:.0f} s: nothing was sent"
TRUST_DIALOG = "{window} shows the folder trust dialog: nothing was sent"
NO_FIELD = "{window} does not show its input field (a dialog open?): nothing was sent"
FIELD_TAKEN = "the field of {window} already holds text ({text}): nothing was sent"
FIELD_FOLDED = (
    "the field of {window} holds a folded paste ({text}), its words not on the"
    " screen: nothing was sent, nothing cleared"
)
FIELD_QUOTED = (
    "the field of {window} held a text never submitted, quoted at the head of"
    " the message, then cleared: {text}"
)
FIELD_NOT_CLEARED = (
    "the field of {window} could not be cleared (still {left}): nothing was sent;"
    " the text it held: {text}"
)
RECEIVED_QUEUED = "received, queued: taken at the end of the current step"
RECEIVED_PLAIN = "received"
RECEIVED_BY_HOOK = "received: its submission seen by the worker's hook"
NOT_HEARD = "not seen by the worker's hook, nor waiting on its screen"
MESSAGE_LOST_UNHEARD = (
    "not seen by the worker's hook, nor waiting, the worker at rest: message lost"
)
NOT_RECEIVED_PLAIN = "not received"
FIELD_GONE = "input field invisible after sending"
TEXT_LEFT = "text left in the field ({text}): Enter sent again"
MARKER_NOWHERE = (
    "marker neither queued nor in the thread: nothing pasted again (worker busy)"
)
MESSAGE_LOST = "empty field, no marker, no work: message lost"

# --- clean ---

WINDOW_BUSY = "window {window} open (worker running)"
UNREADABLE_ANSWER = "{command}: unreadable answer"
ISSUE_STATE = "gh issue view {n} (state)"

# --- quotas, the snapshot ---

STATUSLINE = "Claude quota {parts}"
STATUSLINE_WINDOWS = {"five_hour": "5 h", "seven_day": "week"}
NO_STATUSLINE = "no Claude status line received"
READING_UNREADABLE = "reading unreadable"
READING_UNREADABLE_WHY = "reading unreadable: {error}"
STATUSLINE_OFF = (
    " (status line not given to the workers: [quotas] claude_statusline = false)"
)
NO_BILLING_TOKEN = "no billing token ({path})"
READABLE_BY_OTHERS = "{path} readable by others: chmod 600"
EMPTY_FILE = "{path} is empty"
NO_USAGE_ITEMS = "no usageItems"
BAD_USAGE_ITEMS = "unexpected usageItems"
NO_QUANTITY = "grossQuantity missing"
GITHUB_UNREACHABLE = "GitHub unreachable: {error}"
GITHUB_REFUSED = 'GitHub refused (HTTP {status}): does the token have "Plan: read"?'
BILLING_UNREADABLE = "billing answer unreadable: {error}"

# --- the history base, its backup, its alerts ---

BASE_UNREADABLE = "base unreadable: {error}"
PR_EXPECTED = "which pull request? (?pr=<number>)"
OOM_UNREADABLE = "the session's log is unreadable (journalctl --user)"
SPAN_EXPECTED = "which span? (?days=7, 30 or 90)"
NO_HISTORY_BASE = "no history base ({path}): `serve` creates it"
STATEMENT_FAILED = "statement refused or failed: {error}"
BASE_TOO_NEW = "base at schema {current}, this tracerworks knows {known}"
SERVE_BASE_NEWER = (
    "tracerworks: serve: the history base is at schema {current}, this serve"
    " knows {known}: exit {code}, its unit restarts it on the installed version"
)
NO_BASE_TO_COPY = "no base to copy ({path})"
FAULTY_COPY = "faulty copy: {check}"
VPS_UNREADABLE = "memory, CPU and disk unreadable"
WAITING_UNREADABLE = "issues waiting for the operator unreadable"
RUNS_UNREADABLE = "CI runs unreadable"
PULLS_UNREADABLE = "merged PRs unreadable"
PULLS_TRUNCATED = "{limit} merged PRs or more: backfill refused (list cut short)"
ALERTS_OFF = "Telegram alerts off: {error}"
RUNS_PAGE_UNREADABLE = "page {page} of the runs unreadable"
TOO_MANY_RUNS = "more than 100,000 runs: backfill refused"
RUNS_WITHOUT_JOBS = "{n} run attempt(s) without their jobs: backfill to do again"
USAGE_SEARCH_UNREADABLE = "search of the usage comments unreadable"
TOO_MANY_COMMENTED = "more than 100 specs commented: backfill to paginate"
COMMENTS_UNREADABLE = "comments of #{n} unreadable"
TOO_MANY_COMMENTS = "#{n} has 100 comments or more: backfill to paginate"
HEALTH_LOG_UNREADABLE = "health log unreadable: {error}"

ALERT_WAITING = (
    "#{issue} waiting for {operator} ({label}) for {minutes} min — {project}"
)
ALERT_ASKING = "#{spec} blocked for {minutes} min — {request} — {project}"
ALERT_PARKED_RED = "#{spec} parked for {minutes} min — {why} — {project}"
CARD_PARKED_RED = "parked for {minutes} min — {why}"
WAIT_LABELED = "#{spec} labeled {label}: {why}"
WAIT_UNLABELED = "#{spec} unlabeled {label}: its worker no longer waits"
WAIT_NOT_TOLD = "#{spec}: its wait NOT told on GitHub (next pass): {error}"
MEMORY_BACK = "#{n}: memory back ({mb} MB available) — told to go on"
TEST_STOPPED = (
    "#{n}: test `{command}` stopped after {minutes} min ({mb} MB, pid {pids})"
    " — its worker told"
)
TEST_STOPPED_UNTOLD = ", not received: {error}"
NUDGED = "#{n}: asked a permission it need not ask — nudged ({count}/{most})"
NUDGED_UNSAVED = "#{n}: at rest with unpushed work — nudged ({count}/{most})"
SESSIONS_UNREADABLE = "worker sessions unreadable (tmux)"
NUDGE_LABELED = (
    "#{n}: asked again after {count} nudges without progress — labeled {label}"
)
NUDGE_FAILED = "#{n}: its nudge NOT sent (next pass): {error}"
NUDGE_SENT = "nudged {when} ({count}/{most} without progress)"
NUDGE_GAVE_UP = "nudged in vain, human label put {when}"
CI_REVIVED = (
    "PR #{pull}: head {head} without CI for {minutes} min — its last run re-run"
)
CI_NO_RUN = (
    "PR #{pull}: head {head} without CI for {minutes} min, no run to re-run — said"
)
CI_REVIVAL_FAILED = "PR #{pull}: its CI NOT revived (next pass): {error}"
CONFLICT_CANCELLED = (
    "PR #{pull}: head {head} in conflict — its run {runs} cancelled (#2013)"
)
CONFLICT_CANCEL_FAILED = "PR #{pull}: its run NOT cancelled (next pass): {error}"
CONFLICT_CANCEL_NOT_SAID = (
    "PR #{pull}: its run cancelled, the comment NOT posted: {error}"
)
CI_REVIVAL_UNREADABLE = "open pull requests not read (next pass): {error}"
CONFLICT_TOLD = {
    "message": "PR #{pull} in conflict for {minutes} min — worker #{spec} told to"
    " merge the base",
    "parked": "PR #{pull} in conflict for {minutes} min — #{spec} parked: its resume"
    " says it",
    "comment": "PR #{pull} in conflict for {minutes} min, no worker — commented,"
    " alert open",
}
CONFLICT_CLEARED = "PR #{pull}: mergeable again"
MACHINE_RERUN_DONE = (
    "#{n}: PR #{pull} red without a failed test (a machine's) — failed runs of"
    " {sha} re-run once, its worker left asleep ({runs})"
)
#: a machine's red under the memory's alert threshold (#2096): not re-run, not
#: counted as its head's one re-run, until the memory is back
MACHINE_RERUN_WAITS_MEMORY = "machine red, rerun waits for memory ({mb} MB available)"
RERUN_WAITS_MEMORY = "#{n}: PR #{pull} " + MACHINE_RERUN_WAITS_MEMORY
UNWORKED_RERUN_WAITS_MEMORY = "PR #{pull}: " + MACHINE_RERUN_WAITS_MEMORY
QUEUE_RERUN_WAITS_MEMORY = (
    "merge queue: #{pull} — a machine's red (no failed test in {jobs}): "
    + MACHINE_RERUN_WAITS_MEMORY
    + " ({url})"
)
#: said whether a red waits or not: the memory, not a pull request, holds it
STATUS_RERUN_WAITS_MEMORY = (
    "memory low ({mb} MB available, under the alert threshold): any machine red,"
    " rerun waits for memory"
)
MACHINE_RERUN_NO_WORKER = (
    "PR #{pull}: red without a failed test (a machine's), no worker — failed runs"
    " of {sha} re-run once ({runs})"
)
MACHINE_RERUN_FAILED = "PR #{pull}: its machine's red NOT re-run (next pass): {error}"
MACHINE_RERUN_STATUS = (
    "PR #{pull}: a machine's red re-run once {when} (head {head}, {by};"
    " attempt {attempt} on GitHub)"
)
MACHINE_RERUN_ASKED = (
    "PR #{pull}: a machine's red — re-run requested {when}, GitHub shows no"
    " second attempt yet (head {head}, {by})"
)
MACHINE_RERUN_UNREAD = (
    "PR #{pull}: a machine's red — re-run requested {when}, its attempts on"
    " GitHub unread (head {head}, {by})"
)
MACHINE_LOG_WAITS = (
    "#{n}: PR #{pull} red, a failed job's log not given by GitHub yet — read"
    " again at the next pass (a machine's red only if still none {minutes} min"
    " after the job)"
)
MACHINE_NOTHING_TO_RERUN = (
    "#{n}: PR #{pull} red without a failed test, but no failed run of {sha} to"
    " re-run — read as a test's red: its worker is resumed at the next pass"
)
QUEUE_LOG_WAITS = (
    "merge queue: #{pull} red ({jobs}), a log not given by GitHub yet — read"
    " again at the next pass ({url})"
)
CODE_RED = "PR #{pull}: a code red ({step}), not a machine's — nothing re-run"
CODE_RED_STATUS = (
    "PR #{pull}: a code red {when} (head {head}, {step}) — not re-run: the"
    " code's, not a machine's"
)
MACHINE_RERUN_BY = {
    "parked": "its worker parked",
    "no_worker": "no worker",
    "flaky": "the merge queue",
}
#: the merge queue's one re-run of a short test's red (#2130), in `status`
#: and on the card
FLAKY_RERUN_STATUS = (
    "PR #{pull}: rerun once: {n} flaky test(s) {when} (head {head}, {by}; {seen})"
)
FLAKY_RERUN_SEEN = {
    None: "its attempts on GitHub unread",
    1: "GitHub shows no second attempt yet",
}
FLAKY_RERUN_ATTEMPT = "attempt {attempt} on GitHub"
CONFLICT_NOT_TOLD = "PR #{pull}: its conflict NOT told (next pass): {error}"
CONFLICT_STATUS = "PR #{pull} in conflict with the base for {minutes} min, no worker"
ALERT_MERGE_FAILED = "PR #{pull} left the merge queue, green: {why} — {project}"
ALERT_CONFLICT = (
    "PR #{pull} in conflict with the base for {minutes} min, no worker: no CI"
    " runs — {project}"
)
ALERT_QUOTA = "Claude quota {window} at {percent}% (threshold {threshold:.0f}%)"
ALERT_QUOTA_WINDOWS = {"five_hour": "5 h", "seven_day": "week"}
ALERT_DISK = "VPS disk at {percent:.0f}% (threshold {threshold:.0f}%)"
ALERT_STRAY = (
    "pid {pid} ({name}) has held {percent}% of a CPU core for {minutes} min, outside"
    " the CI, the services and the workers: kill {pid} if nobody waits for it"
)
ALERT_RESOLVED = "✅ resolved: {text}"
NO_TELEGRAM_TOKEN = "no Telegram token ({path})"
TELEGRAM_UNREACHABLE = "Telegram unreachable ({kind})"
TELEGRAM_REFUSED = "Telegram refused (HTTP {status}) {why}"
ALERT_CI_RED = "{branch}'s CI red — {project}: {title} {url}"
HISTORY_SAID = "tracerworks: history: {source}: {text}"
READABLE_AGAIN = "readable again"
SIZE_GB = "{gb:.1f} GB"
SIZE_MB = "{mb} MB"
COPY_FAILED = "copy impossible: {error}"
ACTIONS_UNREADABLE = "Actions minutes unreadable"
LABELS_CARRIED = "#{n}: {labels} put back on PR #{pull}, as on its spec"
BUDGET_LOW = (
    "GitHub Actions minutes at {percent:.0f}% of the month (threshold"
    " {threshold:.0f}%): {variable} set until the month starts over"
)
BUDGET_BACK = (
    "GitHub Actions minutes at {percent:.0f}% of the month: {variable} removed"
)
ALERT_BUDGET = (
    "GitHub Actions minutes at {percent:.0f}% of the month (threshold"
    " {threshold:.0f}%): {variable} applies until the month starts over"
)
ERROR = "error: {error}"
SOURCE_BASE = "base"
SOURCE_BACKUP = "backup"
SOURCE_BACKFILL = "backfill"
RUNS_SHORT = "{seen} runs listed of the {total} GitHub counts: backfill to do again"

# the workers' fine-grained GitHub token (#1529) — its state, never its value
WORKER_TOKEN_ABSENT = "no workers' GitHub token ({path})"
WORKER_TOKEN_UNREADABLE = "workers' GitHub token {path} unreadable ({kind})"
WORKER_TOKEN_REFUSED = (
    "the workers' GitHub token {path} is refused by GitHub (HTTP {status}):"
    " renew it, or remove the file to launch with the machine's authentication"
)
WORKER_TOKEN_USED = "#{n}: the worker uses the workers' fine-grained GitHub token"
WORKER_TOKEN_NOT_USED = (
    "#{n}: WARNING — {why}: the worker uses the machine's GitHub authentication"
    " (the owner's token) until the operator writes it"
)
ALERT_WORKER_TOKEN = (
    "{why}: the workers of {project} hold the owner's GitHub token until the"
    " operator writes theirs (mode 600)"
)

# who is trusted (#1529): the admission's and the markers' reasons
FOREIGN_AUTHOR = "written by {login}, not a trusted login"
LAUNCHED_BY_OTHER = "{label} put by {login}, not by the operator"
LAUNCH_ACTOR_UNKNOWN = (
    "who put {label} cannot be read (issue events): unknown is never the operator"
)

# the worktree's session guard, tried before a launch or a resume (#1529)
GUARD_BROKEN = (
    "session guard absent or broken in {path} ({why}): merge {base} into the"
    " branch, then launch again"
)
GUARD_NOT_WIRED = "no PreToolUse command hook in its Claude Code project settings"
GUARD_ANSWERED = "a {tool} call got exit {code}, {expected} expected"
GUARD_NOT_SEEN = "no PreToolUse hook's matcher covers {tool}"

# --- the merge queue (#1714) ---

QUEUE_ENTERED = "merge queue: #{pull} entered (its light lane green)"
QUEUE_GONE = "merge queue: #{pull} left (closed, or its label taken off)"
QUEUE_GONE_REASON = "closed, merged elsewhere, or its label taken off"
QUEUE_LEFT = "merge queue: #{pull} left — {reason}"
QUEUE_LIGHT_RED = "its light lane is red"
QUEUE_CONFLICT = "a conflict with the base branch"
QUEUE_FULL_RED = "the full suite is red: {jobs} ({url})"
QUEUE_CODE_RED = "the full suite is red, a code red: {jobs} ({url})"
QUEUE_READMITTED = (
    "merge queue: #{pull} back at the head of the queue — its full suite's red"
    " on {sha} judged unrelated by its worker ({tests}, {url}): no rerun"
    " outside the queue"
)
QUEUE_UPDATED = "merge queue: #{pull} in test — branch updated with the base ({sha})"
QUEUE_RERUN = (
    "merge queue: #{pull} in test — up to date with the base ({sha}),"
    " its head run rerun"
)
QUEUE_UPDATING = "merge queue: #{pull} in test — waiting for GitHub's update push"
QUEUE_TESTING = "merge queue: #{pull} in test ({url})"
QUEUE_NO_RUN_YET = "its run not created yet"
QUEUE_NO_RUN = "merge queue: #{pull} has no CI run on its head to rerun"
QUEUE_MOVED = "merge queue: #{pull} tested again — the base moved by code ({sha})"
QUEUE_UNMERGEABLE = (
    "merge queue: #{pull} tested again — GitHub refused its merge, not"
    " mergeable: branch updated with the base ({sha})"
)
QUEUE_MERGE_ERROR = "merge queue: #{pull} merge failed ({n}/{of}), tried again: {error}"
QUEUE_MERGE_FAILED = "the merge failed {n} passes running: {error}"
QUEUE_UNMERGEABLE_UP_TO_DATE = (
    "GitHub refuses the merge, yet the branch is up to date: {said}"
)
QUEUE_MERGED = "merge queue: #{pull} merged — full suite green ({url})"
QUEUE_GREEN_REASON = "full suite green"
QUEUE_MERGED_NOTHING = "merge queue: #{pull} merged — documentation only, no suite"
QUEUE_NOTHING_REASON = "documentation only: the CI ran nothing"
QUEUE_STATUS_GREEN = "Full suite green on the base at {sha}"
QUEUE_STATUS_NOTHING = "Documentation only: the CI ran nothing"
QUEUE_NIGHT = "merge queue: #{pull} waits — the night's suite is running"
QUEUE_UNREADABLE = "merge queue: not moved — {error}"
QUEUE_VIEW_UNREADABLE = "merge queue: unknown (the history base cannot be read)"
QUEUE_VIEW_EMPTY = "merge queue: empty"
QUEUE_VIEW_TESTING = "merge queue: #{pull} in test since {ago} — {run}"
QUEUE_VIEW_WAITING = "merge queue: waiting {pulls}"
QUEUE_VIEW_NONE = "none"
QUEUE_VIEW_LAST = "merge queue: last out #{pull}, {outcome} — {reason} ({ago})"
QUEUE_OUTCOMES = {"merged": "merged", "sent_back": "sent back", "gone": "gone"}
QUEUE_AUTO_OFF = (
    "merge queue: #{pull} — auto-merge taken off (tracerworks alone merges)"
)
QUEUE_AUTO_STUCK = (
    "merge queue: #{pull} — its auto-merge would not come off ({why});"
    " kept out of the queue"
)
QUEUE_AUTO_STILL = "still set when read back"
QUEUE_VIEW_STUCK = (
    "merge queue: #{pull} out of the queue — its auto-merge would not come off:"
    " {why} ({ago})"
)
QUEUE_MACHINE_RERUN = (
    "merge queue: #{pull} — a machine's red (no failed test in {jobs}):"
    " its failed jobs rerun once ({url})"
)
QUEUE_FLAKY_RERUN = (
    "merge queue: #{pull} — rerun once: {n} flaky test(s) ({tests}), none in"
    " a file it modifies: its failed jobs rerun once ({url})"
)
QUEUE_FLAKY_RERUN_WAITS_MEMORY = (
    "merge queue: #{pull} — {n} flaky test(s) ({tests}), rerun waits for memory"
    " ({mb} MB available, {url})"
)
#: the bug a flaky test's green rerun opens, or comments (#2130)
FLAKY_BUG_TITLE = "Flaky test in the merge queue: {test}"
FLAKY_BUG_BODY = (
    "`{test}` failed in the merge queue's full suite of #{pull} ({url}), then"
    " passed when its failed jobs were rerun once: #{pull} merged.\n\n"
    "A flaky test — rerunning it is no fix. Each time it fails so again,"
    " tracerworks comments here.\n\n"
    "Opened by tracerworks, the merge queue's rule for a short test's red (#2130)."
)
FLAKY_BUG_AGAIN = (
    "Flaky again: `{test}` failed in the merge queue's full suite of #{pull}"
    " ({url}), green when its failed jobs were rerun once — #{pull} merged."
)
#: the bug a test recognised flaky opens by itself (#2224, spec #2219)
FLAKY_SELF_TITLE = "Flaky test: {test}"
FLAKY_SELF_BODY = (
    "`{test}` was recognised flaky by the factory {n} times in {days} days"
    " (`quality.flaky_tests`):\n\n{runs}\n\n"
    "**Fix it at its cause, never rerun it**: reproduce the red"
    " deterministically, fix it, put its counter-proof, and file the cause in"
    " `docs/registre/` (spec #2219). No rerun, no delay, no `skip`.\n\n"
    "Each new failure is commented here; {close_days} days without a failure"
    " close this issue.\n\nOpened by tracerworks (#2224)."
)
FLAKY_SELF_RUN = "- {when} — {rules} — {url}"
FLAKY_SELF_AGAIN = "New failure of `{test}`:\n\n{runs}"
FLAKY_SELF_CLOSED = (
    "No failure of `{test}` for {days} days (last: {last}): closed by tracerworks"
    " (#2224). A new flaky failure would open another."
)
FLAKY_SELF_OPENED = "flaky test {test}: bug #{bug} opened ({n} times in {days} days)"
FLAKY_SELF_COMMENTED = "flaky test {test}: bug #{bug} commented ({n} new failure(s))"
FLAKY_SELF_CLOSED_SAID = "flaky test {test}: bug #{bug} closed ({days} days quiet)"
FLAKY_SELF_CLOSED_ELSEWHERE = (
    "flaky test {test}: bug #{bug} closed by somebody else — forgotten"
)
FLAKY_SELF_FAILED = "flaky test {test}: its bug NOT tended: {error}"
FLAKY_SELF_UNREADABLE = "flaky tests' bugs: the history is unreadable: {error}"
QUEUE_FLAKY_BUG = "merge queue: #{pull} — flaky test {test}: bug #{bug} opened"
QUEUE_FLAKY_BUG_COMMENTED = (
    "merge queue: #{pull} — flaky test {test}: bug #{bug} commented"
)
QUEUE_FLAKY_BUG_FAILED = (
    "merge queue: #{pull} — flaky test {test}: its bug NOT opened: {error}"
)
QUEUE_FULL_RED_AGAIN_FLAKY = (
    "the full suite is red again after one rerun for flaky tests: {jobs} ({url})"
)
QUEUE_FULL_RED_AGAIN = (
    "the full suite is red again after one rerun for the machine: {jobs} ({url})"
)
RED_WAITS_QUEUE = (
    "#{n}: PR #{pull} is red in the merge queue's test — the queue decides"
    " first (a machine's red is rerun), its worker after"
)
QUEUE_UPDATE_LOST = "GitHub never pushed the branch's update"
QUEUE_SKIPPED_NO_RUN = "merge queue: #{pull} skipped — no CI run on its head to rerun"
# A light lane's green reused for the same code diff (#1844)
QUEUE_ENTERED_REUSED = (
    "merge queue: #{pull} entered on the light lane of {sha} (same code diff)"
)
QUEUE_REUSE_UNREAD = "merge queue: #{pull} — reuse not judged: {why}"
QUEUE_REPLACING = (
    "merge queue: #{pull} in test — up to date with the base ({sha}), its light"
    " lane cancelled for the full suite"
)
QUEUE_REPLACING_WAIT = (
    "merge queue: #{pull} in test — waiting for its light lane's cancel"
)
QUEUE_REPLACED = "merge queue: #{pull} in test — the full suite replaces its light lane"
# A run of the queue's is never rerun outside it (#2110)
RERUN_QUEUE_RUN = (
    "#{n}: PR #{pull} — run {runs} is the merge queue's: never rerun outside it;"
    " its worker next"
)
QUEUE_RUN_NOT_RECORDED = (
    "#{n}: the queue's run NOT rerun, and that NOT recorded: {error}"
)
MACHINE_QUEUE_RUN = (
    "PR #{pull}: run {runs} is the merge queue's — never rerun outside it"
)
# The runs of a pull request waiting, cancelled (#2109)
QUEUE_CANCELLED = (
    "merge queue: #{pull} waiting — its run {run} cancelled (attempt {attempt}):"
    " the queue reruns everything at its turn"
)
QUEUE_CANCEL_FAILED = (
    "merge queue: #{pull} — its run NOT cancelled (next pass): {error}"
)
QUEUE_CANCEL_NOT_SAID = (
    "merge queue: #{pull} — its run cancelled, the comment NOT posted: {error}"
)
QUEUE_VIEW_CANCELLED = (
    "merge queue: #{pull} waiting — its run {url} cancelled {ago}:"
    " the queue reruns everything at its turn"
)
