from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import (
    ActivityType,
    AuditLog,
    Floor,
    Partner,
    Product,
    ProductComponent,
    FormedPart,
    Project,
    ProjectType,
    Section,
    SectionActivity,
)
from core.services import audit
from core.services.codes import next_project_code
from core.services.schedule import activity_state, add_business_days, set_actual, sync_section_activities

User = get_user_model()


def make_project(contract_date=None, name="테스트 현장"):
    customer, _ = Partner.objects.get_or_create(name="테스트 매출처", defaults={"is_customer": True})
    return Project.objects.create(
        name=name, project_type=ProjectType.objects.first(), customer=customer, contract_date=contract_date
    )


def make_section(project=None, ship=date(2026, 3, 18), **kw):
    project = project or make_project()
    return Section.objects.create(project=project, name="A구간", planned_ship_date=ship, **kw)


def planned(section):
    return {sa.activity_type.code: sa.planned_date for sa in section.activities.select_related("activity_type")}


class BusinessDaysTests(TestCase):
    def test_zero(self):
        self.assertEqual(add_business_days(date(2026, 3, 14), 0), date(2026, 3, 14))  # Saturday stays

    def test_positive_across_weekend(self):
        self.assertEqual(add_business_days(date(2026, 3, 13), 1), date(2026, 3, 16))  # Fri -> Mon
        self.assertEqual(add_business_days(date(2026, 3, 13), 5), date(2026, 3, 20))

    def test_negative_across_weekend(self):
        self.assertEqual(add_business_days(date(2026, 3, 16), -1), date(2026, 3, 13))  # Mon -> Fri
        self.assertEqual(add_business_days(date(2026, 3, 16), -6), date(2026, 3, 6))
        self.assertEqual(add_business_days(date(2026, 3, 14), -1), date(2026, 3, 13))  # Sat -> Fri
        self.assertEqual(add_business_days(date(2026, 3, 15), 1), date(2026, 3, 16))  # Sun -> Mon


class SeedTests(TestCase):
    def test_project_types(self):
        self.assertEqual(
            list(ProjectType.objects.values_list("name", flat=True)),
            ["무해체보거푸집", "DECK PLATE", "거푸집/DECK", "거푸집/기타", "거푸집/DECK/기타"],
        )

    def test_activity_types(self):
        self.assertEqual(ActivityType.objects.count(), 9)
        t = {a.code: a for a in ActivityType.objects.all()}
        self.assertEqual(t["P300"].base_activity.code, "P700")
        self.assertEqual(t["P400"].base_activity.code, "P600")
        self.assertEqual(t["P500"].base_activity.code, "P600")
        self.assertIsNone(t["P100"].base_activity)
        self.assertEqual(t["P100"].lead_days, -21)
        self.assertEqual(t["P900"].actual_source, ActivityType.ActualSource.SHIP)
        self.assertEqual(t["P100"].actual_source, ActivityType.ActualSource.BOM_UPLOAD)


class SyncTests(TestCase):
    # ship Wed 2026-03-18
    def test_all_planned_dates(self):
        s = make_section()
        self.assertEqual(
            planned(s),
            {
                "P100": date(2026, 2, 17),  # -21 business days
                "P200": date(2026, 2, 26),  # -14
                "P300": date(2026, 3, 11),  # P700(03-16) -3
                "P400": date(2026, 3, 2),  # P600(03-09) -5
                "P500": date(2026, 3, 6),  # P600 -1
                "P600": date(2026, 3, 9),  # -7
                "P700": date(2026, 3, 16),  # -2
                "P800": date(2026, 3, 17),  # -1
                "P900": date(2026, 3, 18),
            },
        )

    def test_confirmed_overrides_planned(self):
        s = make_section()
        s.confirmed_ship_date = date(2026, 3, 25)
        s.save()
        p = planned(s)
        self.assertEqual(p["P900"], date(2026, 3, 25))
        self.assertEqual(p["P800"], date(2026, 3, 24))
        self.assertEqual(SectionActivity.objects.filter(section=s).count(), 9)

    def test_resync_idempotent_keeps_manual_actual(self):
        s = make_section()
        set_actual(s, "P200", date(2026, 2, 20), None)
        sync_section_activities(s)
        self.assertEqual(s.activities.get(activity_type__code="P200").actual_date, date(2026, 2, 20))
        self.assertEqual(SectionActivity.objects.filter(section=s).count(), 9)

    def test_p900_actual_follows_completion(self):
        s = make_section()
        p900 = lambda: s.activities.get(activity_type__code="P900").actual_date
        self.assertIsNone(p900())
        s.is_completed = True
        s.save()
        self.assertIsNone(p900())  # completed but no confirmed date
        s.confirmed_ship_date = date(2026, 3, 20)
        s.save()
        self.assertEqual(p900(), date(2026, 3, 20))
        s.is_completed = False
        s.save()
        self.assertIsNone(p900())


