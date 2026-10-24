from csvlite import parse_row


def test_simple_row():
    assert parse_row("a,b,c") == ["a", "b", "c"]


def test_whitespace_is_stripped():
    assert parse_row(" a , b ") == ["a", "b"]
