from .normalization import normalize_text


# Supported assistant intent names.
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


# Detects the most likely assistant intent from the user query.
def detect_intent(query: str) -> str:
    text = normalize_text(query)
    phrase_text = text.replace("-", " ")
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

    if any(k in phrase_text for k in ("compare", "compared to", "versus", " vs ")):
        return INTENT_COMPARE_SALES

    if any(k in phrase_text for k in ("season", "seasonal", "which season")):
        return INTENT_SEASONALITY

    if any(k in phrase_text for k in ("restock", "reorder suggestion", "reorder", "what should i restock")):
        return INTENT_REORDER_SUGGESTIONS

    if any(k in phrase_text for k in forecast_time_phrases):
        return INTENT_FORECAST_BEST_SELLERS

    if any(phrase in phrase_text for phrase in seller_phrases) and any(k in phrase_text for k in forecast_time_phrases):
        return INTENT_FORECAST_BEST_SELLERS

    if "festival" in phrase_text or "eid" in phrase_text or "diwali" in phrase_text or "uplift" in phrase_text:
        return INTENT_FESTIVAL_INSIGHTS

    if "movement" in phrase_text or "stock in" in phrase_text or "stock out" in phrase_text or "adjustment" in phrase_text:
        return INTENT_STOCK_MOVEMENTS

    if any(phrase in phrase_text for phrase in seller_phrases) or "trend" in phrase_text:
        return INTENT_TOP_SELLERS

    if any(k in phrase_text for k in ("sale", "sales", "revenue", "quantity", "qty")):
        return INTENT_SALES_SUMMARY

    if "sold" in phrase_text:
        return INTENT_SALES_SUMMARY

    if (
        "stock" in phrase_text
        or "units do we have" in phrase_text
        or "out of stock" in phrase_text
        or "low stock" in phrase_text
        or "category summary" in phrase_text
    ):
        return INTENT_STOCK_LOOKUP

    return INTENT_UNKNOWN
