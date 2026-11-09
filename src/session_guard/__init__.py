"""The V1 guard-rails of CLAUDE.md, enforced on every session's tool calls.

CLAUDE.md says a session reads the V1 and the production shop and never
writes to them. Until 16/10 that was a sentence, and a sentence only holds
while the agent remembers it. This is a Claude Code PreToolUse hook: it sees
each call before it runs, and refuses three things.

- A claude.ai connector (`mcp__claude_ai_*`): those accounts are not the
  project's.
- A file write under the V1 repository, by Edit/Write or by an obvious shell
  write (redirection, `tee`, `sed -i`, `rm`, `mv`, a copy into it, a git
  command that changes it). The shell's directory starts at the payload's
  `cwd` and follows `cd`, `pushd`, `(cd` (#1529): a `cd` of an earlier call
  is not forgotten; `popd` makes it unknown.
- A shell call that writes to the production shop: a word whose HOST is the
  shop (any letter case, with or without a scheme), in a simple command that
  also carries a GraphQL `mutation`, a write verb or a body — every body
  option of curl. A domain merely cited (a heredoc, an `echo`, a document)
  triggers nothing: heredoc bodies are not commands.

The names it guards live in `.claude/session_guard.json`, the operator's
rulebook beside CLAUDE.md, never in this code (D12). `load_rules` refuses a
rulebook with a missing key or a value of the wrong kind: the hook then
fails CLOSED (`__main__`).

Its limit is the shell's: a script that writes from inside, or a name built at
run time, goes past a text scan. It catches the slip, not the intent.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CONFIG_FILE = Path(__file__).resolve().parents[2] / ".claude" / "session_guard.json"

#: The operator's escape (#1529): read in the HOOK's own environment, which
#: Claude Code hands down from the session's launch — a command of the
#: session runs in a child process and cannot set it. Set it when starting
#: a session (`SESSION_GUARD_REPAIR=1 claude`) to repair a guard that freezes
#: every call; it never opens the V1, the shop or the claude.ai rules.
ESCAPE = "SESSION_GUARD_REPAIR"

QUOTES = "'\""
_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
#: `&&`, `||`, `;`, `|`, a newline — and a lone `&`, but never the `&` of a
#: redirection (`2>&1`, `&>`, `>&`).
_SEPARATORS = re.compile(r"&&|\|\||(?<![<>&])&(?![>&])|[;|\n]")
_REDIRECT = re.compile(r"(?:>>?|\btee\b(?:\s+-a)?)\s*(['\"]?)([^\s'\"|;&]+)")
_IN_PLACE = re.compile(r"^(?:sed|perl)\b.*\s-i")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ANY_ARG_WRITES = {
    "rm",
    "rmdir",
    "mv",
    "touch",
    "mkdir",
    "chmod",
    "chown",
    "truncate",
    "ln",
}
_LAST_ARG_WRITES = {"cp", "rsync", "install"}
_GIT_WRITES = {
    "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean", "commit",
    "merge", "mv", "push", "rebase", "reset", "restore", "revert", "rm", "stash",
    "switch", "tag",
}  # fmt: skip
_CHANGE_DIR = {"cd", "pushd"}
_MUTATION = re.compile(r"\bmutation\b", re.IGNORECASE)
_WRITE_VERB = re.compile(
    r"(?:-X|--request)[\s=]*['\"]?(?:POST|PUT|PATCH|DELETE)\b", re.IGNORECASE
)
#: Every option of curl that sends a body.
_SENDS_BODY = re.compile(
    r"(?:^|\s)(?:-d|--data(?:-raw|-binary|-urlencode|-ascii)?|--json|-F|--form"
    r"(?:-string)?|-T|--upload-file)(?:[\s=]|$)"
)

_KEYS = (
    "read_only_dirs",
    "read_only_shops",
    "forbidden_tool_prefixes",
    "protected_paths",
)
#: Where the guard runs from: the project (the worktree) it guards.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: Claude Code's settings files, wherever they are (project, local, user):
#: their `env` key would hand the escape to the next session.
_SETTINGS_FILE = re.compile(r"(?:^|/)\.claude/settings(?:\.local)?\.json$")
_SETTINGS_IN_COMMAND = re.compile(r"\.claude/settings(?:\.local)?\.json")


class RulebookError(ValueError):
    """The rulebook is not what the guard expects: it cannot judge."""


class PayloadError(ValueError):
    """The tool call is not of the shape Claude Code sends: it cannot judge."""


@dataclass(frozen=True)
class Rules:
    """What the operator's rulebook protects."""

    read_only_dirs: tuple[str, ...]
    read_only_shops: tuple[str, ...]
    forbidden_tool_prefixes: tuple[str, ...]
    #: the guard's own files, relative to the project's root (#1529): its
    #: wiring, the local settings, its rulebook, its code
    protected_paths: tuple[str, ...] = ()


