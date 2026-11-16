from __future__ import annotations

from urllib.parse import urlencode

from .config import ebay_scale_direct_sold_url_enabled

# Pokemon TCG collectible card games category on ebay.com.au (same convention as existing tooling).
EBAY_AU_POKEMON_SACAT = "0"

DEFAULT_ITEMS_PER_PAGE = 240


def build_au_sold_results_url(
    *,
    query_text: str,
    provider_domain: str = "ebay.com.au",
    items_per_page: int = DEFAULT_ITEMS_PER_PAGE,
    category_id: str = EBAY_AU_POKEMON_SACAT,
    market_country: str = "AU",
) -> str:
    """Build a direct sold-results search URL (AU only, HTTPS).

    Used when ``EBAY_SCALE_DIRECT_SOLD_URL`` is enabled on the PC; offline tests assert shape only.
    """
    country = str(market_country or "").strip().upper()
    if country and country != "AU":
        raise ValueError("direct sold results URL is AU-only")
    domain = str(provider_domain or "").strip().lower()
    if not domain.endswith("ebay.com.au"):
        raise ValueError("direct sold results URL requires ebay.com.au domain")
    if not str(query_text or "").strip():
        raise ValueError("query_text is required")
    if items_per_page != DEFAULT_ITEMS_PER_PAGE:
        raise ValueError(f"_ipg must be {DEFAULT_ITEMS_PER_PAGE} for scale sold URL contract")
    params = {
        "_nkw": " ".join(str(query_text).strip().split()),
        "_sacat": category_id,
        "LH_Sold": "1",
        "LH_Complete": "1",
        "_ipg": str(items_per_page),
        "rt": "nc",
    }
    return f"https://www.{domain}/sch/i.html?{urlencode(params)}"


def sold_url_mode_for_query(*, market_country: str) -> str:
    """Return diagnostics label for how sold filtering is applied."""
    if (
        str(market_country or "").strip().upper() == "AU"
        and ebay_scale_direct_sold_url_enabled()
    ):
        return "direct_sold_url_scale"
    return "ui_after_active_search"