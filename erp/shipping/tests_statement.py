from datetime import date
from decimal import Decimal
from io import BytesIO
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import load_workbook

from core.models import (
    BomItem,
    Carrier,
    Floor,
    Partner,
    Plant,
    Section,
    Shipment,
    ShipmentLine,
    ShipmentTruck,
    SiteProfile,
    VehicleType,
)
from core.services import shipping
from core.tests import make_project
from shipping import statement

User = get_user_model()

COMPANY = {"name": "시험상사", "address": "시험시 시험로 1", "phone": "000-000-0000", "fax": "000-000-0001"}


def add_item(section, ass, qty=5, profile="NB400X450", length=5000, **kw):
    return BomItem.objects.create(
        section=section, ass_mark=ass, profile_text=profile, length_mm=Decimal(length), quantity=qty, **kw
    )


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("tester", password="pw")
        cls.project = make_project(name="시험 현장")
        cls.contractor = Partner.objects.create(name="시험 원청", is_contractor=True)
        cls.project.contractor = cls.contractor
        cls.project.save()
        SiteProfile.objects.create(
            project=cls.project, address="시험시 현장로 9", unloading_contact_name="하차담당",
            unloading_contact_phone="111-2222",
        )
        cls.f1 = Floor.objects.create(name="1F", sort_order=1)
        cls.f3 = Floor.objects.create(name="3F", sort_order=3)
        cls.sec_a = Section.objects.create(project=cls.project, name="A구간", floor=cls.f1, planned_ship_date=date(2026, 4, 1))
        cls.sec_b = Section.objects.create(project=cls.project, name="B구간", floor=cls.f3, planned_ship_date=date(2026, 4, 1))
        cls.plant = Plant.objects.create(name="시험제작장")
        cls.carrier = Carrier.objects.create(name="시험운송")
        cls.vt = VehicleType.objects.create(name="시험차종")

    def make_shipment(self, trucks=1):
        s = Shipment.objects.create(
            project=self.project, ship_date=date(2026, 4, 1), arrival_date=date(2026, 4, 2),
            dispatcher="출하담당", plant=self.plant,
        )
        ts = [
            ShipmentTruck.objects.create(
                shipment=s, seq=i + 1, carrier=self.carrier, vehicle_type=self.vt,
                plate_no=f"11가{1000 + i}", driver_name="기사", driver_phone=f"010-0000-000{i}",
            )
            for i in range(trucks)
        ]
        return s, ts

    def load(self, s, truck, item, qty):
        return ShipmentLine.objects.create(truck=truck, bom_item=item, quantity=qty)

    def fill(self, s, truck, n):
        for i in range(n):
            self.load(s, truck, add_item(self.sec_a, f"M{i + 1:03d}", qty=1), 1)


