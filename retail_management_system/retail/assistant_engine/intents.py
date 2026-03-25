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
    seller_phrases = (
        "top seller",
        "top sellers",
        "top selling",
        "best seller",
        "best sellers",
        "best selling",
        "top product",
        "top products",
    )
    forecast_time_phrases = (
        "forecast",
        "predict",
        "predicted",
        "next 7 days",
        "next 14 days",
        "next 30 days",
        "next week",
        "next month",
        "upcoming week",
        "upcoming month",
    )

    if any(k in text for k in ("compare", "compared to", "versus", " vs ")):
        return INTENT_COMPARE_SALES

    if any(k in text for k in ("season", "seasonal", "which season")):
        return INTENT_SEASONALITY

    if any(k in text for k in ("restock", "reorder suggestion", "reorder", "what should i restock")):
        return INTENT_REORDER_SUGGESTIONS

    if any(k in text for k in forecast_time_phrases):
        return INTENT_FORECAST_BEST_SELLERS

    if any(phrase in text for phrase in seller_phrases) and any(k in text for k in forecast_time_phrases):
        return INTENT_FORECAST_BEST_SELLERS

    if "festival" in text or "eid" in text or "diwali" in text or "uplift" in text:
        return INTENT_FESTIVAL_INSIGHTS

    if "movement" in text or "stock in" in text or "stock out" in text or "adjustment" in text:
        return INTENT_STOCK_MOVEMENTS

    if any(k in text for k in ("sale", "sales", "revenue", "quantity", "qty")):
        return INTENT_SALES_SUMMARY

    if any(phrase in text for phrase in seller_phrases) or "trend" in text:
        return INTENT_TOP_SELLERS

    if "sold" in text:
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
