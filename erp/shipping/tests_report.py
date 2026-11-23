from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from unittest import mock
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import load_workbook

from core.models import BomItem, Floor, Plant, Project, Section, Shipment, ShipmentLine, ShipmentTruck
from core.tests import make_project as _make_project

User = get_user_model()
TODAY = date(2026, 3, 18)
D = Decimal


def make_project(name):
    return _make_project(name=name)


def make_section(project, name="A구간", ship=date(2026, 3, 10), floor=None, **kw):
    return Section.objects.create(project=project, name=name, planned_ship_date=ship, floor=floor, **kw)


def make_item(section, ass="B1", qty=10, unit_area="2.00", profile="NB400X450", **kw):
    item = BomItem.objects.create(
        section=section, ass_mark=ass, profile_text=profile, length_mm=D("5640"), quantity=qty, **kw
    )
    BomItem.objects.filter(pk=item.pk).update(unit_area=D(unit_area) if unit_area is not None else None)
    item.refresh_from_db()
    return item


def make_shipment(project, ship_date, status=Shipment.Status.SHIPPED, plant=None):
    return Shipment.objects.create(project=project, ship_date=ship_date, status=status, plant=plant)


def add_truck(shipment, seq=1, supply_no=None):
    return ShipmentTruck.objects.create(shipment=shipment, seq=seq, supply_no=supply_no)


def add_line(truck, item, qty, area="auto"):
    if area == "auto":
        area = item.unit_area * qty if item.unit_area is not None else None
    return ShipmentLine.objects.create(truck=truck, bom_item=item, quantity=qty, area=area)


class ReportBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="pw")
        self.client.force_login(self.user)
        p = mock.patch("shipping.views_report._today", return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)

        self.plant_a = Plant.objects.create(name="가공장")
        self.plant_b = Plant.objects.create(name="나공장")
        self.p1 = make_project("현장1")
        self.p2 = make_project("현장2")
        self.s1 = make_section(self.p1, "A구간", floor=Floor.objects.create(name="3F"))
        self.s2 = make_section(self.p2, "B구간", ship=date(2026, 4, 1))
        self.b1 = make_item(self.s1, "B1", qty=20)
        self.b2 = make_item(self.s1, "B2", qty=5, profile="UNKNOWN", unit_area=None)
        self.b3 = make_item(self.s2, "C1", qty=10, unit_area="1.50")

        # Counted: two shipped shipments inside the default period.
        self.sh1 = make_shipment(self.p1, date(2026, 1, 15), plant=self.plant_a)
        t1 = add_truck(self.sh1, 1, "NEO-2026-01-15-01")
        t2 = add_truck(self.sh1, 2, "NEO-2026-01-15-02")
        add_line(t1, self.b1, 4)  # 8.00
        add_line(t2, self.b1, 2)  # 4.00
        self.sh2 = make_shipment(self.p2, date(2026, 2, 10))  # no plant
        add_line(add_truck(self.sh2, 1, "NEO-2026-02-10-01"), self.b3, 3)  # 4.50
        # Not counted: planned, canceled, and one before the default period.
        self.planned = make_shipment(self.p1, date(2026, 2, 12), Shipment.Status.PLANNED, self.plant_a)
        add_line(add_truck(self.planned), self.b1, 1)
        self.canceled = make_shipment(self.p1, date(2026, 2, 13), Shipment.Status.CANCELED, self.plant_a)
        add_line(add_truck(self.canceled), self.b1, 1)
        self.old = make_shipment(self.p1, date(2025, 12, 30), plant=self.plant_a)
        add_line(add_truck(self.old, 1, "NEO-2025-12-30-01"), self.b1, 5)  # 10.00


class AccessTests(TestCase):
    def test_anonymous_redirected(self):
        for name in ("performance", "lines", "lines_xlsx", "unshipped"):
            r = self.client.get(reverse(f"shipping:{name}"))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])


