from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Shipment, ShipmentTruck

from . import statement as stmt

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _load(request, pk):
    """출하와 (있으면) ?truck= 차량을 읽는다. 다른 출하의 차량이거나 숫자가 아니면 404."""
    try:
        shipment = stmt.load_shipment(pk)
    except Shipment.DoesNotExist:
        raise Http404("출하를 찾을 수 없습니다.")
    truck = None
    raw = request.GET.get("truck", "").strip()
    if raw:
        if not raw.isdigit():
            raise Http404("차량을 찾을 수 없습니다.")
        truck = get_object_or_404(ShipmentTruck, pk=int(raw), shipment=shipment)
    return shipment, truck


@login_required
def statement(request, pk=None):
    shipment, truck = _load(request, pk)
    statements = stmt.build(shipment, truck)
    trucks = list(ShipmentTruck.objects.filter(shipment=shipment).order_by("seq").values_list("pk", "seq"))
    return render(
        request,
        "shipping/statement/statement.html",
        {
            "shipment": shipment,
            "statements": statements,
            "selected_truck": truck,
            "trucks": trucks,
            "notice": stmt.notice_for(shipment),
        },
    )


@login_required
def statement_xlsx(request, pk=None):
    shipment, truck = _load(request, pk)
    statements = stmt.build(shipment, truck)
    if not statements:
        messages.warning(request, "차량이 없어 엑셀을 만들 수 없습니다.")
        return redirect("shipping:detail", pk=shipment.pk)
    name = stmt.xlsx_filename(shipment, statements)
    response = HttpResponse(stmt.workbook_bytes(statements), content_type=XLSX_TYPE)
    response["Content-Disposition"] = f"attachment; filename=\"statement.xlsx\"; filename*=UTF-8''{quote(name)}"
    return response
