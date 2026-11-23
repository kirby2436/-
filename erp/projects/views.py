from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, IntegerField, Value, Max as Max_, Model, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import Floor, ItemPrice, OrderItem, Partner, Project, ProjectType, Section, SectionFile, SiteProfile
from core.services import audit

from .forms import (
    ItemPriceFormSet,
    OrderItemFormSet,
    ProjectForm,
    QuickFloorForm,
    QuickPartnerForm,
    SectionFileForm,
    SectionFormSet,
    SiteProfileForm,
)


def _plain(v):
    if isinstance(v, Model):
        return v.pk
    if isinstance(v, Decimal):
        return str(v)
    if hasattr(v, "name") and hasattr(v, "read"):  # uploaded or stored file
        return v.name
    return v


def _created_changes(form):
    return {k: _plain(v) for k, v in form.cleaned_data.items() if k != "DELETE" and v not in (None, "")}


def _updated_changes(form):
    return {
        k: {"old": _plain(form.initial.get(k)), "new": _plain(form.cleaned_data.get(k))} for k in form.changed_data
    }


# ---------------------------------------------------------------- list / detail


def project_list(request):
    amount = (
        OrderItem.objects.filter(project=OuterRef("pk")).order_by().values("project").annotate(t=Sum("amount")).values("t")
    )
    sec_count = (
        Section.objects.filter(project=OuterRef("pk")).order_by().values("project").annotate(c=Count("pk")).values("c")
    )
    qs = (
        Project.objects.select_related("project_type", "customer", "contractor")
        .annotate(
            amount_total=Coalesce(Subquery(amount), Value(0), output_field=DecimalField()),
            section_count=Coalesce(Subquery(sec_count, output_field=IntegerField()), 0),
        )
        .order_by("-code")
    )
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    ptype = request.GET.get("type", "")
    customer = request.GET.get("customer", "")
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
    if status in Project.Status.values:
        qs = qs.filter(status=status)
    if ptype.isdigit():
        qs = qs.filter(project_type_id=int(ptype))
    if customer.isdigit():
        qs = qs.filter(customer_id=int(customer))
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "projects/list.html",
        {
            "stats": _list_stats(),
            "type_chips": _type_chips(params, q, customer, status, ptype),
            "status_chips": _status_chips(params, q, customer, status, ptype),
            "page": page,
            "q": q,
            "status": status,
            "ptype": ptype,
            "customer": customer,
            "statuses": Project.Status.choices,
            "types": ProjectType.objects.all(),
            "customers": Partner.objects.filter(is_customer=True),
            "qs": params.urlencode(),
        },
    )


def _list_stats():
    """Summary cards: always the whole company, never the current filter."""
    agg = Project.objects.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(status=Project.Status.ACTIVE)),
        no_manager=Count("pk", filter=Q(manager__isnull=True)),
    )
    amount = OrderItem.objects.aggregate(t=Sum("amount"))["t"] or 0
    if amount >= 100_000_000:
        amount_text, amount_unit = f"{amount / 100_000_000:,.1f}", "억원"
    else:
        amount_text, amount_unit = f"{amount:,.0f}", "원"
    agg.update(amount=amount, amount_text=amount_text, amount_unit=amount_unit)
    return agg


def _chip_url(params, key, value):
    p = params.copy()
    if value:
        p[key] = value
    else:
        p.pop(key, None)
    qs = p.urlencode()
    return f"?{qs}" if qs else "?"


def _filtered(q, customer):
    qs = Project.objects.all()
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
    if customer.isdigit():
        qs = qs.filter(customer_id=int(customer))
    return qs


def _type_chips(params, q, customer, status, ptype):
    """Chips per project type. Counts follow the other filters (search, customer, status), one query."""
    base = _filtered(q, customer)
    if status in Project.Status.values:
        base = base.filter(status=status)
    counts = dict(base.order_by().values_list("project_type_id").annotate(c=Count("pk")))
    chips = [{"label": "전체", "count": sum(counts.values()), "url": _chip_url(params, "type", ""), "active": not ptype}]
    for t in ProjectType.objects.all():
        chips.append(
            {"label": t.name, "count": counts.get(t.pk, 0), "url": _chip_url(params, "type", str(t.pk)), "active": ptype == str(t.pk)}
        )
    return chips


def _status_chips(params, q, customer, status, ptype):
    """Chips per status. Counts follow the other filters (search, customer, type), one query."""
    base = _filtered(q, customer)
    if ptype.isdigit():
        base = base.filter(project_type_id=int(ptype))
    counts = dict(base.order_by().values_list("status").annotate(c=Count("pk")))
    chips = [{"label": "전체", "count": sum(counts.values()), "url": _chip_url(params, "status", ""), "active": status not in Project.Status.values}]
    for v, label in Project.Status.choices:
        chips.append({"label": label, "count": counts.get(v, 0), "url": _chip_url(params, "status", v), "active": status == v})
    return chips