class PerformanceTests(ReportBase):
    def get(self, **params):
        r = self.client.get(reverse("shipping:performance"), params)
        self.assertEqual(r.status_code, 200)
        return r

    def test_default_period_is_this_year_to_today(self):
        r = self.get()
        self.assertEqual(r.context["start"], date(2026, 1, 1))
        self.assertEqual(r.context["end"], TODAY)
        self.assertEqual(r.context["axis"], "month")
        self.assertContains(r, "출고 실적")
        for label in ("출고 실적", "부재별 내역", "미출고 부재"):
            self.assertContains(r, label)
        self.assertContains(r, reverse("shipping:list"))

    def test_month_axis_values_and_totals(self):
        r = self.get()
        rows = {row["label"]: row for row in r.context["rows"]}
        self.assertEqual(list(rows), ["2026-01", "2026-02"])  # planned/canceled add no month, old one is out
        jan, feb = rows["2026-01"], rows["2026-02"]
        self.assertEqual((jan["shipments"], jan["trucks"], jan["qty"], jan["area"]), (1, 2, 6, D("12.00")))
        self.assertEqual((feb["shipments"], feb["trucks"], feb["qty"], feb["area"]), (1, 1, 3, D("4.50")))
        t = r.context["totals"]
        self.assertEqual((t["shipments"], t["trucks"], t["qty"], t["area"]), (2, 3, 9, D("16.50")))
        self.assertContains(r, "합계")

    def test_project_axis(self):
        r = self.get(axis="project")
        rows = r.context["rows"]
        self.assertEqual([row["label"] for row in rows], [f"{self.p1.code} 현장1", f"{self.p2.code} 현장2"])
        self.assertEqual([row["qty"] for row in rows], [6, 3])
        self.assertIn(f"project={self.p1.pk}", rows[0]["url"])
        self.assertIn("start=2026-01-01", rows[0]["url"])

    def test_plant_axis_unassigned_last(self):
        r = self.get(axis="plant")
        rows = r.context["rows"]
        self.assertEqual([row["label"] for row in rows], ["가공장", "(미지정)"])
        self.assertEqual([row["area"] for row in rows], [D("12.00"), D("4.50")])
        self.assertIn(f"plant={self.plant_a.pk}", rows[0]["url"])
        self.assertIn("plant=none", rows[1]["url"])

    def test_period_filter(self):
        r = self.get(start="2025-12-01", end="2026-01-31")
        t = r.context["totals"]
        self.assertEqual((t["shipments"], t["qty"], t["area"]), (2, 11, D("22.00")))
        self.assertEqual([row["label"] for row in r.context["rows"]], ["2025-12", "2026-01"])
        r = self.get(start="2026-02-01", end="2026-02-28")
        self.assertEqual(r.context["totals"]["shipments"], 1)

    def test_month_link_is_clamped_to_period(self):
        r = self.get(start="2026-01-20", end="2026-02-05")
        self.assertEqual(r.context["rows"], [])
        r = self.get(start="2026-01-10", end="2026-02-20")
        jan = r.context["rows"][0]
        self.assertEqual(jan["link"], {"start": "2026-01-10", "end": "2026-01-31"})

    def test_invalid_input_falls_back(self):
        r = self.get(start="garbage")
        self.assertEqual(r.context["start"], date(2026, 1, 1))
        self.assertContains(r, "올바른 날짜를 입력하세요")
        r = self.get(start="2026-03-01", end="2026-01-01")
        self.assertContains(r, "종료일은 시작일 이후여야 합니다")
        r = self.get(axis="bogus")
        self.assertContains(r, "기본 조건으로 조회했습니다")

    def test_empty(self):
        r = self.get(start="2024-01-01", end="2024-01-31")
        self.assertContains(r, "출하된 실적이 없습니다")
        self.assertEqual(r.context["totals"]["area"], D("0"))

    def test_query_count_constant(self):
        with CaptureQueriesContext(connection) as small:
            self.get(axis="project")
        p3 = make_project("현장3")
        item = make_item(make_section(p3, "C구간"), "Z1")
        for i in range(4):
            sh = make_shipment(make_project(f"추가{i}"), date(2026, 3, 1))
            add_line(add_truck(sh), item, 1)
        with CaptureQueriesContext(connection) as big:
            self.get(axis="project")
        self.assertEqual(len(small), len(big))


