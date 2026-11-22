from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import ActivityType, AuditLog, Floor, Partner, Project, ProjectType, Section, SectionActivity
from schedule import selectors

User = get_user_model()
TODAY = date(2026, 3, 18)  # Wednesday


def make_project(name="테스트 현장", status=Project.Status.ACTIVE, manager=None):
    customer, _ = Partner.objects.get_or_create(name="테스트 매출처", defaults={"is_customer": True})
    return Project.objects.create(
        name=name, project_type=ProjectType.objects.first(), customer=customer,
        status=status, manager=manager, contract_date=TODAY,
    )


def make_section(project, name="A구간", ship=TODAY + timedelta(days=60), **kw):
    return Section.objects.create(project=project, name=name, planned_ship_date=ship, **kw)


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tester", password="pw")
        self.client.force_login(self.user)
        p = mock.patch("schedule.views._today", return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)


class AccessTests(TestCase):
    def test_anonymous_redirected(self):
        s = make_section(make_project())
        for url in (reverse("schedule:home"), reverse("schedule:board"), reverse("schedule:section", args=[s.pk])):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertIn("/accounts/login/", r["Location"])
        r = self.client.post(reverse("schedule:actual", args=[s.pk]), {"code": "P200"})
        self.assertEqual(r.status_code, 302)