@override_settings(COMPANY_INFO=COMPANY)
class BuildTests(Base):
    def test_rows_sorted_by_section_then_ass_mark_with_totals(self):
        s, (t,) = self.make_shipment()
        b = add_item(self.sec_b, "G1", member_kind="GIRDER", location="L3")
        a10 = add_item(self.sec_a, "B10", qty=4)
        a2 = add_item(self.sec_a, "B2", qty=4, member_kind="BEAM")
        self.load(s, t, b, 2)
        self.load(s, t, a10, 3)
        self.load(s, t, a2, 1)
        [st] = statement.build(s)
        self.assertEqual([i.ass_mark for i in st.items], ["B2", "B10", "G1"])
        self.assertEqual([i.no for i in st.items], [1, 2, 3])
        self.assertEqual(st.items[0].kind, "BEAM")
        self.assertEqual(st.items[2].kind, "GIRDER")
        self.assertEqual(st.items[2].location, "L3")
        self.assertEqual(st.items[0].profile, "NB400X450")
        self.assertEqual((st.items[0].bottom, st.items[0].side1, st.items[0].side2), (400, 450, 450))
        self.assertEqual(st.items[0].length, "5000")
        self.assertEqual(st.total_quantity, 6)
        self.assertEqual(st.total_area, Decimal("6.50") * 6)
        self.assertEqual(st.header.section_label, "1F A구간, 3F B구간")

    def test_header_values(self):
        s, (t,) = self.make_shipment()
        self.load(s, t, add_item(self.sec_a, "B1"), 1)
        [st] = statement.build(s)
        h = st.header
        self.assertEqual(h.supply_no, "(미확정)")
        self.assertEqual((h.supplier_name, h.supplier_address), ("시험상사", "시험시 시험로 1"))
        self.assertIn("000-000-0000", h.supplier_phone)
        self.assertIn("000-000-0001", h.supplier_phone)
        self.assertEqual(h.customer_name, "테스트 매출처")
        self.assertEqual(h.contractor_name, "시험 원청")
        self.assertEqual(h.site_name, "시험 현장")
        self.assertEqual(h.site_address, "시험시 현장로 9")
        self.assertEqual(h.unloading_contact, "하차담당 111-2222")
        self.assertEqual((h.carrier, h.vehicle_type, h.plate_no), ("시험운송", "시험차종", "11가1000"))
        self.assertEqual(st.sheet_name, "1호차")
        self.assertEqual(st.notice, "출하 확정 전 미리보기")

    def test_confirmed_supply_no_and_no_site_profile(self):
        s, (t,) = self.make_shipment()
        self.load(s, t, add_item(self.sec_a, "B1"), 1)
        shipping.confirm(s, self.user)
        SiteProfile.objects.filter(project=self.project).delete()
        s = statement.load_shipment(s.pk)
        [st] = statement.build(s)
        self.assertTrue(st.header.supply_no.startswith("NEO-2026-04-01-"))
        self.assertEqual(st.header.site_address, "")
        self.assertEqual(st.notice, "")

    def test_end_mark_after_last_row(self):
        s, (t,) = self.make_shipment()
        self.load(s, t, add_item(self.sec_a, "B1"), 1)
        [st] = statement.build(s)
        kinds = [r.kind for r in st.pages[0].rows]
        self.assertEqual(kinds[:2], ["item", "end"])
        self.assertEqual(len(kinds), 31)
        self.assertEqual(st.page_count, 1)

    def test_empty_truck_has_one_page_with_end_mark(self):
        s, _ = self.make_shipment()
        [st] = statement.build(s)
        self.assertEqual(st.items, [])
        self.assertEqual(st.page_count, 1)
        self.assertEqual(st.pages[0].rows[0].kind, "end")
        self.assertEqual(st.total_quantity, 0)

    def test_31_rows_one_page_32_rows_two_pages(self):
        s, (t,) = self.make_shipment()
        self.fill(s, t, 31)
        [st] = statement.build(s)
        self.assertEqual(st.page_count, 1)
        self.assertEqual(len(st.pages), 1)
        s2, (t2,) = self.make_shipment()
        self.fill(s2, t2, 32)
        [st2] = statement.build(s2)
        self.assertEqual(st2.page_count, 2)
        self.assertEqual([p.number for p in st2.pages], [1, 2])
        self.assertEqual([p.is_last for p in st2.pages], [False, True])
        self.assertEqual(sum(1 for r in st2.pages[0].rows if r.kind == "item"), 31)
        self.assertEqual(sum(1 for r in st2.pages[1].rows if r.kind == "item"), 1)
        self.assertEqual(st2.pages[1].rows[1].kind, "end")
        self.assertEqual(st2.total_quantity, 32)

    def test_truck_filter(self):
        s, (t1, t2) = self.make_shipment(trucks=2)
        self.load(s, t1, add_item(self.sec_a, "X1"), 1)
        self.load(s, t2, add_item(self.sec_a, "X2"), 1)
        self.assertEqual(len(statement.build(s)), 2)
        [st] = statement.build(s, t2)
        self.assertEqual([i.ass_mark for i in st.items], ["X2"])

    def test_query_count_does_not_grow_with_rows(self):
        s, (t,) = self.make_shipment()
        self.fill(s, t, 2)
        s = statement.load_shipment(s.pk)
        with self.assertNumQueries(2):
            statement.build(s)
        s2, (t2,) = self.make_shipment()
        self.fill(s2, t2, 40)
        s2 = statement.load_shipment(s2.pk)
        with self.assertNumQueries(2):
            statement.build(s2)

    def test_area_prefers_stored_value(self):
        s, (t,) = self.make_shipment()
        item = add_item(self.sec_a, "B1")
        ShipmentLine.objects.create(truck=t, bom_item=item, quantity=2, area=Decimal("1.50"))
        [st] = statement.build(s)
        self.assertEqual(st.items[0].area, Decimal("1.50"))


