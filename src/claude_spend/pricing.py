"""Per-model USD prices per million tokens (Anthropic first-party API, 2026-09).

Used only as a fallback/cross-check. Claude Code sends its own `cost_usd` on
every api_request event and that value is stored as the authoritative cost.
"""

from __future__ import annotations

# (input, output, cache_write_5m, cache_write_1h, cache_read) per MTok
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 12.50, 20.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 12.50, 20.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 12.50, 20.0, 1.0),
    "claude-mythos-5": (10.0, 50.0, 12.50, 20.0, 1.0),
    "claude-opus-5": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-5": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-1": (15.0, 75.0, 18.75, 30.0, 1.50),
    "claude-opus-4": (15.0, 75.0, 18.75, 30.0, 1.50),
    "claude-sonnet-5": (2.0, 10.0, 2.50, 4.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 6.0, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 3.75, 6.0, 0.30),
    "claude-sonnet-4": (3.0, 15.0, 3.75, 6.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 2.0, 0.10),
    "claude-haiku-3-5": (0.80, 4.0, 1.0, 1.60, 0.08),
}

# Longest keys first so "claude-fable-5-1" wins over "claude-fable-5".
_ORDERED = sorted(PRICES, key=len, reverse=True)


def normalize_model(model: str | None) -> str | None:
    """Map any model string (with date suffix, [1m], etc.) to a price-table key."""
    if not model:
        return None
    m = model.lower().split("[")[0].strip()
    for key in _ORDERED:
        if m == key or m.startswith(key + "-") or m.startswith(key + "@"):
            return key
    return None


def estimate_cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    speed: str | None = None,
) -> float | None:
    """Estimate cost. Cache writes are priced at the 5-minute rate (Claude Code default)."""
    key = normalize_model(model)
    if key is None:
        return None
    inp, out, cw5, _cw1h, cr = PRICES[key]
    if speed == "fast" and key in ("claude-opus-5", "claude-opus-4-8"):
        inp, out = 10.0, 50.0
        cw5, cr = inp * 1.25, inp * 0.1
    return (
        input_tokens * inp
        + output_tokens * out
        + cache_creation_tokens * cw5
        + cache_read_tokens * cr
    ) / 1_000_000
