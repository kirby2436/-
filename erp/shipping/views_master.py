from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Model
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from core.models import Carrier, Plant, VehicleType
from core.services import audit

from .forms import CarrierForm, PlantForm, VehicleTypeForm


# ---------------------------------------------------------------- audit helpers (shared with views_shipment)


def _plain(v):
    if isinstance(v, Model):
        return v.pk
    if isinstance(v, Decimal):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def created_changes(form):
    return {k: _plain(v) for k, v in form.cleaned_data.items() if v not in (None, "")}


def updated_changes(form):
    return {k: {"old": _plain(form.initial.get(k)), "new": _plain(form.cleaned_data.get(k))} for k in form.changed_data}


def safe_next(request):
    """Return-to address passed as ?next= (same host only), or ''."""
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return ""


# ---------------------------------------------------------------- generic form screen


def _form_screen(request, form_class, instance, *, tab, label, list_name):
    nxt = safe_next(request)
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by = request.user
        if instance is None:
            obj.created_by = request.user
            changes = created_changes(form)
        else:
            changes = updated_changes(form)
        obj.save()
        if instance is None:
            audit.log(request.user, "create", obj, changes)
            messages.success(request, f"{label} \"{obj.name}\"을(를) 추가했습니다.")
        else:
            if changes:
                audit.log(request.user, "update", obj, changes)
            messages.success(request, f"{label} \"{obj.name}\"을(를) 저장했습니다.")
        return redirect(nxt or f"shipping:{list_name}")
    return render(
        request,
        "shipping/master/form.html",
        {
            "form": form,
            "obj": instance,
            "tab": tab,
            "label": label,
            "next": nxt,
            "list_name": f"shipping:{list_name}",
        },
    )


# ---------------------------------------------------------------- plants


@login_required
def plant_list(request, pk=None):
    plants = list(Plant.objects.select_related("operator").order_by("site", "sort_order", "name"))
    groups = {}
    for p in plants:
        groups.setdefault(p.site or "", []).append(p)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0]))  # blank site last
    return render(
        request,
        "shipping/master/plant_list.html",
        {"groups": [{"site": s, "plants": ps} for s, ps in ordered], "tab": "plant", "count": len(plants)},
    )


@login_required
def plant_create(request, pk=None):
    return _form_screen(request, PlantForm, None, tab="plant", label="제작장", list_name="plant_list")


@login_required
def plant_edit(request, pk=None):
    return _form_screen(
        request, PlantForm, get_object_or_404(Plant, pk=pk), tab="plant", label="제작장", list_name="plant_list"
    )


# ---------------------------------------------------------------- carriers


@login_required
def carrier_list(request, pk=None):
    return render(request, "shipping/master/carrier_list.html", {"carriers": Carrier.objects.all(), "tab": "carrier"})


@login_required
def carrier_create(request, pk=None):
    return _form_screen(request, CarrierForm, None, tab="carrier", label="운송회사", list_name="carrier_list")


@login_required
def carrier_edit(request, pk=None):
    return _form_screen(
        request, CarrierForm, get_object_or_404(Carrier, pk=pk), tab="carrier", label="운송회사", list_name="carrier_list"
    )


# ---------------------------------------------------------------- vehicle types


@login_required
def vehicle_type_list(request, pk=None):
    return render(
        request, "shipping/master/vehicle_type_list.html", {"types": VehicleType.objects.all(), "tab": "vehicle"}
    )


@login_required
def vehicle_type_create(request, pk=None):
    return _form_screen(request, VehicleTypeForm, None, tab="vehicle", label="차종", list_name="vehicle_type_list")


@login_required
def vehicle_type_edit(request, pk=None):
    return _form_screen(
        request,
        VehicleTypeForm,
        get_object_or_404(VehicleType, pk=pk),
        tab="vehicle",
        label="차종",
        list_name="vehicle_type_list",
    )