class LinesTests(ReportBase):
    def get(self, **params):
        r = self.client.get(reverse("shipping:lines"), params)
        self.assertEqual(r.status_code, 200)
        return r

    def test_default_lists_shipped_only(self):
        r = self.get()
        lines = list(r.context["page"])
        self.assertEqual(len(lines), 3)  # b1 on two trucks + b3; planned, canceled and old ones are out
        self.assertEqual({l.truck.shipment_id for l in lines}, {self.sh1.pk, self.sh2.pk})

    def test_totals_cover_whole_filter(self):
        r = self.get()
        self.assertEqual(r.context["totals"]["qty"], 9)
        self.assertEqual(r.context["totals"]["area"], D("16.50"))
        self.assertContains(r, "합계")

    def test_columns_and_links(self):
        r = self.get(project=self.p1.pk)
        self.assertContains(r, "Ass Mark")
        self.assertContains(r, "BOM 등록일")
        self.assertContains(r, "3F A구간")
        self.assertContains(r, "NEO-2026-01-15-01")
        self.assertContains(r, reverse("shipping:detail", args=[self.sh1.pk]))
        self.assertContains(r, "가공장")
        r = self.get(project=self.p2.pk)
        self.assertContains(r, "(미지정)")

    def test_filters(self):
        self.assertEqual(len(self.get(project=self.p2.pk).context["page"].object_list), 1)
        self.assertEqual(len(self.get(section=self.s1.pk).context["page"].object_list), 2)
        self.assertEqual(len(self.get(plant=self.plant_a.pk).context["page"].object_list), 2)
        self.assertEqual(len(self.get(plant=self.plant_b.pk).context["page"].object_list), 0)
        self.assertEqual(len(self.get(plant="none").context["page"].object_list), 1)
        r = self.get(start="2026-02-01", end="2026-02-28")
        self.assertEqual(len(r.context["page"].object_list), 1)
        self.assertEqual(r.context["totals"]["qty"], 3)

    def test_invalid_filter_falls_back(self):
        r = self.get(project="99999")
        self.assertContains(r, "선택할 수 없는 프로젝트입니다")
        self.assertEqual(len(r.context["page"].object_list), 3)

    def test_pagination_keeps_filters_and_total(self):
        sh = make_shipment(self.p2, date(2026, 3, 2), plant=self.plant_b)
        truck = add_truck(sh, 1, "NEO-2026-03-02-01")
        for i in range(55):
            add_line(truck, make_item(self.s2, f"P{i:02d}", qty=2, unit_area="1.00"), 1)
        r = self.get(plant=self.plant_b.pk)
        self.assertEqual(len(r.context["page"].object_list), 50)
        self.assertEqual(r.context["totals"]["qty"], 55)
        self.assertContains(r, f"plant={self.plant_b.pk}&amp;page=2")
        r2 = self.get(plant=self.plant_b.pk, page=2)
        self.assertEqual(len(r2.context["page"].object_list), 5)

    def test_xlsx_link_keeps_filters(self):
        r = self.get(project=self.p1.pk)
        self.assertIn(f"project={self.p1.pk}", r.context["xlsx_url"])
        self.assertIn("start=2026-01-01", r.context["xlsx_url"])

    def test_query_count_constant(self):
        with CaptureQueriesContext(connection) as small:
            self.get()
        truck = add_truck(make_shipment(self.p1, date(2026, 3, 3), plant=self.plant_b), 1, "NEO-2026-03-03-01")
        for i in range(8):
            add_line(truck, make_item(self.s1, f"Q{i}", qty=3), 1)
        with CaptureQueriesContext(connection) as big:
            self.get()
        self.assertEqual(len(small), len(big))


