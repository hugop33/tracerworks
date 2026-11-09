# Installing tracerworks

This guide takes you from nothing to a first spec built by a worker: a Claude
Code session in its own git worktree, which opens a pull request that the
merge queue tests and tracerworks merges. Count about an hour: most of it is
creating labels, a GitHub token and a session guard in your repository.

You need a Linux machine you control (a small VPS is enough), and a
repository on GitHub with a CI.

## Contents

1. [Requirements](#1-requirements)
2. [Install the command](#2-install-the-command)
3. [Write the project file](#3-write-the-project-file)
4. [Create the labels](#4-create-the-labels)
5. [Give the workers their GitHub token](#5-give-the-workers-their-github-token)
6. [Put the session guard in your repository](#6-put-the-session-guard-in-your-repository)
7. [Add the skills](#7-add-the-skills)
8. [Check the setup](#8-check-the-setup)
9. [Start the relauncher](#9-start-the-relauncher)
10. [Serve the dashboard](#10-serve-the-dashboard)
11. [Launch your first spec](#11-launch-your-first-spec)
12. [Optional: alerts and quotas](#12-optional-alerts-and-quotas)
13. [Troubleshooting](#13-troubleshooting)
14. [Update and uninstall](#14-update-and-uninstall)

## 1. Requirements

| What | Why |
|---|---|
| Linux with `systemd --user` | each worker runs in its own scope, capped in memory; the relauncher is a user timer |
| `tmux` | each worker is a tmux window you can attach to |
| `git` and the GitHub CLI `gh`, authenticated | worktrees, branches, issues, pull requests, CI |
| Python 3.12 or later, and `uv` or `pipx` | tracerworks is a Python command, standard library only |
| [Claude Code](https://claude.com/claude-code), logged in | each worker is `claude --permission-mode auto` |
| A GitHub repository with a CI workflow | the workers push to it; the merge queue runs its CI |

Run the factory under a **dedicated Unix account** with no access to your
production secrets: a worker runs the code it writes. If your tests need
Docker, give that account Docker without root privileges (rootless).

Let the account's user services run while nobody is logged in:

```sh
loginctl enable-linger "$USER"
```

## 2. Install the command

```sh
uv tool install git+https://github.com/hugop33/tracerworks
# or
pipx install git+https://github.com/hugop33/tracerworks

tracerworks --help
```

Both install the `tracerworks` command in `~/.local/bin`; the systemd units
of step 9 expect it there.

Keep a clone of this repository too: you will copy the session guard and the
examples from it.

```sh
git clone https://github.com/hugop33/tracerworks ~/tracerworks-src
```

## 3. Write the project file

Every value specific to your project lives in one TOML file, none in the
tool. Start from the commented example:

```sh
mkdir -p ~/.config/tracerworks
cp ~/tracerworks-src/examples/tracerworks.toml ~/.config/tracerworks/project.toml
```

tracerworks reads `--config`, else `$TRACERWORKS_CONFIG`, else
`~/.config/tracerworks/project.toml`. Change at least:

- `[github] repo`: your repository, `owner/name`.
- `[github] operator_login`: your GitHub login. Issues and comments by this
  login (and `worker_logins`) are instructions to a worker; anyone else's are
  data, quoted and never obeyed.
- `[github] runner`: your self-hosted CI runners, if any.
- `[git] repo_dir`: your main clone of the repository, on this machine. Each
  worker gets a worktree of it.
- `[workers] tmux_session`: the tmux session the workers' windows open in.
- `[workers] in_flight`, `memory_max`: how many specs at once, and each
  worker's memory cap.
- `[workers] disallowed_tools`: what no worker may run or read. Name your
  production and your secrets' folders.
- `[workers] primer_rules`: your rules, added to every worker's first
  instruction.

The tmux session must exist before the first launch:

```sh
tmux new-session -d -s your-app
```

## 4. Create the labels

tracerworks reads the queue from GitHub labels. Create them once (the names
are the project file's; change them there and here together):

```sh
for label in spec ticket bug task idea ready-for-agent to-launch priority \
             starred in-progress needs-human merge-queue-testing skip-queue; do
  gh label create "$label" --repo your-name/your-app --force
done
```

| Label | Put by | What it does |
|---|---|---|
| `spec`, `ticket`, `bug`, `task`, `idea` | you, your skills, the workers | the nature of an issue |
| `ready-for-agent` | you | the spec is ready to be built |
| `to-launch` | you | the spec enters the queue |
| `priority` | you | first in the queue |
| `starred` | you | first on the dashboard |
| `in-progress` | the worker | the ticket it is on |
| `needs-human` | the worker, tracerworks | a question waits for you |
| `merge-queue-testing` | tracerworks only | the pull request the queue is testing |
| `skip-queue` | you | a production emergency: merges alone when green |

Optional: one label per model or effort a spec may ask for, such as
`model-sonnet` or `effort-xhigh` (`model_label_prefix`, `effort_label_prefix`).

## 5. Give the workers their GitHub token

The workers push, comment and open pull requests with a token of their own,
never yours. Create a
[fine-grained personal access token](https://github.com/settings/personal-access-tokens/new)
for **this repository only**:

- read and write: Contents, Pull requests, Issues, Workflows;
- read: Actions, Commit statuses, Checks.

Store it in the file `github_token_file` names, readable by you only:

```sh
install -m 600 /dev/null ~/.config/tracerworks/worker-github.token
"${EDITOR:-nano}" ~/.config/tracerworks/worker-github.token   # paste the token
```

## 6. Put the session guard in your repository

**This step is required.** Before every launch and every resume, tracerworks
tries the worktree's guard: a harmless call must pass, and a call to
`guard_probe_tool` must be refused. A worktree with no guard, or a guard
that answers wrong, is not launched, and the refusal says what to repair.

The guard is a Claude Code `PreToolUse` hook. It refuses what no session may
do on your systems: write to a read-only repository, write to your production
shop's API, call a list of forbidden tools. It fails closed: a guard that
cannot judge refuses.

From your repository's root:

```sh
mkdir -p tools .claude
cp -r ~/tracerworks-src/src/session_guard tools/session_guard
cp ~/tracerworks-src/examples/session_guard.json .claude/session_guard.json
```

Then add the hook of
[`examples/claude-settings.json`](examples/claude-settings.json) to your
`.claude/settings.json` (create it with that content if you have none).

Edit `.claude/session_guard.json`, the guard's rulebook:

- `read_only_dirs`: folders no session may write to (another repository
  beside this one, for example).
- `read_only_shops`: hosts no session may write to through the shell.
- `forbidden_tool_prefixes`: tools no session may call. Keep one prefix that
  covers `guard_probe_tool` (the example's `mcp__claude_ai_` covers
  `mcp__claude_ai_tracerworks_probe`).
- `protected_paths`: files the sessions may not change, the guard's own
  among them.

**Commit and push these files on your base branch**: each worker's worktree
starts from it, and the guard tracerworks tries is the worktree's.

## 7. Add the skills

The workers follow skills: the **spec, tickets, implement, TDD and code
review** skills of [mattpocock/skills](https://github.com/mattpocock/skills).
Install them in your repository's `.claude/skills/`, with tracerworks' own
`to-launch` skill:

```sh
cp -r ~/tracerworks-src/.claude/skills/to-launch .claude/skills/to-launch
```

Three changes to `to-tickets` make its tickets readable by tracerworks:

1. **Invocable by the model**: remove `disable-model-invocation: true`.
2. **Blockers on one line**: `Blocked by: #13, #14`.
3. **The parent on one line**: `Parent: #12` (or a `## Parent` section whose
   first reference is the spec).

Commit them on your base branch too.

## 8. Check the setup

```sh
tracerworks status
```

It reads the project file and says what it sees: workers, runners, memory,
provider, quota. A setting missing or of the wrong kind is named.

## 9. Start the relauncher

A user timer runs `tracerworks clean` (it frees the places of finished
workers), then `tracerworks start-next` (it launches the oldest ready spec
whose head ticket is free), every minute:

```sh
mkdir -p ~/.config/systemd/user
cp ~/tracerworks-src/examples/systemd/tracerworks-next.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tracerworks-next.timer
```

Its journal is `relauncher_log` (`~/tracerworks-next.log` in the example). A
refusal there, such as "no free session" or "queue empty", is a normal
answer.

## 10. Serve the dashboard

The dashboard is read only, behind HTTP Basic. Its password lives in
`secret_file`, readable by you only:

```sh
install -m 600 /dev/null ~/.config/tracerworks/dashboard.secret
openssl rand -base64 24 > ~/.config/tracerworks/dashboard.secret
```

Run it as a user service, `~/.config/systemd/user/tracerworks-serve.service`
(the name is `[serve] service`):

```ini
[Unit]
Description=tracerworks: the dashboard

[Service]
Environment=TRACERWORKS_CONFIG=%h/.config/tracerworks/project.toml
ExecStart=%h/.local/bin/tracerworks serve
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now tracerworks-serve
```

It listens on `[serve] host` and `port` (`127.0.0.1:8787` in the example).
Keep it on localhost and put your own HTTPS proxy in front, or reach it
through an SSH tunnel: never on a public interface. Any user name works; the
password is the file's.

## 11. Launch your first spec

1. Write a GitHub issue labelled `spec` and `ready-for-agent`: what to build
   and why. The `to-spec` skill writes one with you.
2. Cut it into tickets, each a GitHub issue labelled `ticket` and
   `ready-for-agent`, whose body names the spec (`Parent: #12`) and its
   blockers (`Blocked by: #13`). The `to-tickets` skill does it.
3. Put `to-launch` on the **spec** (the `to-launch` skill checks it first).

Within a minute, the relauncher starts a worker. Follow it:

```sh
tracerworks status              # its phase, its ticket, its branch
tmux attach -t your-app         # its window, live
tracerworks history 12          # each change of its state, and why
```

The worker implements the tickets one commit each, test first, opens the
pull request ready, and rests once its CI is green. The merge queue then
tests the pull request once more on the base branch of the moment, and
tracerworks merges it.

## 12. Optional: alerts and quotas

- **Alerts on Telegram**: a bot's token in `[alerts] telegram_token_file`
  (mode 600) and your chat's id in `telegram_chat_id`; then
  `tracerworks alert-test`.
- **Claude usage on the dashboard**: set `[quotas] claude_statusline = true`;
  the workers then hand their usage to `tracerworks statusline`.
- **GitHub Actions minutes**: a read-only token ("Plan: read") in
  `[quotas] billing_token_file`.
- **The machine's health**: a journal of memory, load and disk at
  `[history] health_log`, one line every few minutes; the dashboard reads it
  if it exists.

## 13. Troubleshooting

| You see | It means |
|---|---|
| "no free session", "queue empty" in the relauncher's journal | a normal refusal: nothing to launch now |
| "tickets not created yet" on a spec | no open ticket names it as its parent: check `Parent: #n` |
| a launch refused by the guard | step 6: the guard is absent from the base branch, or answers wrong; the refusal says which |
| "no new worker: 3/3" | every session is busy; the spec starts when one is free |
| a worker's question, `needs-human` on its spec | it waits for you: answer on the issue, or `tracerworks message <spec>` |

`tracerworks <command> --help` documents each command.

## 14. Update and uninstall

```sh
uv tool upgrade tracerworks           # or: pipx upgrade tracerworks
```

To remove it:

```sh
systemctl --user disable --now tracerworks-next.timer tracerworks-serve
uv tool uninstall tracerworks         # or: pipx uninstall tracerworks
```

Your worktrees, project file and history base stay where the project file
put them.
