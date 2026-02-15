from datetime import datetime, timedelta
import re
from calendar import monthrange
from difflib import SequenceMatcher

from django.utils import timezone

from .entities import parse_date_window, parse_threshold, resolve_category, resolve_festival_name, resolve_product
from .handlers import (
    handle_compare_sales,
    handle_festival_insights,
    handle_forecast_best_sellers,
    handle_reorder_suggestions,
    handle_sales_summary,
    handle_seasonality,
    handle_stock_lookup,
    handle_stock_movements,
    handle_top_sellers,
)
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
    detect_intent,
)
from .llm_parser import parse_with_ollama
from .normalization import normalize_confirmation
from .ollama_chat import generate_insight


def _parse_iso_date(value):
    if not value:
        return None
    if hasattr(value, "year"):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return None


def _resolve_category_from_name(name: str):
    if not name:
        return None
    from retail.models import Category

    return Category.objects.filter(category_name__icontains=str(name).strip()).first()


def _resolve_product_from_hints(product_name: str | None, sku: str | None):
    from retail.models import Product

    if sku:
        hit = Product.objects.filter(is_active=True, sku__iexact=str(sku).strip()).first()
        if hit:
            return hit
    if product_name:
        hit = Product.objects.filter(is_active=True, product_name__icontains=str(product_name).strip()).first()
        if hit:
            return hit
    return None


