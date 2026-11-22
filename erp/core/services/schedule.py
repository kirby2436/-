from datetime import timedelta

from django.utils import timezone

from core.models import ActivityType, SectionActivity
from core.services import audit

SOON_BUSINESS_DAYS = 3


def add_business_days(d, n):
    """Move `d` by `n` business days (Sat/Sun skipped). `n` may be negative.

    n == 0 returns `d` unchanged. Holidays are not considered.
    """
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d


def sync_section_activities(section):
    """Create/update SectionActivity rows of a section and recompute planned dates."""
    ship = section.confirmed_ship_date or section.planned_ship_date
    types = list(ActivityType.objects.all())
    by_id = {t.pk: t for t in types}
    planned = {}

    def planned_of(t):
        if t.pk in planned:
            return planned[t.pk]
        if t.base_activity_id is None:
            base = ship
        else:
            base = planned_of(by_id[t.base_activity_id])
        planned[t.pk] = add_business_days(base, t.lead_days)
        return planned[t.pk]

    existing = {sa.activity_type_id: sa for sa in section.activities.all()}
    for t in types:
        date = planned_of(t)
        sa = existing.get(t.pk)
        if sa is None:
            sa = SectionActivity(section=section, activity_type=t)
        changed = sa.pk is None or sa.planned_date != date
        sa.planned_date = date
        if t.actual_source == ActivityType.ActualSource.SHIP:
            actual = section.confirmed_ship_date if (section.is_completed and section.confirmed_ship_date) else None
            if sa.actual_date != actual:
                sa.actual_date = actual
                changed = True
        if changed:
            sa.save()


def set_actual(section, code, value, user):
    """Set (date) or clear (None) the actual date of an activity and write an audit record."""
    sa = section.activities.select_related("activity_type").get(activity_type__code=code)
    old = sa.actual_date
    sa.actual_date = value
    sa.save(update_fields=["actual_date", "updated_at"])
    audit.log(
        user,
        "update",
        sa,
        {"actual_date": [old.isoformat() if old else None, value.isoformat() if value else None], "code": code},
    )
    return sa


def activity_state(section_activity, today=None):
    """Return 'done', 'late', 'soon' or 'planned'."""
    today = today or timezone.localdate()
    if section_activity.actual_date:
        return "done"
    if section_activity.planned_date < today:
        return "late"
    if section_activity.planned_date <= add_business_days(today, SOON_BUSINESS_DAYS):
        return "soon"
    return "planned"
