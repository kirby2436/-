from django.db import migrations

VEHICLE_TYPES = ["5톤", "5톤축", "11톤", "25톤", "트레일러"]


def seed(apps, schema_editor):
    VehicleType = apps.get_model("core", "VehicleType")
    for i, name in enumerate(VEHICLE_TYPES, start=1):
        VehicleType.objects.get_or_create(name=name, defaults={"sort_order": i})


def unseed(apps, schema_editor):
    apps.get_model("core", "VehicleType").objects.filter(name__in=VEHICLE_TYPES).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0003_shipping_models")]

    operations = [migrations.RunPython(seed, unseed)]