class BoardTests(Base):
    def ids(self, **params):
        r = self.client.get(reverse("schedule:board"), params)
        self.assertEqual(r.status_code, 200)
        return [s.pk for s in r.context["sections"]], r

    def test_date_range(self):
        p = make_project()
        near = make_section(p, "근접", ship=TODAY + timedelta(days=10))
        far = make_section(p, "먼", ship=TODAY + timedelta(days=200))
        ids, _ = self.ids()
        self.assertIn(near.pk, ids)
        self.assertNotIn(far.pk, ids)
        ids, _ = self.ids(start="2026-10-01", end="2026-12-31")
        self.assertIn(far.pk, ids)
        self.assertNotIn(near.pk, ids)

    def test_project_filter(self):
        a, b = make_project("가"), make_project("나")
        sa, sb = make_section(a, ship=TODAY), make_section(b, ship=TODAY)
        ids, _ = self.ids(project=a.pk)
        self.assertEqual(ids, [sa.pk])

    def test_status_default_excludes_done(self):
        act = make_section(make_project("진행"), ship=TODAY)
        done = make_section(make_project("완료", status=Project.Status.DONE), ship=TODAY)
        forced = make_section(make_project("강제", status=Project.Status.FORCED), ship=TODAY)
        ids, _ = self.ids()
        self.assertEqual(ids, [act.pk])
        ids, _ = self.ids(status="DONE")
        self.assertEqual(ids, [done.pk])
        ids, _ = self.ids(status="FORCED")
        self.assertEqual(ids, [forced.pk])

    def test_manager_filter(self):
        other = User.objects.create_user("other", password="pw")
        s1 = make_section(make_project("가", manager=self.user), ship=TODAY)
        make_section(make_project("나", manager=other), ship=TODAY)
        ids, _ = self.ids(manager=self.user.pk)
        self.assertEqual(ids, [s1.pk])

    def test_late_only(self):
        late = make_section(make_project("지연"), ship=TODAY + timedelta(days=2))  # early activities already past
        ok = make_section(make_project("정상"), ship=TODAY + timedelta(days=60))
        ids, _ = self.ids(start="2026-01-01", end="2026-12-31")
        self.assertIn(ok.pk, ids)
        ids, r = self.ids(start="2026-01-01", end="2026-12-31", late_only="on")
        self.assertEqual(ids, [late.pk])
        self.assertContains(r, "조회 결과 1개 구간")

    def test_state_text_labels(self):
        make_section(make_project(), ship=TODAY + timedelta(days=2))
        r = self.client.get(reverse("schedule:board"))
        for label in ("지연", "임박", "예정"):
            self.assertContains(r, label)
        self.assertContains(r, "state-late")
        self.assertContains(r, "범례")

    def test_done_label(self):
        s = make_section(make_project(), ship=TODAY + timedelta(days=2))
        sa = s.activities.first()
        sa.actual_date = TODAY
        sa.save()
        self.assertContains(self.client.get(reverse("schedule:board")), "완료")

    def test_columns_from_db(self):
        make_section(make_project(), ship=TODAY)
        _, r = self.ids()
        self.assertEqual(len(r.context["types"]), ActivityType.objects.count())

    def test_pagination_keeps_query(self):
        p = make_project()
        for i in range(35):
            make_section(p, f"구간{i:02d}", ship=TODAY + timedelta(days=i % 20))
        ids, r = self.ids(project=p.pk)
        self.assertEqual(len(ids), 30)
        self.assertContains(r, f"project={p.pk}&amp;page=2")
        r2 = self.client.get(reverse("schedule:board"), {"project": p.pk, "page": 2})
        self.assertEqual(len(r2.context["sections"]), 5)

    def test_query_count_constant(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        p = make_project()
        make_section(p, "구간0", ship=TODAY)
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("schedule:board"))
        for i in range(1, 6):
            make_section(p, f"구간{i}", ship=TODAY)
        with CaptureQueriesContext(connection) as big:
            self.client.get(reverse("schedule:board"))
        self.assertEqual(len(small), len(big))

    def test_invalid_filter_date(self):
        make_section(make_project(), ship=TODAY)
        r = self.client.get(reverse("schedule:board"), {"start": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "올바른 날짜를 입력하세요")

    def test_empty_state(self):
        r = self.client.get(reverse("schedule:board"))
        self.assertContains(r, "조건에 맞는 구간이 없습니다")
        self.assertContains(r, reverse("projects:list"))


class SectionScreenTests(Base):
    def setUp(self):
        super().setUp()
        self.section = make_section(make_project(), ship=TODAY + timedelta(days=30), floor=Floor.objects.create(name="3F"))
        self.url = reverse("schedule:section", args=[self.section.pk])
        self.post_url = reverse("schedule:actual", args=[self.section.pk])
        self.manual = ActivityType.objects.filter(actual_source="MANUAL").first()

    def sa(self, code):
        return SectionActivity.objects.get(section=self.section, activity_type__code=code)

    def test_get(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "BOM 업로드 시 자동 반영")
        self.assertContains(r, "구간의 출고확정일과 완료 표시로 자동 반영")
        self.assertContains(r, reverse("bom:section", args=[self.section.pk]))
        self.assertContains(r, reverse("projects:detail", args=[self.section.project.pk]))
        self.assertContains(r, "저장")

    def test_404(self):
        self.assertEqual(self.client.get(reverse("schedule:section", args=[9999])).status_code, 404)

    def test_set_and_clear(self):
        code = self.manual.code
        r = self.client.post(self.post_url, {"code": code, "action": "save", "actual_date": "2026-03-10"})
        self.assertRedirects(r, self.url)
        self.assertEqual(self.sa(code).actual_date, date(2026, 3, 10))
        log = AuditLog.objects.filter(model="core.SectionActivity", object_id=str(self.sa(code).pk)).latest("id")
        self.assertEqual(log.user, self.user)
        r = self.client.get(self.url)
        self.assertContains(r, "2026-03-10")
        self.assertContains(r, "해제")
        self.client.post(self.post_url, {"code": code, "action": "clear"})
        self.assertIsNone(self.sa(code).actual_date)
        self.assertEqual(AuditLog.objects.filter(model="core.SectionActivity").count(), 2)
        self.assertEqual(len(self.client.get(self.url).context["history"]), 2)

    def test_future_date_allowed(self):
        self.client.post(self.post_url, {"code": self.manual.code, "action": "save", "actual_date": "2027-01-01"})
        self.assertEqual(self.sa(self.manual.code).actual_date, date(2027, 1, 1))

    def test_invalid_date(self):
        r = self.client.post(self.post_url, {"code": self.manual.code, "action": "save", "actual_date": "2026-13-45"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "올바른 날짜를 입력하세요")
        self.assertIsNone(self.sa(self.manual.code).actual_date)
        r = self.client.post(self.post_url, {"code": self.manual.code, "action": "save", "actual_date": ""})
        self.assertContains(r, "실적일을 입력하세요")
        self.assertFalse(AuditLog.objects.filter(model="core.SectionActivity").exists())

    def test_auto_source_refused(self):
        for code in ActivityType.objects.exclude(actual_source="MANUAL").values_list("code", flat=True):
            r = self.client.post(self.post_url, {"code": code, "action": "save", "actual_date": "2026-03-10"})
            self.assertRedirects(r, self.url)
            self.assertIsNone(self.sa(code).actual_date)
        self.assertFalse(AuditLog.objects.filter(model="core.SectionActivity").exists())

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.post_url).status_code, 405)


