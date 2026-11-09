"""`tracerworks message`, `pause`, `resume`: never a shell, reception verified (#568).

The unit half plays the worker's pane as a script of captures and checks what
was sent: the bytes handed to `tmux load-buffer` on stdin, and that the message
text appears in NO argument list. The real half pastes a hostile message into
a `cat` on a SEPARATE tmux server (`-L`, never the session `widgets`) and
compares the bytes that came out.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from tests.tracerworks.fakes import (
    TMUX,
    FakeRunner,
    fixture,
    private_tmux_socket,
    recorded_settings,
    recorded_world,
    target,
)
from tracerworks.cli import EXIT_OK, EXIT_REFUSED, RESUME_TEXT, Env, run
from tracerworks.messaging import POLL_SECONDS, Messenger, NotIdleError, envelope
from tracerworks.process import Result, run_process
from tracerworks.providers import CLAUDE
from tracerworks.runtime.tmux import Tmux

HOSTILE = (
    "Ne touche pas à `rm -rf ~` ni à $(touch /tmp/tracerworks-pwned) ; l'apostrophe,\n"
    'les "guillemets", $HOME, ${PATH}, \\n littéral, un ; && || > et | …\n'
    "et de l'unicode : é à ç œ — « » \u276f 🚀\n"
)
W510 = target("worker-510")
LINE = "─" * 60
PROMPT = CLAUDE.prompt_glyph


def pane(thread: str = "", field: str = "", busy: bool = False) -> str:
    status = "  ⏵⏵ auto mode on · esc to interrupt" if busy else "  ⏵⏵ auto mode on"
    return f"{thread}\n\n{LINE}\n{PROMPT}\xa0{field}\n{LINE}\n{status}\n"


class Clock:
    def __init__(self) -> None:
        self.now = 1790413800.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def script(fake: FakeRunner, window_target: str, panes: Sequence[str]) -> None:
    """Every capture of the window answers the next pane; the last one repeats."""
    queue: Iterator[str] = iter(panes)
    last = [panes[0]]

    def answer(_: Sequence[str], __: bytes | None) -> Result:
        last[0] = next(queue, last[0])
        return Result(0, last[0])

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)


def sent_pane(fake: FakeRunner, busy: bool = False) -> str:
    """The pane once the last pasted envelope is in the thread."""
    data = fake.ran(*TMUX, "load-buffer")[-1].stdin
    assert data is not None
    return pane(thread=data.decode(), busy=busy)


def world_with_paste() -> FakeRunner:
    fake = recorded_world()
    fake.answer(*TMUX, "load-buffer", prefix=True)
    fake.answer(*TMUX, "paste-buffer", prefix=True)
    fake.answer(*TMUX, "send-keys", prefix=True)
    return fake


def make_env(
    fake: FakeRunner, tmp_path: Path, clock: Clock, stdin: bytes = b""
) -> tuple[Env, io.StringIO, io.StringIO]:
    out, err = io.StringIO(), io.StringIO()
    env = Env(
        recorded_settings(tmp_path),
        runner=fake,
        now=lambda: clock.now,
        sleep=clock.sleep,
        stdin=io.BytesIO(stdin),
        out=out,
        err=err,
    )
    return env, out, err


def dynamic_panes(fake: FakeRunner, before: list[str], after: list[str | None]) -> None:
    """Panes before the first paste, then after it (None = « the sent pane »)."""
    state = {"i": 0, "j": 0}

    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] != W510:
            return Result(0, pane())
        if not fake.ran(*TMUX, "load-buffer"):
            i = min(state["i"], len(before) - 1)
            state["i"] += 1
            return Result(0, before[i])
        j = min(state["j"], len(after) - 1)
        state["j"] += 1
        chosen = after[j]
        return Result(0, sent_pane(fake) if chosen is None else chosen)

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)


# --- the bytes ------------------------------------------------------------


def test_a_hostile_message_arrives_as_is_and_never_as_an_argument(
    tmp_path: Path,
) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [pane()], [None])
    clock = Clock()
    env, out, _ = make_env(fake, tmp_path, clock, HOSTILE.encode())

    assert run(env, ["message", "510"]) == EXIT_OK

    [load] = fake.ran(*TMUX, "load-buffer")
    assert load.stdin is not None
    sent = load.stdin.decode("utf-8")
    assert sent.startswith("Message de Sam (")
    assert sent.endswith(HOSTILE.strip())
    for call in fake.calls:
        assert not any("pwned" in arg or "rm -rf" in arg for arg in call.argv)
    [paste] = fake.ran(*TMUX, "paste-buffer")
    assert "-p" in paste.argv and "-d" in paste.argv and paste.argv[-1] == W510
    assert "message received by worker-510" in out.getvalue()


def test_the_message_can_come_from_a_file(tmp_path: Path) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [pane()], [None])
    message = tmp_path / "msg.txt"
    message.write_text(HOSTILE, encoding="utf-8")
    env, _, _ = make_env(fake, tmp_path, Clock())

    assert run(env, ["message", "510", "--file", str(message)]) == EXIT_OK
    stdin = fake.ran(*TMUX, "load-buffer")[0].stdin
    assert stdin is not None and HOSTILE.strip() in stdin.decode()


@pytest.mark.parametrize("data", [b"", b"   \n", b"\xff\xfe not utf-8"])
def test_an_empty_or_undecodable_message_is_refused(
    tmp_path: Path, data: bytes
) -> None:
    env, _, err = make_env(world_with_paste(), tmp_path, Clock(), data)

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert "empty message" in err.getvalue()


# --- waiting, verifying, retrying -------------------------------------------


def queued(fake: FakeRunner, name: str, measured: str) -> str:
    """A pane recorded on 30/09, its measured marker swapped for the one sent."""
    data = fake.ran(*TMUX, "load-buffer")[-1].stdin
    assert data is not None
    sent = data.decode().split(", ")[1].split(")")[0]
    return fixture(name).replace(measured, sent)


def test_a_busy_worker_gets_the_message_at_once_and_it_waits_in_its_queue(
    tmp_path: Path,
) -> None:
    """Measured on 30/09: pasted while Claude Code works, the message waits
    above the spinner (« ctrl+x ctrl+s to send now ») and is taken at the end
    of the step."""
    fake = world_with_paste()
    busy = fixture("pane-claude-working-empty-field.txt")

    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] != W510 or not fake.ran(*TMUX, "load-buffer"):
            return Result(0, busy)
        return Result(
            0, queued(fake, "pane-claude-working-message-queued.txt", "m-abc123")
        )

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)
    clock = Clock()
    env, out, err = make_env(fake, tmp_path, clock, b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    assert POLL_SECONDS not in clock.sleeps
    assert sum(clock.sleeps) < 30
    assert len(fake.ran(*TMUX, "load-buffer")) == 1
    assert "queued" in out.getvalue()


def test_a_long_message_queued_is_recognised_by_its_marker(tmp_path: Path) -> None:
    """Folded in the field (« [Pasted text #1 +32 lines] »), unfolded in the queue."""
    fake = world_with_paste()
    busy = fixture("pane-claude-working-empty-field.txt")

    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] != W510 or not fake.ran(*TMUX, "load-buffer"):
            return Result(0, busy)
        name = "pane-claude-working-long-message-queued.txt"
        return Result(0, queued(fake, name, "m-def456"))

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)
    env, out, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    assert "queued" in out.getvalue()


