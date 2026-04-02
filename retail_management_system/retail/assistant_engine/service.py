from datetime import datetime, timedelta
import re
from calendar import monthrange
from difflib import SequenceMatcher

from django.utils import timezone

from .entities import parse_date_window, parse_threshold, resolve_category, resolve_festival_name, resolve_product, resolve_section
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


# Small parsing helpers used while building assistant entities.
def _parse_iso_date(value):
    if not value:
        return None
    if hasattr(value, "year"):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception:
        return None


# Resolves a category from an LLM-provided category name.
def _resolve_category_from_name(name: str):
    if not name:
        return None
    from retail.models import Category

    return Category.objects.filter(category_name__icontains=str(name).strip()).first()


# Resolves a product from hinted product name or SKU values.
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


# Builds the final entity dictionary used by assistant handlers.
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
        "section": resolve_section(query),
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
        # If the LLM only provides one compare range, keep the parser range as the second period.
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


# Decides whether a query should ask the user to confirm a weak product match.
def _needs_product_confirmation(intent: str, query: str, entities: dict | None = None) -> bool:
    text = query.lower()
    if intent == INTENT_STOCK_LOOKUP:
        # Generic stock list/filter queries should not trigger product disambiguation.
        if any(
            p in text
            for p in (
                "low stock",
                "low-stock",
                "below ",
                "under ",
                "less than ",
                "out of stock",
                "no stock",
                "summary by category",
                "stock summary by category",
                "how many items",
                "how many products",
                "show me all",
            )
        ):
            return False
        return True
    if intent == INTENT_FORECAST_BEST_SELLERS and ("for " in text or "product" in text):
        if entities and (entities.get("category") is not None or entities.get("section") is not None):
            return False
        return True
    if intent == INTENT_TOP_SELLERS and "trend for" in text:
        if entities and (entities.get("category") is not None or entities.get("section") is not None):
            return False
        return True
    return False


# Builds a clarification payload when product matching is too weak.
def _low_confidence_product_prompt(intent: str, query: str, entities: dict) -> dict | None:
    if not _needs_product_confirmation(intent, query, entities):
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


# Detects advanced date phrases that may benefit from LLM parsing.
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


# Checks whether the query is explicitly comparing two periods.
def _query_has_compare_phrase(query: str) -> bool:
    text = query.lower()
    return any(p in text for p in ("compare", "compared to", "versus", " vs "))


# Detects "last month" phrasing with some typo tolerance.
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


# Extracts one explicit month-year date range from the query.
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