class DashboardTests(Base):
    def test_numbers_and_lists(self):
        a = make_project("진행", status=Project.Status.ACTIVE)
        make_project("수주", status=Project.Status.ORDERED)
        make_project("수주2", status=Project.Status.ORDERED)
        make_project("완료", status=Project.Status.DONE)
        # Mon 3/16 .. Sun 3/22 is the current week
        in_week = make_section(a, "금주", ship=date(2026, 3, 20))
        confirmed_in = make_section(a, "확정금주", ship=date(2026, 4, 30), confirmed_ship_date=date(2026, 3, 22))
        confirmed_out = make_section(a, "확정이동", ship=date(2026, 3, 19), confirmed_ship_date=date(2026, 3, 30))
        completed = make_section(a, "완료구간", ship=date(2026, 3, 19), is_completed=True)
        next_week = make_section(a, "다음주", ship=date(2026, 3, 23))
        r = self.client.get(reverse("schedule:home"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["active_count"], 1)
        self.assertEqual(r.context["ordered_count"], 2)
        shown = [s.pk for s in r.context["ship_sections"]]
        self.assertEqual(shown, [in_week.pk, confirmed_in.pk])
        for s in (confirmed_out, completed, next_week):
            self.assertNotIn(s.pk, shown)

    def test_late_ordering(self):
        p = make_project()
        s = make_section(p, ship=date(2026, 3, 30))
        SectionActivity.objects.filter(section=s).update(planned_date=TODAY + timedelta(days=5))
        acts = list(s.activities.order_by("activity_type__sort_order"))
        acts[0].planned_date = TODAY - timedelta(days=3)
        acts[1].planned_date = TODAY - timedelta(days=10)
        acts[2].planned_date = TODAY - timedelta(days=5)
        acts[2].actual_date = TODAY - timedelta(days=4)  # done: excluded
        for a in acts[:3]:
            a.save()
        data = selectors.dashboard(TODAY)
        self.assertEqual([x["days_late"] for x in data["late"]], [10, 3])
        self.assertEqual(data["late"][0]["sa"].pk, acts[1].pk)
        r = self.client.get(reverse("schedule:home"))
        self.assertContains(r, "10일 지연")
        self.assertContains(r, reverse("schedule:board") + "?late_only=on")

    def test_late_top_20(self):
        p = make_project()
        for i in range(5):
            make_section(p, f"구간{i}", ship=TODAY - timedelta(days=30 + i))
        data = selectors.dashboard(TODAY)
        self.assertEqual(len(data["late"]), 20)
        self.assertGreater(data["late_total"], 20)
        days = [x["days_late"] for x in data["late"]]
        self.assertEqual(days, sorted(days, reverse=True))

    def test_recent_logs(self):
        s = make_section(make_project())
        for i in range(12):
            AuditLog.objects.create(action="update", model="x", object_repr=f"대상{i}")
        r = self.client.get(reverse("schedule:home"))
        self.assertEqual(len(r.context["recent_logs"]), 10)
        self.assertContains(r, "수정")

    def test_empty_state(self):
        r = self.client.get(reverse("schedule:home"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "출고 예정인 구간이 없습니다")
        self.assertContains(r, "지연된 Activity가 없습니다")
        self.assertContains(r, reverse("projects:list"))


class SelectorTests(TestCase):
    def test_week_range(self):
        self.assertEqual(selectors.week_range(date(2026, 3, 22)), (date(2026, 3, 16), date(2026, 3, 22)))
        self.assertEqual(selectors.week_range(date(2026, 3, 16)), (date(2026, 3, 16), date(2026, 3, 22)))

    def test_default_period(self):
        self.assertEqual(selectors.default_period(TODAY), (date(2026, 3, 4), date(2026, 4, 17)))
