"""The label taxonomy, in one place.

This mapping used to be written out separately in the labelling CLI, the
prompt and the detector, and they drifted: pressing "i" recorded an index as
negative while the rules and the prompt had already started counting it as
positive. Anything that decides what counts as a stock mention imports it
from here.
"""
from __future__ import annotations

# An index or ETF is tradeable and has a ticker, so it counts. Generic market
# talk with no named instrument does not.
CATEGORY_IS_STOCK_RELATED: dict[str, bool] = {
    "specific_equity": True,
    "index_or_etf": True,
    "macro_market": False,
    "not_financial": False,
}

CATEGORIES = tuple(CATEGORY_IS_STOCK_RELATED)


def is_stock_related(category: str) -> bool:
    """Whether a category counts as a stock mention.

    Raises on an unknown category rather than defaulting to False, so a typo
    in a prompt or a CLI cannot quietly turn positives into negatives.
    """
    try:
        return CATEGORY_IS_STOCK_RELATED[category]
    except KeyError:
        raise ValueError(
            f"unknown category {category!r}, expected one of {', '.join(CATEGORIES)}"
        ) from None
