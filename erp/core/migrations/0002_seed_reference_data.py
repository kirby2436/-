from django.db import migrations

PROJECT_TYPES = ["무해체보거푸집", "DECK PLATE", "거푸집/DECK", "거푸집/기타", "거푸집/DECK/기타"]

# code, name, sort_order, lead_days, base code, mark, actual_source
ACTIVITIES = [
    ("P100", "도면/BOM", 1, -21, None, "D", "BOM_UPLOAD"),
    ("P200", "Stirrup발주", 2, -14, None, "SO", "MANUAL"),
    ("P300", "Stirrup입고", 3, -3, "P700", "SP", "MANUAL"),
    ("P400", "COIL발주", 4, -5, "P600", "CO", "MANUAL"),
    ("P500", "COIL입고", 5, -1, "P600", "CP", "MANUAL"),
    ("P600", "성형생산", 6, -7, None, "M", "MANUAL"),
    ("P700", "ASS'Y", 7, -2, None, "A", "MANUAL"),
    ("P800", "PACKING", 8, -1, None, "P", "MANUAL"),
    ("P900", "출고", 9, 0, None, "DE", "SHIP"),
]


def seed(apps, schema_editor):
    ProjectType = apps.get_model("core", "ProjectType")
    ActivityType = apps.get_model("core", "ActivityType")
    for i, name in enumerate(PROJECT_TYPES, start=1):
        ProjectType.objects.get_or_create(name=name, defaults={"sort_order": i})
    created = {}
    for code, name, order, lead, _base, mark, source in ACTIVITIES:
        obj, _ = ActivityType.objects.get_or_create(
            code=code,
            defaults={"name": name, "sort_order": order, "lead_days": lead, "mark": mark, "actual_source": source},
        )
        created[code] = obj
    for code, _n, _o, _l, base, _m, _s in ACTIVITIES:
        if base:
            ActivityType.objects.filter(code=code).update(base_activity=created[base])


def unseed(apps, schema_editor):
    apps.get_model("core", "ActivityType").objects.filter(code__in=[a[0] for a in ACTIVITIES]).delete()
    apps.get_model("core", "ProjectType").objects.filter(name__in=PROJECT_TYPES).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
