from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.models import (
    AuditLog,
    BomItem,
    Floor,
    Plant,
    Section,
    Shipment,
    ShipmentLine,
    ShipmentTruck,
    VehicleType,
)
from core.services import area, shipping
from core.tests import make_project, make_section

User = get_user_model()


def make_item(section, ass="B1", profile="NB400X450", length=5640, qty=10, **kw):
    return BomItem.objects.create(
        section=section, ass_mark=ass, profile_text=profile, length_mm=Decimal(length), quantity=qty, **kw
    )


def make_shipment(project, ship_date=date(2026, 4, 1), trucks=1):
    s = Shipment.objects.create(project=project, ship_date=ship_date)
    return s, [ShipmentTruck.objects.create(shipment=s, seq=i + 1) for i in range(trucks)]


def messages(exc):
    return " | ".join(exc.exception.messages)


class ParseProfileTests(TestCase):
    def test_named_two_sides(self):
        self.assertEqual(area.parse_profile("NB400X450"), (400, 450, 450))
        self.assertEqual(area.parse_profile("DH400X450"), (400, 450, 450))

    def test_named_one_side(self):
        self.assertEqual(area.parse_profile("NBL400X450"), (400, 450, 0))
        self.assertEqual(area.parse_profile("DHL400X450"), (400, 450, 0))

    def test_triple(self):
        self.assertEqual(area.parse_profile("400/450/420"), (400, 450, 420))

    def test_case_space_separator(self):
        for text in ["nb400x450", " NB 400 x 450 ", "NB400*450", "NB400×450", "dhl400X450"]:
            self.assertIsNotNone(area.parse_profile(text), text)
        self.assertEqual(area.parse_profile("dhl 400 * 450"), (400, 450, 0))

    def test_unparsable(self):
        for text in ["", None, "ABC", "NB400", "400/450", "NB400X450X1", "W500"]:
            self.assertIsNone(area.parse_profile(text), text)


class AreaTests(TestCase):
    def test_unit_area_rounds_up(self):
        self.assertEqual(area.unit_area(400, 450, 450, 5640), Decimal("7.34"))  # 7.332

    def test_unit_area_exact_not_rounded(self):
        self.assertEqual(area.unit_area(500, 500, 0, 1000), Decimal("1.00"))
        self.assertEqual(area.unit_area(400, 450, 450, Decimal("5000")), Decimal("6.50"))

    def test_unit_area_float_safe(self):
        # 1300 x 2300 / 1e6 = 2.99 exactly; must not become 3.00
        self.assertEqual(area.unit_area(400, 450, 450, 2300), Decimal("2.99"))

    def test_unit_area_missing(self):
        self.assertIsNone(area.unit_area(None, None, None, 5000))
        self.assertIsNone(area.unit_area(400, 450, 450, None))

    def test_line_area(self):
        self.assertEqual(area.line_area(Decimal("7.34"), 3), Decimal("22.02"))
        self.assertIsNone(area.line_area(None, 3))

    def test_member_kind_blank(self):
        self.assertEqual(area.member_kind("B1"), "")


