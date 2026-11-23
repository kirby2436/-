"""Shipment rules: remaining quantity, supply numbers, loading, confirm / cancel, section sync."""
import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, IntegerField, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from core.models import BomItem, Section, Shipment, ShipmentLine, ShipmentTruck
from core.services import audit
from core.services.area import line_area
from core.services.schedule import sync_section_activities

SUPPLY_PREFIX = "NEO"
_MAX_SUPPLY_RETRY = 5


# ------------------------------------------------------------ remaining qty

def _active_lines():
    """Lines of planned and shipped shipments (canceled ones excluded)."""
    return ShipmentLine.objects.exclude(truck__shipment__status=Shipment.Status.CANCELED)


def shipped_qty(bom_item):
    return _active_lines().filter(bom_item=bom_item).aggregate(t=Sum("quantity"))["t"] or 0


def remaining_qty(bom_item):
    """BOM quantity minus quantity on planned and shipped shipments (canceled excluded)."""
    return bom_item.quantity - shipped_qty(bom_item)


def annotate_remaining(queryset):
    """Annotate BomItem rows with `shipped_total` and `remaining` (no per-row queries).

    `BomItem.shipped_qty` / `remaining_qty` use the annotation when present.
    """
    sub = (
        _active_lines()
        .filter(bom_item=OuterRef("pk"))
        .order_by()
        .values("bom_item")
        .annotate(t=Sum("quantity"))
        .values("t")
    )
    return queryset.annotate(shipped_total=Coalesce(Subquery(sub, output_field=IntegerField()), 0)).annotate(
        remaining=F("quantity") - F("shipped_total")
    )


def prefetch_shipments(queryset):
    """select/prefetch everything Shipment.title and its parts need (list screens)."""
    return queryset.select_related("project", "plant").prefetch_related(
        "trucks__lines__bom_item__section__floor"
    )


# ------------------------------------------------------------ supply number

def next_supply_no(ship_date):
    """Next 'NEO-YYYY-MM-DD-NN' for that date. Numbers on canceled shipments stay taken (never reused)."""
    prefix = f"{SUPPLY_PREFIX}-{ship_date:%Y-%m-%d}-"
    last = 0
    for no in ShipmentTruck.objects.filter(supply_no__startswith=prefix).values_list("supply_no", flat=True):
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", no)
        if m:
            last = max(last, int(m.group(1)))
    return f"{prefix}{last + 1:02d}"


# ------------------------------------------------------------ helpers

def _require_planned(shipment):
    if shipment.status != Shipment.Status.PLANNED:
        raise ValidationError("예정 상태의 출하만 수정할 수 있습니다.")


def _lock(shipment):
    """Re-read the shipment inside the transaction (row lock where the database supports it)."""
    return Shipment.objects.select_for_update().get(pk=shipment.pk)


def _section_ids(shipment):
    return set(
        Section.objects.filter(bom_items__shipment_lines__truck__shipment=shipment).values_list("pk", flat=True)
    )


# ------------------------------------------------------------ loading (planned shipments)

@transaction.atomic
def add_truck(shipment, user):
    shipment = _lock(shipment)
    _require_planned(shipment)
    last = shipment.trucks.order_by("-seq").values_list("seq", flat=True).first() or 0
    truck = ShipmentTruck.objects.create(shipment=shipment, seq=last + 1, created_by=user, updated_by=user)
    audit.log(user, "update", shipment, {"add_truck": truck.seq})
    return truck


@transaction.atomic
def remove_truck(truck, user):
    shipment = _lock(truck.shipment)
    _require_planned(shipment)
    seq = truck.seq
    truck.delete()  # lines are removed with it
    audit.log(user, "update", shipment, {"remove_truck": seq})


@transaction.atomic
def add_line(truck, bom_item, quantity, user):
    """Put `quantity` of a BOM item on a truck. Adds to the existing line when the truck already has it."""
    shipment = _lock(truck.shipment)
    _require_planned(shipment)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValidationError("수량은 1 이상의 정수여야 합니다.")
    if bom_item.section.project_id != shipment.project_id:
        raise ValidationError("다른 프로젝트의 부재는 담을 수 없습니다.")
    remaining = remaining_qty(bom_item)
    if quantity > remaining:
        raise ValidationError(f"{bom_item.ass_mark}: 남은 수량({remaining})을 넘을 수 없습니다.")
    line = ShipmentLine.objects.filter(truck=truck, bom_item=bom_item).first()
    if line:
        line.quantity += quantity
        line.updated_by = user
        line.save(update_fields=["quantity", "updated_by", "updated_at"])
    else:
        line = ShipmentLine.objects.create(
            truck=truck, bom_item=bom_item, quantity=quantity, created_by=user, updated_by=user
        )
    audit.log(
        user,
        "update",
        shipment,
        {"add_line": {"truck": truck.seq, "bom_item": bom_item.pk, "ass_mark": bom_item.ass_mark, "quantity": quantity}},
    )
    return line


@transaction.atomic
def remove_line(line, user):
    shipment = _lock(line.truck.shipment)
    _require_planned(shipment)
    info = {"truck": line.truck.seq, "bom_item": line.bom_item_id, "quantity": line.quantity}
    line.delete()
    audit.log(user, "update", shipment, {"remove_line": info})


