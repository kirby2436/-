"""Query logic for the schedule screens. Every function takes `today` explicitly."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Prefetch, Q

from core.models import ActivityType, AuditLog, Project, Section, SectionActivity
from core.services.schedule import activity_state

STATE_LABELS = {
    "done": ("완료", "✔"),
    "late": ("지연", "!"),
    "soon": ("임박", "▲"),
    "planned": ("예정", "○"),
}

PAGE_SIZE = 30
DEFAULT_PAST_DAYS = 14
DEFAULT_FUTURE_DAYS = 30
CLOSED_STATUSES = (Project.Status.DONE, Project.Status.FORCED)


def state_info(state):
    label, symbol = STATE_LABELS[state]
    return {"code": state, "label": label, "symbol": symbol}


def default_period(today):
    return today - timedelta(days=DEFAULT_PAST_DAYS), today + timedelta(days=DEFAULT_FUTURE_DAYS)


def late_activities_qs(today):
    """Activities past their planned date without an actual date (completed sections excluded)."""
    return SectionActivity.objects.filter(
        actual_date__isnull=True, planned_date__lt=today, section__is_completed=False
    )


def board_sections(today, start=None, end=None, project=None, status=None, manager=None, late_only=False):
    """Sections for the board (queryset, not yet paginated, no per-row queries needed later)."""
    default_start, default_end = default_period(today)
    start = start or default_start
    end = end or default_end
    in_range = SectionActivity.objects.filter(section=OuterRef("pk"), planned_date__range=(start, end))
    qs = Section.objects.filter(Exists(in_range))
    if project:
        qs = qs.filter(project=project)
    if status:
        qs = qs.filter(project__status=status)
    else:
        qs = qs.exclude(project__status__in=CLOSED_STATUSES)
    if manager:
        qs = qs.filter(project__manager=manager)
    if late_only:
        late = late_activities_qs(today).filter(section=OuterRef("pk"))
        qs = qs.filter(Exists(late))
    return qs.select_related("project", "floor").order_by("project__code", "planned_ship_date", "name", "pk")


def attach_cells(sections, today):
    """Prefetch activities and attach `.cells` (one per ActivityType, in order) to each section.

    Returns (activity_types, sections as list).
    """
    types = list(ActivityType.objects.all())
    sections = list(sections)
    by_section = {}
    if sections:
        for sa in SectionActivity.objects.filter(section__in=sections):
            by_section.setdefault(sa.section_id, {})[sa.activity_type_id] = sa
    for s in sections:
        acts = by_section.get(s.pk, {})
        cells = []
        late = False
        for t in types:
            sa = acts.get(t.pk)
            if sa is None:
                cells.append({"type": t, "sa": None})
                continue
            st = activity_state(sa, today)
            late = late or st == "late"
            cells.append({"type": t, "sa": sa, "state": state_info(st)})
        s.cells = cells
        s.has_late = late and not s.is_completed
    return types, sections


def managers():
    User = get_user_model()
    return User.objects.filter(managed_projects__isnull=False).distinct().order_by("username")


def section_rows(section, today):
    rows = []
    for sa in section.activities.select_related("activity_type").order_by("activity_type__sort_order", "activity_type__code"):
        rows.append({"sa": sa, "type": sa.activity_type, "state": state_info(activity_state(sa, today))})
    return rows


def section_history(section, limit=30):
    ids = [str(i) for i in section.activities.values_list("pk", flat=True)]
    logs = list(
        AuditLog.objects.filter(model="core.SectionActivity", object_id__in=ids)
        .select_related("user")
        .order_by("-at", "-id")[:limit]
    )
    names = {str(sa.pk): sa.activity_type for sa in section.activities.select_related("activity_type")}
    for log in logs:
        ch = log.changes or {}
        pair = ch.get("actual_date") or [None, None]
        log.activity_type = names.get(log.object_id)
        log.old_value, log.new_value = (pair + [None, None])[:2] if isinstance(pair, list) else (None, None)
    return logs


def week_range(today):
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def dashboard(today):
    monday, sunday = week_range(today)
    ship_sections = list(
        Section.objects.filter(is_completed=False)
        .filter(
            Q(confirmed_ship_date__range=(monday, sunday))
            | Q(confirmed_ship_date__isnull=True, planned_ship_date__range=(monday, sunday))
        )
        .select_related("project", "floor")
    )
    for s in ship_sections:
        s.ship_date = s.confirmed_ship_date or s.planned_ship_date
        s.ship_confirmed = s.confirmed_ship_date is not None
    ship_sections.sort(key=lambda s: (s.ship_date, s.project.code, s.name))

    late_qs = late_activities_qs(today)
    late_total = late_qs.count()
    late = []
    for sa in late_qs.select_related("section__project", "section__floor", "activity_type").order_by(
        "planned_date", "section__project__code", "section_id", "activity_type__sort_order"
    )[:20]:
        late.append({"sa": sa, "section": sa.section, "days_late": (today - sa.planned_date).days})

    return {
        "monday": monday,
        "sunday": sunday,
        "active_count": Project.objects.filter(status=Project.Status.ACTIVE).count(),
        "ordered_count": Project.objects.filter(status=Project.Status.ORDERED).count(),
        "ship_sections": ship_sections,
        "late": late,
        "late_total": late_total,
        "recent_logs": list(AuditLog.objects.select_related("user").order_by("-at", "-id")[:10]),
    }
