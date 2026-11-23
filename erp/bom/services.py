"""BOM parsing, validation, saving and requirement calculation (no HTTP here)."""
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core import signing
from django.db import transaction
from django.db.models import ProtectedError
from django.db.models import Exists, OuterRef
from django.utils import timezone

from core.models import (
    BomItem,
    BomUpload,
    FormedPart,
    Product,
    ProductComponent,
    SectionActivity,
    ShipmentLine,
)
from core.services import area, audit
from core.services.schedule import set_actual, sync_section_activities

MAX_ROWS = 5000
HEADER_SCAN_ROWS = 30
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_SCAN_ROWS = 200000
MAX_LENGTH = Decimal("99999999.99")
SIGN_SALT = "bom.upload.preview"
SIGN_MAX_AGE = 60 * 60

MODE_REPLACE = "replace"
MODE_APPEND = "append"

FIELD_LABELS = {
    "ass_mark": "Ass Mark",
    "profile": "Profile",
    "length": "Length(mm)",
    "qty": "Q'ty",
    "finish": "FINISH",
    "location": "LOCATION",
}
REQUIRED_FIELDS = ("ass_mark", "profile", "length", "qty")
HEADER_ALIASES = {
    "assmark": "ass_mark",
    "profile": "profile",
    "length": "length",
    "qty": "qty",
    "수량": "qty",
    "finish": "finish",
    "location": "location",
}


class BomParseError(Exception):
    """Raised with a Korean message when the input cannot be used at all."""


# ---------------------------------------------------------------- parsing