def test_a_busy_worker_is_no_proof_for_a_worker_that_was_already_busy(
    tmp_path: Path,
) -> None:
    """Counter-proof: busy before, busy after, no marker anywhere → not received.

    And never pasted twice: a busy worker queues what it gets, so a second copy
    would more likely double the order than save it."""
    fake = world_with_paste()
    busy = fixture("pane-claude-working-empty-field.txt")
    dynamic_panes(fake, [busy], [busy])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert "message NOT received by worker-510" in err.getvalue()
    assert "nothing pasted again" in err.getvalue()
    assert len(fake.ran(*TMUX, "load-buffer")) == 1


def test_a_message_taken_between_two_looks_is_received_once(tmp_path: Path) -> None:
    """Not seen at the first look (step ending), seen at the second: one paste."""
    fake = world_with_paste()
    busy = fixture("pane-claude-working-empty-field.txt")
    dynamic_panes(fake, [busy], [busy, None])
    env, out, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    assert len(fake.ran(*TMUX, "load-buffer")) == 1
    assert "nothing pasted again" in out.getvalue()


def test_a_busy_worker_with_a_draft_in_its_field_gets_nothing(tmp_path: Path) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [pane(field="brouillon de Sam", busy=True)], [None])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert "already holds text" in err.getvalue()
    assert not fake.ran(*TMUX, "load-buffer")


