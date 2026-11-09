"""The one way to say « no »: a `Refused` anywhere becomes exit 1 with its reason.

A refusal is `tracerworks` doing its job (a guard, the cap, unpushed work, a
message not received); it is not a failure to read the world, which is a
`CommandError` (exit 3).
"""


class Refused(Exception):
    """A command declines, and says why on stderr."""