def normalize_header(value):
    """Lower-case, drop parentheses content, apostrophes and whitespace."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\([^)]*\)|（[^）]*）", "", text)
    text = re.sub(r"[\'’‘`´\"]", "", text)
    text = re.sub(r"\s+", "", text)
    return text.lower()


def find_header(rows):
    """Return (index, {field: column}) of the header row within the first rows, else None."""
    for idx, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        mapping = {}
        for col, cell in enumerate(row):
            field = HEADER_ALIASES.get(normalize_header(cell))
            if field and field not in mapping:
                mapping[field] = col
        if all(f in mapping for f in REQUIRED_FIELDS):
            return idx, mapping
    return None


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    return str(value).strip()


def clean_row(raw):
    """Validate one row dict (ass_mark, profile, length, qty, finish, location, row_no).

    Returns a normalised dict: length as str with 2 decimals (or ''), qty int (or None),
    and `errors` (list of Korean messages).
    """
    errors = []
    ass_mark = _cell_text(raw.get("ass_mark"))
    profile = _cell_text(raw.get("profile"))
    finish = _cell_text(raw.get("finish"))
    location = _cell_text(raw.get("location"))
    if not ass_mark:
        errors.append("Ass Mark가 없습니다.")
    if not profile:
        errors.append("Profile이 없습니다.")
    for label, val in (("Ass Mark", ass_mark), ("Profile", profile), ("FINISH", finish), ("LOCATION", location)):
        if len(val) > 100:
            errors.append(f"{label}가 100자를 넘습니다.")

    length = None
    length_raw = raw.get("length")
    if isinstance(length_raw, bool):
        length_raw = None
    try:
        text = str(length_raw).replace(",", "").strip() if length_raw not in (None, "") else ""
        length = Decimal(text) if text else None
        if length is not None and not length.is_finite():
            length = None
    except (InvalidOperation, ValueError):
        length = None
    if length is None:
        errors.append("길이가 숫자가 아닙니다.")
    elif length <= 0:
        errors.append("길이는 0보다 커야 합니다.")
        length = None
    elif length > MAX_LENGTH:
        errors.append("길이가 너무 큽니다.")
        length = None
    else:
        length = length.quantize(Decimal("0.01"))

    qty = None
    qty_raw = raw.get("qty")
    try:
        if isinstance(qty_raw, bool) or qty_raw in (None, ""):
            raise ValueError
        qd = Decimal(str(qty_raw).replace(",", "").strip())
        if not qd.is_finite() or qd != qd.to_integral_value():
            raise ValueError
        qty = int(qd)
    except (InvalidOperation, ValueError):
        errors.append("수량이 정수가 아닙니다.")
    else:
        if qty <= 0:
            errors.append("수량은 1 이상이어야 합니다.")
            qty = None
        elif qty > 2_000_000_000:
            errors.append("수량이 너무 큽니다.")
            qty = None

    return {
        "row_no": raw.get("row_no"),
        "ass_mark": ass_mark,
        "profile": profile,
        "length": f"{length:.2f}" if length is not None else _cell_text(length_raw),
        "qty": qty if qty is not None else _cell_text(qty_raw),
        "finish": finish,
        "location": location,
        "errors": errors,
    }


def rows_from_grid(grid):
    """Turn a list of cell lists into cleaned rows. `grid` includes everything incl. header."""
    found = find_header(grid)
    if not found:
        raise BomParseError(
            "머리글을 찾지 못했습니다. 처음 30행 안에 Ass Mark, Profile, Length(mm), Q'ty 열이 있어야 합니다."
        )
    header_idx, mapping = found
    rows = []
    for offset, cells in enumerate(grid[header_idx + 1:], start=header_idx + 2):
        if all(_cell_text(c) == "" for c in cells):
            continue
        raw = {"row_no": offset}
        for field, col in mapping.items():
            raw[field] = cells[col] if col < len(cells) else None
        rows.append(clean_row(raw))
        if len(rows) > MAX_ROWS:
            raise BomParseError(f"행이 너무 많습니다. 한 번에 최대 {MAX_ROWS:,}행까지 등록할 수 있습니다.")
    if not rows:
        raise BomParseError("머리글 아래에 등록할 데이터가 없습니다.")
    return rows


def parse_text(text):
    """Parse tab-separated text pasted from a spreadsheet."""
    if not text or not text.strip():
        raise BomParseError("붙여넣은 내용이 없습니다.")
    grid = [line.split("\t") for line in text.splitlines()]
    return rows_from_grid(grid)


def parse_xlsx(fileobj, filename=""):
    """Parse the active sheet of an .xlsx file object."""
    from openpyxl import load_workbook

    if not str(filename).lower().endswith(".xlsx"):
        raise BomParseError(".xlsx 파일만 올릴 수 있습니다.")
    size = getattr(fileobj, "size", None)
    if size is not None and size > MAX_FILE_BYTES:
        raise BomParseError("파일이 너무 큽니다(최대 10MB).")
    wb = None
    try:
        wb = load_workbook(fileobj, read_only=True, data_only=True)
        ws = wb.active
        grid = []
        non_empty = 0
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_SCAN_ROWS:
                break
            grid.append(list(row))
            if any(_cell_text(c) != "" for c in row):
                non_empty += 1
            if non_empty > MAX_ROWS + HEADER_SCAN_ROWS:
                raise BomParseError(f"행이 너무 많습니다. 한 번에 최대 {MAX_ROWS:,}행까지 등록할 수 있습니다.")
    except BomParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BomParseError("엑셀 파일을 읽을 수 없습니다. 올바른 .xlsx 파일인지 확인하세요.") from exc
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:  # noqa: BLE001
                pass
    return rows_from_grid(grid)


# ------------------------------------------------------- duplicates, signing

def annotate_duplicates(rows, existing_marks=()):
    """Set row['dup_file'] and row['dup_existing'] flags (warnings only)."""
    existing = set(existing_marks)
    counts = defaultdict(int)
    for r in rows:
        counts[r["ass_mark"]] += 1
    for r in rows:
        r["dup_file"] = bool(r["ass_mark"]) and counts[r["ass_mark"]] > 1
        r["dup_existing"] = bool(r["ass_mark"]) and r["ass_mark"] in existing
    return rows


def sign_rows(section_id, user_id, filename, rows):
    slim = [
        [r["row_no"], r["ass_mark"], r["profile"], r["length"], r["qty"], r["finish"], r["location"]]
        for r in rows
    ]
    return signing.dumps(
        {"s": section_id, "u": user_id, "f": filename, "r": slim}, salt=SIGN_SALT, compress=True
    )


def unsign_rows(token, section_id, user_id):
    """Return (filename, cleaned rows). Raises BomParseError for a bad or foreign payload."""
    try:
        data = signing.loads(token, salt=SIGN_SALT, max_age=SIGN_MAX_AGE)
        if data["s"] != section_id or data["u"] != user_id:
            raise ValueError
        raw_rows = data["r"]
        if not isinstance(raw_rows, list) or len(raw_rows) > MAX_ROWS:
            raise ValueError
        rows = []
        for item in raw_rows:
            row_no, ass_mark, profile, length, qty, finish, location = item
            rows.append(
                clean_row(
                    {
                        "row_no": row_no,
                        "ass_mark": ass_mark,
                        "profile": profile,
                        "length": length,
                        "qty": qty,
                        "finish": finish,
                        "location": location,
                    }
                )
            )
        return str(data.get("f") or "")[:255], rows
    except signing.SignatureExpired as exc:
        raise BomParseError("미리보기가 만료되었습니다. 다시 업로드하세요.") from exc
    except (signing.BadSignature, KeyError, ValueError, TypeError) as exc:
        raise BomParseError("미리보기 데이터가 올바르지 않습니다. 다시 업로드하세요.") from exc


# ------------------------------------------------------------------ saving

def _p100(section):
    qs = SectionActivity.objects.filter(section=section, activity_type__code="P100")
    sa = qs.first()
    if sa is None:
        sync_section_activities(section)
        sa = qs.first()
    return sa


REPLACE_LOCKED_MESSAGE = "출하에 담긴 부재가 있어 추가 등록만 할 수 있습니다."
ITEM_LOCKED_MESSAGE = "출하에 담긴 부재라 삭제할 수 없습니다. 출하 기록(취소 포함)이 있는 부재는 지울 수 없습니다."


def section_has_shipment_lines(section):
    return ShipmentLine.objects.filter(bom_item__section=section).exists()


@transaction.atomic
def save_upload(section, rows, filename, mode, user, today=None):
    """Save validated rows. Returns dict(created, deleted, upload, unlinked)."""
    if mode not in (MODE_REPLACE, MODE_APPEND):
        raise BomParseError("등록 방식이 올바르지 않습니다.")
    rows = [clean_row(r) for r in rows]
    if not rows:
        raise BomParseError("등록할 행이 없습니다.")
    if any(r["errors"] for r in rows):
        raise BomParseError("오류가 있는 행이 있어 저장할 수 없습니다.")
    today = today or timezone.localdate()

    deleted = 0
    if mode == MODE_REPLACE:
        if section_has_shipment_lines(section):
            raise BomParseError(REPLACE_LOCKED_MESSAGE)
        deleted = BomItem.objects.filter(section=section).count()
        try:
            BomItem.objects.filter(section=section).delete()
        except ProtectedError as exc:
            raise BomParseError(REPLACE_LOCKED_MESSAGE) from exc

    upload = BomUpload.objects.create(
        section=section,
        filename=filename[:255],
        is_additional=(mode == MODE_APPEND),
        row_count=len(rows),
        created_by=user,
        updated_by=user,
    )
    names = {r["profile"] for r in rows}
    products = {p.name: p for p in Product.objects.filter(name__in=names)}
    items = [
        BomItem(
            section=section,
            upload=upload,
            ass_mark=r["ass_mark"],
            profile_text=r["profile"],
            product=products.get(r["profile"]),
            length_mm=Decimal(r["length"]),
            quantity=r["qty"],
            finish=r["finish"],
            location=r["location"],
            created_by=user,
            updated_by=user,
        )
        for r in rows
    ]
    area.fill_bom_items(items)  # bulk_create sends no pre_save signal
    BomItem.objects.bulk_create(items)
    unlinked = sum(1 for i in items if i.product is None)

    sa = _p100(section)
    if sa is not None and sa.actual_date is None:
        set_actual(section, "P100", today, user)

    audit.log(
        user,
        "create",
        upload,
        {
            "section": section.pk,
            "mode": mode,
            "rows": len(rows),
            "deleted": deleted,
            "unlinked": unlinked,
            "filename": filename,
        },
    )
    return {"created": len(items), "deleted": deleted, "upload": upload, "unlinked": unlinked}


@transaction.atomic
def delete_item(item, user):
    section = item.section
    if item.shipment_lines.exists():
        raise BomParseError(ITEM_LOCKED_MESSAGE)
    audit.log(
        user,
        "delete",
        item,
        {"section": section.pk, "ass_mark": item.ass_mark, "profile": item.profile_text, "qty": item.quantity},
    )
    try:
        item.delete()
    except ProtectedError as exc:
        raise BomParseError(ITEM_LOCKED_MESSAGE) from exc
    if not BomItem.objects.filter(section=section).exists():
        sa = _p100(section)
        if sa is not None and sa.actual_date is not None:
            set_actual(section, "P100", None, user)


# ----------------------------------------------------------------- resolve

def configured_exists():
    return Exists(ProductComponent.objects.filter(product=OuterRef("product_id")))


def unresolved_profiles(section):
    """[{'profile', 'count', 'has_product'}] of profiles lacking a configured product."""
    items = (
        BomItem.objects.filter(section=section)
        .annotate(configured=configured_exists())
        .filter(configured=False)
        .values_list("profile_text", "product_id")
    )
    agg = {}
    for profile, product_id in items:
        entry = agg.setdefault(profile, {"profile": profile, "count": 0, "has_product": product_id is not None})
        entry["count"] += 1
    return sorted(agg.values(), key=lambda e: e["profile"])


KINDS = (FormedPart.Kind.BOTTOM, FormedPart.Kind.SIDE)
SLOTS = (1, 2, 3)


def blank_form(profile, count=0, has_product=False):
    """Form state for one profile, prefilled from an existing Product when there is one."""
    form = {
        "profile": profile,
        "count": count,
        "has_product": has_product,
        "bottom_thickness": "",
        "side_thickness": "",
        "slots": {k.value: [{"slot": s, "name": "", "qty": "1", "ns": False} for s in SLOTS] for k in KINDS},
        "errors": [],
    }
    product = Product.objects.filter(name=profile).first()
    if product:
        form["bottom_thickness"] = "" if product.bottom_thickness is None else f"{product.bottom_thickness:g}"
        form["side_thickness"] = "" if product.side_thickness is None else f"{product.side_thickness:g}"
        for comp in product.components.select_related("formed_part"):
            slot = form["slots"][comp.kind][comp.slot - 1]
            slot.update(name=comp.formed_part.name, qty=str(comp.quantity), ns=comp.non_standard)
    return _finish(form)


def _finish(form):
    form["groups"] = [
        {"kind": k.value, "label": k.label, "slots": form["slots"][k.value]} for k in KINDS
    ]
    return form


def form_from_post(post, prefix, profile, count=0, has_product=False):
    """Read one profile's form from POST. Returns form state (with errors) and is_blank flag."""
    form = {
        "profile": profile,
        "count": count,
        "has_product": has_product,
        "bottom_thickness": post.get(f"{prefix}-bottom_thickness", "").strip(),
        "side_thickness": post.get(f"{prefix}-side_thickness", "").strip(),
        "slots": {},
        "errors": [],
    }
    any_input = bool(form["bottom_thickness"] or form["side_thickness"])
    component_count = 0
    for kind in KINDS:
        slots = []
        for s in SLOTS:
            key = f"{prefix}-{kind.value}{s}"
            name = post.get(f"{key}-name", "").strip()
            qty = post.get(f"{key}-qty", "").strip()
            ns = post.get(f"{key}-ns") == "on"
            slots.append({"slot": s, "name": name, "qty": qty or "1", "ns": ns, "error": ""})
            if name:
                any_input = True
                component_count += 1
                if len(name) > 50:
                    slots[-1]["error"] = "규격은 50자 이하여야 합니다."
                try:
                    if int(qty or "1") < 1 or int(qty or "1") > 1000:
                        raise ValueError
                except ValueError:
                    slots[-1]["error"] = "수량은 1~1000의 정수여야 합니다."
        form["slots"][kind.value] = slots
    for field, label in (("bottom_thickness", "하판 두께"), ("side_thickness", "측판 두께")):
        if form[field]:
            try:
                d = Decimal(form[field])
                if not d.is_finite() or d <= 0 or d >= 1000:
                    raise InvalidOperation
            except InvalidOperation:
                form["errors"].append(f"{label}는 0보다 큰 숫자여야 합니다.")
    if any_input and component_count == 0:
        form["errors"].append("하판 또는 측판 구성을 한 개 이상 입력하세요.")
    for kind in KINDS:
        for slot in form["slots"][kind.value]:
            if slot.get("error"):
                form["errors"].append(f"{FormedPart.Kind(kind).label} {slot['slot']}: {slot['error']}")
    form["is_blank"] = not any_input
    return _finish(form)


