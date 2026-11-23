from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import BomItem, Section
from .services import area
from .services.schedule import sync_section_activities

_AREA_INPUTS = {"profile_text", "length_mm", "bottom_width", "side1_width", "side2_width"}


@receiver(post_save, sender=Section, dispatch_uid="core_section_sync_activities")
def section_saved(sender, instance, raw=False, **kwargs):
    if raw:
        return
    sync_section_activities(instance)


@receiver(pre_save, sender=BomItem, dispatch_uid="core_bomitem_fill_area")
def bom_item_pre_save(sender, instance, raw=False, update_fields=None, **kwargs):
    """Keep widths / unit area in step with Profile and length on every single save().

    bulk_create does not send pre_save; bom.services.save_upload calls area.fill_bom_items instead.
    """
    if raw:
        return
    if update_fields is not None and not (_AREA_INPUTS & set(update_fields)):
        return
    area.fill_bom_item(instance, save=False)
