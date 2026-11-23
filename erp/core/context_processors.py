"""Sidebar menu for base.html: groups, items and which single item is active."""
from django.urls import reverse

# (key, label, url name, icon name)
MENU = (
    ("", (("home", "홈", "schedule:home", "home"),)),
    ("영업·현장", (("projects", "프로젝트", "projects:list", "folder"),)),
    ("설계", (("bom", "구간 BOM", "bom:index", "table"),)),
    (
        "생산·출하",
        (
            ("board", "진행현황", "schedule:board", "gantt"),
            ("shipping", "출하", "shipping:list", "truck"),
            ("performance", "출고 실적", "shipping:performance", "chart"),
            ("unshipped", "미출고 부재", "shipping:unshipped", "box"),
        ),
    ),
    (
        "기준정보",
        (
            ("plants", "제작장", "shipping:plant_list", "factory"),
            ("carriers", "운송회사", "shipping:carrier_list", "building"),
            ("vehicles", "차종", "shipping:vehicle_type_list", "van"),
        ),
    ),
)

_SHIPPING_KEYS = {
    "performance": "performance",
    "lines": "performance",
    "lines_xlsx": "performance",
    "unshipped": "unshipped",
}


def active_key(match):
    """The one menu key for the current page ('' when no menu item applies)."""
    if match is None:
        return ""
    app, name = match.app_name, match.url_name or ""
    if app == "projects":
        return "projects"
    if app == "bom":
        return "bom"
    if app == "schedule":
        if name == "home":
            return "home"
        return "board" if name in ("board", "section") else ""
    if app == "shipping":
        if name in _SHIPPING_KEYS:
            return _SHIPPING_KEYS[name]
        if name.startswith("plant_"):
            return "plants"
        if name.startswith("carrier_"):
            return "carriers"
        if name.startswith("vehicle_type_"):
            return "vehicles"
        return "shipping"
    return ""


def nav(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    current = active_key(getattr(request, "resolver_match", None))
    groups = [
        {
            "title": title,
            "items": [
                {"key": k, "label": label, "url": reverse(url), "icon": icon, "active": k == current}
                for k, label, url, icon in items
            ],
        }
        for title, items in MENU
    ]
    return {"nav_groups": groups}
