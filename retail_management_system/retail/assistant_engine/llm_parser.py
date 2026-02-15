import json
from typing import Any

from django.conf import settings
from django.utils import timezone

from .intents import (
    INTENT_COMPARE_SALES,
    INTENT_FESTIVAL_INSIGHTS,
    INTENT_FORECAST_BEST_SELLERS,
    INTENT_REORDER_SUGGESTIONS,
    INTENT_SALES_SUMMARY,
    INTENT_SEASONALITY,
    INTENT_STOCK_LOOKUP,
    INTENT_STOCK_MOVEMENTS,
    INTENT_TOP_SELLERS,
    INTENT_UNKNOWN,
)


ALLOWED_INTENTS = {
    INTENT_COMPARE_SALES,
    INTENT_SEASONALITY,
    INTENT_STOCK_LOOKUP,
    INTENT_SALES_SUMMARY,
    INTENT_TOP_SELLERS,
    INTENT_STOCK_MOVEMENTS,
    INTENT_FORECAST_BEST_SELLERS,
    INTENT_REORDER_SUGGESTIONS,
    INTENT_FESTIVAL_INSIGHTS,
    INTENT_UNKNOWN,
}


def _extract_json(content: str) -> dict[str, Any] | None:
    if not content:
        return None
    text = content.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            try:
                data = json.loads(text[first : last + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def parse_with_ollama(query: str) -> dict[str, Any] | None:
    if not getattr(settings, "ASSISTANT_OLLAMA_ENABLED", True):
        return None

    model = getattr(settings, "ASSISTANT_OLLAMA_MODEL", "llama3.1:8b")
    host = getattr(settings, "ASSISTANT_OLLAMA_HOST", "http://127.0.0.1:11434")
    timeout = getattr(settings, "ASSISTANT_OLLAMA_TIMEOUT", 10)

    try:
        import ollama
    except Exception:
        return None

    today = timezone.localdate().isoformat()
    system_prompt = (
        "You are a strict intent+entity extractor for a retail analytics assistant. "
        f"Today's date is {today}. "
        "Return ONLY valid JSON with schema: "
        "{\"intent\": string, \"confidence\": number, \"entities\": {"
        "\"product\": string|null, \"sku\": string|null, \"category\": string|null, "
        "\"start_date\": \"YYYY-MM-DD\"|null, \"end_date\": \"YYYY-MM-DD\"|null, "
        "\"compare_start_date\": \"YYYY-MM-DD\"|null, \"compare_end_date\": \"YYYY-MM-DD\"|null, "
        "\"threshold\": number|null, \"horizon_days\": number|null, \"festival_name\": string|null"
        "}}. "
        "Allowed intents: CompareSales, Seasonality, StockLookup, SalesSummary, TopSellers, StockMovements, "
        "ForecastBestSellers, ReorderSuggestions, FestivalInsights, Unknown. "
        "Use CompareSales when prompt compares two periods. "
        "Use Seasonality for 'which season sells best' style queries. "
        "Use TopSellers for top/best seller requests. "
        "Use SalesSummary for total revenue/quantity summaries. "
        "For date phrases like 'last month' or 'january this year', convert to absolute dates."
    )

    try:
        client = ollama.Client(host=host, timeout=timeout)
        response = client.chat(
            model=model,
            options={"temperature": 0},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            format="json",
        )
    except Exception:
        return None

    content = ""
    if isinstance(response, dict):
        content = response.get("message", {}).get("content", "") or ""
    else:
        content = getattr(getattr(response, "message", None), "content", "") or ""

    parsed = _extract_json(content)
    if not parsed:
        return None

    intent = parsed.get("intent", INTENT_UNKNOWN)
    if intent not in ALLOWED_INTENTS:
        intent = INTENT_UNKNOWN

    confidence = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    entities = parsed.get("entities") if isinstance(parsed.get("entities"), dict) else {}
    clean_entities = {
        "product": entities.get("product"),
        "sku": entities.get("sku"),
        "category": entities.get("category"),
        "start_date": entities.get("start_date"),
        "end_date": entities.get("end_date"),
        "compare_start_date": entities.get("compare_start_date"),
        "compare_end_date": entities.get("compare_end_date"),
        "threshold": entities.get("threshold"),
        "horizon_days": entities.get("horizon_days"),
        "festival_name": entities.get("festival_name"),
    }
    return {"intent": intent, "confidence": confidence, "entities": clean_entities}
