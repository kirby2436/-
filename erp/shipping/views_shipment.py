from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from core.models import BomItem, Plant, Project, Section, Shipment, ShipmentLine, ShipmentTruck
from core.services import audit, shipping

from . import reports
from .forms import CancelForm, ShipmentForm, TruckForm
from .views_master import created_changes, updated_changes

PLANNED = Shipment.Status.PLANNED
SHIPPED = Shipment.Status.SHIPPED
CANCELED = Shipment.Status.CANCELED

POOL_LIMIT = 500  # unshipped rows shown at once on the loading screen


def _get_shipment(pk):
    """Shipment with everything the screens need, in a fixed number of queries."""
    trucks = ShipmentTruck.objects.select_related("carrier", "vehicle_type").prefetch_related(
        Prefetch("lines", queryset=ShipmentLine.objects.select_related("bom_item__section__floor"))
    )
    qs = Shipment.objects.select_related("project", "plant").prefetch_related(Prefetch("trucks", queryset=trucks))
    return get_object_or_404(qs, pk=pk)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flash_errors(request, exc):
    for m in exc.messages:
        messages.error(request, m)


def _planned_only(request, shipment, what):
    """Return a redirect response when the shipment is not planned, else None."""
    if shipment.status != PLANNED:
        messages.error(request, f"예정 상태의 출하만 {what}할 수 있습니다. (현재: {shipment.get_status_display()})")
        return redirect("shipping:detail", pk=shipment.pk)
    return None


def _allowed_types(project):
    """Vehicle types allowed at the site (empty list = no restriction known)."""
    try:
        return list(project.site_profile.allowed_vehicle_types.all())
    except ObjectDoesNotExist:
        return []


def _vehicle_warning(truck, allowed):
    """Warning text when the truck's vehicle type is not allowed at the site, else ''."""
    vt = truck.vehicle_type
    if vt is None or not allowed or vt in allowed:
        return ""
    names = ", ".join(a.name for a in allowed)
    return f"주의: 현장개요의 진입 가능 차량({names})에 없는 차종입니다. 저장은 되지만 현장 진입을 확인하세요."


# ---------------------------------------------------------------- list


@login_required
def shipment_list(request, pk=None):
    qs = shipping.prefetch_shipments(Shipment.objects.all()).order_by("-ship_date", "-id")
    g = request.GET
    date_from, date_to = parse_date(g.get("from", "") or ""), parse_date(g.get("to", "") or "")
    project, plant, status = g.get("project", ""), g.get("plant", ""), g.get("status", "")
    if date_from:
        qs = qs.filter(ship_date__gte=date_from)
    if date_to:
        qs = qs.filter(ship_date__lte=date_to)
    if project.isdigit():
        qs = qs.filter(project_id=int(project))
    if plant.isdigit():
        qs = qs.filter(plant_id=int(plant))
    if status in Shipment.Status.values:
        qs = qs.filter(status=status)
    page = Paginator(qs, 20).get_page(g.get("page"))
    params = g.copy()
    params.pop("page", None)
    return render(
        request,
        "shipping/shipment/list.html",
        {
            "stats": _list_stats(),
            "status_chips": _status_chips(params, date_from, date_to, project, plant, status),
            "page": page,
            "date_from": g.get("from", "") if date_from else "",
            "date_to": g.get("to", "") if date_to else "",
            "project": project,
            "plant": plant,
            "status": status,
            "statuses": Shipment.Status.choices,
            "projects": Project.objects.all(),
            "plants": Plant.objects.all(),
            "qs": params.urlencode(),
        },
    )


def _list_stats():
    """Summary cards: this month's shipped totals plus the number of planned shipments (whole company)."""
    today = timezone.localdate()
    month_start = today.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    totals = reports.period_totals(month_start, month_end)
    return {
        "month": month_start.month,
        "shipments": totals["shipments"],
        "trucks": totals["trucks"],
        "area": totals["area"],
        "planned": Shipment.objects.filter(status=PLANNED).count(),
    }


