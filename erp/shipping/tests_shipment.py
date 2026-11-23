from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import (
    AuditLog,
    BomItem,
    Carrier,
    Floor,
    Partner,
    Plant,
    Project,
    ProjectType,
    Section,
    Shipment,
    ShipmentLine,
    ShipmentTruck,
    SiteProfile,
    VehicleType,
)
from core.services import area, shipping

User = get_user_model()


def msgs(response):
    return [str(m) for m in response.context["messages"]]


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("tester", password="pw")
        cls.customer = Partner.objects.create(name="테스트건설", is_customer=True)
        cls.ptype = ProjectType.objects.first()
        cls.floor = Floor.objects.create(name="3F")
        cls.plant = Plant.objects.create(name="가제작장", site="본사 공장")
        cls.project = cls.make_project("테스트 현장")
        cls.sec_a = Section.objects.create(
            project=cls.project, name="A구간", floor=cls.floor, planned_ship_date=date(2026, 4, 1)
        )
        cls.sec_b = Section.objects.create(project=cls.project, name="B구간", planned_ship_date=date(2026, 4, 10))
        cls.item1 = cls.make_item(cls.sec_a, "B1", qty=10)
        cls.item2 = cls.make_item(cls.sec_a, "B2", qty=4, length=3000)
        cls.item3 = cls.make_item(cls.sec_b, "G1", qty=6, profile="NB300X400")
        cls.item_noarea = cls.make_item(cls.sec_b, "X9", qty=2, profile="알수없음")

    @classmethod
    def make_project(cls, name):
        return Project.objects.create(
            name=name, project_type=cls.ptype, customer=cls.customer, contract_date=date(2026, 3, 5)
        )

    @staticmethod
    def make_item(section, ass, qty=10, profile="NB400X450", length=5640):
        item = BomItem.objects.create(
            section=section, ass_mark=ass, profile_text=profile, length_mm=Decimal(length), quantity=qty
        )
        return area.fill_bom_item(item)

    def setUp(self):
        self.client.force_login(self.user)

    def make_shipment(self, project=None, ship_date=date(2026, 4, 1), trucks=1, **kw):
        s = Shipment.objects.create(project=project or self.project, ship_date=ship_date, **kw)
        ts = [ShipmentTruck.objects.create(shipment=s, seq=i + 1) for i in range(trucks)]
        return s, ts

    def load_url(self, s, query=""):
        return reverse("shipping:load", kwargs={"pk": s.pk}) + query

    def post_load(self, s, data, query=""):
        return self.client.post(self.load_url(s, query), data, follow=True)


class AccessTests(Base):
    def test_anonymous_redirects_to_login(self):
        s, (t,) = self.make_shipment()
        urls = [
            reverse("shipping:list"),
            reverse("shipping:create"),
            reverse("shipping:detail", kwargs={"pk": s.pk}),
            reverse("shipping:edit", kwargs={"pk": s.pk}),
            reverse("shipping:load", kwargs={"pk": s.pk}),
            reverse("shipping:confirm", kwargs={"pk": s.pk}),
            reverse("shipping:cancel", kwargs={"pk": s.pk}),
            reverse("shipping:truck_edit", kwargs={"pk": t.pk}),
        ]
        self.client.logout()
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r["Location"], url)

    def test_screens_ok_when_planned(self):
        s, (t,) = self.make_shipment()
        for name in ("list", "create", "detail", "edit", "load", "confirm", "cancel"):
            url = reverse(f"shipping:{name}") if name in ("list", "create") else reverse(f"shipping:{name}", kwargs={"pk": s.pk})
            self.assertEqual(self.client.get(url).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("shipping:truck_edit", kwargs={"pk": t.pk})).status_code, 200)

    def test_unknown_shipment_404(self):
        self.assertEqual(self.client.get(reverse("shipping:detail", kwargs={"pk": 9999})).status_code, 404)


