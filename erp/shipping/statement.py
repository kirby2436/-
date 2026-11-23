"""거래명세서 데이터 구성과 엑셀 작성. 인쇄 화면과 엑셀이 같은 build() 결과를 쓴다."""
import re
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.pagebreak import Break
from openpyxl.worksheet.properties import PageSetupProperties

from core.models import Shipment, ShipmentLine, ShipmentTruck

ROWS_PER_PAGE = 31  # 원본 양식의 부재 행 수(15~45행)
UNCONFIRMED = "(미확정)"
END_MARK = "이하여백"
PREVIEW_NOTICE = "출하 확정 전 미리보기"
CANCELED_NOTICE = "취소된 출하"
ZERO = Decimal("0")


def _natural(text):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text or "")]


def _fmt_length(value):
    if value is None:
        return ""
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _width(value):
    return "" if value is None else value


def load_shipment(pk):
    """거래명세서에 필요한 관계를 한 번에 가져온다. 없으면 Shipment.DoesNotExist."""
    return Shipment.objects.select_related(
        "project__customer", "project__contractor", "project__site_profile", "plant"
    ).get(pk=pk)


def _section_label(section):
    return f"{section.floor.name} {section.name}" if section.floor_id else section.name


def notice_for(shipment):
    if shipment.status == Shipment.Status.CANCELED:
        return CANCELED_NOTICE
    if shipment.status == Shipment.Status.PLANNED:
        return PREVIEW_NOTICE
    return ""


def _site_profile(project):
    try:
        return project.site_profile
    except ObjectDoesNotExist:
        return None


def _truck_header(shipment, truck, labels):
    project = shipment.project
    profile = _site_profile(project)
    company = settings.COMPANY_INFO
    phone = company.get("phone", "")
    if company.get("fax"):
        phone = f"{phone} / FAX {company['fax']}" if phone else f"FAX {company['fax']}"
    contact = ""
    if profile:
        contact = " ".join(p for p in (profile.unloading_contact_name, profile.unloading_contact_phone) if p)
    return SimpleNamespace(
        supply_no=truck.supply_no or UNCONFIRMED,
        supplier_name=company.get("name", ""),
        supplier_address=company.get("address", ""),
        supplier_phone=phone,
        customer_name=project.customer.name,
        contractor_name=project.contractor.name if project.contractor_id else "",
        site_name=project.name,
        site_address=profile.address if profile else "",
        unloading_contact=contact,
        ship_date=shipment.ship_date,
        arrival_date=shipment.arrival_date,
        dispatcher=shipment.dispatcher,
        carrier=truck.carrier.name if truck.carrier_id else "",
        vehicle_type=truck.vehicle_type.name if truck.vehicle_type_id else "",
        plate_no=truck.plate_no,
        driver_name=truck.driver_name,
        driver_phone=truck.driver_phone,
        section_label=", ".join(labels),
    )


def _paginate(items):
    """부재 행을 쪽 단위로 나눈다. 마지막 쪽 첫 빈 칸에 이하여백, 나머지는 빈 줄로 채운다."""
    chunks = [items[i : i + ROWS_PER_PAGE] for i in range(0, len(items), ROWS_PER_PAGE)] or [[]]
    pages = []
    for idx, chunk in enumerate(chunks, start=1):
        rows = [SimpleNamespace(kind="item", item=it) for it in chunk]
        if idx == len(chunks):
            rows.append(SimpleNamespace(kind="end", item=None))  # 꽉 찬 쪽이면 한 줄 늘어난다
        rows += [SimpleNamespace(kind="blank", item=None)] * (ROWS_PER_PAGE - len(rows))
        pages.append(SimpleNamespace(number=idx, total=len(chunks), rows=rows, is_last=idx == len(chunks)))
    return pages


