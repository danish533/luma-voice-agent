"""The agent, its guards, and the wording a caller hears.

Split out of a single 1,000-line module so the guarantees can be read on their
own: `guards.py` is the list of things that must hold no matter what the model
decides, and it is now a page of named functions rather than branches
interleaved with API plumbing.
"""

from .agent import LumaAgent

__all__ = ["LumaAgent"]