@transaction.atomic
def apply_product_form(form, user):
    """Create/update the Product and its components from a validated form; link BOM items."""
    profile = form["profile"]
    product = Product.objects.filter(name=profile).first()
    created = product is None
    if created:
        product = Product(name=profile, created_by=user)
    product.bottom_thickness = Decimal(form["bottom_thickness"]) if form["bottom_thickness"] else None
    product.side_thickness = Decimal(form["side_thickness"]) if form["side_thickness"] else None
    product.updated_by = user
    product.save()
    product.components.all().delete()
    made = 0
    for kind in KINDS:
        for slot in form["slots"][kind.value]:
            if not slot["name"]:
                continue
            part, _ = FormedPart.objects.get_or_create(
                kind=kind.value, name=slot["name"], defaults={"created_by": user, "updated_by": user}
            )
            ProductComponent.objects.create(
                product=product,
                kind=kind.value,
                slot=slot["slot"],
                formed_part=part,
                quantity=int(slot["qty"] or 1),
                non_standard=slot["ns"],
                created_by=user,
                updated_by=user,
            )
            made += 1
    linked = BomItem.objects.filter(profile_text=profile, product__isnull=True).update(
        product=product, updated_by=user, updated_at=timezone.now()
    )
    audit.log(
        user,
        "create" if created else "update",
        product,
        {"components": made, "linked_items": linked, "profile": profile},
    )
    return product, linked