@transaction.atomic
def move_line(line, target_truck, user):
    """Move a line to another truck of the same shipment (quantities merge when the target has the item)."""
    shipment = _lock(line.truck.shipment)
    _require_planned(shipment)
    if target_truck.shipment_id != shipment.pk:
        raise ValidationError("같은 출하의 다른 차량으로만 옮길 수 있습니다.")
    if target_truck.pk == line.truck_id:
        return line
    src_seq = line.truck.seq
    existing = ShipmentLine.objects.filter(truck=target_truck, bom_item=line.bom_item).first()
    if existing:
        existing.quantity += line.quantity
        existing.updated_by = user
        existing.save(update_fields=["quantity", "updated_by", "updated_at"])
        line.delete()
        result = existing
    else:
        line.truck = target_truck
        line.updated_by = user
        line.save(update_fields=["truck", "updated_by", "updated_at"])
        result = line
    audit.log(
        user,
        "update",
        shipment,
        {"move_line": {"bom_item": result.bom_item_id, "from": src_seq, "to": target_truck.seq}},
    )
    return result


# ------------------------------------------------------------ confirm / cancel

def validate_for_confirm(shipment):
    errors = []
    trucks = list(shipment.trucks.prefetch_related("lines__bom_item").order_by("seq"))
    if not trucks:
        errors.append("차량이 1대 이상 있어야 합니다.")
    bom_ids = set()
    for truck in trucks:
        lines = list(truck.lines.all())
        if not lines:
            errors.append(f"{truck.seq}호차에 담긴 부재가 없습니다.")
        for line in lines:
            bom_ids.add(line.bom_item_id)
            if line.bom_item.unit_area is None:
                errors.append(f"{line.bom_item.ass_mark}: 면적이 계산되지 않았습니다. 규격을 확인하세요.")
    for item in annotate_remaining(BomItem.objects.filter(pk__in=bom_ids)):
        if item.remaining < 0:
            errors.append(f"{item.ass_mark}: 출하 수량이 BOM 수량({item.quantity})을 넘습니다.")
    if errors:
        raise ValidationError(errors)
    return trucks


def _assign_supply_no(truck, ship_date):
    for _ in range(_MAX_SUPPLY_RETRY):
        try:
            with transaction.atomic():
                truck.supply_no = next_supply_no(ship_date)
                truck.save(update_fields=["supply_no", "updated_at"])
            return
        except IntegrityError:
            truck.supply_no = None
    raise ValidationError("공급번호를 부여하지 못했습니다. 다시 시도하세요.")


@transaction.atomic
def confirm(shipment, user):
    """PLANNED -> SHIPPED: validate, give each truck a supply number, store line areas, sync sections."""
    shipment = _lock(shipment)
    if shipment.status != Shipment.Status.PLANNED:
        raise ValidationError("예정 상태의 출하만 확정할 수 있습니다.")
    trucks = validate_for_confirm(shipment)
    numbers = []
    for truck in trucks:
        _assign_supply_no(truck, shipment.ship_date)
        numbers.append(truck.supply_no)
        for line in truck.lines.select_related("bom_item"):
            line.area = line_area(line.bom_item.unit_area, line.quantity)
            line.save(update_fields=["area", "updated_at"])
    shipment.status = Shipment.Status.SHIPPED
    shipment.updated_by = user
    shipment.save(update_fields=["status", "updated_by", "updated_at"])
    _sync_sections(shipment)
    audit.log(user, "update", shipment, {"status": ["PLANNED", "SHIPPED"], "supply_nos": numbers})
    return shipment


@transaction.atomic
def cancel(shipment, user, reason):
    """PLANNED or SHIPPED -> CANCELED. Supply numbers stay on the trucks so they are never reused."""
    shipment = _lock(shipment)
    if shipment.status == Shipment.Status.CANCELED:
        raise ValidationError("이미 취소된 출하입니다.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("취소 사유를 입력하세요.")
    old = shipment.status
    shipment.status = Shipment.Status.CANCELED
    shipment.canceled_reason = reason[:200]
    shipment.updated_by = user
    shipment.save(update_fields=["status", "canceled_reason", "updated_by", "updated_at"])
    _sync_sections(shipment)
    audit.log(user, "update", shipment, {"status": [old, "CANCELED"], "reason": reason})
    return shipment


def _sync_sections(shipment):
    for section in Section.objects.filter(pk__in=_section_ids(shipment)):
        sync_section_shipping(section)


# ------------------------------------------------------------ section sync

@transaction.atomic
def sync_section_shipping(section):
    """Complete the section when every BOM item is fully shipped (SHIPPED status only), else reopen it.

    A section without BOM items is left untouched. Afterwards the P900 actual date follows.
    """
    items = list(BomItem.objects.filter(section=section).values_list("pk", "quantity"))
    if not items:
        return section
    shipped_by_item = dict(
        ShipmentLine.objects.filter(bom_item__section=section, truck__shipment__status=Shipment.Status.SHIPPED)
        .values_list("bom_item")
        .annotate(t=Sum("quantity"))
    )
    complete = all(shipped_by_item.get(pk, 0) >= qty for pk, qty in items)
    changed = False
    if complete:
        last = (
            Shipment.objects.filter(status=Shipment.Status.SHIPPED, trucks__lines__bom_item__section=section)
            .order_by("-ship_date")
            .values_list("ship_date", flat=True)
            .first()
        )
        if not section.is_completed:
            section.is_completed = True
            changed = True
        if section.confirmed_ship_date != last:
            section.confirmed_ship_date = last
            changed = True
    elif section.is_completed:
        section.is_completed = False
        changed = True
    if changed:
        section.save(update_fields=["is_completed", "confirmed_ship_date", "updated_at"])  # post_save syncs activities
    else:
        sync_section_activities(section)
    return section
