from django.core.paginator import Paginator


PAGE_SIZE_CHOICES = (25, 50, 100)


def paginate_items(request, items, default=25):
    """Paginate a queryset/list while preserving active filters."""
    requested = str(request.GET.get("per_page", default)).strip().lower()
    query = request.GET.copy()
    query.pop("page", None)
    query.pop("per_page", None)
    base_query = query.urlencode()

    if requested == "all":
        rows = list(items)
        return {
            "items": rows,
            "page_obj": None,
            "per_page": "all",
            "page_size_choices": PAGE_SIZE_CHOICES,
            "pagination_query": base_query,
            "filtered_count": len(rows),
        }

    try:
        page_size = int(requested)
    except (TypeError, ValueError):
        page_size = default
    if page_size not in PAGE_SIZE_CHOICES:
        page_size = default

    page_obj = Paginator(items, page_size).get_page(request.GET.get("page", 1))
    return {
        "items": list(page_obj.object_list),
        "page_obj": page_obj,
        "per_page": page_size,
        "page_size_choices": PAGE_SIZE_CHOICES,
        "pagination_query": base_query,
        "filtered_count": page_obj.paginator.count,
    }
