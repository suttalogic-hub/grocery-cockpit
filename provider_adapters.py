"""Provider metadata and policies shared by Grocery Cockpit's Python services."""

from __future__ import annotations

import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ProviderAdapter:
    id: str
    name: str
    kind: str
    status: str
    search_url: str
    open_strategy: str = "product_or_search"
    auto_scan_timeout_seconds: int = 120
    focused_scan_timeout_seconds: int = 150

    def public_config(self) -> dict[str, Any]:
        """Return the provider fields that belong in user configuration."""
        return {
            key: value
            for key, value in asdict(self).items()
            if key not in {"open_strategy", "auto_scan_timeout_seconds", "focused_scan_timeout_seconds"}
        }


ADAPTERS = (
    ProviderAdapter(
        id="zepto",
        name="Zepto",
        kind="quick-commerce",
        status="browser-ready",
        search_url="https://www.zepto.com/search?query={query}",
    ),
    ProviderAdapter(
        id="blinkit",
        name="Blinkit",
        kind="quick-commerce",
        status="browser-ready",
        search_url="https://blinkit.com/s/?q={query}",
    ),
    ProviderAdapter(
        id="swiggy_instamart",
        name="Swiggy Instamart",
        kind="quick-commerce",
        status="browser-ready",
        search_url="https://www.swiggy.com/instamart/search?query={query}",
    ),
    ProviderAdapter(
        id="amazon_fresh",
        name="Amazon Now",
        kind="marketplace-grocery",
        status="browser-ready",
        search_url="https://www.amazon.in/s?k={query}&i=nowstore&almBrandId=ctnow&fpw=alm",
        open_strategy="amazon_now_handoff",
        auto_scan_timeout_seconds=360,
        focused_scan_timeout_seconds=360,
    ),
    ProviderAdapter(
        id="jiomart",
        name="JioMart",
        kind="grocery",
        status="browser-ready",
        search_url="https://www.jiomart.com/search/{query}",
        open_strategy="search_only",
    ),
    ProviderAdapter(
        id="dmart",
        name="DMart Ready",
        kind="grocery",
        status="browser-ready",
        search_url="https://www.dmart.in/search?searchTerm={query}",
        auto_scan_timeout_seconds=360,
        focused_scan_timeout_seconds=360,
    ),
    ProviderAdapter(
        id="bigbasket",
        name="BigBasket",
        kind="grocery",
        status="browser-ready",
        search_url="https://www.bigbasket.com/ps/?q={query}",
        auto_scan_timeout_seconds=420,
        focused_scan_timeout_seconds=480,
    ),
)

ADAPTERS_BY_ID = {adapter.id: adapter for adapter in ADAPTERS}


def provider_ids() -> list[str]:
    return [adapter.id for adapter in ADAPTERS]


def provider_catalog() -> list[dict[str, Any]]:
    return [adapter.public_config() for adapter in ADAPTERS]


def configured_provider_map(providers: Iterable[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    catalog = providers if providers is not None else provider_catalog()
    return {provider["id"]: provider for provider in catalog}


def adapter_for(provider_id: str) -> ProviderAdapter:
    try:
        return ADAPTERS_BY_ID[provider_id]
    except KeyError as error:
        raise ValueError(f"Unknown provider adapter: {provider_id}") from error


def build_search_url(provider: dict[str, Any], query: str) -> str:
    """Build a provider search URL without needing a browser session."""
    clean_query = str(query or "").strip()
    adapter = ADAPTERS_BY_ID.get(str(provider.get("id") or ""))
    template = str(provider.get("search_url") or (adapter.search_url if adapter else ""))
    if not template:
        raise ValueError(f"Provider {provider.get('id')!r} has no search URL template.")
    encoded = urllib.parse.quote(clean_query) if provider.get("id") == "jiomart" else urllib.parse.quote_plus(clean_query)
    return template.format(query=encoded)


def is_amazon_product_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(str(url))
    except ValueError:
        return False
    return parsed.netloc.lower().endswith("amazon.in") and (
        "/dp/" in parsed.path.lower() or "/gp/product/" in parsed.path.lower()
    )


def amazon_now_handoff_url(search_url: str = "") -> str:
    query = ""
    if search_url:
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(search_url).query).get("k", [""])[0]
        except ValueError:
            query = ""
    if query:
        return f"/amazon-now?query={urllib.parse.quote_plus(query)}"
    return "/amazon-now"


def choose_open_url(provider_id: str, product_url: str | None, fallback_search_url: str) -> tuple[str, str]:
    adapter = ADAPTERS_BY_ID.get(provider_id)
    strategy = adapter.open_strategy if adapter else "product_or_search"
    if strategy == "search_only":
        return fallback_search_url, "search"
    if strategy == "amazon_now_handoff":
        return amazon_now_handoff_url(fallback_search_url), "now"
    if product_url:
        return str(product_url), "product"
    return fallback_search_url, "search"


def scan_mode(provider: dict[str, Any]) -> str:
    status = provider.get("status", "")
    if status in {"browser-ready", "official-api-ready", "order-history-ready"}:
        return "ready"
    if status in {"browser-profile-needed", "needs-access"}:
        return "setup"
    if status in {"manual-link", "official-api-needed", "chrome-assisted"}:
        return "manual"
    return "planned"


def scan_timeout(provider_id: str, baseline_seconds: int, *, focused: bool = False) -> int:
    adapter = ADAPTERS_BY_ID.get(provider_id)
    if adapter is None:
        return int(baseline_seconds)
    minimum = adapter.focused_scan_timeout_seconds if focused else adapter.auto_scan_timeout_seconds
    return max(int(baseline_seconds), minimum)