@override_settings(COMPANY_INFO=COMPANY)
class ViewTests(Base):
    def setUp(self):
        self.client.force_login(self.user)

    def url(self, s, name="shipping:statement"):
        return reverse(name, args=[s.pk])

    def test_login_required(self):
        s, _ = self.make_shipment()
        self.client.logout()
        for name in ("shipping:statement", "shipping:statement_xlsx"):
            r = self.client.get(self.url(s, name))
            self.assertEqual(r.status_code, 302)
            self.assertIn("/login", r["Location"])

    def test_screen_content(self):
        s, (t,) = self.make_shipment()
        self.load(s, t, add_item(self.sec_a, "B1", location="LOC-9"), 2)
        r = self.client.get(self.url(s))
        self.assertEqual(r.status_code, 200)
        for text in ("거 래 명 세 서", "공급번호", "(미확정)", "공급자", "공급받는자", "시험상사", "시험 현장",
                     "테스트 매출처", "시험시 현장로 9", "DWG.NO", "Location", "LOC-9", "이하여백", "쪽 1/1",
                     "인쇄", "엑셀", "출하 상세로", "차량 선택", "1호차", "shipping/statement.css"):
            self.assertContains(r, text)

    def test_notices_by_status(self):
        s, (t,) = self.make_shipment()
        self.load(s, t, add_item(self.sec_a, "B1"), 1)
        self.assertContains(self.client.get(self.url(s)), "출하 확정 전 미리보기")
        shipping.confirm(s, self.user)
        r = self.client.get(self.url(s))
        self.assertNotContains(r, "출하 확정 전 미리보기")
        self.assertNotContains(r, "취소된 출하")
        self.assertContains(r, "NEO-2026-04-01-01")
        shipping.cancel(s, self.user, "시험")
        r = self.client.get(self.url(s))
        self.assertContains(r, "취소된 출하")
        self.assertContains(r, "NEO-2026-04-01-01")  # 공급번호는 취소 뒤에도 남는다

    def test_truck_filter_and_all_trucks(self):
        s, (t1, t2) = self.make_shipment(trucks=2)
        self.load(s, t1, add_item(self.sec_a, "ONLY-ONE"), 1)
        self.load(s, t2, add_item(self.sec_a, "ONLY-TWO"), 1)
        r = self.client.get(self.url(s))
        self.assertContains(r, "ONLY-ONE")
        self.assertContains(r, "ONLY-TWO")
        r = self.client.get(self.url(s) + f"?truck={t2.pk}")
        self.assertContains(r, "ONLY-TWO")
        self.assertNotContains(r, "ONLY-ONE")
        self.assertContains(r, f"statement.xlsx?truck={t2.pk}")

    def test_two_pages_shown(self):
        s, (t,) = self.make_shipment()
        self.fill(s, t, 32)
        r = self.client.get(self.url(s))
        self.assertContains(r, "쪽 1/2")
        self.assertContains(r, "쪽 2/2")
        self.assertEqual(r.content.decode().count('<tr class="total">'), 2)

    def test_total_values_only_on_last_page(self):
        s, (t,) = self.make_shipment()
        self.fill(s, t, 32)
        html = self.client.get(self.url(s)).content.decode()
        first, second = html.split("쪽 1/2")
        self.assertNotIn("208.00", first)
        self.assertIn("208.00", second)

    def test_other_shipments_truck_is_404(self):
        s1, (t1,) = self.make_shipment()
        s2, (t2,) = self.make_shipment()
        self.assertEqual(self.client.get(self.url(s1) + f"?truck={t2.pk}").status_code, 404)
        self.assertEqual(self.client.get(self.url(s1, "shipping:statement_xlsx") + f"?truck={t2.pk}").status_code, 404)
        self.assertEqual(self.client.get(self.url(s1) + "?truck=abc").status_code, 404)
        self.assertEqual(self.client.get(self.url(s1) + "?truck=999999").status_code, 404)

    def test_unknown_shipment_404(self):
        self.assertEqual(self.client.get(reverse("shipping:statement", args=[999999])).status_code, 404)

    def test_no_money_wording(self):
        s, (t,) = self.make_shipment()
        self.load(s, t, add_item(self.sec_a, "B1"), 1)
        html = self.client.get(self.url(s)).content.decode()
        for word in ("금액", "단가", "공급가액", "부가세", "세액"):
            self.assertNotIn(word, html)
        wb = load_workbook(BytesIO(self.client.get(self.url(s, "shipping:statement_xlsx")).content))
        texts = " ".join(str(c.value) for ws in wb for row in ws.iter_rows() for c in row if c.value is not None)
        for word in ("금액", "단가", "공급가액", "부가세"):
            self.assertNotIn(word, texts)

    def test_no_trucks(self):
        s, _ = self.make_shipment(trucks=0)
        self.assertContains(self.client.get(self.url(s)), "차량이 없습니다")
        self.assertEqual(self.client.get(self.url(s, "shipping:statement_xlsx")).status_code, 302)