# Extracts two explicit month-year ranges for comparison queries.
def _extract_two_month_year_ranges(query: str):
    text = query.lower()
    month_map = {
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
    hits = list(
        re.finditer(
            r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s*(20\d{2})\b",
            text,
        )
    )
    if len(hits) < 2:
        return None

    def _to_month_range(month_name: str, year_text: str):
        month = month_map[month_name]
        year = int(year_text)
        last_day = monthrange(year, month)[1]
        return datetime(year, month, 1).date(), datetime(year, month, last_day).date()

    a = _to_month_range(hits[0].group(1), hits[0].group(2))
    b = _to_month_range(hits[1].group(1), hits[1].group(2))
    return a, b


# Extracts one month paired with two different years for comparisons.
def _extract_month_with_two_years_ranges(query: str):
    text = query.lower()
    month_map = {
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
    m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s*(20\d{2})\b.*?\b(20\d{2})\b",
        text,
    )
    if not m:
        return None
    month = month_map[m.group(1)]
    y1 = int(m.group(2))
    y2 = int(m.group(3))
    if y1 == y2:
        return None
    a_last = monthrange(y1, month)[1]
    b_last = monthrange(y2, month)[1]
    a = (datetime(y1, month, 1).date(), datetime(y1, month, a_last).date())
    b = (datetime(y2, month, 1).date(), datetime(y2, month, b_last).date())
    return a, b


# Decides whether an explicit month range should be treated as the main range.
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


# Returns the date range for the previous calendar month.
def _last_month_range():
    today = timezone.localdate()
    first_this_month = today.replace(day=1)
    end = first_this_month - timedelta(days=1)
    start = end.replace(day=1)
    return start, end


# Checks whether the query is mostly just a product reference.
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


# Detects generic stock-count questions that should not force product matching.
def _is_generic_stock_count_query(query: str) -> bool:
    text = query.lower()
    if "stock" not in text:
        return False
    if "how many" not in text:
        return False
    return any(k in text for k in ("set", "sets", "items", "products", "skus"))


# Detects generic stock-movement queries with no explicit product filter.
def _is_generic_stock_movements_query(query: str) -> bool:
    text = query.lower()
    if "movement" not in text:
        return False
    generic_markers = ("stock movement", "stock movements", "movements", "movement", "last 30 days", "this week", "last week")
    if not any(marker in text for marker in generic_markers):
        return False
    explicit_product_markers = (" for ", " of ", " sku ", "product ")
    return not any(marker in text for marker in explicit_product_markers)


# Detects simple greeting or small-talk messages.
def _is_small_talk(query: str) -> bool:
    text = re.sub(r"[^a-z0-9\s]", "", query.lower().strip())
    return text in {
        "hi",
        "hello",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "how are you",
        "thanks",
        "thank you",
    }


# Decides when to try Ollama entity extraction in addition to rule parsing.
def _should_try_ollama(intent: str, entities: dict, query: str) -> bool:
    if intent == INTENT_UNKNOWN:
        return True
    # Weak product matches are a good reason to try LLM extraction first.
    if entities.get("product") is not None and entities.get("product_confidence", 1.0) < 0.65:
        return True
    # Rule parsing is weaker for more complex month/year comparisons.
    if _query_has_advanced_time_phrase(query) and entities.get("date_label") == "today":
        return True
    return False


# Uses remembered context to rewrite short follow-up questions.
def _apply_memory_to_query(query: str, memory: dict) -> str:
    if not memory:
        return query
    rewritten = (query or "").strip()
    text = rewritten.lower()

    follow_up_terms = ("what about", "and for")
    if any(t in text for t in follow_up_terms):
        intent_seed = {
            INTENT_COMPARE_SALES: "compare sales",
            INTENT_SEASONALITY: "seasonality",
            INTENT_STOCK_LOOKUP: "stock",
            INTENT_STOCK_MOVEMENTS: "stock movements",
            INTENT_SALES_SUMMARY: "sales",
            INTENT_TOP_SELLERS: "top sellers",
            INTENT_FORECAST_BEST_SELLERS: "forecast",
            INTENT_REORDER_SUGGESTIONS: "reorder suggestions",
            INTENT_FESTIVAL_INSIGHTS: "festival insights",
        }.get(memory.get("last_intent"))
        if intent_seed and intent_seed not in text:
            rewritten = f"{intent_seed} {rewritten}"
            text = rewritten.lower()

    carry_terms = ("this product", "that product", "same product", "it", "that one")
    if any(t in text for t in carry_terms):
        last_product = memory.get("last_product_name")
        if last_product:
            rewritten = f"{rewritten} {last_product}"
            text = rewritten.lower()
    if any(t in text for t in ("same category", "that category")):
        last_category = memory.get("last_category_name")
        if last_category:
            rewritten = f"{rewritten} {last_category}"
    return rewritten


# Splits text into sentence-like chunks.
def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p and p.strip()]


# Cleans and deduplicates assistant reply text.
def _clean_answer_text(raw_text: str) -> str:
    text = (raw_text or "").replace("\r", "").strip()
    if not text:
        return ""

    # Removes common markdown artifacts and normalizes bullet-style text.
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^[\-\*\u2022]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text).strip()

    seen: set[str] = set()
    deduped: list[str] = []
    for sentence in _split_sentences(text):
        normalized = re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(sentence)

    if not deduped:
        return text
    return "\n".join(deduped)


# Identifies intents that should return shorter answers.
def _is_simple_intent(intent: str) -> bool:
    return intent in {
        INTENT_STOCK_LOOKUP,
        INTENT_SALES_SUMMARY,
        INTENT_TOP_SELLERS,
        INTENT_REORDER_SUGGESTIONS,
        INTENT_UNKNOWN,
    }


# Builds the final short answer text shown to the user.
def _build_answer_text(intent: str, payload: dict) -> str:
    raw = payload.get("summary") or payload.get("ai_insight") or ""
    cleaned = _clean_answer_text(raw)
    if not cleaned:
        return "No response."

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return "No response."

    max_lines = 2 if _is_simple_intent(intent) else 4
    return "\n".join(lines[:max_lines]).strip()


# Adds answer_text into the returned payload.
def _with_answer_text(intent: str, payload: dict) -> dict:
    out = dict(payload or {})
    out["answer_text"] = _build_answer_text(intent, out)
    return out


