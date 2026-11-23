"""Aggregation queries for the shipping report screens (shipped shipments only unless noted)."""
from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.db.models import Count, DecimalField, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth

from core.models import BomItem, Project, Shipment, ShipmentLine
from core.services import shipping

NO_PLANT = "none"  # filter value for shipments without a plant
NO_PLANT_LABEL = "(미지정)"
AXES = (("month", "월별"), ("project", "프로젝트별"), ("plant", "제작장별"))
CLOSED_STATUSES = (Project.Status.DONE, Project.Status.FORCED)

_ZERO = Value(Decimal("0"), output_field=DecimalField(max_digits=14, decimal_places=2))


def _metrics():
    return {
        "shipments": Count("truck__shipment", distinct=True),
        "trucks": Count("truck", distinct=True),
        "qty": Coalesce(Sum("quantity"), 0),
        "area": Coalesce(Sum("area"), _ZERO, output_field=DecimalField(max_digits=14, decimal_places=2)),
    }


def default_period(today):
    return date(today.year, 1, 1), today


def shipped_lines(start, end, project=None, section=None, plant=None):
    """Lines of SHIPPED shipments with ship_date in [start, end]. `plant` is a Plant, NO_PLANT or None."""
    qs = ShipmentLine.objects.filter(
        truck__shipment__status=Shipment.Status.SHIPPED,
        truck__shipment__ship_date__range=(start, end),
    )
    if project:
        qs = qs.filter(truck__shipment__project=project)
    if section:
        qs = qs.filter(bom_item__section=section)
    if plant == NO_PLANT:
        qs = qs.filter(truck__shipment__plant__isnull=True)
    elif plant:
        qs = qs.filter(truck__shipment__plant=plant)
    return qs


def period_totals(start, end):
    """Shipments / trucks / pieces / area of shipped shipments in the period."""
    return shipped_lines(start, end).aggregate(**_metrics())


def performance(start, end, axis):
    """Return (rows, totals). Each row: label, shipments, trucks, qty, area, link (query dict for the lines screen)."""
    qs = shipped_lines(start, end).order_by()
    totals = qs.aggregate(**_metrics())
    base = {"start": start.isoformat(), "end": end.isoformat()}
    rows = []
    if axis == "project":
        data = qs.values(
            "truck__shipment__project", "truck__shipment__project__code", "truck__shipment__project__name"
        ).annotate(**_metrics()).order_by("truck__shipment__project__code")
        for d in data:
            d["label"] = f"{d['truck__shipment__project__code']} {d['truck__shipment__project__name']}"
            d["link"] = {**base, "project": d["truck__shipment__project"]}
            rows.append(d)
    elif axis == "plant":
        data = list(
            qs.values("truck__shipment__plant", "truck__shipment__plant__name")
            .annotate(**_metrics())
            .order_by("truck__shipment__plant__name")
        )
        data.sort(key=lambda d: d["truck__shipment__plant"] is None)  # (미지정) last, stable
        for d in data:
            pid = d["truck__shipment__plant"]
            d["label"] = d["truck__shipment__plant__name"] if pid else NO_PLANT_LABEL
            d["link"] = {**base, "plant": pid if pid else NO_PLANT}
            rows.append(d)
    else:
        data = qs.annotate(month=TruncMonth("truck__shipment__ship_date")).values("month").annotate(
            **_metrics()
        ).order_by("month")
        for d in data:
            first = d["month"]
            last = date(first.year, first.month, monthrange(first.year, first.month)[1])
            d["label"] = f"{first:%Y-%m}"
            d["link"] = {"start": max(first, start).isoformat(), "end": min(last, end).isoformat()}
            rows.append(d)
    return rows, totals


def lines_queryset(start, end, project=None, section=None, plant=None):
    qs = shipped_lines(start, end, project, section, plant).select_related(
        "truck__shipment__project",
        "truck__shipment__plant",
        "bom_item__section__floor",
    )
    return qs.order_by(
        "truck__shipment__ship_date",
        "truck__shipment__project__code",
        "bom_item__section__name",
        "truck__seq",
        "bom_item__ass_mark",
        "id",
    )


def lines_totals(start, end, project=None, section=None, plant=None):
    agg = shipped_lines(start, end, project, section, plant).order_by().aggregate(
        qty=Coalesce(Sum("quantity"), 0),
        area=Coalesce(Sum("area"), _ZERO, output_field=DecimalField(max_digits=14, decimal_places=2)),
    )
    return agg


def unshipped_sections(today, project=None, overdue_only=False):
    """Sections that still have remaining quantity (planned shipments count as taken, canceled do not).

    Returns a list of dicts: overdue sections first, then by date. `shipped_qty` counts SHIPPED only,
    `planned_qty` is what sits on planned shipments, `remaining_qty` follows shipping.annotate_remaining.
    """
    items = shipping.annotate_remaining(
        BomItem.objects.exclude(section__project__status__in=CLOSED_STATUSES)
    ).filter(remaining__gt=0)
    if project:
        items = items.filter(section__project=project)
    items = list(items.select_related("section__project", "section__floor").order_by("ass_mark", "id"))
    if not items:
        return []

    by_section = {}
    for it in items:
        by_section.setdefault(it.section_id, []).append(it)
    ids = list(by_section)

    bom_qty = dict(
        BomItem.objects.filter(section_id__in=ids).order_by().values_list("section").annotate(t=Sum("quantity"))
    )
    status_qty = {}
    for sid, status, qty in (
        ShipmentLine.objects.filter(
            bom_item__section_id__in=ids,
            truck__shipment__status__in=(Shipment.Status.SHIPPED, Shipment.Status.PLANNED),
        )
        .order_by()
        .values_list("bom_item__section", "truck__shipment__status")
        .annotate(t=Sum("quantity"))
    ):
        status_qty[(sid, status)] = qty

    rows = []
    for sid, sec_items in by_section.items():
        section = sec_items[0].section
        due = section.confirmed_ship_date or section.planned_ship_date
        overdue = due < today
        if overdue_only and not overdue:
            continue
        remaining_area = Decimal("0")
        missing = 0
        for it in sec_items:
            if it.unit_area is None:
                missing += 1
            else:
                remaining_area += it.unit_area * it.remaining
        rows.append(
            {
                "section": section,
                "project": section.project,
                "due": due,
                "due_confirmed": section.confirmed_ship_date is not None,
                "overdue": overdue,
                "days_late": (today - due).days if overdue else 0,
                "bom_qty": bom_qty.get(sid, 0),
                "shipped_qty": status_qty.get((sid, Shipment.Status.SHIPPED), 0),
                "planned_qty": status_qty.get((sid, Shipment.Status.PLANNED), 0),
                "remaining_qty": sum(it.remaining for it in sec_items),
                "remaining_area": remaining_area,
                "area_missing": missing,
                "items": sec_items,
            }
        )
    rows.sort(key=lambda r: (not r["overdue"], r["due"], r["project"].code, r["section"].name, r["section"].pk))
    return rows