class FillBomItemTests(TestCase):
    def setUp(self):
        self.section = make_section()

    def test_fill_on_save(self):
        item = make_item(self.section)
        item.refresh_from_db()
        self.assertEqual((item.bottom_width, item.side1_width, item.side2_width), (400, 450, 450))
        self.assertEqual(item.unit_area, Decimal("7.34"))

    def test_fill_explicit_save(self):
        item = make_item(self.section)
        BomItem.objects.filter(pk=item.pk).update(unit_area=None)
        item.refresh_from_db()
        area.fill_bom_item(item)
        item.refresh_from_db()
        self.assertEqual(item.unit_area, Decimal("7.34"))

    def test_unparsable_keeps_manual_widths(self):
        item = make_item(self.section, profile="특수규격", bottom_width=300, side1_width=200, side2_width=0)
        item.refresh_from_db()
        self.assertEqual((item.bottom_width, item.side1_width, item.side2_width), (300, 200, 0))
        self.assertEqual(item.unit_area, Decimal("2.82"))  # 500 x 5640 = 2.82

    def test_unparsable_without_widths(self):
        item = make_item(self.section, profile="특수규격")
        self.assertIsNone(item.unit_area)

    def test_profile_change_recalculates(self):
        item = make_item(self.section)
        item.profile_text = "NBL400X450"
        item.save()
        item.refresh_from_db()
        self.assertEqual((item.bottom_width, item.side1_width, item.side2_width), (400, 450, 0))
        self.assertEqual(item.unit_area, Decimal("4.80"))  # 850 x 5640 = 4.794

    def test_member_kind_not_overwritten(self):
        item = make_item(self.section, member_kind="BEAM")
        item.refresh_from_db()
        self.assertEqual(item.member_kind, "BEAM")

    def test_bom_upload_fills_area(self):
        from bom import services as bom_services

        rows = [{"ass_mark": "A1", "profile": "NB400X450", "length": "5640", "qty": "2", "finish": "", "location": ""}]
        bom_services.save_upload(self.section, rows, "x.xlsx", bom_services.MODE_REPLACE, None)
        item = BomItem.objects.get(section=self.section)
        self.assertEqual(item.unit_area, Decimal("7.34"))

    def test_recalc_command(self):
        item = make_item(self.section)
        BomItem.objects.filter(pk=item.pk).update(unit_area=None, bottom_width=None, side1_width=None, side2_width=None)
        out = StringIO()
        call_command("recalc_bom_area", stdout=out)
        item.refresh_from_db()
        self.assertEqual(item.unit_area, Decimal("7.34"))
        self.assertIn("변경 1건", out.getvalue())


class RemainingQtyTests(TestCase):
    def setUp(self):
        self.project = make_project()
        self.section = make_section(self.project)
        self.item = make_item(self.section, qty=10)
        self.shipment, (self.truck,) = make_shipment(self.project)

    def test_planned_counts_canceled_does_not(self):
        self.assertEqual(shipping.remaining_qty(self.item), 10)
        shipping.add_line(self.truck, self.item, 4, None)
        self.assertEqual(shipping.remaining_qty(self.item), 6)
        self.assertEqual(self.item.shipped_qty, 4)
        self.assertEqual(self.item.remaining_qty, 6)
        Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.CANCELED)
        self.assertEqual(shipping.remaining_qty(self.item), 10)

    def test_annotate_remaining_no_n_plus_one(self):
        other = make_item(self.section, ass="B2", qty=5)
        shipping.add_line(self.truck, self.item, 3, None)
        with self.assertNumQueries(1):
            rows = list(shipping.annotate_remaining(BomItem.objects.filter(section=self.section)).order_by("pk"))
            self.assertEqual([r.remaining_qty for r in rows], [7, 5])
            self.assertEqual([r.shipped_qty for r in rows], [3, 0])
        self.assertEqual(other.pk, rows[1].pk)


class LoadingTests(TestCase):
    def setUp(self):
        self.project = make_project()
        self.section = make_section(self.project)
        self.item = make_item(self.section, qty=10)
        self.shipment, (self.t1,) = make_shipment(self.project)

    def test_add_line_over_remaining_rejected(self):
        with self.assertRaises(ValidationError):
            shipping.add_line(self.t1, self.item, 11, None)
        self.assertEqual(ShipmentLine.objects.count(), 0)

    def test_add_line_merges_same_truck(self):
        shipping.add_line(self.t1, self.item, 4, None)
        shipping.add_line(self.t1, self.item, 3, None)
        self.assertEqual(self.t1.lines.get().quantity, 7)
        with self.assertRaises(ValidationError):
            shipping.add_line(self.t1, self.item, 4, None)  # only 3 left

    def test_add_line_bad_quantity(self):
        for q in (0, -1, 1.5):
            with self.assertRaises(ValidationError):
                shipping.add_line(self.t1, self.item, q, None)

    def test_add_line_other_project_rejected(self):
        other = make_item(make_section(make_project(name="다른 현장")), ass="Z1")
        with self.assertRaises(ValidationError):
            shipping.add_line(self.t1, other, 1, None)

    def test_split_across_trucks(self):
        t2 = shipping.add_truck(self.shipment, None)
        self.assertEqual(t2.seq, 2)
        shipping.add_line(self.t1, self.item, 6, None)
        shipping.add_line(t2, self.item, 4, None)
        self.assertEqual(shipping.remaining_qty(self.item), 0)
        self.assertEqual(self.shipment.piece_count, 10)

    def test_move_and_remove_line(self):
        t2 = shipping.add_truck(self.shipment, None)
        line = shipping.add_line(self.t1, self.item, 6, None)
        shipping.add_line(t2, self.item, 2, None)
        merged = shipping.move_line(line, t2, None)
        self.assertEqual(merged.quantity, 8)
        self.assertEqual(self.t1.lines.count(), 0)
        shipping.remove_line(merged, None)
        self.assertEqual(shipping.remaining_qty(self.item), 10)

    def test_move_to_other_shipment_rejected(self):
        _, (foreign,) = make_shipment(self.project)
        line = shipping.add_line(self.t1, self.item, 1, None)
        with self.assertRaises(ValidationError):
            shipping.move_line(line, foreign, None)

    def test_remove_truck_only_when_planned(self):
        shipping.add_line(self.t1, self.item, 3, None)
        shipping.remove_truck(self.t1, None)
        self.assertEqual(shipping.remaining_qty(self.item), 10)
        t = shipping.add_truck(self.shipment, None)
        Shipment.objects.filter(pk=self.shipment.pk).update(status=Shipment.Status.SHIPPED)
        with self.assertRaises(ValidationError):
            shipping.remove_truck(ShipmentTruck.objects.get(pk=t.pk), None)

    def test_edit_blocked_after_confirm(self):
        shipping.add_line(self.t1, self.item, 3, None)
        shipping.confirm(self.shipment, None)
        with self.assertRaises(ValidationError):
            shipping.add_line(ShipmentTruck.objects.get(pk=self.t1.pk), self.item, 1, None)


class ConfirmTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u1", password="pw12345!")
        self.project = make_project()
        self.section = make_section(self.project)
        self.item = make_item(self.section, qty=10)

    def test_no_truck(self):
        s = Shipment.objects.create(project=self.project, ship_date=date(2026, 4, 1))
        with self.assertRaises(ValidationError) as cm:
            shipping.confirm(s, self.user)
        self.assertIn("차량", messages(cm))

    def test_empty_truck(self):
        s, _ = make_shipment(self.project)
        with self.assertRaises(ValidationError) as cm:
            shipping.confirm(s, self.user)
        self.assertIn("1호차", messages(cm))

    def test_area_missing(self):
        bad = make_item(self.section, ass="X1", profile="특수규격")
        s, (t,) = make_shipment(self.project)
        shipping.add_line(t, bad, 1, self.user)
        with self.assertRaises(ValidationError) as cm:
            shipping.confirm(s, self.user)
        self.assertIn("X1", messages(cm))
        s.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.PLANNED)

    def test_over_bom_quantity(self):
        s, (t,) = make_shipment(self.project)
        shipping.add_line(t, self.item, 10, self.user)
        BomItem.objects.filter(pk=self.item.pk).update(quantity=8)  # BOM reduced after loading
        with self.assertRaises(ValidationError) as cm:
            shipping.confirm(s, self.user)
        self.assertIn("넘습니다", messages(cm))

    def test_not_planned(self):
        s, (t,) = make_shipment(self.project)
        shipping.add_line(t, self.item, 1, self.user)
        shipping.confirm(s, self.user)
        with self.assertRaises(ValidationError):
            shipping.confirm(s, self.user)

    def test_success(self):
        s, (t,) = make_shipment(self.project, ship_date=date(2026, 4, 1))
        shipping.add_line(t, self.item, 3, self.user)
        shipping.confirm(s, self.user)
        s.refresh_from_db()
        t.refresh_from_db()
        line = t.lines.get()
        self.assertEqual(s.status, Shipment.Status.SHIPPED)
        self.assertEqual(t.supply_no, "NEO-2026-04-01-01")
        self.assertEqual(line.area, Decimal("22.02"))
        self.assertEqual(s.total_area, Decimal("22.02"))
        self.assertTrue(AuditLog.objects.filter(model="core.Shipment", object_id=str(s.pk), user=self.user).exists())

    def test_area_kept_after_bom_change(self):
        s, (t,) = make_shipment(self.project)
        shipping.add_line(t, self.item, 1, self.user)
        shipping.confirm(s, self.user)
        self.item.length_mm = Decimal("1000")
        self.item.save()
        self.assertEqual(t.lines.get().area, Decimal("7.34"))

    def test_supply_numbers_run_per_truck_across_shipments(self):
        item2 = make_item(self.section, ass="B2", qty=10)
        s1, (a, b) = make_shipment(self.project, trucks=2)
        shipping.add_line(a, self.item, 1, self.user)
        shipping.add_line(b, item2, 1, self.user)
        shipping.confirm(s1, self.user)
        s2, (c,) = make_shipment(self.project)
        shipping.add_line(c, self.item, 1, self.user)
        shipping.confirm(s2, self.user)
        s3, (d,) = make_shipment(self.project, ship_date=date(2026, 4, 2))
        shipping.add_line(d, self.item, 1, self.user)
        shipping.confirm(s3, self.user)
        nos = [ShipmentTruck.objects.get(pk=x.pk).supply_no for x in (a, b, c, d)]
        self.assertEqual(nos, ["NEO-2026-04-01-01", "NEO-2026-04-01-02", "NEO-2026-04-01-03", "NEO-2026-04-02-01"])

    def test_next_supply_no_empty(self):
        self.assertEqual(shipping.next_supply_no(date(2026, 12, 31)), "NEO-2026-12-31-01")


class CancelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u1", password="pw12345!")
        self.project = make_project()
        self.section = make_section(self.project)
        self.item = make_item(self.section, qty=10)

    def _shipped(self, qty, ship_date=date(2026, 4, 1)):
        s, (t,) = make_shipment(self.project, ship_date=ship_date)
        shipping.add_line(t, self.item, qty, self.user)
        shipping.confirm(s, self.user)
        return s, t

    def test_cancel_restores_quantity_and_keeps_number(self):
        s, t = self._shipped(4)
        self.assertEqual(shipping.remaining_qty(self.item), 6)
        shipping.cancel(s, self.user, "오등록")
        s.refresh_from_db()
        t.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.CANCELED)
        self.assertEqual(s.canceled_reason, "오등록")
        self.assertEqual(shipping.remaining_qty(self.item), 10)
        self.assertEqual(t.supply_no, "NEO-2026-04-01-01")
        s2, t2 = self._shipped(4)
        t2.refresh_from_db()
        self.assertEqual(t2.supply_no, "NEO-2026-04-01-02")  # 01 not reused

    def test_cancel_requires_reason_and_not_twice(self):
        s, _ = self._shipped(1)
        with self.assertRaises(ValidationError):
            shipping.cancel(s, self.user, "  ")
        shipping.cancel(s, self.user, "사유")
        with self.assertRaises(ValidationError):
            shipping.cancel(s, self.user, "사유")

    def test_cancel_planned(self):
        s, (t,) = make_shipment(self.project)
        shipping.add_line(t, self.item, 5, self.user)
        shipping.cancel(s, self.user, "계획 변경")
        self.assertEqual(shipping.remaining_qty(self.item), 10)

    def test_cancel_audited(self):
        s, _ = self._shipped(1)
        shipping.cancel(s, self.user, "사유")
        self.assertTrue(
            AuditLog.objects.filter(model="core.Shipment", object_id=str(s.pk), changes__reason="사유").exists()
        )


class SectionSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u1", password="pw12345!")
        self.project = make_project()
        self.section = make_section(self.project, floor=Floor.objects.create(name="3F"))
        self.item = make_item(self.section, qty=10)

    def _p900(self):
        return self.section.activities.get(activity_type__code="P900")

    def test_partial_then_complete_then_cancel(self):
        s1, (t1,) = make_shipment(self.project, ship_date=date(2026, 4, 1))
        shipping.add_line(t1, self.item, 4, self.user)
        shipping.confirm(s1, self.user)
        self.section.refresh_from_db()
        self.assertFalse(self.section.is_completed)
        self.assertIsNone(self._p900().actual_date)

        s2, (t2,) = make_shipment(self.project, ship_date=date(2026, 4, 3))
        shipping.add_line(t2, self.item, 6, self.user)
        self.assertFalse(Section.objects.get(pk=self.section.pk).is_completed)  # planned does not complete
        shipping.confirm(s2, self.user)
        self.section.refresh_from_db()
        self.assertTrue(self.section.is_completed)
        self.assertEqual(self.section.confirmed_ship_date, date(2026, 4, 3))
        self.assertEqual(self._p900().actual_date, date(2026, 4, 3))

        shipping.cancel(s2, self.user, "취소")
        self.section.refresh_from_db()
        self.assertFalse(self.section.is_completed)
        self.assertIsNone(self._p900().actual_date)

    def test_section_without_bom_untouched(self):
        empty = make_section(self.project, ship=date(2026, 5, 1))
        empty.name = "빈구간"
        empty.save()
        before = (empty.is_completed, empty.confirmed_ship_date)
        shipping.sync_section_shipping(empty)
        empty.refresh_from_db()
        self.assertEqual((empty.is_completed, empty.confirmed_ship_date), before)

    def test_multi_section_shipment(self):
        sec2 = make_section(self.project, ship=date(2026, 4, 10))
        sec2.name = "B구간"
        sec2.save()
        item2 = make_item(sec2, ass="C1", qty=2)
        s, (t1, t2) = make_shipment(self.project, trucks=2)
        shipping.add_line(t1, self.item, 10, self.user)
        shipping.add_line(t1, item2, 1, self.user)
        shipping.add_line(t2, item2, 1, self.user)
        shipping.confirm(s, self.user)
        self.section.refresh_from_db()
        sec2.refresh_from_db()
        self.assertTrue(self.section.is_completed)
        self.assertTrue(sec2.is_completed)
        s.refresh_from_db()
        self.assertEqual(s.section_label, "3F A구간, B구간")
        self.assertEqual(s.piece_count, 12)