def build_entities(query: str, overrides: dict | None = None) -> dict:
    start, end, date_label, horizon_days = parse_date_window(query)
    parsed_start, parsed_end = start, end
    product_resolution = resolve_product(query)
    entities = {
        "start": start,
        "end": end,
        "compare_start": None,
        "compare_end": None,
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
    if not overrides:
        return entities

    o_start = _parse_iso_date(overrides.get("start_date"))
    o_end = _parse_iso_date(overrides.get("end_date"))
    if o_start and o_end:
        if o_start > o_end:
            o_start, o_end = o_end, o_start
        entities["start"] = o_start
        entities["end"] = o_end
        entities["date_label"] = f"{o_start} to {o_end}"

    c_start = _parse_iso_date(overrides.get("compare_start_date"))
    c_end = _parse_iso_date(overrides.get("compare_end_date"))
    if c_start and c_end:
        if c_start > c_end:
            c_start, c_end = c_end, c_start
        entities["compare_start"] = c_start
        entities["compare_end"] = c_end
    elif (o_start and o_end) and (parsed_start, parsed_end) != (o_start, o_end):
        # LLM provided only one period in a compare prompt; keep parser period as the second range.
        entities["compare_start"] = parsed_start
        entities["compare_end"] = parsed_end

    if overrides.get("threshold") is not None:
        try:
            entities["threshold"] = int(overrides["threshold"])
        except Exception:
            pass

    if overrides.get("horizon_days") is not None:
        try:
            entities["horizon_days"] = int(overrides["horizon_days"])
        except Exception:
            pass

    if overrides.get("festival_name"):
        festival = str(overrides["festival_name"]).strip()
        if festival.lower() not in {"unknown", "n/a", "none", "null", ""}:
            entities["festival_name"] = festival

    if overrides.get("category"):
        cat = _resolve_category_from_name(overrides.get("category"))
        if cat:
            entities["category"] = cat

    hinted_product = _resolve_product_from_hints(overrides.get("product"), overrides.get("sku"))
    if hinted_product:
        entities["product"] = hinted_product
        entities["product_candidates"] = [{"id": hinted_product.id, "name": hinted_product.product_name, "sku": hinted_product.sku}]
        entities["product_confidence"] = 1.0

    return entities


def _needs_product_confirmation(intent: str, query: str) -> bool:
    text = query.lower()
    if intent == INTENT_STOCK_LOOKUP:
        return True
    if intent == INTENT_FORECAST_BEST_SELLERS and ("for " in text or "product" in text):
        return True
    if intent == INTENT_TOP_SELLERS and "trend for" in text:
        return True
    return False


def _low_confidence_product_prompt(intent: str, query: str, entities: dict) -> dict | None:
    if not _needs_product_confirmation(intent, query):
        return None
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


def _query_has_advanced_time_phrase(query: str) -> bool:
    text = query.lower()
    month_words = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    if any(m in text for m in month_words):
        return True
    if any(p in text for p in ("this year", "last year", "compare", "versus", " vs ")):
        return True
    if _has_last_month_phrase(query):
        return True
    if re.search(r"\b(20\d{2})\b", text):
        return True
    return False


def _query_has_compare_phrase(query: str) -> bool:
    text = query.lower()
    return any(p in text for p in ("compare", "compared to", "versus", " vs "))


def _has_last_month_phrase(query: str) -> bool:
    text = query.lower()
    if "last month" in text:
        return True
    tokens = re.findall(r"[a-z0-9]+", text)
    for i, tok in enumerate(tokens[:-1]):
        if tok == "last":
            nxt = tokens[i + 1]
            if SequenceMatcher(None, nxt, "month").ratio() >= 0.7:
                return True
    return False


def _extract_explicit_month_year_range(query: str):
    text = query.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    years = [int(y) for y in re.findall(r"(20\d{2})", text)]
    if not years:
        return None

    month_aliases = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    canonical_months = [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]

    month_num = None
    for t in tokens:
        t_letters = re.sub(r"\d+", "", t)
        if t in month_aliases:
            month_num = month_aliases[t]
            break
        if t_letters in month_aliases:
            month_num = month_aliases[t_letters]
            break
        best = max(
            ((SequenceMatcher(None, t_letters or t, m).ratio(), i + 1) for i, m in enumerate(canonical_months)),
            key=lambda x: x[0],
        )
        if best[0] >= 0.72:
            month_num = best[1]
            break
    if not month_num:
        return None

    year = years[0]
    last_day = monthrange(year, month_num)[1]
    return datetime(year, month_num, 1).date(), datetime(year, month_num, last_day).date()


def _should_explicit_range_be_primary(query: str) -> bool:
    text = query.lower()
    idx_last_month = text.find("last month")
    if idx_last_month < 0:
        idx_last_month = text.find("last ")
    if idx_last_month < 0:
        return False

    explicit_markers = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"]
    positions = [text.find(m) for m in explicit_markers if text.find(m) >= 0]
    if not positions:
        year_matches = list(re.finditer(r"\b20\d{2}\b", text))
        if not year_matches:
            return False
        explicit_pos = year_matches[0].start()
    else:
        explicit_pos = min(positions)

    return explicit_pos < idx_last_month


def _last_month_range():
    today = timezone.localdate()
    first_this_month = today.replace(day=1)
    end = first_this_month - timedelta(days=1)
    start = end.replace(day=1)
    return start, end


def _query_is_product_only(query: str) -> bool:
    text = query.lower()
    keywords = (
        "sale",
        "revenue",
        "top",
        "stock",
        "forecast",
        "compare",
        "season",
        "festival",
        "movement",
        "reorder",
        "restock",
    )
    return not any(k in text for k in keywords)


def _should_try_ollama(intent: str, entities: dict, query: str) -> bool:
    if intent == INTENT_UNKNOWN:
        return True
    # If rules resolved to product with weak confidence for a product-required query, try LLM extraction first.
    if entities.get("product") is not None and entities.get("product_confidence", 1.0) < 0.65:
        return True
    # Rule parser does not cover complex month/year comparisons well.
    if _query_has_advanced_time_phrase(query) and entities.get("date_label") == "today":
        return True
    return False


def _apply_memory_to_query(query: str, memory: dict) -> str:
    if not memory:
        return query
    text = query.lower()
    carry_terms = ("this product", "that product", "same product", "it", "that one")
    if any(t in text for t in carry_terms):
        last_product = memory.get("last_product_name")
        if last_product:
            return f"{query} {last_product}"
    if any(t in text for t in ("same category", "that category")):
        last_category = memory.get("last_category_name")
        if last_category:
            return f"{query} {last_category}"
    if any(t in text for t in ("what about", "and for")) and memory.get("last_user_query"):
        return f"{memory['last_user_query']} ; follow-up: {query}"
    return query


def process_query(query: str, session: dict) -> dict:
    query = _apply_memory_to_query(query, session)
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

    intent = detect_intent(query)
    entities = build_entities(query)
    parser_used = "rules"
    parser_confidence = 1.0

    if _should_try_ollama(intent, entities, query):
        llm_result = parse_with_ollama(query)
        if llm_result:
            parser_used = "ollama_fallback"
            parser_confidence = llm_result.get("confidence", 0.0) or None
            llm_intent = llm_result.get("intent", INTENT_UNKNOWN)
            if parser_confidence is not None and parser_confidence <= 0:
                parser_confidence = None
            if intent == INTENT_UNKNOWN and llm_intent != INTENT_UNKNOWN:
                intent = llm_intent
            elif _query_has_compare_phrase(query) and llm_intent == INTENT_COMPARE_SALES:
                intent = llm_intent
            entities = build_entities(query, llm_result.get("entities") or {})

    if intent == INTENT_COMPARE_SALES:
        explicit = _extract_explicit_month_year_range(query)
        has_last_month = _has_last_month_phrase(query)
        if explicit and has_last_month:
            lm_start, lm_end = _last_month_range()
            if _should_explicit_range_be_primary(query):
                entities["start"], entities["end"] = explicit
                entities["compare_start"], entities["compare_end"] = lm_start, lm_end
            else:
                entities["start"], entities["end"] = lm_start, lm_end
                entities["compare_start"], entities["compare_end"] = explicit
        else:
            a = (entities.get("start"), entities.get("end"))
            b = (entities.get("compare_start"), entities.get("compare_end"))
            if (not all(b)) or (a == b):
                inferred = explicit
                if inferred and inferred != a:
                    entities["compare_start"], entities["compare_end"] = inferred
                elif has_last_month:
                    lm_start, lm_end = _last_month_range()
                    if (lm_start, lm_end) != a:
                        entities["compare_start"], entities["compare_end"] = lm_start, lm_end
            if _should_explicit_range_be_primary(query):
                b = (entities.get("compare_start"), entities.get("compare_end"))
                if all(b):
                    entities["start"], entities["end"], entities["compare_start"], entities["compare_end"] = (
                        b[0],
                        b[1],
                        entities["start"],
                        entities["end"],
                    )

    if intent in (INTENT_UNKNOWN, INTENT_SALES_SUMMARY) and entities.get("product") is not None and _query_is_product_only(query):
        intent = INTENT_STOCK_LOOKUP

    low_confidence = _low_confidence_product_prompt(intent, query, entities)
    if low_confidence:
        session["assistant_pending"] = {
            "query": query,
            "product_id": entities["product_candidates"][0]["id"],
            "created_at": timezone.now().isoformat(),
        }
        return {"intent": intent, "parser_used": parser_used, "parser_confidence": parser_confidence, **low_confidence}

    if intent == INTENT_COMPARE_SALES:
        payload = handle_compare_sales(query, entities)
    elif intent == INTENT_SEASONALITY:
        payload = handle_seasonality(query, entities)
    elif intent == INTENT_STOCK_LOOKUP:
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

    # ── Ollama conversational insight ─────────────────────────────────
    chat_history = session.get("_chat_history", [])
    ai_insight = generate_insight(query, payload, history=chat_history)
    if ai_insight:
        payload["ai_insight"] = ai_insight
    # keep a rolling window of recent exchanges for follow-up context
    chat_history.append({"role": "user", "content": query})
    chat_history.append({"role": "assistant", "content": ai_insight or payload.get("summary", "")})
    session["_chat_history"] = chat_history[-12:]   # last 6 turns

    session["last_user_query"] = query
    session["last_intent"] = intent
    if entities.get("product") is not None:
        session["last_product_name"] = entities["product"].product_name
        session["last_product_sku"] = entities["product"].sku
    if entities.get("category") is not None:
        session["last_category_name"] = entities["category"].category_name

    return {"intent": intent, "parser_used": parser_used, "parser_confidence": parser_confidence, **payload}