def load_rules(path: Path = CONFIG_FILE) -> Rules:
    """The rulebook, or `RulebookError` saying what to repair — absent,
    unreadable, not JSON, a key missing or unknown, a value that is not a
    non-empty list of non-empty strings."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RulebookError(f"règlement {path} illisible ({exc})") from exc
    except ValueError as exc:
        raise RulebookError(f"règlement {path} : JSON invalide ({exc})") from exc
    if not isinstance(data, dict):
        raise RulebookError(f"règlement {path} : un objet JSON est attendu")
    unknown = sorted(set(data) - set(_KEYS))
    if unknown:
        raise RulebookError(f"règlement {path} : clé(s) inconnue(s) {unknown}")
    for key in _KEYS:
        value = data.get(key)
        if not (
            isinstance(value, list)
            and value
            and all(isinstance(v, str) and v.strip() for v in value)
        ):
            raise RulebookError(
                f"règlement {path} : « {key} » doit être une liste non vide de textes"
            )
    for protected in data["protected_paths"]:
        pure = Path(protected)
        if pure.is_absolute() or ".." in pure.parts or protected.startswith("~"):
            raise RulebookError(
                f"règlement {path} : « protected_paths » : {protected} doit être"
                " relatif à la racine du projet"
            )
    return Rules(
        read_only_dirs=tuple(data["read_only_dirs"]),
        read_only_shops=tuple(s.lower() for s in data["read_only_shops"]),
        forbidden_tool_prefixes=tuple(data["forbidden_tool_prefixes"]),
        protected_paths=tuple(p.strip("/") for p in data["protected_paths"]),
    )


def escaped(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the operator started this session with the escape."""
    return bool((os.environ if environ is None else environ).get(ESCAPE, "").strip())


def checked_payload(payload: object) -> tuple[str, dict[str, Any], str | None]:
    """(tool, its input, the session's directory), or `PayloadError`."""
    if not isinstance(payload, dict):
        raise PayloadError("la charge n'est pas un objet JSON")
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        raise PayloadError("la charge n'a pas de « tool_name »")
    args = payload.get("tool_input")
    if not isinstance(args, dict):
        raise PayloadError(f"« tool_input » de {tool} n'est pas un objet")
    cwd = payload.get("cwd")
    return tool, args, cwd if isinstance(cwd, str) and cwd else None


def refusal(
    payload: dict[str, Any],
    rules: Rules,
    *,
    escape: bool = False,
    root: Path = PROJECT_ROOT,
) -> str | None:
    """Why this tool call must not run, or None when it may. A payload of an
    unexpected shape raises `PayloadError`: the caller refuses it.

    `escape`: the operator's escape is open — it lets the guard's own files
    be written, and nothing else; `root`: the project the guard guards."""
    tool, args, cwd = checked_payload(payload)
    if any(tool.startswith(prefix) for prefix in rules.forbidden_tool_prefixes):
        return (
            f"{tool} : les connecteurs claude.ai ne servent jamais dans ce projet "
            "(CLAUDE.md, Garde-fous absolus)."
        )
    if tool in _FILE_TOOLS:
        target = str(args.get("file_path") or args.get("notebook_path") or "")
        if cwd and target and not target.startswith(("/", "~")):
            target = posixpath.join(cwd, target)
        for name in rules.read_only_dirs:
            if _in_dir(target, name):
                return (
                    f"{target} : le dépôt V1 se lit, il ne s'écrit jamais (CLAUDE.md)."
                )
        if _SETTINGS_FILE.search(target) and ESCAPE in _written_text(args):
            return _ESCAPE_IN_SETTINGS.format(path=target)
        if not escape:
            protected = _protected(target, cwd or str(root), rules, root)
            if protected is not None:
                return _PROTECTED.format(path=protected)
    if tool == "Bash":
        command = args.get("command", "")
        if not isinstance(command, str):
            raise PayloadError("« command » de Bash n'est pas un texte")
        if ESCAPE in command and _SETTINGS_IN_COMMAND.search(command):
            return _ESCAPE_IN_SETTINGS.format(path="un fichier de réglages")
        return _bash_refusal(command, rules, cwd) or (
            None if escape else _bash_protected(command, rules, cwd, root)
        )
    return None


