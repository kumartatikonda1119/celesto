def page_items(items, page, per_page):
    """Return one page of items. Pages are 1-based."""
    start = page * per_page
    return items[start:start + per_page]
