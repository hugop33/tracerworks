# Security policy

tracerworks runs coding agents that write and execute code on your machine,
so a flaw in it can matter. Thank you for reporting one privately.

## Reporting a vulnerability

Use GitHub's private reporting: the **Security** tab of this repository,
then **Report a vulnerability**. Please do not open a public issue or
discussion for a security problem.

Say what you found, how to reproduce it, and what an attacker could do with
it. This is a one-person project: allow a few days for an answer. Once a fix
is published, you are credited in its commit, unless you prefer not to be.

## Scope

In scope: tracerworks itself (the command, the dashboard, the merge queue)
and the session guard (`src/session_guard`), for example:

- a way for an untrusted issue, comment or pull request to become an
  instruction to a worker;
- a way past the session guard, or past the tools denied to a worker;
- the dashboard readable or writable without its password;
- a secret written to a log, a comment or the dashboard.

Out of scope: Claude Code, GitHub and your own CI, which have their own
security policies, and a setup that ignores the security model of the
README (a dashboard on a public interface, workers holding your own token).

## Supported versions

Only the latest commit of the `main` branch is supported.
