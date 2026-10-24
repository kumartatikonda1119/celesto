def parse_row(line: str) -> list[str]:
    """Split one CSV line into stripped fields."""
    return [field.strip() for field in line.split(",")]
