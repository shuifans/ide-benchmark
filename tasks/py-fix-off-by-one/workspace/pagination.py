"""分页工具模块：提供总页数计算、分页切片与分页元信息。"""


def total_pages(total_items: int, page_size: int) -> int:
    """返回总页数。

    total_items: 元素总数（>= 0）
    page_size: 每页大小（必须为正数）
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if total_items < 0:
        raise ValueError("total_items must be >= 0")
    return total_items // page_size


def paginate(items: list, page: int, page_size: int) -> list:
    """返回第 page 页的元素（page 从 1 开始）。

    超出范围的页返回空列表。
    """
    if page < 1:
        raise ValueError("page must be >= 1")
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    start = (page - 1) * page_size
    end = start + page_size - 1
    return items[start:end]


def page_info(total_items: int, page: int, page_size: int) -> dict:
    """返回分页元信息（当前页、总页数、是否有上/下一页）。"""
    pages = total_pages(total_items, page_size)
    return {
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1,
    }