_PROTECTED = (
    "{path} : fichier du garde-fou de session — la session qu'il garde ne"
    " l'écrit pas. Le modifier est un geste de l'opérateur : hors session, ou dans"
    f" une session qu'il lance avec {ESCAPE}=1 (RUNBOOK_VPS_DEV §10). Demande-le"
    " (needs-human) plutôt que de chercher un détour."
)
_ESCAPE_IN_SETTINGS = (
    f"{{path}} : le nom de l'échappée {ESCAPE} n'entre dans aucun fichier de"
    " réglages de Claude Code — sa clé `env` la donnerait à la session suivante"
    " (RUNBOOK_VPS_DEV §10)."
)


def _written_text(args: dict[str, Any]) -> str:
    """What an edit tool would write: Write's content, Edit's new string,
    MultiEdit's new strings, NotebookEdit's source."""
    parts = [args.get("content"), args.get("new_string"), args.get("new_source")]
    edits = args.get("edits")
    if isinstance(edits, list):
        parts += [e.get("new_string") for e in edits if isinstance(e, dict)]
    return "\n".join(p for p in parts if isinstance(p, str))


def _resolved(word: str, here: str) -> str:
    path = word.strip(QUOTES)
    if path.startswith("~"):
        path = str(Path(path).expanduser())
    if not path.startswith("/"):
        path = posixpath.join(here, path)
    return posixpath.normpath(path)


def _protected(word: str, here: str, rules: Rules, root: Path) -> str | None:
    """The protected path `word` names (from the directory `here`), or None."""
    if not word:
        return None
    path = _resolved(word, here)
    for protected in rules.protected_paths:
        full = posixpath.normpath(str(root / protected))
        if path == full or path.startswith(full + "/"):
            return protected
    return None


def _bash_protected(
    command: str, rules: Rules, cwd: str | None, root: Path
) -> str | None:
    """A shell write naming one of the guard's files — the forms of the V1
    rule: redirection, `tee`, `sed -i`, `rm`, `mv`, a copy's target, a git
    command that names it. A `git merge` or `git pull` naming none passes."""
    here = cwd or str(root)
    for simple in _simple_commands(_without_heredocs(command)):
        words = _words(simple.lstrip("( ").rstrip(") "))
        if not words:
            continue
        if words[0] in _CHANGE_DIR:
            moved = _moved(here, words[1:])
            here = _resolved(moved, here) if moved is not None else str(root)
            continue
        if words[0] == "popd":
            here = str(root)
            continue
        named: list[str] = [target for _, target in _REDIRECT.findall(simple)]
        program, rest = words[0], words[1:]
        base = here
        if program == "git" and "-C" in rest:
            at = rest.index("-C")
            if at + 1 < len(rest):
                base = _resolved(rest[at + 1], here)
            rest = rest[:at] + rest[at + 2 :]
        if _IN_PLACE.search(simple) or program in _ANY_ARG_WRITES:
            named += rest
        elif program in _LAST_ARG_WRITES and rest:
            named.append(rest[-1])
        elif program == "git":
            sub = next((word for word in rest if not word.startswith("-")), "")
            if sub in _GIT_WRITES:
                named += [word for word in rest if word != sub]
        for word in named:
            if word.startswith("-"):
                continue
            hit = _protected(word, base, rules, root)
            if hit is not None:
                return _PROTECTED.format(path=hit)
    return None


def _in_dir(path: str, name: str) -> bool:
    return f"/{name}/" in f"/{path.strip(QUOTES)}/"


