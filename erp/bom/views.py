from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Exists, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import BomItem, BomUpload, FormedPart, ProductComponent, Section
from core.services import audit

from . import services

PAGE_SIZE = 100
INDEX_PAGE_SIZE = 30
PREVIEW_LIMIT = 500


def _section(pk):
    return get_object_or_404(Section.objects.select_related("project", "floor"), pk=pk)


def _count_subquery(qs):
    return Coalesce(
        Subquery(qs.order_by().values("section").annotate(c=Count("pk")).values("c"), output_field=IntegerField()),
        0,
    )


@login_required
def index(request):
    q = request.GET.get("q", "").strip()
    unresolved_items = BomItem.objects.filter(section=OuterRef("pk")).annotate(
        configured=Exists(ProductComponent.objects.filter(product=OuterRef("product_id")))
    ).filter(configured=False)
    sections = (
        Section.objects.select_related("project", "floor")
        .annotate(
            bom_count=_count_subquery(BomItem.objects.filter(section=OuterRef("pk"))),
            unresolved_count=_count_subquery(unresolved_items),
        )
        .order_by("-project__code", "planned_ship_date", "name")
    )
    if q:
        sections = sections.filter(
            Q(project__code__icontains=q) | Q(project__name__icontains=q) | Q(name__icontains=q)
        )
    page = Paginator(sections, INDEX_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "bom/index.html", {"page": page, "q": q})


@login_required
def section_bom(request, pk):
    section = _section(pk)
    items_qs = (
        BomItem.objects.filter(section=section)
        .select_related("product")
        .annotate(configured=Exists(ProductComponent.objects.filter(product=OuterRef("product_id"))))
        .order_by("id")
    )
    totals = BomItem.objects.filter(section=section).aggregate(rows=Count("pk"), qty=Coalesce(Sum("quantity"), 0))
    page = Paginator(items_qs, PAGE_SIZE).get_page(request.GET.get("page"))
    req_rows, excluded = services.summarize_requirements(section)
    uploads = BomUpload.objects.filter(section=section).select_related("created_by")[:10]
    return render(
        request,
        "bom/section.html",
        {
            "section": section,
            "page": page,
            "totals": totals,
            "req_rows": req_rows,
            "excluded": excluded,
            "uploads": uploads,
        },
    )


def _render_upload_form(request, section, **extra):
    ctx = {"section": section, "existing_count": BomItem.objects.filter(section=section).count()}
    ctx.update(extra)
    return render(request, "bom/upload.html", ctx)


def _render_preview(request, section, token, filename, rows, mode, error=None):
    existing = BomItem.objects.filter(section=section)
    services.annotate_duplicates(rows, existing.values_list("ass_mark", flat=True))
    error_rows = sum(1 for r in rows if r["errors"])
    warn_rows = sum(1 for r in rows if (r["dup_file"] or r["dup_existing"]) and not r["errors"])
    if len(rows) > PREVIEW_LIMIT:
        shown = [r for r in rows if r["errors"] or r["dup_file"] or r["dup_existing"]][:PREVIEW_LIMIT]
        truncated = True
    else:
        shown, truncated = rows, False
    existing_count = existing.count()
    return render(
        request,
        "bom/preview.html",
        {
            "section": section,
            "token": token,
            "filename": filename,
            "rows": shown,
            "truncated": truncated,
            "total_rows": len(rows),
            "error_rows": error_rows,
            "warn_rows": warn_rows,
            "existing_count": existing_count,
            "mode": mode or (services.MODE_APPEND if existing_count else services.MODE_REPLACE),
            "error": error,
        },
    )