class SetActualTests(TestCase):
    def test_writes_audit_log(self):
        user = User.objects.create_user("u1", password="pw")
        s = make_section()
        set_actual(s, "P200", date(2026, 2, 20), user)
        sa = s.activities.get(activity_type__code="P200")
        self.assertEqual(sa.actual_date, date(2026, 2, 20))
        log = AuditLog.objects.filter(model="core.SectionActivity").latest("id")
        self.assertEqual(log.user, user)
        self.assertEqual(log.action, "update")
        self.assertEqual(log.changes["actual_date"], [None, "2026-02-20"])
        set_actual(s, "P200", None, user)
        sa.refresh_from_db()
        self.assertIsNone(sa.actual_date)
        self.assertEqual(AuditLog.objects.filter(model="core.SectionActivity").count(), 2)

    def test_audit_log_function(self):
        p = make_project()
        entry = audit.log(None, "create", p, {"name": [None, p.name]})
        self.assertEqual(entry.object_id, str(p.pk))
        self.assertIsNone(entry.user)


class ActivityStateTests(TestCase):
    def test_states(self):
        s = make_section()
        sa = s.activities.get(activity_type__code="P900")  # 2026-03-18 (Wed)
        sa.planned_date = date(2026, 3, 18)
        sa.actual_date = None
        today = date(2026, 3, 16)  # Mon; +3 business days = Thu 03-19
        self.assertEqual(activity_state(sa, today), "soon")
        self.assertEqual(activity_state(sa, date(2026, 3, 19)), "late")
        sa.planned_date = date(2026, 3, 19)
        self.assertEqual(activity_state(sa, date(2026, 3, 16)), "soon")
        sa.planned_date = date(2026, 3, 20)
        self.assertEqual(activity_state(sa, date(2026, 3, 16)), "planned")
        sa.planned_date = date(2026, 3, 15)
        self.assertEqual(activity_state(sa, date(2026, 3, 16)), "late")
        sa.actual_date = date(2026, 3, 14)
        self.assertEqual(activity_state(sa, date(2026, 3, 16)), "done")
        # today equal to planned date and not done is not late
        sa.actual_date = None
        sa.planned_date = date(2026, 3, 16)
        self.assertEqual(activity_state(sa, date(2026, 3, 16)), "soon")


class ProjectCodeTests(TestCase):
    def test_sequence_within_month(self):
        a = make_project(date(2023, 11, 5))
        b = make_project(date(2023, 11, 20), name="두번째")
        c = make_project(date(2023, 12, 1), name="다음달")
        self.assertEqual(a.code, "P2311-001")
        self.assertEqual(b.code, "P2311-002")
        self.assertEqual(c.code, "P2312-001")
        self.assertEqual(next_project_code(date(2023, 11, 30)), "P2311-003")

    def test_code_not_changed_on_resave(self):
        a = make_project(date(2023, 11, 5))
        a.contract_date = date(2024, 1, 1)
        a.name = "변경"
        a.save()
        a.refresh_from_db()
        self.assertEqual(a.code, "P2311-001")

    def test_default_uses_today(self):
        from django.utils import timezone

        p = make_project(None)
        self.assertTrue(p.code.startswith(f"P{timezone.localdate():%y%m}-"))


class ModelPropertyTests(TestCase):
    def test_order_amount(self):
        p = make_project()
        self.assertEqual(p.order_amount, 0)
        p.order_items.create(seq=1, item_name="a", quantity=2, amount=1000)
        p.order_items.create(seq=2, item_name="b", quantity=1, amount=500)
        self.assertEqual(p.order_amount, 1500)
        self.assertEqual(p.order_items.get(seq=1).unit_price, 500)

    def test_is_configured(self):
        prod = Product.objects.create(name="DH600X2700")
        self.assertFalse(prod.is_configured)
        part = FormedPart.objects.create(kind="BOTTOM", name="W500")
        ProductComponent.objects.create(product=prod, kind="BOTTOM", slot=1, formed_part=part, quantity=1)
        self.assertTrue(prod.is_configured)

    def test_section_str_with_floor(self):
        s = make_section(floor=Floor.objects.create(name="3F"))
        self.assertIn("3F", str(s))


class AuthTests(TestCase):
    def test_login_required_redirect(self):
        for name in ["schedule:home", "projects:list", "bom:index", "schedule:board", "shipping:list"]:
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])

    def test_login_page_public(self):
        r = self.client.get(reverse("login"))
        self.assertEqual(r.status_code, 200)

    def test_admin_login_reachable(self):
        r = self.client.get("/admin/login/")
        self.assertEqual(r.status_code, 200)

    def test_login_and_nav(self):
        User.objects.create_user("u1", password="pw12345!")
        self.assertTrue(self.client.login(username="u1", password="pw12345!"))
        for name in ["schedule:home", "projects:list", "bom:index", "schedule:board", "shipping:list"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("schedule:section", args=[999999])).status_code, 404)
        r = self.client.get(reverse("schedule:home"))
        self.assertContains(r, reverse("projects:list"))
        self.assertNotContains(r, "/admin/")

    def test_staff_sees_admin_link(self):
        User.objects.create_user("st", password="pw12345!", is_staff=True)
        self.client.login(username="st", password="pw12345!")
        self.assertContains(self.client.get(reverse("schedule:home")), "/admin/")

    def test_logout_post(self):
        User.objects.create_user("u1", password="pw12345!")
        self.client.login(username="u1", password="pw12345!")
        r = self.client.post(reverse("logout"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse("schedule:home")).status_code, 302)