def _bash_refusal(command: str, rules: Rules, cwd: str | None) -> str | None:
    simples = _simple_commands(_without_heredocs(command))
    for shop in rules.read_only_shops:
        if any(_writes_to_shop(simple, shop) for simple in simples):
            return (
                f"{shop} : une session lit la boutique de production, elle n'y "
                "écrit jamais ; le produit y écrit par son port (CLAUDE.md, "
                "Garde-fous absolus)."
            )
    for name in rules.read_only_dirs:
        if _writes_to_dir(simples, name, cwd):
            return f"{name} : le dépôt V1 se lit, il ne s'écrit jamais (CLAUDE.md)."
    return None


def _without_heredocs(command: str) -> str:
    """The command with every heredoc's body taken out: its lines are text a
    program reads, not commands — the audit's `cat > doc.md <<EOF` citing the
    shop next to `--data` was refused (#1529)."""
    lines = command.split("\n")
    kept: list[str] = []
    waiting: list[str] = []
    for line in lines:
        if waiting:
            if line.strip() == waiting[0]:
                waiting.pop(0)
            continue
        kept.append(line)
        waiting = [match.group(2) for match in _HEREDOC.finditer(line)]
    return "\n".join(kept)


def _simple_commands(command: str) -> list[str]:
    return [part.strip() for part in _SEPARATORS.split(command) if part.strip()]


def _hosts(word: str) -> list[str]:
    """The hosts a word names: a URL's, or a bare `host/path`; `--opt=URL` too."""
    hosts = []
    for candidate in (word, word.split("=", 1)[1] if "=" in word else ""):
        candidate = candidate.strip(QUOTES)
        if not candidate:
            continue
        if "://" in candidate:
            host = urlparse(candidate).hostname or ""
        else:
            host = re.split(r"[/:?#]", candidate, maxsplit=1)[0]
        hosts.append(host.lower())
    return hosts


def _writes_to_shop(simple: str, shop: str) -> bool:
    words = _words(simple)
    if not any(shop in _hosts(word) for word in words):
        return False
    if "graphql" in simple.lower():
        return bool(_MUTATION.search(simple))
    return bool(_WRITE_VERB.search(simple) or _SENDS_BODY.search(simple))


def _writes_to_dir(simples: list[str], name: str, cwd: str | None) -> bool:
    here: str | None = cwd  # the shell's directory; None: unknown
    for simple in simples:
        words = _words(simple.lstrip("( ").rstrip(") "))
        if not words:
            continue
        if words[0] in _CHANGE_DIR:
            here = _moved(here, words[1:])
            continue
        if words[0] == "popd":
            here = None
            continue
        inside = here is not None and _in_dir(here, name)
        if not (inside or name in simple):
            continue
        for _, target in _REDIRECT.findall(simple):
            if (inside and not target.startswith("/")) or _in_dir(target, name):
                return True
        if _IN_PLACE.search(simple) and (inside or name in simple):
            return True
        if _command_writes(words, name, inside):
            return True
    return False


def _moved(here: str | None, args: list[str]) -> str | None:
    target = next((a for a in args if not a.startswith("-")), "~")
    if target == "-":
        return None
    if target.startswith(("/", "~")):
        return str(Path(target).expanduser())
    if here is None:
        return target  # relative to an unknown place: judged by its name
    return posixpath.normpath(posixpath.join(here, target))


def _command_writes(words: list[str], name: str, inside: bool) -> bool:
    program, rest = words[0], words[1:]
    touches = inside or any(_in_dir(word, name) for word in rest)
    if program in _ANY_ARG_WRITES:
        return touches
    if program in _LAST_ARG_WRITES:
        return bool(rest) and (inside or _in_dir(rest[-1], name))
    if program == "git":
        if "-C" in rest:
            at = rest.index("-C")
            in_repo = at + 1 < len(rest) and _in_dir(rest[at + 1], name)
            rest = rest[:at] + rest[at + 2 :]
        else:
            in_repo = inside
        sub = next((word for word in rest if not word.startswith("-")), "")
        return in_repo and sub in _GIT_WRITES
    return False


def _words(simple: str) -> list[str]:
    try:
        words = shlex.split(simple)
    except ValueError:
        words = simple.split()
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    if words and words[0] == "env":  # `env VAR=1 rm …` is `rm …`
        words = words[1:]
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
    return words