@login_required
def upload(request, pk):
    section = _section(pk)
    if request.method != "POST":
        return _render_upload_form(request, section)

    step = request.POST.get("step", "preview")
    if step == "confirm":
        token = request.POST.get("payload", "")
        try:
            filename, rows = services.unsign_rows(token, section.pk, request.user.pk)
        except services.BomParseError as exc:
            return _render_upload_form(request, section, error=str(exc))
        mode = request.POST.get("mode", "")
        error = None
        existing = BomItem.objects.filter(section=section).count()
        if mode not in (services.MODE_REPLACE, services.MODE_APPEND):
            error = "등록 방식을 선택하세요."
        elif any(r["errors"] for r in rows):
            error = "오류가 있는 행이 있어 저장할 수 없습니다. 파일을 고쳐 다시 올려 주세요."
        elif mode == services.MODE_REPLACE and existing and request.POST.get("confirm_replace") != "on":
            error = f"기존 BOM {existing:,}건을 모두 삭제하려면 확인란에 체크하세요."
        if error:
            return _render_preview(request, section, token, filename, rows, mode, error=error)
        try:
            result = services.save_upload(section, rows, filename, mode, request.user)
        except services.BomParseError as exc:
            return _render_preview(request, section, token, filename, rows, mode, error=str(exc))
        msg = f"BOM {result['created']:,}건을 등록했습니다."
        if result["deleted"]:
            msg += f" (기존 {result['deleted']:,}건 삭제)"
        messages.success(request, msg)
        if result["unlinked"]:
            messages.warning(
                request, f"품목 미등록 {result['unlinked']:,}건이 있습니다. '누락 품목 정리'에서 구성을 등록하세요."
            )
        return redirect("bom:section", pk=section.pk)

    # step 1: parse
    upfile = request.FILES.get("file")
    text = request.POST.get("text", "")
    try:
        if upfile:
            filename = upfile.name
            rows = services.parse_xlsx(upfile, upfile.name)
        elif text.strip():
            filename = ""
            rows = services.parse_text(text)
        else:
            raise services.BomParseError(".xlsx 파일을 선택하거나 엑셀에서 복사한 표를 붙여넣으세요.")
    except services.BomParseError as exc:
        return _render_upload_form(request, section, error=str(exc), text=text)
    token = services.sign_rows(section.pk, request.user.pk, filename, rows)
    return _render_preview(request, section, token, filename, rows, None)


@login_required
def resolve(request, pk):
    section = _section(pk)
    pending = {e["profile"]: e for e in services.unresolved_profiles(section)}
    saved = 0
    if request.method == "POST":
        names = request.POST.getlist("profile")
        failed = {}
        for i, name in enumerate(names):
            entry = pending.get(name)
            if entry is None:
                continue
            f = services.form_from_post(request.POST, f"f{i}", name, entry["count"], entry["has_product"])
            if f["errors"]:
                failed[name] = f
            elif not f["is_blank"]:
                services.apply_product_form(f, request.user)
                saved += 1
        if saved:
            messages.success(request, f"{saved}개 품목의 구성을 저장하고 BOM에 연결했습니다.")
        pending = {e["profile"]: e for e in services.unresolved_profiles(section)}
        if not failed:
            if not saved:
                messages.info(request, "입력된 내용이 없어 저장하지 않았습니다.")
            if not pending:
                return redirect("bom:section", pk=section.pk)
            return redirect("bom:resolve", pk=section.pk)
        messages.error(request, "입력을 확인하세요. 오류가 없는 품목만 저장했습니다.")
        forms = [failed.get(p) or services.blank_form(p, e["count"], e["has_product"]) for p, e in pending.items()]
    else:
        forms = [services.blank_form(p, e["count"], e["has_product"]) for p, e in pending.items()]
    return render(
        request,
        "bom/resolve.html",
        {
            "section": section,
            "forms": forms,
            "formed_parts": {
                kind.value: list(FormedPart.objects.filter(kind=kind.value).values_list("name", flat=True))
                for kind in services.KINDS
            },
            "kinds": [(k.value, k.label) for k in services.KINDS],
            "slots": services.SLOTS,
        },
    )


@login_required
@require_POST
def item_delete(request, pk):
    item = get_object_or_404(BomItem.objects.select_related("section"), pk=pk)
    section_pk = item.section_id
    services.delete_item(item, request.user)
    messages.success(request, "BOM 행을 삭제했습니다.")
    return redirect("bom:section", pk=section_pk)


@login_required
@require_POST
def item_note(request, pk):
    item = get_object_or_404(BomItem, pk=pk)
    note = request.POST.get("note", "").strip()
    if len(note) > 200:
        messages.error(request, "비고는 200자 이하로 입력하세요.")
    else:
        old = item.note
        item.note = note
        item.updated_by = request.user
        item.save(update_fields=["note", "updated_by", "updated_at"])
        audit.log(request.user, "update", item, {"note": [old, note]})
        messages.success(request, "비고를 저장했습니다.")
    url = redirect("bom:section", pk=item.section_id).url
    page = request.POST.get("page", "")
    if page.isdigit():
        url += f"?page={page}"
    return redirect(url)
