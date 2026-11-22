from core.models import AuditLog


def log(user, action, obj, changes=None):
    """Record who did what to which object. `action`: create / update / delete."""
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    return AuditLog.objects.create(
        user=user,
        action=action,
        model=obj._meta.label,
        object_id=str(obj.pk) if obj.pk is not None else "",
        object_repr=str(obj)[:200],
        changes=changes or {},
    )