class ListTests(Base):
    def setUp(self):
        super().setUp()
        self.other = self.make_project("다른 현장")
        self.s1, _ = self.make_shipment(ship_date=date(2026, 4, 1), plant=self.plant)
        self.s2, _ = self.make_shipment(project=self.other, ship_date=date(2026, 5, 1), status=Shipment.Status.SHIPPED)
        self.s3, _ = self.make_shipment(ship_date=date(2026, 6, 1), status=Shipment.Status.CANCELED)

    def ids(self, query):
        r = self.client.get(reverse("shipping:list") + query)
        self.assertEqual(r.status_code, 200)
        return {s.pk for s in r.context["page"]}

    def test_no_filter_lists_all(self):
        self.assertEqual(self.ids(""), {self.s1.pk, self.s2.pk, self.s3.pk})

    def test_filter_status_project_plant_dates(self):
        self.assertEqual(self.ids("?status=SHIPPED"), {self.s2.pk})
        self.assertEqual(self.ids(f"?project={self.other.pk}"), {self.s2.pk})
        self.assertEqual(self.ids(f"?plant={self.plant.pk}"), {self.s1.pk})
        self.assertEqual(self.ids("?from=2026-04-15&to=2026-05-15"), {self.s2.pk})
        self.assertEqual(self.ids("?from=2026-05-01"), {self.s2.pk, self.s3.pk})
        self.assertEqual(self.ids("?to=2026-04-30"), {self.s1.pk})

    def test_bad_filter_values_are_ignored(self):
        self.assertEqual(self.ids("?from=abc&project=x&status=ZZ"), {self.s1.pk, self.s2.pk, self.s3.pk})

    def test_row_shows_title_status_text_and_register_button(self):
        r = self.client.get(reverse("shipping:list"))
        self.assertContains(r, self.s1.title)
        for word in ("예정", "출하", "취소"):
            self.assertContains(r, word)
        self.assertContains(r, reverse("shipping:create"))

    def test_pagination(self):
        for i in range(25):
            Shipment.objects.create(project=self.project, ship_date=date(2026, 7, 1))
        r = self.client.get(reverse("shipping:list"))
        self.assertEqual(len(r.context["page"]), 20)
        self.assertTrue(r.context["page"].has_next())

    def test_query_count_does_not_grow_with_rows(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("shipping:list"))
            return len(ctx)

        _, (t0,) = self.make_shipment(ship_date=date(2026, 7, 30), plant=self.plant)
        ShipmentLine.objects.create(truck=t0, bom_item=self.item1, quantity=1)
        before = count()
        for i in range(8):
            s, (t,) = self.make_shipment(ship_date=date(2026, 8, 1 + i), plant=self.plant)
            ShipmentLine.objects.create(truck=t, bom_item=self.item1, quantity=1)
        self.assertEqual(count(), before)


