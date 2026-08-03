"""Speech behaviour that belongs to the agent rather than to any one tool."""

from __future__ import annotations

import contextlib
from typing import Any

from livekit.agents import RunContext

# What a host actually says while they look something up. Silence during a tool
# call is the single most robotic thing a voice agent does -- a real person
# fills it without thinking. These fire only once the pause becomes noticeable,
# so a fast lookup does not gain a pointless preamble.
LOOKUP_FILLERS = ("Let me check that for you.", "One moment.", "Just looking now.")
FILLER_DELAY_S = 0.7


def filler(ctx: RunContext | None, *, delay: float = FILLER_DELAY_S) -> Any:
    """Cover a slow tool call with natural speech.

    Tolerates a missing context so the guardrail tests can drive tools directly,
    without a live session.
    """
    if ctx is None:
        return contextlib.nullcontext()
    return ctx.with_filler(
        lambda step: LOOKUP_FILLERS[step % len(LOOKUP_FILLERS)],
        delay=delay,
        interval=6.0,
        max_steps=2,
    )
