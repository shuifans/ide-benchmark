import pytest

from pagination import total_pages, paginate, page_info

ITEMS = list(range(10))


def test_total_pages_exact():
    assert total_pages(9, 3) == 3


def test_total_pages_remainder():
    assert total_pages(10, 3) == 4


def test_total_pages_partial_single():
    assert total_pages(1, 5) == 1


def test_total_pages_zero_items():
    assert total_pages(0, 5) == 0


def test_first_page_full():
    assert paginate(ITEMS, 1, 3) == [0, 1, 2]


def test_middle_page_full():
    assert paginate(ITEMS, 2, 3) == [3, 4, 5]


def test_last_partial_page():
    assert paginate(ITEMS, 4, 3) == [9]


def test_page_info_consistency():
    info = page_info(10, 2, 3)
    assert info["total_pages"] == 4
    assert info["has_next"] is True
    assert info["has_prev"] is True


def test_invalid_page():
    with pytest.raises(ValueError):
        paginate(ITEMS, 0, 3)