TRUST_DIALOG = (
    " Accessing workspace:\n /tmp/probe826\n"
    " Quick safety check: Is this a project you created or one you trust?\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    f" {PROMPT} No, exit\n   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel\n"
)


def test_the_trust_dialog_of_a_new_folder_gets_nothing(tmp_path: Path) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [TRUST_DIALOG], [None])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert "folder trust dialog" in err.getvalue()
    assert not fake.ran(*TMUX, "load-buffer")


def test_wait_idle_waits_for_the_worker_to_be_at_rest_before_pasting(
    tmp_path: Path,
) -> None:
    fake = world_with_paste()
    # The first captures are the status readings that find the worker.
    busy = pane(busy=True)
    dynamic_panes(fake, [busy, busy, busy, busy, pane()], [None])
    clock = Clock()
    env, _, _ = make_env(fake, tmp_path, clock, b"stop X")

    assert run(env, ["message", "510", "--wait-idle"]) == EXIT_OK
    assert clock.sleeps[:2] == [POLL_SECONDS, POLL_SECONDS]


def test_wait_idle_sends_nothing_to_a_worker_busy_for_the_whole_wait(
    tmp_path: Path,
) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [pane(busy=True)], [None])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510", "--wait-idle"]) == EXIT_REFUSED
    assert "nothing was sent" in err.getvalue()
    assert not fake.ran(*TMUX, "load-buffer")


def test_text_left_in_the_field_is_not_a_message_received(tmp_path: Path) -> None:
    """The marker is on screen — in the field. Enter was lost: press it again."""
    fake = world_with_paste()
    stuck = pane(field="Message de Sam (26/09 10h30, m-aaaaaa) : stop X")
    dynamic_panes(fake, [pane()], [stuck, None])
    env, out, _ = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK
    assert len(fake.ran(*TMUX, "load-buffer")) == 1
    enters = [c for c in fake.ran(*TMUX, "send-keys") if c.argv[-1] == "Enter"]
    assert len(enters) == 2
    assert "Enter sent again" in out.getvalue()


def test_a_lost_message_is_pasted_again_under_a_new_marker(tmp_path: Path) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [pane()], [pane(), pane(), None])
    env, out, _ = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK
    first, second = (c.stdin for c in fake.ran(*TMUX, "load-buffer"))
    assert first is not None and second is not None
    marker = second.decode().split(", ")[1].split(")")[0]
    assert marker.startswith("m-") and marker not in first.decode()
    assert "message lost" in out.getvalue()


def test_after_one_retry_the_failure_is_said_clearly(tmp_path: Path) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [pane()], [pane()])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert "message NOT received by worker-510" in err.getvalue()
    assert len(fake.ran(*TMUX, "load-buffer")) == 2


NO_FIELD = "Do you want to proceed?\n  1. Yes\n  2. No\n"


@pytest.mark.parametrize(
    ("screen", "why"),
    [
        (NO_FIELD, "does not show its input field"),
        (pane(field="brouillon de Sam", busy=True), "already holds text"),
        (pane(field="[Pasted text #1 +30 lines]"), "folded paste"),
    ],
    ids=["dialog", "busy-draft", "folded-paste"],
)
def test_nothing_is_pasted_onto_a_dialog_or_a_draft(
    tmp_path: Path, screen: str, why: str
) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [screen], [None])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert why in err.getvalue() and "nothing was sent" in err.getvalue()
    assert not fake.ran(*TMUX, "load-buffer")


def test_no_second_paste_when_the_field_vanished_after_the_send(tmp_path: Path) -> None:
    """A dialog took the screen: a second copy would be typed into it."""
    fake = world_with_paste()
    dynamic_panes(fake, [pane()], [NO_FIELD])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert "input field invisible after sending" in err.getvalue()
    assert len(fake.ran(*TMUX, "load-buffer")) == 1


def test_a_worker_back_at_work_has_received_it(tmp_path: Path) -> None:
    """A long paste can be folded in the thread; going busy is the proof then."""
    fake = world_with_paste()
    dynamic_panes(fake, [pane()], [pane(thread="[Pasted text #1 +3 lines]", busy=True)])
    env, _, _ = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK


