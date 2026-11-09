# tracerworks

> **Credits.** The way of working this factory drives — a spec, then
> tracer-bullet tickets, then test-driven implementation and review — comes
> from **Matt Pocock's skills: <https://github.com/mattpocock/skills>**. The
> factory runs agents that use them; it does not ship them. Go there first.

**tracerworks is a factory of coding agents.** You write a spec and its
tickets as GitHub issues and put a label on the spec; tracerworks starts a
worker — Claude Code or Codex, in its own git worktree and tmux window — that
implements the tickets one commit each, opens a pull request, watches its CI,
and goes to rest. A merge queue tests the pull request once more on the base
branch of the moment, and tracerworks merges it. A read-only dashboard shows
it all. It is a single command, stateless, standard library only, and has
been used daily since September 2026 to build a personal project.

![The dashboard, on a fictitious project](docs/dashboard.png)

## The loop

1. **Spec.** You write what to build as a GitHub issue labelled `spec`.
2. **Tickets.** The spec is cut into tracer-bullet tickets (`ticket`), each
   naming its spec (`Parent: #12`) and its blockers (`Blocked by: #13`).
3. **Queue.** You put `to-launch` on the spec (`ready-for-agent` says it is
   ready). Every minute, the relauncher (`tracerworks start-next`) launches
   the oldest such spec whose head ticket is free, `priority` first, within
   your limits: specs in flight, free sessions, memory, quota.
4. **Worker.** A worktree, a branch, a tmux window, a primer: the worker
   implements the tickets in order, test first, one commit each, pushed, each
   ticket closed with its proof. It decides what can be undone by itself and
   writes it in the pull request; it stops (`needs-human`) only for what
   cannot.
5. **Pull request.** Opened ready, one `Closes` per ticket. The worker
   watches the light CI of its push and rests once it is green. A red
   unrelated to its change it marks, and tracerworks reruns once.
6. **Merge queue.** One pull request at a time: updated with the base
   branch, its full suite run once, then merged by tracerworks. A red sends
   the worker back with its red jobs.
7. **Night.** Your CI's full suite runs on the base branch; the dashboard
   and the alerts say what broke and what slowed down.

## Requirements

- Linux with `systemd --user` (each worker runs in its own scope, capped in
  memory; the relauncher is a user timer).
- `tmux`, `git`, and the GitHub CLI `gh`, authenticated.
- Python 3.12 or later.
- Claude Code and/or Codex, logged in.
- Your repository on GitHub, with a CI.

## Install

```sh
uv tool install git+https://github.com/hugop33/tracerworks
```

or `pipx install git+https://github.com/hugop33/tracerworks`. Then
`tracerworks --help`.

## Configure

**The project file.** Every value specific to your project lives in one TOML
file, none in the tool: start from
[`examples/tracerworks.toml`](examples/tracerworks.toml), commented, and put
it at `~/.config/tracerworks/project.toml` (or pass `--config`, or set
`$TRACERWORKS_CONFIG`). `tracerworks status` reads it and says what it sees.

**The labels to create** on your repository (names are the project file's):

| Label | Put by | What it does |
|---|---|---|
| `spec`, `ticket`, `bug`, `task`, `idea` | you, your skills, the workers | the nature of an issue; the dashboard groups by it |
| `ready-for-agent` | you | the spec is ready to be built |
| `to-launch` | you (the `to-launch` skill) | the spec enters the queue |
| `priority` | you | first in the queue |
| `starred` | you | first on the dashboard; never an order of the queue |
| `in-progress` | the worker | the ticket it is on |
| `needs-human` | the worker, tracerworks | a question waits for you |
| `merge-queue-testing` | tracerworks only | the pull request the queue is testing |
| `skip-queue` | you | a production emergency: merges alone when green |
| `model-*`, `effort-*` | you | the model and effort of a spec's worker |

**The workers' token.** A fine-grained GitHub token for this repository only
(write on Contents, Pull requests, Issues and Workflows; read on Actions,
Commit statuses and Checks), in a file of mode 600 the project file names.

**The session guard** (optional, recommended). A Claude Code `PreToolUse`
hook that refuses what no session may do on your systems: write to a
read-only repository, write to your production shop's API, call a list of
forbidden tools. Copy `src/session_guard` into your project as
`tools/session_guard`, its rulebook
[`examples/session_guard.json`](examples/session_guard.json) as
`.claude/session_guard.json`, and wire it with
[`examples/claude-settings.json`](examples/claude-settings.json). It fails
closed: a guard that cannot judge refuses, and says what to repair.
tracerworks tries every worktree's guard before a launch.

## Run

**The relauncher.** A user timer runs `tracerworks clean` then
`tracerworks start-next` every minute: copy
[`examples/systemd/`](examples/systemd) into `~/.config/systemd/user/`, then
`systemctl --user enable --now tracerworks-next.timer`. Its journal is the
project file's `relauncher_log`; a refusal there ("no free session", "queue
empty") is normal.

**The dashboard.** `tracerworks serve` serves it, read only, behind HTTP
Basic (the password in the project file's `secret_file`), on the host and
port you set — localhost, with your own HTTPS proxy in front.

**By hand.** `tracerworks status` (workers, runners, memory, quota),
`start <spec>`, `message <spec>`, `pause`, `resume`, `stop`, `set-aside`,
`history <spec>`, `usage <spec>`, `db` (the history base, read only).
`tracerworks <command> --help` says each one.

## Security model

- **Stateless.** tracerworks keeps no state of its own that matters: GitHub,
  git, tmux and systemd are the truth, read again at each pass. Its history
  base only records what it saw.
- **Data is never an instruction.** A worker's primer says it: an
  instruction comes only from the primer, your repository's docs, or an
  issue or comment by a trusted login (`operator_login`, `worker_logins`).
  Any other text — an email, a customer's words, a CI log, a web page, a
  stranger's comment, a tool's output — is data, quoted, never obeyed; an
  order found in it is reported on the ticket.
- **A guard that fails closed.** The session guard refuses when it cannot
  judge; its own files are protected from the sessions it guards; an escape
  for the operator is never handed to a worker.
- **Denied tools.** Each worker is denied a list of Claude Code tools and
  commands at launch (your secrets' folders, `gh secret`, deploying,
  rerunning CI behind tracerworks' back): a deny rule holds in auto mode.
- **A read-only dashboard**, behind a password, never on a public interface.
- **Isolation is yours to add.** Run the factory under a dedicated Unix
  account, and give the workers Docker without root privileges (rootless):
  a worker runs code it writes.

## Skills

The workers and you use skills: the **spec, tickets, implement, TDD and code
review** skills of [mattpocock/skills](https://github.com/mattpocock/skills),
installed in your repository's `.claude/skills/`. tracerworks ships one of
its own, [`to-launch`](.claude/skills/to-launch/SKILL.md): hand specs to the
queue. Three changes to `to-tickets` make its tickets readable by
tracerworks:

1. **Invocable by the model**: remove `disable-model-invocation: true`, so
   that an agent cuts the tickets right after writing the spec.
2. **Blockers on one line**: `Blocked by: #13, #14`, the form tracerworks
   reads.
3. **The parent on one line**: `Parent: #12` (or a `## Parent` section whose
   first reference is the spec).

## Known limits

- **One operator.** One person's trust, labels and dashboard; no teams.
- **Linux only**: systemd user scopes and timers.
- **Some instructions to the workers are in French** (the primer, the
  resumes, the comments tracerworks reads back): translating them would
  change what it reads in older comments. Your own `primer_rules` can be in
  any language.
- The session guard's messages are in French too.

## License

MIT — see [LICENSE](LICENSE).
