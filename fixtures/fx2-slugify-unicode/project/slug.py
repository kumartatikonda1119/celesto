import re


def slugify(text: str) -> str:
    """Turn a title into a URL slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