class CreateEditTests(Base):
    def test_create_success_goes_to_load(self):
        r = self.client.post(
            reverse("shipping:create"),
            {"project": self.project.pk, "ship_date": "2026-04-01", "plant": self.plant.pk, "dispatcher": "담당자1"},
        )
        s = Shipment.objects.get()
        self.assertRedirects(r, reverse("shipping:load", kwargs={"pk": s.pk}))
        self.assertEqual((s.status, s.dispatcher, s.created_by), (Shipment.Status.PLANNED, "담당자1", self.user))
        self.assertTrue(AuditLog.objects.filter(model="core.Shipment", action="create").exists())

    def test_create_errors_next_to_fields(self):
        r = self.client.post(reverse("shipping:create"), {"ship_date": "", "arrival_date": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("project", r.context["form"].errors)
        self.assertIn("ship_date", r.context["form"].errors)
        r = self.client.post(
            reverse("shipping:create"), {"project": self.project.pk, "ship_date": "2026-04-10", "arrival_date": "2026-04-01"}
        )
        self.assertIn("arrival_date", r.context["form"].errors)
        self.assertEqual(Shipment.objects.count(), 0)

    def test_default_dispatcher_is_last_one_of_this_user(self):
        other = User.objects.create_user("other", password="pw")
        Shipment.objects.create(project=self.project, ship_date=date(2026, 4, 1), dispatcher="남의담당", created_by=other)
        r = self.client.get(reverse("shipping:create"))
        self.assertEqual(r.context["form"].initial.get("dispatcher"), None)
        Shipment.objects.create(project=self.project, ship_date=date(2026, 4, 2), dispatcher="내담당", created_by=self.user)
        Shipment.objects.create(project=self.project, ship_date=date(2026, 4, 3), dispatcher="", created_by=self.user)
        r = self.client.get(reverse("shipping:create"))
        self.assertEqual(r.context["form"].initial["dispatcher"], "내담당")

    def test_project_preselected_with_section_plant(self):
        self.sec_a.plant = self.plant
        self.sec_a.save()
        r = self.client.get(reverse("shipping:create") + f"?project={self.project.pk}")
        self.assertEqual(r.context["form"].initial["project"], self.project.pk)
        self.assertEqual(r.context["form"].initial["plant"], self.plant.pk)
        self.assertContains(r, reverse("shipping:plant_create"))  # "새로 추가"

    def test_edit_cannot_change_project(self):
        other = self.make_project("다른 현장")
        s, _ = self.make_shipment()
        r = self.client.post(
            reverse("shipping:edit", kwargs={"pk": s.pk}),
            {"project": other.pk, "ship_date": "2026-04-05", "dispatcher": "바꿈"},
        )
        self.assertRedirects(r, reverse("shipping:detail", kwargs={"pk": s.pk}))
        s.refresh_from_db()
        self.assertEqual(s.project, self.project)
        self.assertEqual((s.ship_date, s.dispatcher), (date(2026, 4, 5), "바꿈"))
        self.assertTrue(AuditLog.objects.filter(model="core.Shipment", action="update", object_id=str(s.pk)).exists())

    def test_edit_blocked_unless_planned(self):
        for status in (Shipment.Status.SHIPPED, Shipment.Status.CANCELED):
            s, _ = self.make_shipment(status=status)
            url = reverse("shipping:edit", kwargs={"pk": s.pk})
            r = self.client.get(url, follow=True)
            self.assertRedirects(r, reverse("shipping:detail", kwargs={"pk": s.pk}))
            self.assertTrue(any("예정 상태" in m for m in msgs(r)))
            self.client.post(url, {"ship_date": "2030-01-01"})
            s.refresh_from_db()
            self.assertEqual(s.ship_date, date(2026, 4, 1))


class LoadScreenTests(Base):
    def test_pool_lists_items_with_remaining_and_filters(self):
        s, _ = self.make_shipment()
        r = self.client.get(self.load_url(s))
        self.assertEqual({b.pk for b in r.context["pool"]}, {self.item1.pk, self.item2.pk, self.item3.pk, self.item_noarea.pk})
        self.assertContains(r, "규격 확인 필요")
        self.assertContains(r, "확정 때 막힙니다")
        r = self.client.get(self.load_url(s, f"?section={self.sec_b.pk}"))
        self.assertEqual({b.pk for b in r.context["pool"]}, {self.item3.pk, self.item_noarea.pk})
        r = self.client.get(self.load_url(s, "?q=b2"))
        self.assertEqual([b.pk for b in r.context["pool"]], [self.item2.pk])

    def test_fully_loaded_item_leaves_pool_and_other_project_items_never_shown(self):
        other = self.make_project("다른 현장")
        sec = Section.objects.create(project=other, name="Z", planned_ship_date=date(2026, 4, 1))
        foreign = self.make_item(sec, "FOREIGN")
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item1, 10, self.user)
        r = self.client.get(self.load_url(s))
        pks = {b.pk for b in r.context["pool"]}
        self.assertNotIn(self.item1.pk, pks)
        self.assertNotIn(foreign.pk, pks)

    def test_partial_remaining_shown(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item1, 4, self.user)
        r = self.client.get(self.load_url(s))
        item = next(b for b in r.context["pool"] if b.pk == self.item1.pk)
        self.assertEqual(item.remaining, 6)

    def test_not_planned_is_blocked(self):
        for status in (Shipment.Status.SHIPPED, Shipment.Status.CANCELED):
            s, (t,) = self.make_shipment(status=status)
            r = self.client.get(self.load_url(s), follow=True)
            self.assertRedirects(r, reverse("shipping:detail", kwargs={"pk": s.pk}))
            self.assertTrue(any("예정 상태" in m for m in msgs(r)))
            self.client.post(self.load_url(s), {"action": "add_truck"})
            self.assertEqual(s.trucks.count(), 1)

    def test_query_count_stable(self):
        s, (t1,) = self.make_shipment()

        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(self.load_url(s))
            return len(ctx)

        shipping.add_line(t1, self.item1, 1, self.user)
        before = count()
        t2 = shipping.add_truck(s, self.user)
        shipping.add_line(t1, self.item2, 1, self.user)
        shipping.add_line(t2, self.item3, 1, self.user)
        shipping.add_line(t2, self.item_noarea, 1, self.user)
        self.assertEqual(count(), before)


class LoadActionTests(Base):
    def test_add_lines_creates_first_truck_automatically(self):
        s, _ = self.make_shipment(trucks=0)
        r = self.post_load(
            s,
            {
                "action": "add_lines",
                "truck": "",
                "pick": [self.item1.pk, self.item3.pk],
                f"qty_{self.item1.pk}": "10",
                f"qty_{self.item3.pk}": "2",
            },
        )
        self.assertEqual(s.trucks.count(), 1)
        truck = s.trucks.get()
        self.assertEqual(truck.seq, 1)
        self.assertEqual({(ln.bom_item_id, ln.quantity) for ln in truck.lines.all()}, {(self.item1.pk, 10), (self.item3.pk, 2)})
        self.assertTrue(any("1호차" in m and "2건" in m for m in msgs(r)))
        # several sections in one shipment
        self.assertEqual(Shipment.objects.get(pk=s.pk).section_label, "3F A구간, B구간")

    def test_add_lines_split_quantity(self):
        s, (t,) = self.make_shipment()
        self.post_load(s, {"action": "add_lines", "truck": t.pk, "pick": [self.item1.pk], f"qty_{self.item1.pk}": "3"})
        self.assertEqual(shipping.remaining_qty(self.item1), 7)

    def test_over_remaining_message_and_partial_success(self):
        s, (t,) = self.make_shipment()
        r = self.post_load(
            s,
            {
                "action": "add_lines",
                "truck": t.pk,
                "pick": [self.item1.pk, self.item2.pk],
                f"qty_{self.item1.pk}": "11",
                f"qty_{self.item2.pk}": "4",
            },
        )
        ms = msgs(r)
        self.assertTrue(any("B1" in m and "남은 수량(10)" in m for m in ms), ms)
        self.assertEqual(list(t.lines.values_list("bom_item_id", flat=True)), [self.item2.pk])  # the OK one stayed
        self.assertTrue(any("1건" in m for m in ms))

    def test_bad_quantity_and_nothing_picked(self):
        s, (t,) = self.make_shipment()
        r = self.post_load(s, {"action": "add_lines", "truck": t.pk, "pick": [self.item1.pk], f"qty_{self.item1.pk}": "abc"})
        self.assertTrue(any("정수" in m for m in msgs(r)))
        r = self.post_load(s, {"action": "add_lines", "truck": t.pk, "pick": [self.item1.pk], f"qty_{self.item1.pk}": "0"})
        self.assertTrue(any("1 이상" in m for m in msgs(r)))
        r = self.post_load(s, {"action": "add_lines", "truck": t.pk})
        self.assertTrue(any("선택" in m for m in msgs(r)))
        self.assertEqual(ShipmentLine.objects.count(), 0)

    def test_other_project_item_is_refused(self):
        other = self.make_project("다른 현장")
        sec = Section.objects.create(project=other, name="Z", planned_ship_date=date(2026, 4, 1))
        foreign = self.make_item(sec, "FOREIGN")
        s, (t,) = self.make_shipment()
        r = self.post_load(s, {"action": "add_lines", "truck": t.pk, "pick": [foreign.pk], f"qty_{foreign.pk}": "1"})
        self.assertEqual(ShipmentLine.objects.count(), 0)
        self.assertTrue(any("없는 부재" in m for m in msgs(r)))

    def test_truck_of_another_shipment_is_404(self):
        s, _ = self.make_shipment()
        _, (other_truck,) = self.make_shipment()
        r = self.client.post(self.load_url(s), {"action": "add_lines", "truck": other_truck.pk, "pick": [self.item1.pk]})
        self.assertEqual(r.status_code, 404)

    def test_filter_kept_after_post(self):
        s, (t,) = self.make_shipment()
        url = self.load_url(s, f"?section={self.sec_a.pk}&q=B")
        r = self.client.post(url, {"action": "add_truck"})
        self.assertRedirects(r, url)

    def test_add_and_remove_truck(self):
        s, (t1,) = self.make_shipment()
        self.post_load(s, {"action": "add_truck"})
        self.assertEqual(list(s.trucks.values_list("seq", flat=True)), [1, 2])
        t2 = s.trucks.get(seq=2)
        r = self.post_load(s, {"action": "remove_truck", "truck": t2.pk})
        self.assertEqual(s.trucks.count(), 1)
        self.assertTrue(any("삭제" in m for m in msgs(r)))

    def test_cannot_remove_truck_with_lines(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item1, 1, self.user)
        r = self.post_load(s, {"action": "remove_truck", "truck": t.pk})
        self.assertTrue(ShipmentTruck.objects.filter(pk=t.pk).exists())
        self.assertTrue(any("삭제할 수 없습니다" in m for m in msgs(r)))

    def test_remove_line_restores_remaining(self):
        s, (t,) = self.make_shipment()
        line = shipping.add_line(t, self.item1, 10, self.user)
        self.assertEqual(shipping.remaining_qty(self.item1), 0)
        self.post_load(s, {"action": "remove_line", "line": line.pk})
        self.assertEqual(shipping.remaining_qty(self.item1), 10)
        self.assertFalse(ShipmentLine.objects.exists())

    def test_move_line_and_merge(self):
        s, (t1, t2) = self.make_shipment(trucks=2)
        line = shipping.add_line(t1, self.item1, 4, self.user)
        shipping.add_line(t2, self.item1, 2, self.user)
        r = self.post_load(s, {"action": "move_line", "line": line.pk, "target": t2.pk})
        self.assertTrue(any("2호차" in m for m in msgs(r)))
        self.assertEqual(t1.lines.count(), 0)
        self.assertEqual(t2.lines.get().quantity, 6)

    def test_move_to_other_shipment_truck_is_404(self):
        s, (t1,) = self.make_shipment()
        _, (foreign,) = self.make_shipment()
        line = shipping.add_line(t1, self.item1, 1, self.user)
        r = self.client.post(self.load_url(s), {"action": "move_line", "line": line.pk, "target": foreign.pk})
        self.assertEqual(r.status_code, 404)

    def test_unknown_action(self):
        s, _ = self.make_shipment()
        r = self.post_load(s, {"action": "zzz"})
        self.assertTrue(any("알 수 없는" in m for m in msgs(r)))

    def test_load_screen_shows_totals_and_unknown_area_notice(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item1, 2, self.user)  # 7.34 x 2
        shipping.add_line(t, self.item_noarea, 1, self.user)
        r = self.client.get(self.load_url(s))
        self.assertContains(r, "차량 1대 · 부재 3개 · 면적 14.68㎡")
        self.assertContains(r, "빼기")
        self.assertContains(r, "차량 추가")
        self.assertContains(r, "규격 확인 필요")


class DetailTests(Base):
    def test_planned_buttons(self):
        s, _ = self.make_shipment()
        r = self.client.get(reverse("shipping:detail", kwargs={"pk": s.pk}))
        for word in ("기본정보 수정", "상차 편성", "출하 확정", "취소"):
            self.assertContains(r, word)
        self.assertNotContains(r, reverse("shipping:statement", kwargs={"pk": s.pk}))
        self.assertContains(r, reverse("projects:detail", kwargs={"pk": self.project.pk}))

    def test_shipped_buttons_and_supply_no(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item1, 3, self.user)
        shipping.confirm(s, self.user)
        r = self.client.get(reverse("shipping:detail", kwargs={"pk": s.pk}))
        t.refresh_from_db()
        self.assertTrue(t.supply_no)
        self.assertContains(r, t.supply_no)
        self.assertContains(r, reverse("shipping:statement", kwargs={"pk": s.pk}))
        self.assertContains(r, reverse("shipping:statement_xlsx", kwargs={"pk": s.pk}))
        self.assertNotContains(r, "기본정보 수정")
        self.assertNotContains(r, "출하 확정")
        self.assertContains(r, "운송 정보 수정")

    def test_canceled_shows_reason_and_no_buttons(self):
        s, (t,) = self.make_shipment()
        shipping.cancel(s, self.user, "현장 사정")
        r = self.client.get(reverse("shipping:detail", kwargs={"pk": s.pk}))
        self.assertContains(r, "현장 사정")
        self.assertNotContains(r, "운송 정보 수정")
        self.assertNotContains(r, reverse("shipping:cancel", kwargs={"pk": s.pk}))

    def test_detail_query_count_stable(self):
        s, (t1,) = self.make_shipment()

        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("shipping:detail", kwargs={"pk": s.pk}))
            return len(ctx)

        shipping.add_line(t1, self.item1, 1, self.user)
        before = count()
        t2 = shipping.add_truck(s, self.user)
        t3 = shipping.add_truck(s, self.user)
        shipping.add_line(t2, self.item2, 1, self.user)
        shipping.add_line(t3, self.item3, 1, self.user)
        self.assertEqual(count(), before)