# ------------------------------------------------------------- requirements

def summarize_requirements(section):
    """Return (rows, excluded) for a section.

    rows: list of dict(kind, kind_label, name, thickness, count, total_length) grouped by
    (kind, formed part name, thickness). excluded: number of BOM rows without a configured product.
    """
    items = list(BomItem.objects.filter(section=section).values_list("product_id", "length_mm", "quantity"))
    per_product = defaultdict(lambda: [0, Decimal("0")])
    excluded = 0
    for product_id, length, qty in items:
        if product_id is None:
            excluded += 1
            continue
        acc = per_product[product_id]
        acc[0] += qty
        acc[1] += length * qty
    if not per_product:
        return [], excluded
    comps = (
        ProductComponent.objects.filter(product_id__in=per_product.keys())
        .select_related("product", "formed_part")
        .order_by("product_id", "kind", "slot")
    )
    configured = set()
    groups = {}
    for comp in comps:
        configured.add(comp.product_id)
        qty_sum, len_sum = per_product[comp.product_id]
        thickness = comp.product.bottom_thickness if comp.kind == FormedPart.Kind.BOTTOM else comp.product.side_thickness
        key = (comp.kind, comp.formed_part.name, thickness)
        g = groups.setdefault(
            key,
            {
                "kind": comp.kind,
                "kind_label": FormedPart.Kind(comp.kind).label,
                "name": comp.formed_part.name,
                "thickness": thickness,
                "count": 0,
                "total_length": Decimal("0"),
            },
        )
        g["count"] += qty_sum * comp.quantity
        g["total_length"] += len_sum * comp.quantity
    for product_id, (qty_sum, _len) in per_product.items():
        if product_id not in configured:
            # product exists but has no components: its BOM rows are excluded too
            excluded += sum(1 for pid, _l, _q in items if pid == product_id)
    rows = sorted(
        groups.values(),
        key=lambda g: (g["kind"] != FormedPart.Kind.BOTTOM, g["name"], g["thickness"] if g["thickness"] is not None else Decimal("-1")),
    )
    return rows, excluded