# --- who can be messaged ------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "why"), [(520, "is not running"), (530, "dead"), (999, "is not running")]
)
def test_only_a_live_worker_is_messaged(tmp_path: Path, spec: int, why: str) -> None:
    fake = world_with_paste()
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", str(spec)]) == EXIT_REFUSED
    assert why in err.getvalue()
    assert not fake.ran(*TMUX, "load-buffer")


def test_pause_presses_escape_and_resume_sends_the_resume_instruction(
    tmp_path: Path,
) -> None:
    fake = world_with_paste()
    dynamic_panes(fake, [pane()], [None])
    env, _, _ = make_env(fake, tmp_path, Clock())

    assert run(env, ["pause", "510"]) == EXIT_OK
    assert fake.ran(*TMUX, "send-keys", "-t", W510, "Escape")

    extra = tmp_path / "extra.txt"
    extra.write_text("Commence par le ticket #503.", encoding="utf-8")
    assert run(env, ["resume", "510", "--file", str(extra)]) == EXIT_OK
    stdin = fake.ran(*TMUX, "load-buffer")[-1].stdin
    assert stdin is not None
    assert RESUME_TEXT.format(operator="Sam") in stdin.decode()
    assert "ticket #503" in stdin.decode()


def test_wait_idle_is_bounded(tmp_path: Path) -> None:
    fake = FakeRunner()
    script(fake, W510, [pane(busy=True)])
    clock = Clock()
    messenger = Messenger(
        Tmux(fake, "widgets", None),
        "worker-510",
        CLAUDE,
        "Sam",
        clock.sleep,
        lambda: clock.now,
    )

    with pytest.raises(NotIdleError):
        messenger.wait_idle(timeout=30)
    assert sum(clock.sleeps) == 30


# --- the real tmux, on a server of its own ----------------------------------


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_real_tmux_pastes_the_bytes_and_runs_nothing(tmp_path: Path) -> None:
    """A `cat` stands in for the worker: what it receives is what was sent.

    The window runs `cat > received.txt` on a private tmux server; the hostile
    message goes through `Tmux.paste`, then Ctrl-D closes the file. If any
    shell had parsed the text, `$(touch …)` would have created `pwned`.
    """
    socket = private_tmux_socket(tmp_path)
    received = tmp_path / "received.txt"
    pwned = tmp_path / "pwned"
    message = HOSTILE.replace("/tmp/tracerworks-pwned", str(pwned))
    base = ["tmux", "-L", socket]
    subprocess.run(
        [*base, "new-session", "-d", "-s", "widgets", "-x", "200", "-y", "50"],
        check=True,
    )
    try:
        subprocess.run(
            [
                *base,
                "new-window",
                "-d",
                "-t",
                "=widgets:",
                "-n",
                "worker-1",
                "-c",
                str(tmp_path),
                "bash",
                "--norc",
                "-c",
                'stty -echo; exec cat > "$0"',
                str(received),
            ],
            check=True,
        )
        tmux = Tmux(run_process, "widgets", socket)
        tmux.paste("worker-1", message.encode("utf-8"), buffer="tracerworks-test")
        tmux.send_keys("worker-1", "C-d")
        for _ in range(50):
            if not tmux.list_windows() or all(
                w.name != "worker-1" for w in tmux.list_windows()
            ):
                break
            subprocess.run(["sleep", "0.1"], check=True)
        assert received.read_text(encoding="utf-8") == message
        assert not pwned.exists()
    finally:
        subprocess.run([*base, "kill-server"], check=False)


def test_the_envelope_names_operator_the_time_and_the_marker() -> None:
    assert envelope("  stop X \n", "m-abc123", "26/09 10h32", "Sam") == (
        "Message de Sam (26/09 10h32, m-abc123) :\nstop X"
    )


def test_a_worker_whose_field_shows_a_suggestion_gets_the_message(
    tmp_path: Path,
) -> None:
    fake = world_with_paste()
    suggested = pane().replace(
        f"{PROMPT}\xa0\n", f"{PROMPT}\xa0\x1b[2mfais un commit\x1b[0m\n"
    )
    dynamic_panes(fake, [suggested], [None])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    assert any("-e" in c.argv for c in fake.ran(*TMUX, "capture-pane"))


# --- a text left in the field of a worker at rest (#2003) -------------------


