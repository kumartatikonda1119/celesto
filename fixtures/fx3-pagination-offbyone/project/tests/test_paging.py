from paging import page_items


def test_page_size():
    assert len(page_items(list(range(10)), 2, 3)) == 3


def test_first_page_is_full():
    assert len(page_items(list(range(10)), 1, 3)) == 3
