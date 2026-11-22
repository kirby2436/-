from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import ActivityType, Section
from core.services.schedule import set_actual

from . import selectors
from .forms import ActualDateForm, BoardFilterForm


def _today():
    return timezone.localdate()


@login_required
def home(request):
    today = _today()
    ctx = selectors.dashboard(today)
    ctx["today"] = today
    return render(request, "schedule/home.html", ctx)


@login_required
def board(request):
    today = _today()
    form = BoardFilterForm(request.GET or None)
    default_start, default_end = selectors.default_period(today)
    cleaned = form.cleaned_data if form.is_bound and form.is_valid() else {}
    qs = selectors.board_sections(
        today,
        start=cleaned.get("start"),
        end=cleaned.get("end"),
        project=cleaned.get("project"),
        status=cleaned.get("status") or None,
        manager=cleaned.get("manager"),
        late_only=cleaned.get("late_only", False),
    )
    paginator = Paginator(qs, selectors.PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    types, sections = selectors.attach_cells(page.object_list, today)
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "schedule/board.html", {
        "form": form,
        "page": page,
        "paginator": paginator,
        "sections": sections,
        "types": types,
        "legend": [selectors.state_info(s) for s in ("done", "late", "soon", "planned")],
        "querystring": params.urlencode(),
        "today": today,
        "default_start": default_start,
        "default_end": default_end,
        "filter_invalid": form.is_bound and not form.is_valid(),
    })


def _section_context(section, today, errors=None, values=None):
    rows = selectors.section_rows(section, today)
    for r in rows:
        r["manual"] = r["type"].actual_source == ActivityType.ActualSource.MANUAL
        r["error"] = (errors or {}).get(r["type"].code)
        r["value"] = (values or {}).get(r["type"].code, r["sa"].actual_date)
    return {
        "section": section,
        "project": section.project,
        "rows": rows,
        "history": selectors.section_history(section),
        "today": today,
    }


def _get_section(pk):
    return get_object_or_404(Section.objects.select_related("project", "floor", "fabricator"), pk=pk)


@login_required
def section_schedule(request, pk):
    section = _get_section(pk)
    return render(request, "schedule/section.html", _section_context(section, _today()))


@login_required
@require_POST
def section_actual(request, pk):
    section = _get_section(pk)
    today = _today()
    code = request.POST.get("code", "")
    action = request.POST.get("action", "save")
    atype = ActivityType.objects.filter(code=code).first()
    if atype is None or not section.activities.filter(activity_type=atype).exists():
        messages.error(request, "알 수 없는 Activity입니다.")
        return redirect("schedule:section", pk=section.pk)
    if atype.actual_source != ActivityType.ActualSource.MANUAL:
        messages.error(request, f"{atype.name}은(는) 자동으로 반영되는 단계라 직접 입력할 수 없습니다.")
        return redirect("schedule:section", pk=section.pk)
    if action == "clear":
        set_actual(section, code, None, request.user)
        messages.success(request, f"{atype.name} 실적일을 해제했습니다.")
        return redirect("schedule:section", pk=section.pk)
    form = ActualDateForm(request.POST)
    if not form.is_valid():
        ctx = _section_context(section, today, errors={code: form.errors["actual_date"][0]},
                               values={code: request.POST.get("actual_date", "")})
        return render(request, "schedule/section.html", ctx)
    set_actual(section, code, form.cleaned_data["actual_date"], request.user)
    messages.success(request, f"{atype.name} 실적일을 저장했습니다.")
    return redirect("schedule:section", pk=section.pk)
