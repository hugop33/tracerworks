"""Sending a message to a worker, and proving it arrived (architecture §7.1).

The procedure of the skill `pilotage-vps`, measured on the VPS, made into code:

1. the text never enters a command line: it is loaded into a tmux buffer from
   stdin and pasted (bracketed), so `$(…)`, backticks, apostrophes and `$HOME`
   stay bytes (#514, V33 — they used to run under `dev`);
2. send AT ONCE, busy or not (#826): measured on 30/09, a message submitted
   while Claude Code works waits in its queue, shown above the spinner, and is
   taken at the end of the step, unfolded in the thread with its marker. The
   losses of 23/09 (two out of four, typed while it thought) did not show on
   v2.1.285; waiting for rest (up to 900 s, #514) left orders undelivered for a
   whole CI run instead. `--wait-idle` keeps the old way.
   Either way, paste only onto an EMPTY, VISIBLE input field — never into a
   permission dialog, the trust dialog of a new folder, or someone's draft;
3. submit, then VERIFY: the input field is empty AND the fresh marker is in
   the queue or the thread, or — for a worker that was at rest — it went back
   to work (a long paste can be folded into « [Pasted text #1] » in the
   thread, hiding the marker; the queue shows it unfolded). A worker busy
   before the send proves nothing by being busy after. Text present in the
   field is not a message received;
4. retry once — re-press Enter if the text is still in the field, paste
   again under a NEW marker only if the field is empty with no trace of the
   message AND the worker was at rest: a busy worker queues what it gets, so
   a second paste would more likely double the order than save it (#826), and
   it is only looked at once more — then say clearly that it failed. When no
   input field is visible after the send, nothing is pasted again: a second
   copy could land in whatever took the screen.

A TEXT LEFT IN THE FIELD OF A WORKER AT REST (#2003). On 26/10 worker-1586
stayed at rest for hours, a text typed in its field and never submitted;
`Enter` did nothing, and a refusal of the taken field let no resume through.
Now the text is said in tracerworks's log first (never lost, whatever follows),
quoted at the head of what is pasted, and the field emptied — Ctrl-U, which
deletes one line of it and does nothing on an empty field, measured on Claude
Code 2.1.312 (28/10); never Escape, which would interrupt a busy worker.
A long paste folded into « [Pasted text #1 +30 lines] » shows none of its
words: emptied it would be lost, so it is refused as before. A busy worker's
field is someone's draft: refused as before.

A WORKER WITH HOOKS PROVES IT ITSELF (#1731). The screen said « lost » for
messages that had arrived (12/10, 14/10, 19/10: queued while the worker
thought, its marker scrolled away): a refusal that would have doubled them.
When the worker's state file exists, « received » means its
`UserPromptSubmit` hook saw the marker in a submitted prompt; « queued »,
that the screen shows it waiting with no submission yet — taken at the end of
the step, never sent again; « lost », neither within `RECEIPT_SECONDS`. The
screen still says where to paste, and whether the text sits in the field.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass

from tracerworks import texts
from tracerworks.observe import (
    FOLDED_PASTE,
    bottom_lines,
    input_field,
    is_busy,
    pending_field,
    plain,
)
from tracerworks.providers import Provider
from tracerworks.runtime.tmux import Tmux

WAIT_IDLE_SECONDS = 900.0
POLL_SECONDS = 5.0
SETTLE_SECONDS = 2.0  # between the paste and its Enter
VERIFY_SECONDS = 8.0  # before reading the pane back
HISTORY_LINES = 200
DIALOG_LINES = 12  # where the trust dialog is looked for, from the bottom
#: How long a worker's hook is waited for to see the message (#1731), and
#: how often its state file is read meanwhile.
RECEIPT_SECONDS = 30.0
RECEIPT_POLL_SECONDS = 2.0

#: How many times a text left in a field is cleared and looked at again.
CLEAR_ROUNDS = 3
#: Ctrl-U deletes one line of the field: one per line, and one more for a
#: line the screen wrapped (measured on Claude Code 2.1.312, 28/10).
EXTRA_CLEARS = 1
#: What heads a text left in a field, quoted in the message (#2003).
PENDING_HEAD = "Texte resté dans ton champ, non soumis (tapé dans ta fenêtre) :"

#: Did the worker's hook see a prompt with this marker? None: no hooks.
Heard = Callable[[str], bool | None]


class NotIdleError(RuntimeError):
    """The worker is not ready for a message; nothing was sent."""


@dataclass(frozen=True)
class Delivery:
    received: bool
    marker: str
    attempts: list[str]  # what each attempt concluded, for the report


def new_marker() -> str:
    return f"m-{secrets.token_hex(3)}"


def with_pending(pending: str, text: str) -> str:
    """The message, a text left in the field quoted at its head."""
    if not pending:
        return text
    return f"{PENDING_HEAD}\n{pending}\n\n{text.strip()}"


def envelope(text: str, marker: str, stamp: str, sender: str) -> str:
    """The message as the worker reads it: dated, signed, with its marker."""
    return f"Message de {sender} ({stamp}, {marker}) :\n{text.strip()}"


@dataclass
class Messenger:
    tmux: Tmux
    window: str
    provider: Provider
    sender: str
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    make_marker: Callable[[], str] = new_marker
    heard: Heard | None = None  # its hooks' proof (#1731); None: the screen
    note: Callable[[str], None] = lambda line: None  # tracerworks's log (#2003)

    def wait_idle(self, timeout: float = WAIT_IDLE_SECONDS) -> str:
        """Wait for rest, then require a visible input field; what was left
        in it, quoted then cleared ("" none)."""
        deadline = self.clock() + timeout
        while is_busy(
            plain(pane := self.tmux.capture(self.window, escapes=True)), self.provider
        ):
            if self.clock() >= deadline:
                raise NotIdleError(
                    texts.STAYED_BUSY.format(window=self.window, seconds=timeout)
                )
            self.sleep(POLL_SECONDS)
        return self._take_field(pane, busy=False)

    def busy_before_send(self, wait_idle: bool, text: str) -> tuple[bool, str]:
        """Whether the worker is busy, once its field is known to take a
        paste; and the text to paste, what was left in the field quoted at
        its head, the field cleared."""
        if wait_idle:
            return False, with_pending(self.wait_idle(), text)
        pane = self.tmux.capture(self.window, escapes=True)
        busy = is_busy(plain(pane), self.provider)
        return busy, with_pending(self._take_field(pane, busy), text)

    def _take_field(self, pane: str, busy: bool) -> str:
        dialog = self.provider.trust_dialog
        if dialog is not None and dialog in "\n".join(
            bottom_lines(plain(pane), DIALOG_LINES)
        ):
            raise NotIdleError(texts.TRUST_DIALOG.format(window=self.window))
        shown = input_field(pane, self.provider)
        if shown is None:
            raise NotIdleError(texts.NO_FIELD.format(window=self.window))
        if not shown:
            return ""
        field = pending_field(pane, self.provider)
        if busy or field is None:  # someone's draft; or no field closed by its rule
            raise NotIdleError(
                texts.FIELD_TAKEN.format(window=self.window, text=repr(shown[:40]))
            )
        if FOLDED_PASTE.search(field):  # its words are not on the screen
            raise NotIdleError(
                texts.FIELD_FOLDED.format(window=self.window, text=repr(field[:40]))
            )
        # said before anything is cleared: never lost, whatever follows
        self.note(texts.FIELD_QUOTED.format(window=self.window, text=repr(field)))
        left: str | None = field
        for _ in range(CLEAR_ROUNDS):
            for _ in range(field.count("\n") + 1 + EXTRA_CLEARS):
                self.tmux.send_keys(self.window, "C-u")
            self.sleep(SETTLE_SECONDS)
            left = input_field(
                self.tmux.capture(self.window, escapes=True), self.provider
            )
            if left == "":
                return field
        raise NotIdleError(
            texts.FIELD_NOT_CLEARED.format(
                window=self.window, text=repr(field), left=repr((left or "")[:40])
            )
        )

    def _verdict(self, marker: str, busy_before: bool) -> tuple[bool, str | None]:
        pane = self.tmux.capture(self.window, history=HISTORY_LINES, escapes=True)
        field = input_field(pane, self.provider)
        text = plain(pane)
        seen = marker in text or (not busy_before and is_busy(text, self.provider))
        return field == "" and seen, field

    def _hint_after(self, marker: str) -> tuple[bool, str | None]:
        """Whether the hint of a queued message follows OUR marker on the
        screen, and what sits in the input field."""
        hint = self.provider.queued_hint
        pane = self.tmux.capture(self.window, history=HISTORY_LINES, escapes=True)
        text = plain(pane)
        after = text.rpartition(marker)[2] if marker in text else ""
        return hint is not None and hint in after, input_field(pane, self.provider)

    def _received(self, marker: str) -> str:
        """« queued » when the hint of a queued message follows OUR marker."""
        queued, _ = self._hint_after(marker)
        return texts.RECEIVED_QUEUED if queued else texts.RECEIVED_PLAIN

    def _submit(self, text: str, marker: str, stamp: str) -> None:
        data = envelope(text, marker, stamp, self.sender).encode("utf-8")
        self.tmux.paste(self.window, data, buffer=f"tracerworks-{marker}")
        self.sleep(SETTLE_SECONDS)
        self.tmux.send_keys(self.window, "Enter")
        self.sleep(VERIFY_SECONDS)

    def _outcome(self, marker: str, received: bool) -> str:
        return self._received(marker) if received else texts.NOT_RECEIVED_PLAIN

    def _queued(self, marker: str) -> bool:
        """The screen shows OUR message waiting in the queue, the field empty."""
        queued, field = self._hint_after(marker)
        return queued and field == ""

    def _receipt(self, marker: str) -> str | None:
        """What the hook and the screen say within `RECEIPT_SECONDS`:
        received, queued, or None — neither."""
        assert self.heard is not None
        deadline = self.clock() + RECEIPT_SECONDS
        while True:
            if self.heard(marker):
                return texts.RECEIVED_BY_HOOK
            if self._queued(marker):
                return texts.RECEIVED_QUEUED
            if self.clock() >= deadline:
                return None
            self.sleep(RECEIPT_POLL_SECONDS)

    def _send_heard(
        self, text: str, stamp: str, marker: str, busy: bool, wait_idle: bool
    ) -> Delivery:
        """The hook's proof (#1731); the screen only for the queue and for
        what to retry — the same retry rules, never a paste over a doubt."""
        said = self._receipt(marker)
        if said is not None:
            return Delivery(True, marker, [said])
        field = input_field(self.tmux.capture(self.window, escapes=True), self.provider)
        if field:
            first = texts.TEXT_LEFT.format(text=repr(field[:40]))
            self.tmux.send_keys(self.window, "Enter")
            said = self._receipt(marker)
            outcome = said or texts.NOT_HEARD
            return Delivery(said is not None, marker, [first, outcome])
        if field is None or busy:
            # no field to paste into, or a busy worker that may still take it
            return Delivery(False, marker, [texts.NOT_HEARD])
        # at rest, nothing seen: lost — pasted again once, under a new marker
        _, text = self.busy_before_send(wait_idle, text)  # its field must take it
        again = self.make_marker()
        self._submit(text, again, stamp)
        said = self._receipt(again)
        attempts = [texts.MESSAGE_LOST_UNHEARD, said or texts.NOT_HEARD]
        return Delivery(said is not None, again, attempts)

    def send(self, text: str, stamp: str, wait_idle: bool = False) -> Delivery:
        busy, text = self.busy_before_send(wait_idle, text)
        marker = self.make_marker()
        self._submit(text, marker, stamp)
        if self.heard is not None and self.heard(marker) is not None:
            return self._send_heard(text, stamp, marker, busy, wait_idle)
        received, field = self._verdict(marker, busy)
        if received:
            return Delivery(True, marker, [self._received(marker)])
        if field is None:
            # No input field on screen: look once more, never paste again.
            self.sleep(VERIFY_SECONDS)
            received, field = self._verdict(marker, busy)
            first = texts.FIELD_GONE
            return Delivery(received, marker, [first, self._outcome(marker, received)])
        if field:
            # The text sits in the field: the Enter was lost, not the message.
            first = texts.TEXT_LEFT.format(text=repr(field[:40]))
            self.tmux.send_keys(self.window, "Enter")
            self.sleep(VERIFY_SECONDS)
            received, field = self._verdict(marker, busy)
            return Delivery(received, marker, [first, self._outcome(marker, received)])
        if busy:
            # A busy worker queues what it gets: look once more, never paste again.
            self.sleep(VERIFY_SECONDS)
            received, _ = self._verdict(marker, busy)
            first = texts.MARKER_NOWHERE
            outcome = self._outcome(marker, received)
            return Delivery(received, marker, [first, outcome])
        # At rest, empty field, no marker, no work: lost. Paste again under a
        # new marker — the old one could be matched by the first, lost attempt.
        first = texts.MESSAGE_LOST
        busy, text = self.busy_before_send(wait_idle, text)
        marker = self.make_marker()
        self._submit(text, marker, stamp)
        received, _ = self._verdict(marker, busy)
        return Delivery(received, marker, [first, self._outcome(marker, received)])