class ShipmentPropertyTests(TestCase):
    def test_title_and_totals(self):
        project = make_project(name="예시 현장")
        section = make_section(project, floor=Floor.objects.create(name="2F"))
        item = make_item(section, qty=10)
        plant = Plant.objects.create(name="제1제작장")
        s, (t,) = make_shipment(project, ship_date=date(2026, 4, 1))
        s.plant = plant
        s.save()
        shipping.add_line(t, item, 3, None)
        self.assertEqual(s.title, "2026-04-01 예시 현장 2F A구간 1대 3부재 22.02㎡ 제1제작장")
        self.assertEqual(s.truck_count, 1)

    def test_list_queries_constant(self):
        project = make_project()
        section = make_section(project)
        item = make_item(section, qty=100)
        for i in range(3):
            s, (t,) = make_shipment(project, ship_date=date(2026, 4, 1 + i))
            shipping.add_line(t, item, 1, None)
        with self.assertNumQueries(5):
            titles = [s.title for s in shipping.prefetch_shipments(Shipment.objects.all())]
        self.assertEqual(len(titles), 3)

    def test_vehicle_type_seed(self):
        self.assertEqual(
            list(VehicleType.objects.values_list("name", flat=True)), ["5톤", "5톤축", "11톤", "25톤", "트레일러"]
        )


class ShippingSkeletonTests(TestCase):
    NAMES = [
        "list", "create", "detail", "edit", "load", "confirm", "cancel", "truck_edit",
        "plant_list", "plant_create", "plant_edit", "carrier_list", "carrier_create", "carrier_edit",
        "vehicle_type_list", "vehicle_type_create", "vehicle_type_edit",
        "statement", "statement_xlsx", "performance", "lines", "lines_xlsx", "unshipped",
    ]
    WITH_PK = {
        "detail", "edit", "load", "confirm", "cancel", "truck_edit", "plant_edit", "carrier_edit",
        "vehicle_type_edit", "statement", "statement_xlsx",
    }

    def urls(self):
        return [reverse(f"shipping:{n}", args=[1] if n in self.WITH_PK else []) for n in self.NAMES]

    def test_all_reverse(self):
        self.assertEqual(len(set(self.urls())), len(self.NAMES))

    def test_login_required(self):
        for url in self.urls():
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/accounts/login/", r["Location"])

    def test_screens_without_object_render(self):
        User.objects.create_user("u1", password="pw12345!")
        self.client.login(username="u1", password="pw12345!")
        for name in self.NAMES:
            if name in self.WITH_PK:
                continue
            r = self.client.get(reverse(f"shipping:{name}"))
            self.assertEqual(r.status_code, 200, name)
        self.assertContains(self.client.get(reverse("shipping:list")), 'class="active"')

    def test_missing_object_is_404(self):
        User.objects.create_user("u1", password="pw12345!")
        self.client.login(username="u1", password="pw12345!")
        for name in sorted(self.WITH_PK):
            r = self.client.get(reverse(f"shipping:{name}", args=[999999]))
            self.assertEqual(r.status_code, 404, name)

    def test_company_info_setting(self):
        from django.conf import settings

        self.assertEqual(set(settings.COMPANY_INFO), {"name", "address", "phone", "fax"})
