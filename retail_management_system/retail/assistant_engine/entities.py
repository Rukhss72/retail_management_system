import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
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

    if "this week" in text:
        start = today - timedelta(days=today.weekday())
        end = today
        label = "this week"
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
