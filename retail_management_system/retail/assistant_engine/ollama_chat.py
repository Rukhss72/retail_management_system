"""
Conversational analytics module — sends handler output to local Ollama
so that the chatbot returns rich, natural-language analytics insights
instead of bare one-line summaries.
"""

import json
import logging
from typing import Any

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── helpers ────────────────────────────────────────────────────────────

def _serialize_payload_for_prompt(payload: dict) -> str:
    """Compact text representation of handler output for the LLM context."""
    parts: list[str] = []

    if payload.get("summary"):
        parts.append(f"Summary: {payload['summary']}")

    table = payload.get("table") or {}
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    if columns and rows:
        parts.append("Data table:")
        parts.append(" | ".join(columns))
        for row in rows[:15]:                       # cap to avoid token overflow
            parts.append(" | ".join(str(row.get(c, "")) for c in columns))

    chart = payload.get("chart")
    if chart:
        charts = chart.get("charts") if isinstance(chart.get("charts"), list) else [chart]
        for ch in charts:
            labels = ch.get("labels") or []
            datasets = ch.get("datasets") or []
            if labels and datasets:
                parts.append(f"Chart ({ch.get('type', 'bar')}): labels={labels[:10]}")
                for ds in datasets:
                    parts.append(f"  {ds.get('label', 'data')}: {ds.get('data', [])[:10]}")

    source = payload.get("source")
    if source:
        parts.append(f"Data sources: {', '.join(source)}")

    return "\n".join(parts)


def _build_history_messages(history: list[dict]) -> list[dict]:
    """Convert thread history into Ollama chat messages (last 6 turns max)."""
    msgs: list[dict] = []
    for entry in history[-12:]:                     # 6 user + 6 assistant
        role = entry.get("role", "user")
        content = entry.get("content", "")
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    return msgs


# ── main entry point ───────────────────────────────────────────────────

def generate_insight(
    query: str,
    payload: dict,
    history: list[dict] | None = None,
) -> str | None:
    """
    Send the user's query + handler analytics data to local Ollama and
    return a natural-language insight.

    Returns ``None`` on any failure so the caller can fall back to the
    rule-based ``summary``.
    """
    if not getattr(settings, "ASSISTANT_OLLAMA_CHAT_ENABLED", True):
        return None

    model = getattr(settings, "ASSISTANT_OLLAMA_MODEL", "qwen2.5:1.5b")
    host = getattr(settings, "ASSISTANT_OLLAMA_HOST", "http://127.0.0.1:11434")
    timeout = getattr(settings, "ASSISTANT_OLLAMA_TIMEOUT", 120)

    try:
        import ollama                              # noqa: E402
    except ImportError:
        logger.warning("ollama package not installed — skipping AI insight.")
        return None

    today = timezone.localdate().isoformat()
    data_context = _serialize_payload_for_prompt(payload)

    system_prompt = (
        "You are a friendly, expert retail analytics assistant embedded inside "
        "a retail management system. The store is based in Mauritius.\n"
        f"Today's date is {today}.\n\n"
        "The system has already queried the database and produced the analytics "
        "data shown below. Your job is to explain the data to the user in a "
        "clear, insightful, and conversational way.\n\n"
        "Guidelines:\n"
        "• Highlight key numbers, trends, and actionable insights.\n"
        "• Use bullet points or short paragraphs — keep it concise but informative.\n"
        "• If the data shows something noteworthy (spikes, drops, out-of-stock, "
        "low stock), call it out.\n"
        "• Do NOT invent data that is not present below.\n"
        "• Do NOT repeat the raw table verbatim — instead, summarize it.\n"
        "• Keep your response under 200 words unless the data is complex.\n"
        "• Be warm and professional. Use emojis sparingly (1-2 max).\n\n"
        "─── ANALYTICS DATA ───\n"
        f"{data_context}\n"
        "─── END DATA ───"
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
    ]

    # inject recent chat history for conversational context
    if history:
        messages.extend(_build_history_messages(history))

    messages.append({"role": "user", "content": query})

    try:
        client = ollama.Client(host=host, timeout=timeout)
        response = client.chat(
            model=model,
            options={"temperature": 0.4, "num_predict": 400},
            messages=messages,
        )
    except Exception as exc:
        logger.info("Ollama chat unavailable (%s) — falling back to rule summary.", exc)
        return None

    # extract text from response
    content = ""
    if isinstance(response, dict):
        content = response.get("message", {}).get("content", "") or ""
    else:
        content = getattr(getattr(response, "message", None), "content", "") or ""

    return content.strip() or None