class LinesXlsxTests(ReportBase):
    def sheet(self, **params):
        r = self.client.get(reverse("shipping:lines_xlsx"), params)
        self.assertEqual(r.status_code, 200)
        return r, load_workbook(BytesIO(r.content)).active

    def test_rows_columns_and_total(self):
        r, ws = self.sheet()
        self.assertIn("spreadsheetml.sheet", r["Content-Type"])
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(len(rows), 1 + 3 + 1)  # header + 3 shipped lines + total
        self.assertEqual(
            list(rows[0]),
            ["출고일", "프로젝트", "층·구간", "Ass Mark", "Profile", "길이(mm)", "수량", "면적(㎡)", "보 구분",
             "Location", "BOM 등록일", "제작장", "공급번호"],
        )
        self.assertEqual(ws.freeze_panes, "A2")
        total = rows[-1]
        self.assertEqual(total[0], "합계")
        self.assertEqual(total[6], 9)
        self.assertAlmostEqual(total[7], 16.5)
        body = rows[1:-1]
        self.assertEqual(sum(r[6] for r in body), 9)
        self.assertAlmostEqual(sum(r[7] for r in body), 16.5)
        self.assertIsInstance(body[0][6], int)
        self.assertIsInstance(body[0][7], (int, float))
        self.assertIsInstance(body[0][5], (int, float))
        self.assertEqual(body[0][0].date(), date(2026, 1, 15))
        self.assertEqual(body[0][2], "3F A구간")
        self.assertEqual(body[-1][11], "(미지정)")

    def test_filter_and_filename(self):
        r, ws = self.sheet(project=self.p2.pk, start="2026-01-01", end="2026-03-18")
        self.assertEqual(ws.max_row, 3)
        name = "출고실적_2026-01-01_2026-03-18.xlsx"
        self.assertIn(f"filename*=UTF-8''{quote(name)}", r["Content-Disposition"])
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))

    def test_default_filename_uses_default_period(self):
        r, _ = self.sheet()
        self.assertIn(quote("출고실적_2026-01-01_2026-03-18.xlsx"), r["Content-Disposition"])

    def test_invalid_filter_redirects_to_screen(self):
        r = self.client.get(reverse("shipping:lines_xlsx"), {"project": "99999"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("shipping:lines"), r["Location"])

    def test_empty_has_only_header_and_total(self):
        _, ws = self.sheet(start="2024-01-01", end="2024-01-02")
        self.assertEqual(ws.max_row, 2)
        self.assertEqual(ws.cell(row=2, column=7).value, 0)


class UnshippedTests(ReportBase):
    def setUp(self):
        super().setUp()
        # b1 (qty 20): shipped 6 + 5(old) = 11 in SHIPPED, planned 1, canceled 1  -> remaining 20 - 11 - 1 = 8
        # b2 (qty 5, no area) untouched -> remaining 5.  Section s1 due 2026-03-10 -> overdue by 8 days.
        # Section s3: due 2026-02-01 (45 days overdue), confirmed date 2026-03-25 -> due = confirmed, not overdue.
        self.s3 = make_section(self.p2, "C구간", ship=date(2026, 2, 1), confirmed_ship_date=date(2026, 3, 25))
        make_item(self.s3, "D1", qty=7, unit_area="3.00")
        # Fully shipped section: excluded.
        self.s4 = make_section(self.p2, "D구간", ship=date(2026, 1, 5))
        full = make_item(self.s4, "E1", qty=2)
        add_line(add_truck(make_shipment(self.p2, date(2026, 3, 1)), 1, "NEO-2026-03-01-01"), full, 2)
        # Section far in the past, to check ordering (overdue first, oldest first).
        self.s5 = make_section(self.p1, "E구간", ship=date(2026, 1, 20))
        make_item(self.s5, "F1", qty=4, unit_area="1.00")

    def get(self, **params):
        r = self.client.get(reverse("shipping:unshipped"), params)
        self.assertEqual(r.status_code, 200)
        return r

    def rows(self, **params):
        return {row["section"].pk: row for row in self.get(**params).context["rows"]}

    def test_order_overdue_first_then_date(self):
        order = [row["section"].pk for row in self.get().context["rows"]]
        # overdue: s5 (1/20), s1 (3/10); then s3 (confirmed 3/25), s2 (4/1)
        self.assertEqual(order, [self.s5.pk, self.s1.pk, self.s3.pk, self.s2.pk])

    def test_quantities_and_area(self):
        row = self.rows()[self.s1.pk]
        self.assertEqual(row["bom_qty"], 25)  # 20 + 5
        self.assertEqual(row["shipped_qty"], 11)  # SHIPPED only (planned and canceled left out)
        self.assertEqual(row["planned_qty"], 1)
        self.assertEqual(row["remaining_qty"], 13)  # 25 - 11 - 1 planned (canceled does not count)
        self.assertEqual(row["remaining_area"], D("16.00"))  # 8 x 2.00; B2 has no area
        self.assertEqual(row["area_missing"], 1)
        self.assertEqual([it.ass_mark for it in row["items"]], ["B1", "B2"])
        self.assertEqual([it.remaining for it in row["items"]], [8, 5])

    def test_overdue_marking(self):
        rows = self.rows()
        self.assertTrue(rows[self.s1.pk]["overdue"])
        self.assertEqual(rows[self.s1.pk]["days_late"], 8)
        self.assertFalse(rows[self.s3.pk]["overdue"])  # confirmed date wins over the older planned date
        self.assertEqual(rows[self.s3.pk]["due"], date(2026, 3, 25))
        r = self.get()
        self.assertContains(r, "지연 8일")
        self.assertContains(r, "지연 57일")
        self.assertContains(r, "면적 미계산 1건")

    def test_completed_sections_excluded(self):
        self.assertNotIn(self.s4.pk, self.rows())

    def test_filters(self):
        rows = self.rows(overdue_only="on")
        self.assertEqual(set(rows), {self.s1.pk, self.s5.pk})
        rows = self.rows(project=self.p2.pk)
        self.assertEqual(set(rows), {self.s2.pk, self.s3.pk})

    def test_links_and_explanation(self):
        r = self.get()
        self.assertContains(r, f"{reverse('shipping:create')}?project={self.p1.pk}")
        self.assertContains(r, "<details")
        self.assertContains(r, "출하 수량")
        self.assertContains(r, "예정 출하")

    def test_closed_projects_left_out(self):
        Project.objects.filter(pk=self.p2.pk).update(status=Project.Status.DONE)
        self.assertEqual(set(self.rows()), {self.s1.pk, self.s5.pk})

    def test_empty(self):
        r = self.get(project=self.p1.pk, overdue_only="on")
        self.assertEqual(len(r.context["rows"]), 2)
        Section.objects.filter(project=self.p1).update(planned_ship_date=TODAY + timedelta(days=5))
        r = self.get(project=self.p1.pk, overdue_only="on")
        self.assertContains(r, "미출고 부재가 없습니다")

    def test_query_count_constant(self):
        with CaptureQueriesContext(connection) as small:
            self.get()
        for i in range(4):
            sec = make_section(self.p1, f"추가{i}", ship=date(2026, 2, 1))
            for j in range(3):
                make_item(sec, f"X{j}", qty=3)
        with CaptureQueriesContext(connection) as big:
            self.get()
        self.assertEqual(len(small), len(big))
