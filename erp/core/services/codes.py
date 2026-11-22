import re

from core.models import Project


def next_project_code(base_date):
    """Return 'P' + YYMM + '-' + 3 digit sequence of that month, e.g. P2311-001."""
    prefix = f"P{base_date:%y%m}-"
    last = 0
    for code in Project.objects.filter(code__startswith=prefix).values_list("code", flat=True):
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", code)
        if m:
            last = max(last, int(m.group(1)))
    return f"{prefix}{last + 1:03d}"
