"""LLM provider fallback wiring.

Synchronous, and in their own module so the guardrail suite's module-level
asyncio mark does not apply to them.
"""

from __future__ import annotations



def test_llm_fallback_is_wired_when_a_second_provider_is_configured(monkeypatch) -> None:
    """An LLM outage mid-call is dead air, so a standby is worth its complexity.

    Verified live once by breaking the primary key: the turn recovered on the
    secondary in ~2.8s. That test needs two real keys, so what is asserted here
    is the wiring -- that a second provider actually produces a FallbackAdapter
    rather than being silently ignored.
    """
    from livekit.agents import llm as llm_mod

    from luma.config import Settings
    from luma.runtime import build_llm

    # Dummy keys: nothing is called, only constructed, so the suite stays
    # runnable without credentials.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-not-used")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-5.4-nano")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "google")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "gemini-3.1-flash-lite")
    assert isinstance(build_llm(Settings.from_env()), llm_mod.FallbackAdapter)

    # Blank model means no standby -- better than one whose key is missing and
    # that 401s on every retry.
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "")
    assert not isinstance(build_llm(Settings.from_env()), llm_mod.FallbackAdapter)


def test_fallback_attempt_timeout_respects_geminis_floor() -> None:
    """Gemini rejects any deadline under 10s outright ("Manually set deadline
    4s is too short"), so a tighter timeout breaks the fallback rather than
    speeding it up -- the standby 400s before it can answer."""
    import inspect

    from luma import runtime

    source = inspect.getsource(runtime.build_llm)
    assert "attempt_timeout=10.0" in source