# Main assistant entry point that parses the query, runs a handler, and updates memory.
def process_query(query: str, session: dict) -> dict:
    raw_query = query
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
        payload = {
            "intent": "Clarification",
            "summary": "No problem. Please type product name, SKU, date range, or click a quick action.",
            "table": {"columns": [], "rows": []},
            "chart": None,
            "source": [],
        }
        return _with_answer_text("Clarification", payload)

    if _is_small_talk(query):
        payload = {
            "summary": "Hi. Ask about sales, stock, compare periods, forecast, or reorder.",
            "table": {"columns": [], "rows": []},
            "chart": None,
            "source": [],
        }
        payload = _with_answer_text(INTENT_UNKNOWN, payload)
        return {"intent": INTENT_UNKNOWN, "parser_used": "rules", "parser_confidence": 1.0, **payload}

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
        pair_ranges = _extract_two_month_year_ranges(query) or _extract_month_with_two_years_ranges(query)
        if pair_ranges:
            (a_start, a_end), (b_start, b_end) = pair_ranges
            entities["start"], entities["end"] = a_start, a_end
            entities["compare_start"], entities["compare_end"] = b_start, b_end

        explicit = _extract_explicit_month_year_range(query)
        has_last_month = _has_last_month_phrase(query)
        if (not pair_ranges) and explicit and has_last_month:
            lm_start, lm_end = _last_month_range()
            if _should_explicit_range_be_primary(query):
                entities["start"], entities["end"] = explicit
                entities["compare_start"], entities["compare_end"] = lm_start, lm_end
            else:
                entities["start"], entities["end"] = lm_start, lm_end
                entities["compare_start"], entities["compare_end"] = explicit
        elif not pair_ranges:
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

    if (
        intent in (INTENT_UNKNOWN, INTENT_SALES_SUMMARY)
        and entities.get("product") is not None
        and entities.get("product_confidence", 0) >= 0.75
        and _query_is_product_only(query)
    ):
        intent = INTENT_STOCK_LOOKUP

    if intent == INTENT_STOCK_LOOKUP and (_is_generic_stock_count_query(query) or (entities.get("category") is not None and "how many" in query.lower() and "stock" in query.lower())):
        entities["product"] = None
        entities["product_candidates"] = []
        entities["product_confidence"] = 1.0
    if intent == INTENT_SEASONALITY and (entities.get("category") is not None or entities.get("section") is not None):
        entities["product"] = None
        entities["product_candidates"] = []
        entities["product_confidence"] = 1.0
    if intent == INTENT_STOCK_LOOKUP and entities.get("section") is not None and "how many" in query.lower() and "stock" in query.lower():
        entities["product"] = None
        entities["product_candidates"] = []
        entities["product_confidence"] = 1.0
    elif intent == INTENT_STOCK_MOVEMENTS and _is_generic_stock_movements_query(query):
        entities["product"] = None
        entities["product_candidates"] = []
        entities["product_confidence"] = 1.0

    low_confidence = _low_confidence_product_prompt(intent, query, entities)
    if low_confidence:
        session["assistant_pending"] = {
            "query": query,
            "product_id": entities["product_candidates"][0]["id"],
            "created_at": timezone.now().isoformat(),
        }
        low_confidence = _with_answer_text(intent, low_confidence)
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
    # Optionally asks Ollama to turn the structured payload into a conversational reply.
    chat_history = session.get("_chat_history", [])
    ai_insight = generate_insight(query, payload, history=chat_history)
    if ai_insight:
        payload["ai_insight"] = ai_insight
    # Keeps a rolling history window for future follow-up context.
    chat_history.append({"role": "user", "content": query})
    chat_history.append({"role": "assistant", "content": ai_insight or payload.get("summary", "")})
    session["_chat_history"] = chat_history[-12:]   # last 6 turns

    session["last_user_query"] = raw_query
    session["last_resolved_query"] = query
    session["last_intent"] = intent
    session["last_entities"] = {
        "start": str(entities["start"]) if entities.get("start") else None,
        "end": str(entities["end"]) if entities.get("end") else None,
        "compare_start": str(entities["compare_start"]) if entities.get("compare_start") else None,
        "compare_end": str(entities["compare_end"]) if entities.get("compare_end") else None,
        "date_label": entities.get("date_label"),
    }
    if entities.get("product") is not None:
        session["last_product_name"] = entities["product"].product_name
        session["last_product_sku"] = entities["product"].sku
    if entities.get("category") is not None:
        session["last_category_name"] = entities["category"].category_name
    if entities.get("section") is not None:
        session["last_section_name"] = entities["section"]

    payload = _with_answer_text(intent, payload)
    return {"intent": intent, "parser_used": parser_used, "parser_confidence": parser_confidence, **payload}
