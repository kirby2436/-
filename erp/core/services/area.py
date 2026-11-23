"""Member area calculation for BOM items (no HTTP, no model imports at module level)."""
import re
from decimal import ROUND_CEILING, Decimal

AREA_QUANT = Decimal("0.01")
_MM2_PER_M2 = Decimal(1_000_000)

_NAMED = re.compile(r"(?:NB|DH)(L?)(\d+)X(\d+)")
_TRIPLE = re.compile(r"(\d+)/(\d+)/(\d+)")


def parse_profile(text):
    """Return (bottom, side1, side2) in mm, or None when the Profile cannot be read.

    NB400X450 / DH400X450  -> (400, 450, 450)   one bottom plate, two side plates
    NBL400X450 / DHL400X450 -> (400, 450, 0)    one side plate only
    400/450/420             -> (400, 450, 420)
    Case, spaces and the separators x / X / * / x(multiplication sign) are ignored.
    """
    if not text:
        return None
    s = re.sub(r"\s+", "", str(text)).upper().replace("*", "X").replace("×", "X")
    m = _NAMED.fullmatch(s)
    if m:
        bottom, side = int(m.group(2)), int(m.group(3))
        return (bottom, side, 0 if m.group(1) else side)
    m = _TRIPLE.fullmatch(s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def unit_area(bottom, side1, side2, length_mm):
    """(bottom + side1 + side2) x length / 1,000,000 m2, rounded UP at the 2nd decimal place.

    Returns None when the bottom width or the length is missing or the result is not positive.
    Missing side widths count as 0. Decimal arithmetic only, so there is no float error.
    """
    if bottom is None or length_mm is None:
        return None
    width = int(bottom) + int(side1 or 0) + int(side2 or 0)
    value = Decimal(width) * Decimal(str(length_mm)) / _MM2_PER_M2
    if value <= 0:
        return None
    return value.quantize(AREA_QUANT, rounding=ROUND_CEILING)


def line_area(unit_area_value, quantity):
    """Area of `quantity` pieces: unit area (already rounded up) x quantity. None if unit area unknown.

    Spec 10-4 assumption: round one piece first, then multiply. Change only here if the rule changes.
    """
    if unit_area_value is None:
        return None
    return Decimal(unit_area_value) * int(quantity)


def member_kind(ass_mark):
    """BEAM / GIRDER from the Ass Mark. No rule is decided yet (spec 10-5), so always ''."""
    return ""


def fill_bom_item(bom_item, save=True):
    """Fill widths, unit area and member kind of a BomItem.

    Widths come from the Profile when it can be read. When it cannot, manually entered widths are
    kept and only the area is recalculated. A member kind that is already set is never overwritten.
    """
    parsed = parse_profile(bom_item.profile_text)
    if parsed is not None:
        bom_item.bottom_width, bom_item.side1_width, bom_item.side2_width = parsed
    bom_item.unit_area = unit_area(
        bom_item.bottom_width, bom_item.side1_width, bom_item.side2_width, bom_item.length_mm
    )
    if not bom_item.member_kind:
        bom_item.member_kind = member_kind(bom_item.ass_mark) or ""
    if save:
        if bom_item.pk is None:
            bom_item.save()
        else:
            type(bom_item).objects.filter(pk=bom_item.pk).update(
                bottom_width=bom_item.bottom_width,
                side1_width=bom_item.side1_width,
                side2_width=bom_item.side2_width,
                unit_area=bom_item.unit_area,
                member_kind=bom_item.member_kind,
            )
    return bom_item


def fill_bom_items(items):
    """Fill many unsaved BomItems (for bulk_create). Does not save."""
    for item in items:
        fill_bom_item(item, save=False)
    return items
