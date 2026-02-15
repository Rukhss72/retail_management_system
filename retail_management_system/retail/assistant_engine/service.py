from django.utils import timezone

from .entities import parse_date_window, parse_threshold, resolve_category, resolve_festival_name, resolve_product
from .handlers import (
    handle_festival_insights,
    handle_forecast_best_sellers,
    handle_reorder_suggestions,
    handle_sales_summary,
    handle_stock_lookup,
    handle_stock_movements,
    handle_top_sellers,
)
from .intents import (
    INTENT_FESTIVAL_INSIGHTS,
    INTENT_FORECAST_BEST_SELLERS,
    INTENT_REORDER_SUGGESTIONS,
    INTENT_SALES_SUMMARY,
    INTENT_STOCK_LOOKUP,
    INTENT_STOCK_MOVEMENTS,
    INTENT_TOP_SELLERS,
    INTENT_UNKNOWN,
    detect_intent,
)
from .normalization import normalize_confirmation


def build_entities(query: str) -> dict:
    start, end, date_label, horizon_days = parse_date_window(query)
    product_resolution = resolve_product(query)
    return {
        "start": start,
        "end": end,
        "date_label": date_label,
        "horizon_days": horizon_days,
        "threshold": parse_threshold(query),
        "category": resolve_category(query),
        "product": product_resolution.product,
        "product_candidates": [
            {"id": p.id, "name": p.product_name, "sku": p.sku}
            for p in product_resolution.candidates
        ],
        "product_confidence": product_resolution.confidence,
        "festival_name": resolve_festival_name(query),
    }


def _low_confidence_product_prompt(entities: dict) -> dict | None:
    if entities["product"] is None:
        return None
    if entities["product_confidence"] >= 0.65:
        return None
    if not entities["product_candidates"]:
        return None

    top = entities["product_candidates"][:3]
    names = ", ".join(f"{x['name']} ({x['sku']})" for x in top)
    return {
        "summary": f"I am not fully sure about the product match. Did you mean: {names}?",
        "table": {"columns": ["option", "sku"], "rows": [{"option": x["name"], "sku": x["sku"]} for x in top]},
        "chart": None,
        "source": ["Product"],
        "needs_confirmation": True,
    }


def process_query(query: str, session: dict) -> dict:
    confirmation = normalize_confirmation(query)
    pending = session.get("assistant_pending")

    if confirmation == "YES" and pending:
        query = pending["query"]
        if pending.get("product_id"):
            from retail.models import Product

            product = Product.objects.filter(id=pending["product_id"]).first()
            if product:
                query = f"{query} {product.product_name}"
        session.pop("assistant_pending", None)
    elif confirmation == "NO" and pending:
        session.pop("assistant_pending", None)
        return {
            "intent": "Clarification",
            "summary": "No problem. Please type product name, SKU, date range, or click a quick action.",
            "table": {"columns": [], "rows": []},
            "chart": None,
            "source": [],
        }

    entities = build_entities(query)
    intent = detect_intent(query)
    low_confidence = _low_confidence_product_prompt(entities)
    if low_confidence:
        session["assistant_pending"] = {
            "query": query,
            "product_id": entities["product_candidates"][0]["id"],
            "created_at": timezone.now().isoformat(),
        }
        return {"intent": intent, **low_confidence}

    if intent == INTENT_STOCK_LOOKUP:
        payload = handle_stock_lookup(query, entities)
    elif intent == INTENT_STOCK_MOVEMENTS:
        payload = handle_stock_movements(query, entities)
    elif intent == INTENT_SALES_SUMMARY:
        payload = handle_sales_summary(query, entities)
    elif intent == INTENT_TOP_SELLERS:
        payload = handle_top_sellers(query, entities)
    elif intent == INTENT_FORECAST_BEST_SELLERS:
        payload = handle_forecast_best_sellers(query, entities)
    elif intent == INTENT_REORDER_SUGGESTIONS:
        payload = handle_reorder_suggestions(query, entities)
    elif intent == INTENT_FESTIVAL_INSIGHTS:
        payload = handle_festival_insights(query, entities)
    else:
        payload = {
            "summary": (
                "I can help with stock, sales, top sellers, stock movements, forecast, and reorder suggestions. "
                "Try a quick action."
            ),
            "table": {"columns": [], "rows": []},
            "chart": None,
            "source": [],
        }

    return {"intent": intent, **payload}