class ConfirmCancelTests(Base):
    def test_confirm_page_shows_problems_without_confirming(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item_noarea, 1, self.user)
        r = self.client.get(reverse("shipping:confirm", kwargs={"pk": s.pk}))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "X9")
        self.assertContains(r, "면적이 계산되지 않았습니다")
        s.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.PLANNED)

    def test_confirm_page_empty_shipment_problem(self):
        s, _ = self.make_shipment(trucks=0)
        r = self.client.get(reverse("shipping:confirm", kwargs={"pk": s.pk}))
        self.assertContains(r, "차량이 1대 이상")

    def test_confirm_post_failure_shows_service_messages(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item_noarea, 1, self.user)
        r = self.client.post(reverse("shipping:confirm", kwargs={"pk": s.pk}), follow=True)
        self.assertRedirects(r, reverse("shipping:confirm", kwargs={"pk": s.pk}))
        self.assertTrue(any("면적이 계산되지 않았습니다" in m for m in msgs(r)))
        s.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.PLANNED)

    def test_confirm_post_success(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item1, 10, self.user)
        r = self.client.post(reverse("shipping:confirm", kwargs={"pk": s.pk}), follow=True)
        self.assertRedirects(r, reverse("shipping:detail", kwargs={"pk": s.pk}))
        s.refresh_from_db()
        t.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.SHIPPED)
        self.assertEqual(t.supply_no, "NEO-2026-04-01-01")
        self.assertContains(r, "NEO-2026-04-01-01")
        # second confirm is refused
        r = self.client.get(reverse("shipping:confirm", kwargs={"pk": s.pk}), follow=True)
        self.assertTrue(any("예정 상태" in m for m in msgs(r)))

    def test_cancel_get_and_reason_required(self):
        s, _ = self.make_shipment()
        url = reverse("shipping:cancel", kwargs={"pk": s.pk})
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, {"reason": "   "})
        self.assertEqual(r.status_code, 200)
        self.assertIn("reason", r.context["form"].errors)
        s.refresh_from_db()
        self.assertEqual(s.status, Shipment.Status.PLANNED)

    def test_cancel_restores_remaining(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item1, 10, self.user)
        shipping.confirm(s, self.user)
        self.assertEqual(shipping.remaining_qty(self.item1), 0)
        r = self.client.post(reverse("shipping:cancel", kwargs={"pk": s.pk}), {"reason": "취소 테스트"}, follow=True)
        self.assertRedirects(r, reverse("shipping:detail", kwargs={"pk": s.pk}))
        s.refresh_from_db()
        self.assertEqual((s.status, s.canceled_reason), (Shipment.Status.CANCELED, "취소 테스트"))
        self.assertEqual(shipping.remaining_qty(self.item1), 10)
        # confirm and cancel are logged
        self.assertGreaterEqual(AuditLog.objects.filter(model="core.Shipment", object_id=str(s.pk)).count(), 2)
        # canceled shipment cannot be canceled again
        r = self.client.get(reverse("shipping:cancel", kwargs={"pk": s.pk}), follow=True)
        self.assertTrue(any("이미 취소" in m for m in msgs(r)))

    def test_cancel_planned_shipment(self):
        s, (t,) = self.make_shipment()
        shipping.add_line(t, self.item1, 5, self.user)
        self.client.post(reverse("shipping:cancel", kwargs={"pk": s.pk}), {"reason": "일정 변경"})
        self.assertEqual(shipping.remaining_qty(self.item1), 10)