def field_until_cleared(fake: FakeRunner, left: str, clears: bool = True) -> None:
    """Worker-510's field holds `left` until a Ctrl-U (if `clears`), then is
    empty; once pasted, the sent pane."""

    def answer(argv: Sequence[str], _: bytes | None) -> Result:
        if argv[-1] != W510:
            return Result(0, pane())
        if fake.ran(*TMUX, "load-buffer"):
            return Result(0, sent_pane(fake))
        cleared = clears and fake.ran(*TMUX, "send-keys", "-t", W510, "C-u")
        return Result(0, pane() if cleared else left)

    fake.on(*TMUX, "capture-pane", prefix=True)(answer)


def keys(fake: FakeRunner) -> list[str]:
    """The keys sent and the pastes, in order."""
    return [
        "paste" if c.argv[1] == "load-buffer" else c.argv[-1]
        for c in fake.calls
        if c.argv[:2] in (("tmux", "send-keys"), ("tmux", "load-buffer"))
    ]


@pytest.mark.parametrize(
    ("left", "quoted"),
    [
        (pane(field="ouvre la PR de #1587"), "ouvre la PR de #1587"),
        (
            pane(field="ligne une\n  ligne deux"),
            "ligne une\nligne deux",
        ),
    ],
    ids=["one-line", "two-lines"],
)
def test_a_text_left_at_rest_is_quoted_the_field_cleared_then_all_sent(
    tmp_path: Path, left: str, quoted: str
) -> None:
    """26/10: worker-1586 at rest, a text typed and never submitted; `Enter`
    did nothing, `message` refused the field. Now the text is quoted at the
    head of what is pasted, the field emptied (Ctrl-U, line by line), then
    the whole submitted — and the text said in tracerworks's log first."""
    fake = world_with_paste()
    field_until_cleared(fake, left)
    env, out, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_OK, err.getvalue()
    sent = fake.ran(*TMUX, "load-buffer")[0].stdin
    assert sent is not None
    body = sent.decode().split(":\n", 1)[1]
    assert body == (
        f"Texte resté dans ton champ, non soumis (tapé dans ta fenêtre) :\n{quoted}"
        "\n\nstop X"
    )
    order = keys(fake)
    assert order[: order.index("paste")] == ["C-u"] * (quoted.count("\n") + 2)
    assert order[order.index("paste") + 1 :] == ["Enter"]
    assert quoted.splitlines()[0] in err.getvalue() + out.getvalue()


def test_a_field_that_will_not_clear_gets_nothing_and_its_text_is_said(
    tmp_path: Path,
) -> None:
    fake = world_with_paste()
    field_until_cleared(fake, pane(field="ouvre la PR de #1587"), clears=False)
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert "could not be cleared" in err.getvalue()
    assert "ouvre la PR de #1587" in err.getvalue()  # said, never lost
    assert not fake.ran(*TMUX, "load-buffer")


def test_a_resume_takes_a_text_left_in_the_field_the_same_way(tmp_path: Path) -> None:
    """`resume`, like every sending of tracerworks's (the memory's resume, a red
    told, a nudge), goes through the same delivery: the text is carried."""
    fake = world_with_paste()
    field_until_cleared(fake, pane(field="ouvre la PR de #1587"))
    env, _, err = make_env(fake, tmp_path, Clock())

    assert run(env, ["resume", "510"]) == EXIT_OK, err.getvalue()
    sent = fake.ran(*TMUX, "load-buffer")[0].stdin
    assert sent is not None
    assert (
        "non soumis (tapé dans ta fenêtre) :\nouvre la PR de #1587\n\n" in sent.decode()
    )
    assert RESUME_TEXT.split()[0] in sent.decode()


def test_a_past_prompt_under_a_dialog_is_no_field_to_clear(tmp_path: Path) -> None:
    """A glyph's line no rule closes is a past prompt in the thread: refused as
    before, nothing cleared into whatever holds the screen."""
    fake = world_with_paste()
    past = f"{PROMPT}\xa0fais le commit\n  et pousse\n\nUpdate available\n"
    dynamic_panes(fake, [past], [None])
    env, _, err = make_env(fake, tmp_path, Clock(), b"stop X")

    assert run(env, ["message", "510"]) == EXIT_REFUSED
    assert "already holds text" in err.getvalue()
    assert not fake.ran(*TMUX, "send-keys", "-t", W510, "C-u")
    assert not fake.ran(*TMUX, "load-buffer")