def _make_statement(shipment, truck, notice):
    lines = sorted(
        truck.lines.all(),
        key=lambda ln: (
            ln.bom_item.section.floor.sort_order if ln.bom_item.section.floor_id else -1,
            ln.bom_item.section.floor_id or 0,
            ln.bom_item.section.name,
            ln.bom_item.section_id,
            _natural(ln.bom_item.ass_mark),
            ln.id,
        ),
    )
    items, labels = [], []
    for no, ln in enumerate(lines, start=1):
        bom = ln.bom_item
        label = _section_label(bom.section)
        if label not in labels:
            labels.append(label)
        items.append(
            SimpleNamespace(
                no=no,
                ass_mark=bom.ass_mark,
                profile=bom.profile_text,
                bottom=_width(bom.bottom_width),
                side1=_width(bom.side1_width),
                side2=_width(bom.side2_width),
                length=_fmt_length(bom.length_mm),
                quantity=ln.quantity,
                area=ln.effective_area,
                kind=bom.get_member_kind_display() if bom.member_kind else "",
                location=bom.location,
            )
        )
    return SimpleNamespace(
        truck_id=truck.id,
        seq=truck.seq,
        sheet_name=f"{truck.seq}호차",
        header=_truck_header(shipment, truck, labels),
        items=items,
        pages=_paginate(items),
        page_count=max(1, -(-len(items) // ROWS_PER_PAGE)),
        total_quantity=sum(i.quantity for i in items),
        total_area=sum((i.area for i in items), ZERO),
        notice=notice,
        status=shipment.status,
    )


def build(shipment, truck=None):
    """차량별 거래명세서 데이터 목록. truck을 주면 그 차량 하나(같은 출하의 차량이어야 함).

    쿼리는 부재 수와 무관하게 고정(차량 + 상차 부재 + 그에 딸린 부재·구간·층).
    """
    qs = ShipmentTruck.objects.filter(shipment=shipment)
    if truck is not None:
        qs = qs.filter(pk=truck.pk)
    qs = qs.select_related("carrier", "vehicle_type").prefetch_related(
        Prefetch(
            "lines",
            queryset=ShipmentLine.objects.select_related("bom_item__section__floor"),
        )
    )
    notice = notice_for(shipment)
    return [_make_statement(shipment, t, notice) for t in qs.order_by("seq", "id")]


# ------------------------------------------------------------ 파일명

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def xlsx_filename(shipment, statements):
    """Shipment.title과 같은 요소 순서: 날짜, 현장, 층·구간, N대, M부재, 면적, 제작장."""
    labels = []
    for st in statements:
        for part in st.header.section_label.split(", "):
            if part and part not in labels:
                labels.append(part)
    qty = sum(st.total_quantity for st in statements)
    area = sum((st.total_area for st in statements), ZERO)
    d = shipment.ship_date
    parts = [
        f"{d:%y}년 {d:%m}월 {d:%d}일",
        shipment.project.name,
        ", ".join(labels),
        f"{len(statements)}대",
        f"{qty}부재",
        f"({area:.2f}헤베)",
        "거래명세서",
    ]
    if shipment.plant_id:
        parts.append(f"({shipment.plant.name})")
    name = " ".join(p for p in parts if p)
    name = re.sub(r"\s+", " ", _BAD_CHARS.sub("", name)).strip().rstrip(".")
    return f"{name[:150].strip()}.xlsx"


# ------------------------------------------------------------ 엑셀

# 원본 양식 B~O 열 너비. 규격(E)은 Profile 문자열이 들어가도록 넓혔다.
COL_WIDTHS = {
    "A": 0.4, "B": 4.3, "C": 9.0, "D": 6.5, "E": 10.5, "F": 5.4, "G": 5.4, "H": 5.4,
    "I": 1.8, "J": 5.3, "K": 1.8, "L": 5.3, "M": 9.3, "N": 8.0, "O": 15.0,
}
FIRST_DATA_ROW = 15
HEAD_LAST_ROW = 14
FONT = "굴림"
THIN, MEDIUM = Side(style="thin"), Side(style="medium")
LABEL_FILL = PatternFill("solid", fgColor="F2F2F2")
RECEIVER_FILL = PatternFill("solid", fgColor="FFFF99")
STACKED = Alignment(horizontal="center", vertical="center", text_rotation=255)  # 글자를 세로로 쌓기


def _put(ws, rng, value=None, size=10, bold=False, align=None, fill=None, color=None, shrink=False, wrap=False):
    first = rng.split(":")[0]
    if ":" in rng:
        ws.merge_cells(rng)
    cell = ws[first]
    if value is not None:
        cell.value = value
    cell.font = Font(name=FONT, size=size, bold=bold, color=color)
    cell.alignment = Alignment(
        horizontal=align or "center", vertical="center", shrink_to_fit=shrink, wrap_text=wrap
    )
    if fill:
        for row in ws[rng] if ":" in rng else [[cell]]:
            for c in row:
                c.fill = fill
    return cell


def _box(ws, rng, side):
    """범위 바깥 테두리를 side 선으로(안쪽 선은 그대로)."""
    rows = ws[rng]
    last_r, last_c = len(rows) - 1, len(rows[0]) - 1
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            b = cell.border
            cell.border = Border(
                left=side if c == 0 else b.left,
                right=side if c == last_c else b.right,
                top=side if r == 0 else b.top,
                bottom=side if r == last_r else b.bottom,
            )


def _grid(ws, rng):
    for row in ws[rng]:
        for cell in row:
            cell.border = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _fill_sheet(ws, st):
    h = st.header
    for col, w in COL_WIDTHS.items():
        ws.column_dimensions[col].width = w
    n_slots = max(ROWS_PER_PAGE, len(st.items) + 1)
    last_row = HEAD_LAST_ROW + n_slots
    total_row = last_row + 1
    ws.sheet_view.showGridLines = False

    # 제목과 안내 문구
    _put(ws, "B1:O2", "거 래 명 세 서", size=18, bold=True)
    notice_color = "C00000"
    _put(ws, "B8:O8", st.notice, size=11, bold=True, color=notice_color)
    for r, ht in {1: 17.25, 2: 17.25, 3: 18.75, 4: 18.75, 5: 20.25, 6: 20.25, 7: 21.0, 8: 17.25}.items():
        ws.row_dimensions[r].height = ht

    # 공급자
    _put(ws, "B3:B7", "공급자", size=11, bold=True, fill=LABEL_FILL).alignment = STACKED
    _put(ws, "C3", "공급번호", bold=True, fill=LABEL_FILL)
    _put(ws, "D3:I3", h.supply_no, size=11, bold=True)
    _put(ws, "C4:C5", "상    호", fill=LABEL_FILL)
    _put(ws, "D4:I5", h.supplier_name, size=14, bold=True, shrink=True)
    _put(ws, "C6", "주    소", fill=LABEL_FILL)
    _put(ws, "D6:I6", h.supplier_address, size=9, shrink=True)
    _put(ws, "C7", "전화번호", fill=LABEL_FILL)
    _put(ws, "D7:I7", h.supplier_phone, size=10, shrink=True)

    # 공급받는자 (상호 3줄: 매출처 / 층·구간 / 현장명)
    _put(ws, "J3:J7", "공급받는자", size=10, bold=True, fill=RECEIVER_FILL).alignment = STACKED
    _put(ws, "K3:L5", "상호", bold=True, fill=LABEL_FILL)
    customer = h.customer_name + (f" (원청 {h.contractor_name})" if h.contractor_name else "")
    _put(ws, "M3:O3", customer, size=12, bold=True, shrink=True)
    _put(ws, "M4:O4", h.section_label, size=12, bold=True, shrink=True)
    _put(ws, "M5:O5", h.site_name, size=12, bold=True, shrink=True)
    _put(ws, "K6:L6", "주소", fill=LABEL_FILL)
    _put(ws, "M6:O6", h.site_address, size=10, shrink=True)
    _put(ws, "K7:L7", "담당자", fill=LABEL_FILL)
    _put(ws, "M7:O7", h.unloading_contact, size=10, shrink=True)

    # 출하·차량 정보
    date_fmt = "%Y-%m-%d"
    _put(ws, "B9:C9", "출 하 일", fill=LABEL_FILL)
    _put(ws, "D9:H9", h.ship_date.strftime(date_fmt), size=11, bold=True)
    _put(ws, "B10:C10", "현장 도착일", fill=LABEL_FILL)
    _put(ws, "D10:H10", h.arrival_date.strftime(date_fmt) if h.arrival_date else "", size=11, bold=True)
    _put(ws, "B11:C11", "출 하 담 당", fill=LABEL_FILL)
    _put(ws, "D11:H11", h.dispatcher, size=10, shrink=True)
    _put(ws, "I9:J9", "운송회사", size=9, fill=LABEL_FILL)
    _put(ws, "K9:O9", h.carrier, shrink=True)
    _put(ws, "I10:J10", "차종", size=9, fill=LABEL_FILL)
    _put(ws, "K10:L10", h.vehicle_type, shrink=True)
    _put(ws, "M10", "차량번호", fill=LABEL_FILL)
    _put(ws, "N10:O10", h.plate_no, shrink=True)
    _put(ws, "I11:J11", "운전기사", size=9, fill=LABEL_FILL)
    _put(ws, "K11:L11", h.driver_name, shrink=True)
    _put(ws, "M11", "연 락 처", fill=LABEL_FILL)
    _put(ws, "N11:O11", h.driver_phone, shrink=True)
    for r in (9, 10, 11):
        ws.row_dimensions[r].height = 17.25
    ws.row_dimensions[12].height = 6

    # 표 머리
    for rng, text in (
        ("B13:B14", "no"), ("C13:D14", "DWG.NO"), ("E13:H13", "규격"), ("I13:K14", "길이"),
        ("L13:L14", "수량"), ("M13:M14", "면적(㎡)"), ("N13:N14", "위치"), ("O13:O14", "Location"),
    ):
        _put(ws, rng, text, bold=True, fill=LABEL_FILL)
    for col, text in (("E", "규격"), ("F", "하판"), ("G", "측판1"), ("H", "측판2")):
        _put(ws, f"{col}14", text, size=10, bold=True, fill=LABEL_FILL)
    for r in (13, 14):
        ws.row_dimensions[r].height = 17.25

    # 부재 행
    end_slot = len(st.items)  # 이하여백은 첫 빈 칸
    for i in range(n_slots):
        r = FIRST_DATA_ROW + i
        ws.row_dimensions[r].height = 17.25
        _put(ws, f"C{r}:D{r}", None, align="left", shrink=True)
        _put(ws, f"I{r}:K{r}", None)
        for col in "BEFGHLMNO":
            _put(ws, f"{col}{r}")
        if i < len(st.items):
            it = st.items[i]
            ws[f"B{r}"] = it.no
            ws[f"C{r}"] = it.ass_mark
            ws[f"E{r}"].value = it.profile
            ws[f"E{r}"].font = Font(name=FONT, size=9)
            ws[f"E{r}"].alignment = Alignment(horizontal="center", vertical="center", shrink_to_fit=True)
            ws[f"F{r}"] = it.bottom if it.bottom != "" else None
            ws[f"G{r}"] = it.side1 if it.side1 != "" else None
            ws[f"H{r}"] = it.side2 if it.side2 != "" else None
            ws[f"I{r}"] = float(it.length) if it.length else None
            ws[f"I{r}"].number_format = "#,##0"
            ws[f"L{r}"] = it.quantity
            ws[f"M{r}"] = float(it.area)
            ws[f"M{r}"].number_format = "0.00"
            ws[f"N{r}"] = it.kind
            ws[f"N{r}"].font = Font(name=FONT, size=9)
            ws[f"O{r}"] = it.location
            ws[f"O{r}"].alignment = Alignment(horizontal="center", vertical="center", shrink_to_fit=True)
        elif i == end_slot:
            ws[f"I{r}"] = END_MARK

    # 합계 (금액 칸 없음)
    _put(ws, f"B{total_row}:K{total_row}", "계", size=11, bold=True, align="right")
    _put(ws, f"L{total_row}", f"=SUM(L{FIRST_DATA_ROW}:L{last_row})", size=11, bold=True)
    _put(ws, f"M{total_row}", f"=SUM(M{FIRST_DATA_ROW}:M{last_row})", size=11, bold=True)
    ws[f"M{total_row}"].number_format = "0.00"
    _put(ws, f"N{total_row}:O{total_row}")
    ws.row_dimensions[total_row].height = 17.25

    # 얇은 격자 위에 굵은 바깥선
    _grid(ws, f"B1:O{total_row}")
    _box(ws, "B1:O2", MEDIUM)
    _box(ws, "B3:O7", MEDIUM)
    _box(ws, "B9:O11", MEDIUM)
    _box(ws, f"B13:O14", MEDIUM)
    _box(ws, f"B1:O{total_row}", MEDIUM)
    _box(ws, f"B{total_row}:O{total_row}", MEDIUM)

    # 인쇄: A4 세로, 폭 한 장 맞춤, 머리(1~14행) 반복, 31행마다 쪽 나눔
    ws.print_area = f"A1:O{total_row}"
    ws.print_title_rows = f"1:{HEAD_LAST_ROW}"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "portrait"
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.3
    ws.page_margins.top = ws.page_margins.bottom = 0.3
    ws.page_margins.header = ws.page_margins.footer = 0.1
    ws.oddFooter.center.text = "쪽 &P/&N"
    for k in range(1, -(-len(st.items) // ROWS_PER_PAGE)):
        ws.row_breaks.append(Break(id=HEAD_LAST_ROW + k * ROWS_PER_PAGE))


def build_workbook(statements):
    """차량당 시트 1개(시트명 '1호차' 식). 차량이 없으면 빈 통합 문서가 되므로 호출 쪽에서 막는다."""
    wb = Workbook()
    wb.remove(wb.active)
    for st in statements:
        ws = wb.create_sheet(st.sheet_name)
        _fill_sheet(ws, st)
    return wb


def workbook_bytes(statements):
    buf = BytesIO()
    build_workbook(statements).save(buf)
    return buf.getvalue()
