import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from calendar import monthrange
from difflib import SequenceMatcher

from django.db.models import Q
from django.utils import timezone

from retail.models import Category, Product
from .normalization import normalize_text


@dataclass
class ProductResolution:
    product: Product | None
    candidates: list[Product]
    confidence: float


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


PRODUCT_STOPWORDS = {
    "how",
    "many",
    "units",
    "do",
    "we",
    "have",
    "for",
    "show",
    "me",
    "stock",
    "of",
    "the",
    "item",
    "items",
    "product",
    "today",
    "this",
    "week",
    "month",
    "last",
    "30",
    "days",
    "sales",
    "trend",
    "forecast",
}


def _product_search_text(text: str) -> str:
    tokens = [t for t in text.split() if t not in PRODUCT_STOPWORDS and len(t) > 1]
    return " ".join(tokens) if tokens else text


def parse_threshold(query: str) -> int | None:
    text = normalize_text(query)
    match = re.search(r"(below|under|less than)\s+(\d+)", text)
    if match:
        return int(match.group(2))
    return None


def parse_date_window(query: str) -> tuple:
    text = normalize_text(query)
    today = timezone.localdate()
    start = today
    end = today
    label = "today"
    horizon_days = 7

    m = re.search(r"(\d{4}-\d{2}-\d{2}).*?(\d{4}-\d{2}-\d{2})", text)
    if m:
        start = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        end = datetime.strptime(m.group(2), "%Y-%m-%d").date()
        if start > end:
            start, end = end, start
        label = f"{start} to {end}"
        return start, end, label, (end - start).days + 1

    # Handle month-year phrases like "december 2025" or "jan2023"
    month_map = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    compact = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)(20\d{2})\b",
        text,
    )
    spaced = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+(20\d{2})\b",
        text,
    )
    hit = compact or spaced
    if hit:
        m_name = hit.group(1)
        year = int(hit.group(2))
        month = month_map[m_name]
        last_day = monthrange(year, month)[1]
        start = datetime(year, month, 1).date()
        end = datetime(year, month, last_day).date()
        label = f"{start} to {end}"
        return start, end, label, (end - start).days + 1

    tokens = re.findall(r"[a-z0-9]+", text)

    def has_last_month_phrase() -> bool:
        if "last month" in text:
            return True
        for i, tok in enumerate(tokens[:-1]):
            if tok == "last":
                nxt = tokens[i + 1]
                if SequenceMatcher(None, nxt, "month").ratio() >= 0.7:
                    return True
        return False

    if "this week" in text:
        start = today - timedelta(days=today.weekday())
        end = today
        label = "this week"
    elif has_last_month_phrase():
        first_this_month = today.replace(day=1)
        end = first_this_month - timedelta(days=1)
        start = end.replace(day=1)
        label = "last month"
    elif "this month" in text:
        start = today.replace(day=1)
        end = today
        label = "this month"
    elif "last 30 days" in text:
        start = today - timedelta(days=29)
        end = today
        label = "last 30 days"

    horizon_match = re.search(r"next\s+(7|14|30)\s+days", text)
    if horizon_match:
        horizon_days = int(horizon_match.group(1))

    return start, end, label, horizon_days


def resolve_category(query: str) -> Category | None:
    text = normalize_text(query)
    for category in Category.objects.all():
        name = normalize_text(category.category_name)
        if name and name in text:
            return category
    return None


def resolve_product(query: str) -> ProductResolution:
    text = normalize_text(query)
    search_text = _product_search_text(text)
    products = list(Product.objects.filter(is_active=True).only("id", "sku", "product_name", "qty_in_stock"))

    exact = None
    for p in products:
        if normalize_text(p.sku) in text or normalize_text(p.product_name) in text:
            exact = p
            break
    if exact:
        return ProductResolution(product=exact, candidates=[exact], confidence=1.0)

    icontains_hits = list(
        Product.objects.filter(is_active=True)
        .filter(Q(product_name__icontains=search_text) | Q(sku__icontains=search_text))
        .only("id", "sku", "product_name", "qty_in_stock")[:5]
    )
    if icontains_hits:
        return ProductResolution(product=icontains_hits[0], candidates=icontains_hits, confidence=0.8)

    scored = []
    for p in products:
        score = max(
            _similarity(search_text, normalize_text(p.product_name)),
            _similarity(search_text, normalize_text(p.sku)),
        )
        if score >= 0.45:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = [p for _, p in scored[:5]]
    if candidates:
        return ProductResolution(product=candidates[0], candidates=candidates, confidence=scored[0][0])

    return ProductResolution(product=None, candidates=[], confidence=0.0)


def resolve_festival_name(query: str) -> str | None:
    text = normalize_text(query)
    words = [w for w in text.split() if len(w) > 2]
    for w in words:
        if w in {"eid", "diwali", "christmas", "newyear"}:
            return w
    m = re.search(r"during\s+([a-z0-9 _-]+)", text)
    if m:
        return m.group(1).strip()
    return None
