from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Section
from .services.schedule import sync_section_activities


@receiver(post_save, sender=Section, dispatch_uid="core_section_sync_activities")
def section_saved(sender, instance, raw=False, **kwargs):
    if raw:
        return
    sync_section_activities(instance)
