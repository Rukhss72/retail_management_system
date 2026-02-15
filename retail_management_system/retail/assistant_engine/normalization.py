import re


YES_WORDS = {"yes", "ya", "yep", "y", "sure", "ok", "okay", "alright", "k"}
NO_WORDS = {"no", "nope", "nah", "n", "not now", "cancel"}


def normalize_text(text: str) -> str:
    cleaned = (text or "").strip().lower()
    cleaned = re.sub(r"[^\w\s-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def normalize_confirmation(text: str) -> str | None:
    value = normalize_text(text)
    if value in YES_WORDS:
        return "YES"
    if value in NO_WORDS:
        return "NO"
    return None