def _status_chips(params, date_from, date_to, project, plant, status):
    """All / planned / shipped / canceled chips with counts under the other filters, one query."""
    qs = Shipment.objects.all()
    if date_from:
        qs = qs.filter(ship_date__gte=date_from)
    if date_to:
        qs = qs.filter(ship_date__lte=date_to)
    if project.isdigit():
        qs = qs.filter(project_id=int(project))
    if plant.isdigit():
        qs = qs.filter(plant_id=int(plant))
    counts = dict(qs.order_by().values_list("status").annotate(c=Count("pk")))

    def url(value):
        p = params.copy()
        if value:
            p["status"] = value
        else:
            p.pop("status", None)
        encoded = p.urlencode()
        return f"?{encoded}" if encoded else "?"

    chips = [{"label": "전체", "count": sum(counts.values()), "url": url(""), "active": status not in Shipment.Status.values}]
    for v, label in Shipment.Status.choices:
        chips.append({"label": label, "count": counts.get(v, 0), "url": url(v), "active": status == v})
    return chips


# ---------------------------------------------------------------- create / edit


@login_required
def shipment_create(request, pk=None):
    if request.method == "POST":
        form = ShipmentForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.created_by = obj.updated_by = request.user
            obj.save()
            audit.log(request.user, "create", obj, created_changes(form))
            messages.success(request, "출하를 등록했습니다. 이어서 상차를 편성하세요.")
            return redirect("shipping:load", pk=obj.pk)
        messages.error(request, "입력 내용을 확인하세요. 오류가 있는 항목 옆에 안내가 표시됩니다.")
    else:
        initial = {"ship_date": timezone.localdate()}
        last = (
            Shipment.objects.filter(created_by=request.user).exclude(dispatcher="").order_by("-id").values_list("dispatcher", flat=True).first()
        )
        if last:
            initial["dispatcher"] = last
        pid = _int(request.GET.get("project"))
        if pid and Project.objects.filter(pk=pid).exists():
            initial["project"] = pid
            sec = Section.objects.filter(project_id=pid, plant__isnull=False, plant__is_active=True).select_related("plant").first()
            if sec:
                initial["plant"] = sec.plant_id
        form = ShipmentForm(initial=initial)
    return render(request, "shipping/shipment/form.html", {"form": form, "shipment": None})


@login_required
def shipment_edit(request, pk=None):
    shipment = get_object_or_404(Shipment.objects.select_related("project"), pk=pk)
    blocked = _planned_only(request, shipment, "수정")
    if blocked:
        return blocked
    form = ShipmentForm(request.POST or None, instance=shipment)
    if request.method == "POST":
        if form.is_valid():
            changes = updated_changes(form)
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            if changes:
                audit.log(request.user, "update", obj, changes)
            messages.success(request, "기본정보를 저장했습니다.")
            return redirect("shipping:detail", pk=obj.pk)
        messages.error(request, "입력 내용을 확인하세요. 오류가 있는 항목 옆에 안내가 표시됩니다.")
    return render(request, "shipping/shipment/form.html", {"form": form, "shipment": shipment})


# ---------------------------------------------------------------- detail


@login_required
def shipment_detail(request, pk=None):
    shipment = _get_shipment(pk)
    trucks = list(shipment.trucks.all())
    allowed = _allowed_types(shipment.project)
    for t in trucks:
        t.warning = _vehicle_warning(t, allowed)
    unknown_area = sum(1 for t in trucks for ln in t.lines.all() if ln.bom_item.unit_area is None)
    return render(
        request,
        "shipping/shipment/detail.html",
        {"shipment": shipment, "trucks": trucks, "unknown_area": unknown_area},
    )


# ---------------------------------------------------------------- loading


