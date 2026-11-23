from urllib.parse import quote, urlencode

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from core.models import Plant, Project, Section

from . import reports

PAGE_SIZE = 50
DATE_ERR = "올바른 날짜를 입력하세요. 예: 2026-03-18"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _today():
    return timezone.localdate()


class PeriodForm(forms.Form):
    start = forms.DateField(label="시작일", required=False, widget=forms.DateInput(attrs={"type": "date"}),
                            error_messages={"invalid": DATE_ERR})
    end = forms.DateField(label="종료일", required=False, widget=forms.DateInput(attrs={"type": "date"}),
                          error_messages={"invalid": DATE_ERR})

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            self.add_error("end", "종료일은 시작일 이후여야 합니다.")
        return data


class PerformanceForm(PeriodForm):
    axis = forms.ChoiceField(label="집계 기준", required=False, choices=reports.AXES,
                             error_messages={"invalid_choice": "선택할 수 없는 집계 기준입니다."})


class LinesForm(PeriodForm):
    project = forms.ModelChoiceField(label="프로젝트", required=False, queryset=Project.objects.none(),
                                     empty_label="전체", error_messages={"invalid_choice": "선택할 수 없는 프로젝트입니다."})
    section = forms.ModelChoiceField(label="구간", required=False, queryset=Section.objects.none(),
                                     empty_label="전체", error_messages={"invalid_choice": "선택할 수 없는 구간입니다."})
    plant = forms.ChoiceField(label="제작장", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.order_by("-code")
        self.fields["section"].queryset = Section.objects.select_related("project", "floor").order_by(
            "project__code", "planned_ship_date", "name"
        )
        self.fields["section"].label_from_instance = lambda s: " ".join(
            p for p in (s.project.code, str(s.floor) if s.floor_id else "", s.name) if p
        )
        self.plants = {str(p.pk): p for p in Plant.objects.order_by("sort_order", "name")}
        self.fields["plant"].choices = (
            [("", "전체"), (reports.NO_PLANT, reports.NO_PLANT_LABEL)]
            + [(k, p.name) for k, p in self.plants.items()]
        )
        self.fields["plant"].error_messages["invalid_choice"] = "선택할 수 없는 제작장입니다."

    def plant_value(self):
        value = self.cleaned_data.get("plant")
        if value == reports.NO_PLANT:
            return reports.NO_PLANT
        return self.plants.get(value) if value else None


class UnshippedForm(forms.Form):
    project = forms.ModelChoiceField(label="프로젝트", required=False, queryset=Project.objects.none(),
                                     empty_label="전체", error_messages={"invalid_choice": "선택할 수 없는 프로젝트입니다."})
    overdue_only = forms.BooleanField(label="출고예정일 지난 것만", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.order_by("-code")


def _bind(form_class, request, today):
    """Bound form where a blank period falls back to the default; an invalid form falls back too."""
    default_start, default_end = reports.default_period(today)
    data = request.GET.copy()
    if not data.get("start"):
        data["start"] = default_start.isoformat()
    if not data.get("end"):
        data["end"] = default_end.isoformat()
    form = form_class(data)
    if form.is_valid():
        return form, form.cleaned_data, False
    return form, {"start": default_start, "end": default_end}, True


def _tabs():
    return [
        ("performance", "출고 실적", reverse("shipping:performance")),
        ("lines", "부재별 내역", reverse("shipping:lines")),
    ]


def _render(request, template, ctx):
    ctx["tabs"] = _tabs()
    ctx["active_tab"] = request.resolver_match.url_name if request.resolver_match else ""
    return render(request, template, ctx)


@login_required
def performance(request, pk=None):
    today = _today()
    form, cleaned, invalid = _bind(PerformanceForm, request, today)
    axis = cleaned.get("axis") or "month"
    start, end = cleaned["start"], cleaned["end"]
    rows, totals = reports.performance(start, end, axis)
    lines_url = reverse("shipping:lines")
    for r in rows:
        r["url"] = f"{lines_url}?{urlencode(r['link'])}"
    return _render(request, "shipping/report/performance.html", {
        "form": form, "invalid": invalid, "axis": axis, "axis_label": dict(reports.AXES)[axis],
        "rows": rows, "totals": totals, "start": start, "end": end,
    })


def _lines_filters(request):
    form, cleaned, invalid = _bind(LinesForm, request, _today())
    if invalid:
        return form, True, {"start": cleaned["start"], "end": cleaned["end"]}
    return form, False, {
        "start": cleaned["start"], "end": cleaned["end"],
        "project": cleaned.get("project"), "section": cleaned.get("section"), "plant": form.plant_value(),
    }


@login_required
def lines(request, pk=None):
    form, invalid, f = _lines_filters(request)
    qs = reports.lines_queryset(**f)
    totals = reports.lines_totals(**f)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    xlsx_params = {k: v for k, v in params.items() if v}
    xlsx_params.setdefault("start", f["start"].isoformat())
    xlsx_params.setdefault("end", f["end"].isoformat())
    return _render(request, "shipping/report/lines.html", {
        "form": form, "invalid": invalid, "page": page, "totals": totals,
        "qs": params.urlencode(), "start": f["start"], "end": f["end"],
        "xlsx_url": f"{reverse('shipping:lines_xlsx')}?{urlencode(xlsx_params)}",
    })


XLSX_COLUMNS = [
    ("출고일", 12), ("프로젝트", 28), ("층·구간", 18), ("Ass Mark", 14), ("Profile", 14), ("길이(mm)", 11),
    ("수량", 8), ("면적(㎡)", 11), ("보 구분", 9), ("Location", 14), ("BOM 등록일", 12), ("제작장", 14),
    ("공급번호", 20),
]


def section_label(section):
    return f"{section.floor.name} {section.name}" if section.floor_id else section.name


@login_required
def lines_xlsx(request, pk=None):
    form, invalid, f = _lines_filters(request)
    if invalid:
        return redirect(f"{reverse('shipping:lines')}?{request.GET.urlencode()}")
    wb = Workbook()
    ws = wb.active
    ws.title = "부재별 내역"
    ws.append([name for name, _ in XLSX_COLUMNS])
    bold = Font(bold=True)
    for col, (_, width) in enumerate(XLSX_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col)
        cell.font = bold
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    n = 1
    for line in reports.lines_queryset(**f).iterator():
        shipment, item = line.truck.shipment, line.bom_item
        ws.append([
            shipment.ship_date, shipment.project.name, section_label(item.section), item.ass_mark, item.profile_text,
            float(item.length_mm), line.quantity, float(line.area) if line.area is not None else None,
            item.get_member_kind_display() if item.member_kind else "", item.location,
            timezone.localtime(item.created_at).date(),
            shipment.plant.name if shipment.plant_id else reports.NO_PLANT_LABEL, line.truck.supply_no or "",
        ])
        n += 1
        row = ws[n]
        row[0].number_format = "yyyy-mm-dd"
        row[5].number_format = "#,##0.##"
        row[6].number_format = "#,##0"
        row[7].number_format = "#,##0.00"
        row[10].number_format = "yyyy-mm-dd"
    totals = reports.lines_totals(**f)
    ws.append(["합계", None, None, None, None, None, totals["qty"], float(totals["area"])])
    last = ws.max_row
    for cell in ws[last]:
        cell.font = bold
    ws.cell(row=last, column=7).number_format = "#,##0"
    ws.cell(row=last, column=8).number_format = "#,##0.00"
    name = f"출고실적_{f['start']:%Y-%m-%d}_{f['end']:%Y-%m-%d}.xlsx"
    response = HttpResponse(content_type=XLSX_TYPE)
    response["Content-Disposition"] = f"attachment; filename=\"report.xlsx\"; filename*=UTF-8''{quote(name)}"
    wb.save(response)
    return response


@login_required
def unshipped(request, pk=None):
    today = _today()
    form = UnshippedForm(request.GET)
    valid = form.is_valid()
    cleaned = form.cleaned_data if valid else {}
    rows = reports.unshipped_sections(
        today, project=cleaned.get("project"), overdue_only=cleaned.get("overdue_only", False)
    )
    return _render(request, "shipping/report/unshipped.html", {
        "form": form, "invalid": not valid, "rows": rows, "today": today,
        "create_url": reverse("shipping:create"),
    })
