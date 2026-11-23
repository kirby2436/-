"""Sidebar menu, summary cards and filter chips (screen design)."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse

from core.context_processors import active_key
from core.models import Partner, Project, ProjectType, Shipment, ShipmentTruck, OrderItem

User = get_user_model()


class ActiveKeyTests(TestCase):
    def key(self, path):
        return active_key(resolve(path))

    def test_rules(self):
        cases = {
            "/": "home",
            "/schedule/": "board",
            "/projects/": "projects",
            "/bom/": "bom",
            "/shipping/": "shipping",
            "/shipping/new/": "shipping",
            "/shipping/reports/performance/": "performance",
            "/shipping/reports/lines/": "performance",
            "/shipping/reports/lines.xlsx": "performance",
            "/shipping/reports/unshipped/": "unshipped",
            "/shipping/masters/plants/new/": "plants",
            "/shipping/masters/carriers/": "carriers",
            "/shipping/masters/vehicle-types/": "vehicles",
        }
        for path, expected in cases.items():
            self.assertEqual(self.key(path), expected, path)

    def test_no_match(self):
        self.assertEqual(active_key(None), "")


class SidebarTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("u1", password="pw12345!")
        cls.staff = User.objects.create_user("boss", password="pw12345!", is_staff=True)

    def test_exactly_one_active_item_per_page(self):
        self.client.force_login(self.user)
        for name in ("schedule:home", "schedule:board", "projects:list", "bom:index", "shipping:list",
                     "shipping:performance", "shipping:unshipped", "shipping:plant_list",
                     "shipping:carrier_list", "shipping:vehicle_type_list", "shipping:lines"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertEqual(html.count('aria-current="page"'), 1, name)
            self.assertEqual(html.count('class="active"'), 1, name)

    def test_menu_groups_and_user(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("schedule:home"))
        for text in ("영업·현장", "설계", "생산·출하", "기준정보", "구간 BOM", "출고 실적", "미출고 부재", "제작장", "운송회사", "차종"):
            self.assertContains(r, text)
        self.assertContains(r, 'action="/accounts/logout/"')
        self.assertNotContains(r, reverse("admin:index"))

    def test_staff_sees_admin_link(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("schedule:home")), reverse("admin:index"))

    def test_login_page_has_no_sidebar(self):
        r = self.client.get(reverse("login"))
        self.assertNotContains(r, 'class="sidebar"')
        self.assertNotContains(r, "nav-toggle")
        self.assertContains(r, "로그인")

    def test_statement_print_hides_chrome(self):
        css = open("shipping/static/shipping/statement.css", encoding="utf-8").read()
        self.assertIn(".sidebar", css.split("@media print")[1])


class ListDesignTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("u1", password="pw12345!")
        cls.customer = Partner.objects.create(name="테스트건설", is_customer=True)
        cls.t1 = ProjectType.objects.create(name="구분가", sort_order=1)
        cls.t2 = ProjectType.objects.create(name="구분나", sort_order=2)
        mk = lambda n, t, st, mgr=None: Project.objects.create(name=n, project_type=t, customer=cls.customer, status=st, manager=mgr)
        cls.p1 = mk("현장1", cls.t1, Project.Status.ACTIVE, cls.user)
        cls.p2 = mk("현장2", cls.t1, Project.Status.ORDERED)
        cls.p3 = mk("현장3", cls.t2, Project.Status.ACTIVE)
        OrderItem.objects.create(project=cls.p1, item_name="가", amount=1500)
        OrderItem.objects.create(project=cls.p2, item_name="나", amount=2500)

    def setUp(self):
        self.client.force_login(self.user)

    def test_project_stats_ignore_filters(self):
        r = self.client.get(reverse("projects:list"), {"status": "ORDERED", "q": "현장2"})
        s = r.context["stats"]
        self.assertEqual((s["total"], s["active"], s["no_manager"], s["amount"]), (3, 2, 2, 4000))
        self.assertContains(r, 'id="stat-total"')

    def test_project_chip_counts_and_links(self):
        r = self.client.get(reverse("projects:list"), {"q": "현장", "customer": self.customer.pk})
        types = {c["label"]: c["count"] for c in r.context["type_chips"]}
        self.assertEqual(types["전체"], 3)
        self.assertEqual((types["구분가"], types["구분나"]), (2, 1))
        statuses = {c["label"]: c["count"] for c in r.context["status_chips"]}
        self.assertEqual((statuses["전체"], statuses["수주"], statuses["진행"], statuses["완료"]), (3, 1, 2, 0))
        url = next(c["url"] for c in r.context["type_chips"] if c["label"] == "구분가")
        self.assertIn(f"type={self.t1.pk}", url)
        self.assertIn("q=", url)  # other filters are kept
        self.assertContains(r, 'aria-current="true"')

    def test_project_chip_counts_follow_other_dimension(self):
        r = self.client.get(reverse("projects:list"), {"type": self.t1.pk})
        statuses = {c["label"]: c["count"] for c in r.context["status_chips"]}
        self.assertEqual((statuses["전체"], statuses["진행"]), (2, 1))
        active = [c["label"] for c in r.context["type_chips"] if c["active"]]
        self.assertEqual(active, ["구분가"])

    def test_project_list_query_count_is_constant(self):
        with self.assertNumQueries(10):
            self.client.get(reverse("projects:list"))

    def test_shipment_list_stats_and_chips(self):
        Shipment.objects.create(project=self.p1, ship_date=date(2026, 1, 5), status=Shipment.Status.PLANNED)
        Shipment.objects.create(project=self.p1, ship_date=date(2026, 1, 6), status=Shipment.Status.PLANNED)
        sh = Shipment.objects.create(project=self.p2, ship_date=date(2026, 1, 7), status=Shipment.Status.SHIPPED)
        ShipmentTruck.objects.create(shipment=sh, seq=1)
        r = self.client.get(reverse("shipping:list"))
        self.assertEqual(r.context["stats"]["planned"], 2)
        counts = {c["label"]: c["count"] for c in r.context["status_chips"]}
        self.assertEqual((counts["전체"], counts["예정"], counts["출하"], counts["취소"]), (3, 2, 1, 0))
        r = self.client.get(reverse("shipping:list"), {"status": "PLANNED", "project": self.p1.pk})
        self.assertEqual([c["label"] for c in r.context["status_chips"] if c["active"]], ["예정"])
        self.assertEqual(r.context["page"].paginator.count, 2)