def _handle_load_post(request, shipment):
    user = request.user
    action = request.POST.get("action", "")
    try:
        if action == "add_truck":
            truck = shipping.add_truck(shipment, user)
            messages.success(request, f"{truck.seq}호차를 추가했습니다.")
        elif action == "remove_truck":
            truck = get_object_or_404(ShipmentTruck, pk=_int(request.POST.get("truck")), shipment=shipment)
            if truck.lines.exists():
                messages.error(request, "부재가 담긴 차량은 삭제할 수 없습니다. 먼저 부재를 빼거나 다른 차량으로 옮기세요.")
            else:
                seq = truck.seq
                shipping.remove_truck(truck, user)
                messages.success(request, f"{seq}호차를 삭제했습니다.")
        elif action == "remove_line":
            line = get_object_or_404(
                ShipmentLine.objects.select_related("bom_item", "truck"),
                pk=_int(request.POST.get("line")),
                truck__shipment=shipment,
            )
            mark, seq = line.bom_item.ass_mark, line.truck.seq
            shipping.remove_line(line, user)
            messages.success(request, f"{seq}호차에서 {mark}을(를) 뺐습니다.")
        elif action == "move_line":
            line = get_object_or_404(
                ShipmentLine.objects.select_related("bom_item", "truck"),
                pk=_int(request.POST.get("line")),
                truck__shipment=shipment,
            )
            target = get_object_or_404(ShipmentTruck, pk=_int(request.POST.get("target")), shipment=shipment)
            shipping.move_line(line, target, user)
            messages.success(request, f"{line.bom_item.ass_mark}을(를) {target.seq}호차로 옮겼습니다.")
        elif action == "add_lines":
            _add_lines(request, shipment)
        else:
            messages.error(request, "알 수 없는 요청입니다.")
    except ValidationError as exc:
        _flash_errors(request, exc)


def _add_lines(request, shipment):
    ids = [i for i in (_int(v) for v in request.POST.getlist("pick")) if i]
    if not ids:
        messages.warning(request, "담을 부재를 먼저 선택하세요.")
        return
    truck_id = _int(request.POST.get("truck"))
    trucks = list(shipment.trucks.all())
    if truck_id:
        truck = get_object_or_404(ShipmentTruck, pk=truck_id, shipment=shipment)
    elif not trucks:
        truck = shipping.add_truck(shipment, request.user)
        messages.info(request, f"차량이 없어 {truck.seq}호차를 만들었습니다.")
    else:
        messages.error(request, "부재를 담을 차량을 선택하세요.")
        return
    items = {
        b.pk: b
        for b in BomItem.objects.filter(pk__in=ids, section__project_id=shipment.project_id).select_related("section")
    }
    done = 0
    for bid in ids:
        item = items.get(bid)
        if item is None:
            messages.error(request, "이 프로젝트에 없는 부재가 선택되어 건너뛰었습니다.")
            continue
        qty = _int((request.POST.get(f"qty_{bid}") or "").strip())
        if qty is None:
            messages.error(request, f"{item.ass_mark}: 수량을 정수로 입력하세요.")
            continue
        try:
            shipping.add_line(truck, item, qty, request.user)
            done += 1
        except ValidationError as exc:
            _flash_errors(request, exc)
    if done:
        messages.success(request, f"{done}건을 {truck.seq}호차에 담았습니다.")