def project_detail(request, pk):
    project = get_object_or_404(Project.objects.select_related("project_type", "customer", "contractor", "manager"), pk=pk)
    sections = project.sections.select_related("floor", "fabricator", "plant").annotate(bom_count=Count("bom_items"))
    site_profile = (
        SiteProfile.objects.prefetch_related("allowed_vehicle_types").filter(project=project).first()
    )
    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "site_profile": site_profile,
            "order_items": project.order_items.all(),
            "prices": project.item_prices.all(),
            "sections": sections,
            "files": project.files.select_related("section", "section__floor"),
        },
    )


# ---------------------------------------------------------------- basic info


def project_create(request):
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.created_by = project.updated_by = request.user
        project.save()
        audit.log(request.user, "create", project, _created_changes(form))
        messages.success(request, f"프로젝트 {project.code}을(를) 등록했습니다.")
        return redirect("projects:detail", pk=project.pk)
    return render(request, "projects/project_form.html", {"form": form, "project": None})


def project_edit(request, pk):
    project = get_object_or_404(Project, pk=pk)
    form = ProjectForm(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid():
        changes = _updated_changes(form)
        project = form.save(commit=False)
        project.updated_by = request.user
        project.save()
        if changes:
            audit.log(request.user, "update", project, changes)
        messages.success(request, "기본정보를 저장했습니다.")
        return redirect("projects:detail", pk=project.pk)
    return render(request, "projects/project_form.html", {"form": form, "project": project})


# ---------------------------------------------------------------- site profile


def site_profile(request, pk):
    project = get_object_or_404(Project, pk=pk)
    profile = SiteProfile.objects.filter(project=project).first()
    is_new = profile is None
    form = SiteProfileForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        changes = {}
        for name in form.changed_data:
            if name == "allowed_vehicle_types":
                old = sorted(v.name for v in form.instance.allowed_vehicle_types.all()) if not is_new else []
                new = sorted(v.name for v in form.cleaned_data[name])
                changes[name] = {"old": old, "new": new}
            else:
                changes[name] = {"old": _plain(form.initial.get(name)), "new": _plain(form.cleaned_data.get(name))}
        with transaction.atomic():
            obj = form.save(commit=False)
            obj.project = project
            obj.updated_by = request.user
            if is_new:
                obj.created_by = request.user
            obj.save()
            form.save_m2m()
            if is_new:
                changes = {k: v["new"] for k, v in changes.items() if v["new"] not in (None, "", [], False)}
            if changes or is_new:
                audit.log(request.user, "create" if is_new else "update", obj, changes)
        messages.success(request, "현장개요를 저장했습니다.")
        return redirect("projects:detail", pk=project.pk)
    if request.method == "POST":
        messages.error(request, "입력 내용을 확인하세요. 오류가 있는 항목 옆에 안내가 표시됩니다.")
    return render(request, "projects/site_profile_form.html", {"project": project, "form": form, "is_new": is_new})


# ---------------------------------------------------------------- formset screens


def _save_formset(request, project, formset, before_save=None):
    """Create/update/delete rows of an inline formset with audit logging. Returns counts."""
    counts = {"create": 0, "update": 0, "delete": 0}
    with transaction.atomic():
        for form in formset.forms:
            cd = getattr(form, "cleaned_data", None)
            if cd is None:
                continue
            inst = form.instance
            if cd.get("DELETE"):
                if inst.pk:
                    rep = str(inst)
                    pk = inst.pk
                    snapshot = {"id": pk, "repr": rep}
                    inst.delete()
                    inst.pk = pk  # keep id for the log
                    audit.log(request.user, "delete", inst, snapshot)
                    counts["delete"] += 1
                continue
            if not form.has_changed():
                continue
            is_new = inst.pk is None
            changes = _created_changes(form) if is_new else _updated_changes(form)
            obj = form.save(commit=False)
            obj.project = project
            obj.updated_by = request.user
            if is_new:
                obj.created_by = request.user
            if before_save:
                before_save(obj, is_new)
            obj.save()
            audit.log(request.user, "create" if is_new else "update", obj, changes)
            counts["create" if is_new else "update"] += 1
    return counts


def _formset_screen(request, project, formset_class, *, title, template_extra, before_save=None, after_save=None, queryset=None):
    kwargs = {"instance": project}
    if queryset is not None:
        kwargs["queryset"] = queryset
    if request.method == "POST":
        formset = formset_class(request.POST, **kwargs)
        if formset.is_valid():
            try:
                counts = _save_formset(request, project, formset, before_save)
            except IntegrityError:
                formset._non_form_errors = formset.error_class(
                    ["저장 중 중복된 값이 발견되었습니다. 이름과 층이 같은 구간이 없는지 확인하세요."]
                )
            else:
                if after_save:
                    after_save()
                total = sum(counts.values())
                if total:
                    messages.success(
                        request,
                        f"저장했습니다. (추가 {counts['create']}건, 수정 {counts['update']}건, 삭제 {counts['delete']}건)",
                    )
                else:
                    messages.info(request, "변경된 내용이 없습니다.")
                return redirect("projects:detail", pk=project.pk)
        else:
            messages.error(request, "입력 내용을 확인하세요. 오류가 있는 항목 옆에 안내가 표시됩니다.")
    else:
        formset = formset_class(**kwargs)
    ctx = {"project": project, "formset": formset, "title": title}
    ctx.update(template_extra)
    return render(request, "projects/formset_edit.html", ctx)


def order_items_edit(request, pk):
    project = get_object_or_404(Project, pk=pk)

    def before(obj, is_new):
        if is_new:
            obj.seq = 100000 + OrderItem.objects.filter(project=project).count() + 1

    def renumber():
        for i, item in enumerate(project.order_items.order_by("seq", "id"), 1):
            if item.seq != i:
                OrderItem.objects.filter(pk=item.pk).update(seq=i)

    return _formset_screen(
        request, project, OrderItemFormSet, title="수주정보 수정", template_extra={"kind": "order"},
        before_save=before, after_save=renumber,
    )


def prices_edit(request, pk):
    project = get_object_or_404(Project, pk=pk)
    return _formset_screen(request, project, ItemPriceFormSet, title="품목 단가 수정", template_extra={"kind": "price"})


def sections_edit(request, pk):
    project = get_object_or_404(Project, pk=pk)
    qs = project.sections.select_related("floor").annotate(bom_count=Count("bom_items"))
    return _formset_screen(
        request, project, SectionFormSet, title="구간 수정", template_extra={"kind": "section"}, queryset=qs
    )


# ---------------------------------------------------------------- files


def files(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if request.method == "POST":
        form = SectionFileForm(request.POST, request.FILES, project=project)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.project = project
            obj.seq = (project.files.aggregate(m=Max_("seq"))["m"] or 0) + 1
            obj.created_by = obj.updated_by = request.user
            obj.save()
            changes = _created_changes(form)
            changes["seq"] = obj.seq
            audit.log(request.user, "create", obj, changes)
            messages.success(request, f"파일을 등록했습니다. (순번 {obj.seq})")
            return redirect("projects:files", pk=project.pk)
        messages.error(request, "입력 내용을 확인하세요.")
    else:
        form = SectionFileForm(project=project)
    return render(
        request,
        "projects/files.html",
        {"project": project, "form": form, "files": project.files.select_related("section", "section__floor")},
    )


def file_delete(request, pk, file_pk):
    project = get_object_or_404(Project, pk=pk)
    obj = get_object_or_404(SectionFile, pk=file_pk, project=project)
    if request.method == "POST":
        name = obj.file.name
        rep = str(obj)
        file_id = obj.pk
        obj.delete()
        obj.pk = file_id
        audit.log(request.user, "delete", obj, {"file": name, "seq": obj.seq})
        try:
            obj.file.storage.delete(name)
        except Exception:  # file already gone; the record is what matters
            pass
        messages.success(request, f"{rep} 파일을 삭제했습니다.")
        return redirect("projects:files", pk=project.pk)
    return render(request, "projects/file_confirm_delete.html", {"project": project, "file": obj})


# ---------------------------------------------------------------- quick add (JSON)


def _quick_error(form, label, limit):
    if "role" in form.errors:
        return "거래처 구분이 올바르지 않습니다."
    codes = [e.code for e in form.errors.as_data().get("name", [])]
    if "max_length" in codes:
        return f"{label}은(는) {limit}자 이하로 입력하세요."
    return f"{label}을(를) 입력하세요."


@require_POST
def quick_add_partner(request):
    form = QuickPartnerForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"ok": False, "error": _quick_error(form, "거래처명", 100)}, status=400)
    name = form.cleaned_data["name"].strip()
    flag = "is_" + form.cleaned_data["role"]
    existing = Partner.objects.filter(name=name).first()
    if existing:
        if not getattr(existing, flag):
            setattr(existing, flag, True)
            existing.updated_by = request.user
            existing.save()
            audit.log(request.user, "update", existing, {flag: {"old": False, "new": True}})
        return JsonResponse(
            {"ok": True, "existing": True, "id": existing.pk, "name": existing.name,
             "message": "이미 등록된 거래처라 기존 항목을 선택했습니다."}
        )
    partner = Partner(name=name, created_by=request.user, updated_by=request.user, **{flag: True})
    partner.save()
    audit.log(request.user, "create", partner, {"name": name, flag: True})
    return JsonResponse({"ok": True, "existing": False, "id": partner.pk, "name": partner.name})


@require_POST
def quick_add_floor(request):
    form = QuickFloorForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"ok": False, "error": _quick_error(form, "층 이름", 30)}, status=400)
    name = form.cleaned_data["name"].strip()
    existing = Floor.objects.filter(name=name).first()
    if existing:
        return JsonResponse(
            {"ok": True, "existing": True, "id": existing.pk, "name": existing.name,
             "message": "이미 등록된 층이라 기존 항목을 선택했습니다."}
        )
    floor = Floor(name=name, created_by=request.user, updated_by=request.user)
    floor.save()
    audit.log(request.user, "create", floor, {"name": name})
    return JsonResponse({"ok": True, "existing": False, "id": floor.pk, "name": floor.name})
