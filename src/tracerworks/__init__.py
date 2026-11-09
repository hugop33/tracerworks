"""`tracerworks` — a stateless CLI that drives the development workers of the VPS.

Spec #565; its architecture is written beside the project it builds. There
is no database: every command rebuilds the state of the world from GitHub,
`git worktree list`, tmux and `/proc`, by naming conventions
(`conventions.py`). The only file it writes is its configuration
(`state_file.py`).

Standard library only, on purpose: it is installed from `main` as a bare copy of
this directory (`tracerworks self-update`), next to — never inside — the project's
virtualenv, so a worker editing its own worktree cannot change the tool that
drives it.
"""