@override_settings(COMPANY_INFO=COMPANY)
class XlsxTests(Base):
    def setUp(self):
        self.client.force_login(self.user)

    def get_wb(self, s, query=""):
        r = self.client.get(reverse("shipping:statement_xlsx", args=[s.pk]) + query)
        self.assertEqual(r.status_code, 200)
        return r, load_workbook(BytesIO(r.content))

    def test_response_headers_and_filename(self):
        s, (t1, t2) = self.make_shipment(trucks=2)
        self.load(s, t1, add_item(self.sec_a, "B1"), 3)
        self.load(s, t2, add_item(self.sec_b, "B2"), 2)
        r, wb = self.get_wb(s)
        self.assertEqual(r["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        expected = "26년 04월 01일 시험 현장 1F A구간, 3F B구간 2대 5부재 (32.50헤베) 거래명세서 (시험제작장).xlsx"
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn("filename*=UTF-8''" + quote(expected), r["Content-Disposition"])
        self.assertEqual(wb.sheetnames, ["1호차", "2호차"])

    def test_filename_without_plant_and_bad_chars(self):
        s, (t,) = self.make_shipment()
        Shipment.objects.filter(pk=s.pk).update(plant=None)
        self.project.name = 'a/b:c*"?<>|d'
        self.project.save()
        self.load(s, t, add_item(self.sec_a, "B1"), 1)
        s = statement.load_shipment(s.pk)
        name = statement.xlsx_filename(s, statement.build(s))
        self.assertEqual(name, "26년 04월 01일 abcd 1F A구간 1대 1부재 (6.50헤베) 거래명세서.xlsx")

    def test_cell_values_and_sum_formulas(self):
        s, (t,) = self.make_shipment()
        self.load(s, t, add_item(self.sec_a, "B2", location="LOC-2", member_kind="BEAM"), 3)
        self.load(s, t, add_item(self.sec_a, "B10", length=6200, member_kind="GIRDER"), 1)
        r, wb = self.get_wb(s)
        ws = wb["1호차"]
        self.assertEqual(ws["B1"].value, "거 래 명 세 서")
        self.assertEqual(ws["D3"].value, "(미확정)")
        self.assertEqual(ws["D4"].value, "시험상사")
        self.assertEqual(ws["D6"].value, "시험시 시험로 1")
        self.assertIn("000-000-0000", ws["D7"].value)
        self.assertEqual(ws["M3"].value, "테스트 매출처 (원청 시험 원청)")
        self.assertEqual(ws["M4"].value, "1F A구간")
        self.assertEqual(ws["M5"].value, "시험 현장")
        self.assertEqual(ws["M6"].value, "시험시 현장로 9")
        self.assertEqual(ws["M7"].value, "하차담당 111-2222")
        self.assertEqual(ws["D9"].value, "2026-04-01")
        self.assertEqual(ws["D10"].value, "2026-04-02")
        self.assertEqual(ws["D11"].value, "출하담당")
        self.assertEqual(ws["K9"].value, "시험운송")
        self.assertEqual(ws["K10"].value, "시험차종")
        self.assertEqual(ws["N10"].value, "11가1000")
        self.assertEqual(ws["K11"].value, "기사")
        self.assertEqual(ws["B8"].value, "출하 확정 전 미리보기")
        row15 = [ws.cell(15, c).value for c in (2, 3, 5, 6, 7, 8, 9, 12, 13, 14, 15)]
        self.assertEqual(row15, [1, "B2", "NB400X450", 400, 450, 450, 5000, 3, 19.5, "BEAM", "LOC-2"])
        self.assertEqual(ws["C16"].value, "B10")
        self.assertEqual(ws["I16"].value, 6200)
        self.assertEqual(ws["N16"].value, "GIRDER")
        self.assertEqual(ws["I17"].value, "이하여백")
        self.assertEqual(ws["B46"].value, "계")
        self.assertEqual(ws["L46"].value, "=SUM(L15:L45)")
        self.assertEqual(ws["M46"].value, "=SUM(M15:M45)")
        self.assertIn("B13:B14", {str(m) for m in ws.merged_cells.ranges})
        self.assertEqual(ws.print_title_rows, "$1:$14")
        self.assertEqual(ws.page_setup.paperSize, 9)
        self.assertEqual(ws.page_setup.fitToWidth, 1)
        self.assertTrue(ws.sheet_properties.pageSetUpPr.fitToPage)

    def test_more_than_31_rows_grow_sheet(self):
        s, (t,) = self.make_shipment()
        self.fill(s, t, 40)
        r, wb = self.get_wb(s)
        ws = wb["1호차"]
        self.assertEqual(ws["C54"].value, "M040")
        self.assertEqual(ws["I55"].value, "이하여백")
        self.assertEqual(ws["B56"].value, "계")
        self.assertEqual(ws["L56"].value, "=SUM(L15:L55)")
        self.assertEqual(ws["M56"].value, "=SUM(M15:M55)")
        self.assertEqual(ws.print_area, "'1호차'!$A$1:$O$56")
        self.assertEqual([b.id for b in ws.row_breaks.brk], [45])

    def test_single_truck_download(self):
        s, (t1, t2) = self.make_shipment(trucks=2)
        self.load(s, t1, add_item(self.sec_a, "B1"), 1)
        self.load(s, t2, add_item(self.sec_a, "B2"), 1)
        r, wb = self.get_wb(s, f"?truck={t2.pk}")
        self.assertEqual(wb.sheetnames, ["2호차"])
        self.assertEqual(wb["2호차"]["C15"].value, "B2")
        self.assertIn(quote("1대 1부재"), r["Content-Disposition"])

    def test_confirmed_supply_no_in_sheet(self):
        s, (t,) = self.make_shipment()
        self.load(s, t, add_item(self.sec_a, "B1"), 1)
        shipping.confirm(s, self.user)
        r, wb = self.get_wb(s)
        self.assertTrue(wb["1호차"]["D3"].value.startswith("NEO-2026-04-01-"))
        self.assertIn(wb["1호차"]["B8"].value, (None, ""))
