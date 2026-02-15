from .normalization import normalize_text


INTENT_STOCK_LOOKUP = "StockLookup"
INTENT_SALES_SUMMARY = "SalesSummary"
INTENT_TOP_SELLERS = "TopSellers"
INTENT_COMPARE_SALES = "CompareSales"
INTENT_SEASONALITY = "Seasonality"
INTENT_STOCK_MOVEMENTS = "StockMovements"
INTENT_FORECAST_BEST_SELLERS = "ForecastBestSellers"
INTENT_REORDER_SUGGESTIONS = "ReorderSuggestions"
INTENT_FESTIVAL_INSIGHTS = "FestivalInsights"
INTENT_UNKNOWN = "Unknown"


def detect_intent(query: str) -> str:
    text = normalize_text(query)

    if any(k in text for k in ("compare", "compared to", "versus", " vs ")):
        return INTENT_COMPARE_SALES

    if any(k in text for k in ("season", "seasonal", "which season")):
        return INTENT_SEASONALITY

    if any(k in text for k in ("restock", "reorder suggestion", "reorder", "what should i restock")):
        return INTENT_REORDER_SUGGESTIONS

    if any(k in text for k in ("predict", "forecast", "next 7 days", "next 14 days", "next 30 days")):
        return INTENT_FORECAST_BEST_SELLERS

    if "festival" in text or "eid" in text or "diwali" in text or "uplift" in text:
        return INTENT_FESTIVAL_INSIGHTS

    if "movement" in text or "stock in" in text or "stock out" in text or "adjustment" in text:
        return INTENT_STOCK_MOVEMENTS

    if (
        "top seller" in text
        or "top selling" in text
        or "best-selling" in text
        or "best selling" in text
        or "trend" in text
    ):
        return INTENT_TOP_SELLERS

    if "sold" in text:
        return INTENT_TOP_SELLERS

    if "sale" in text or "revenue" in text:
        return INTENT_SALES_SUMMARY

    if (
        "stock" in text
        or "units do we have" in text
        or "out of stock" in text
        or "low stock" in text
        or "category summary" in text
    ):
        return INTENT_STOCK_LOOKUP

    return INTENT_UNKNOWN