class TruckEditTests(Base):
    def setUp(self):
        super().setUp()
        self.carrier = Carrier.objects.create(name="가운송")
        self.v5 = VehicleType.objects.create(name="테스트5톤")
        self.v25 = VehicleType.objects.create(name="테스트25톤")
        self.s, (self.t,) = self.make_shipment()
        self.url = reverse("shipping:truck_edit", kwargs={"pk": self.t.pk})
        self.data = {
            "carrier": self.carrier.pk,
            "vehicle_type": self.v25.pk,
            "plate_no": "12가3456",
            "driver_name": "기사1",
            "driver_phone": "010-1111-2222",
            "load_note": "앞쪽 적재",
        }

    def test_save_and_audit(self):
        r = self.client.post(self.url, self.data)
        self.assertRedirects(r, reverse("shipping:detail", kwargs={"pk": self.s.pk}))
        self.t.refresh_from_db()
        self.assertEqual((self.t.carrier, self.t.plate_no, self.t.driver_name), (self.carrier, "12가3456", "기사1"))
        log = AuditLog.objects.filter(model="core.ShipmentTruck", action="update").first()
        self.assertIsNotNone(log)
        self.assertIn("plate_no", log.changes)

    def test_warning_when_type_not_allowed_but_still_saved(self):
        profile = SiteProfile.objects.create(project=self.project)
        profile.allowed_vehicle_types.add(self.v5)
        r = self.client.post(self.url, self.data, follow=True)
        self.t.refresh_from_db()
        self.assertEqual(self.t.vehicle_type, self.v25)
        self.assertTrue(any("진입 가능 차량" in m and "테스트5톤" in m for m in msgs(r)), msgs(r))
        # the warning stays visible on the detail screen
        self.assertContains(r, "진입 가능 차량")

    def test_no_warning_when_allowed_or_no_profile(self):
        r = self.client.post(self.url, self.data, follow=True)
        self.assertFalse(any("진입 가능" in m for m in msgs(r)))
        profile = SiteProfile.objects.create(project=self.project)
        profile.allowed_vehicle_types.add(self.v25)
        r = self.client.post(self.url, self.data, follow=True)
        self.assertFalse(any("진입 가능" in m for m in msgs(r)))
        profile.allowed_vehicle_types.clear()  # empty list = no restriction known
        r = self.client.post(self.url, self.data, follow=True)
        self.assertFalse(any("진입 가능" in m for m in msgs(r)))

    def test_allowed_in_shipped_but_not_canceled(self):
        shipping.add_line(self.t, self.item1, 1, self.user)
        shipping.confirm(self.s, self.user)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.client.post(self.url, self.data)
        self.t.refresh_from_db()
        self.assertEqual(self.t.plate_no, "12가3456")
        shipping.cancel(self.s, self.user, "사유")
        r = self.client.get(self.url, follow=True)
        self.assertRedirects(r, reverse("shipping:detail", kwargs={"pk": self.s.pk}))
        self.client.post(self.url, {**self.data, "plate_no": "바뀜"})
        self.t.refresh_from_db()
        self.assertEqual(self.t.plate_no, "12가3456")

    def test_inactive_choices_hidden_and_add_links(self):
        VehicleType.objects.create(name="쉬는차종", is_active=False)
        r = self.client.get(self.url)
        names = [v.name for v in r.context["form"].fields["vehicle_type"].queryset]
        self.assertNotIn("쉬는차종", names)
        self.assertContains(r, reverse("shipping:carrier_create"))
        self.assertContains(r, reverse("shipping:vehicle_type_create"))
        self.assertContains(r, "새로 추가")
