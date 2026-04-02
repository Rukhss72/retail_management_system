"""
Conversational analytics module - sends handler output to local Ollama
so that the chatbot can return natural-language analytics insights.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# Converts handler output into compact text for the LLM prompt.
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
        for row in rows[:15]:
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


# Converts recent thread history into Ollama chat messages.
def _build_history_messages(history: list[dict]) -> list[dict]:
    """Convert thread history into Ollama chat messages (last 6 turns max)."""
    msgs: list[dict] = []
    for entry in history[-12:]:
        role = entry.get("role", "user")
        content = entry.get("content", "")
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content})
    return msgs


# Generates a natural-language insight from handler output using Ollama.
def generate_insight(
    query: str,
    payload: dict,
    history: list[dict] | None = None,
) -> str | None:
    """
    Send the user's query + handler analytics data to local Ollama and
    return a natural-language insight.

    Returns None on any failure so the caller can fall back to rule summary.
    """
    if not getattr(settings, "ASSISTANT_OLLAMA_CHAT_ENABLED", True):
        return None

    model = getattr(settings, "ASSISTANT_OLLAMA_MODEL", "qwen2.5:1.5b")
    host = getattr(settings, "ASSISTANT_OLLAMA_HOST", "http://127.0.0.1:11434")
    timeout = getattr(settings, "ASSISTANT_OLLAMA_TIMEOUT", 120)

    try:
        import ollama 
    except ImportError:
        logger.warning("ollama package not installed - skipping AI insight.")
        return None

    today = timezone.localdate().isoformat()
    data_context = _serialize_payload_for_prompt(payload)

    system_prompt = (
        "You are a retail analytics assistant embedded in a retail management system.\n"
        f"Today's date is {today}.\n\n"
        "The system already queried the database and provided the analytics data below.\n"
        "Explain that data clearly and do not repeat yourself.\n\n"
        "Guidelines:\n"
        "- Straightforward questions: answer in 1-2 short lines.\n"
        "- Compare/forecast/festival/seasonality questions: up to 4 short lines.\n"
        "- Mention key numbers and one practical action when relevant.\n"
        "- No repeated phrasing and no restating the same fact.\n"
        "- Do not invent data.\n"
        "- Do not repeat raw table rows verbatim.\n"
        "- Plain text only.\n\n"
        "--- ANALYTICS DATA ---\n"
        f"{data_context}\n"
        "--- END DATA ---"
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

    if history:
        messages.extend(_build_history_messages(history))

    messages.append({"role": "user", "content": query})

    try:
        client = ollama.Client(host=host, timeout=timeout)
        response = client.chat(
            model=model,
            options={"temperature": 0.2, "num_predict": 140},
            messages=messages,
        )
    except Exception as exc:
        logger.info("Ollama chat unavailable (%s) - falling back to rule summary.", exc)
        return None

    content = ""
    if isinstance(response, dict):
        content = response.get("message", {}).get("content", "") or ""
    else:
        content = getattr(getattr(response, "message", None), "content", "") or ""

    return content.strip() or None