@login_required
def shipment_load(request, pk=None):
    shipment = _get_shipment(pk)
    blocked = _planned_only(request, shipment, "상차를 편성")
    if blocked:
        return blocked
    if request.method == "POST":
        _handle_load_post(request, shipment)
        return redirect(request.get_full_path())

    project = shipment.project
    section = request.GET.get("section", "")
    q = request.GET.get("q", "").strip()
    pool_qs = shipping.annotate_remaining(
        BomItem.objects.filter(section__project=project).select_related("section__floor")
    ).filter(remaining__gt=0)
    if section.isdigit():
        pool_qs = pool_qs.filter(section_id=int(section))
    if q:
        pool_qs = pool_qs.filter(ass_mark__icontains=q)
    pool_qs = pool_qs.order_by("section__planned_ship_date", "section__name", "section__floor__sort_order", "id")
    pool = list(pool_qs[: POOL_LIMIT + 1])
    truncated = len(pool) > POOL_LIMIT
    pool = pool[:POOL_LIMIT]

    trucks = list(shipment.trucks.all())
    unknown_area = sum(1 for t in trucks for ln in t.lines.all() if ln.bom_item.unit_area is None)
    return render(
        request,
        "shipping/shipment/load.html",
        {
            "shipment": shipment,
            "project": project,
            "pool": pool,
            "truncated": truncated,
            "pool_limit": POOL_LIMIT,
            "pool_no_area": sum(1 for b in pool if b.unit_area is None),
            "trucks": trucks,
            "last_truck": trucks[-1] if trucks else None,
            "sections": project.sections.select_related("floor").order_by("planned_ship_date", "name"),
            "section": section,
            "q": q,
            "qs": request.GET.urlencode(),
            "unknown_area": unknown_area,
        },
    )


# ---------------------------------------------------------------- truck info


@login_required
def truck_edit(request, pk=None):
    truck = get_object_or_404(
        ShipmentTruck.objects.select_related("shipment__project", "carrier", "vehicle_type"), pk=pk
    )
    shipment = truck.shipment
    if shipment.status == CANCELED:
        messages.error(request, "취소된 출하의 차량 정보는 수정할 수 없습니다.")
        return redirect("shipping:detail", pk=shipment.pk)
    form = TruckForm(request.POST or None, instance=truck)
    if request.method == "POST":
        if form.is_valid():
            changes = updated_changes(form)
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            if changes:
                audit.log(request.user, "update", obj, {"truck_info": obj.seq, **changes})
            messages.success(request, f"{obj.seq}호차 운송 정보를 저장했습니다.")
            warn = _vehicle_warning(obj, _allowed_types(shipment.project))
            if warn:
                messages.warning(request, warn)
            return redirect("shipping:detail", pk=shipment.pk)
        messages.error(request, "입력 내용을 확인하세요. 오류가 있는 항목 옆에 안내가 표시됩니다.")
    return render(
        request,
        "shipping/shipment/truck_form.html",
        {"form": form, "truck": truck, "shipment": shipment, "warning": _vehicle_warning(truck, _allowed_types(shipment.project))},
    )


# ---------------------------------------------------------------- confirm / cancel


@login_required
def shipment_confirm(request, pk=None):
    shipment = _get_shipment(pk)
    blocked = _planned_only(request, shipment, "확정")
    if blocked:
        return blocked
    if request.method == "POST":
        try:
            shipping.confirm(shipment, request.user)
        except ValidationError as exc:
            _flash_errors(request, exc)
            return redirect("shipping:confirm", pk=shipment.pk)
        messages.success(request, "출하를 확정했습니다. 차량마다 공급번호가 부여되었습니다.")
        return redirect("shipping:detail", pk=shipment.pk)
    try:
        shipping.validate_for_confirm(shipment)  # preview only; nothing is saved
        problems = []
    except ValidationError as exc:
        problems = exc.messages
    return render(
        request,
        "shipping/shipment/confirm.html",
        {"shipment": shipment, "trucks": list(shipment.trucks.all()), "problems": problems},
    )


@login_required
def shipment_cancel(request, pk=None):
    shipment = _get_shipment(pk)
    if shipment.status == CANCELED:
        messages.error(request, "이미 취소된 출하입니다.")
        return redirect("shipping:detail", pk=shipment.pk)
    form = CancelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            shipping.cancel(shipment, request.user, form.cleaned_data["reason"])
        except ValidationError as exc:
            _flash_errors(request, exc)
        else:
            messages.success(request, "출하를 취소했습니다. 담았던 부재의 남은 수량이 되돌아갔습니다.")
            return redirect("shipping:detail", pk=shipment.pk)
    return render(request, "shipping/shipment/cancel.html", {"shipment": shipment, "form": form})
