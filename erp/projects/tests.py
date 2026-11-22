import json
import shutil
import tempfile
from datetime import date

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    AuditLog,
    BomItem,
    Floor,
    ItemPrice,
    OrderItem,
    Partner,
    Project,
    ProjectType,
    Section,
    SectionActivity,
    SectionFile,
)

User = get_user_model()


def mgmt(prefix, total, initial=0):
    return {
        f"{prefix}-TOTAL_FORMS": total,
        f"{prefix}-INITIAL_FORMS": initial,
        f"{prefix}-MIN_NUM_FORMS": 0,
        f"{prefix}-MAX_NUM_FORMS": 1000,
    }


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("tester", password="pw")
        cls.customer = Partner.objects.create(name="테스트건설", is_customer=True)
        cls.contractor = Partner.objects.create(name="테스트원청", is_contractor=True)
        cls.ptype = ProjectType.objects.first()

    def setUp(self):
        self.client.force_login(self.user)

    def make_project(self, name="테스트 현장", **kw):
        return Project.objects.create(
            name=name, project_type=self.ptype, customer=self.customer, contract_date=kw.pop("contract_date", date(2026, 3, 5)), **kw
        )

    def make_section(self, project, name="A구간", floor=None, **kw):
        return Section.objects.create(project=project, name=name, floor=floor, planned_ship_date=date(2026, 4, 1), **kw)


class AccessTests(Base):
    def test_anonymous_redirects(self):
        self.client.logout()
        p = self.make_project()
        urls = [
            reverse("projects:list"), reverse("projects:create"), reverse("projects:detail", args=[p.pk]),
            reverse("projects:edit", args=[p.pk]), reverse("projects:order_items", args=[p.pk]),
            reverse("projects:prices", args=[p.pk]), reverse("projects:sections", args=[p.pk]),
            reverse("projects:files", args=[p.pk]),
        ]
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/accounts/login/", r["Location"])
        r = self.client.post(reverse("projects:quick_floor"), {"name": "1F"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Floor.objects.exists())


class ListTests(Base):
    def test_search_filter_and_counts(self):
        a = self.make_project("알파 현장")
        b = self.make_project("베타 현장", status=Project.Status.ACTIVE)
        other = Partner.objects.create(name="다른매출처", is_customer=True)
        c = Project.objects.create(name="감마 현장", project_type=self.ptype, customer=other)
        OrderItem.objects.create(project=a, seq=1, item_name="x", quantity=1, amount=1234567)
        OrderItem.objects.create(project=a, seq=2, item_name="y", quantity=1, amount=1000)
        self.make_section(a, "S1")
        self.make_section(a, "S2")
        url = reverse("projects:list")
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "1,235,567")
        row = [p for p in r.context["page"] if p.pk == a.pk][0]
        self.assertEqual(row.section_count, 2)
        self.assertEqual(row.amount_total, 1235567)
        self.assertEqual(len(self.client.get(url, {"q": "알파"}).context["page"]), 1)
        self.assertEqual(len(self.client.get(url, {"q": a.code}).context["page"]), 1)
        self.assertEqual([p.pk for p in self.client.get(url, {"status": "ACTIVE"}).context["page"]], [b.pk])
        self.assertEqual([p.pk for p in self.client.get(url, {"customer": other.pk}).context["page"]], [c.pk])
        self.assertEqual(len(self.client.get(url, {"type": self.ptype.pk}).context["page"]), 3)

    def test_query_count_constant_and_pagination(self):
        for i in range(25):
            self.make_project(f"현장{i}", contract_date=date(2026, 1, 1))
        r = self.client.get(reverse("projects:list"))
        self.assertEqual(len(r.context["page"]), 20)
        r2 = self.client.get(reverse("projects:list"), {"page": 2, "q": "현장"})
        self.assertEqual(len(r2.context["page"]), 5)
        self.assertContains(r2, "q=%ED%98%84%EC%9E%A5")


class ProjectFormTests(Base):
    def data(self, **kw):
        d = {
            "name": "신규 현장", "project_type": self.ptype.pk, "status": "ORDERED", "customer": self.customer.pk,
            "contract_date": "2026-05-10",
        }
        d.update(kw)
        return d

    def test_create_assigns_code_and_logs(self):
        r = self.client.get(reverse("projects:create"))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'name="code"')
        r = self.client.post(reverse("projects:create"), self.data(code="HACK"))
        p = Project.objects.get(name="신규 현장")
        self.assertRedirects(r, reverse("projects:detail", args=[p.pk]))
        self.assertEqual(p.code, "P2605-001")
        self.assertEqual(p.created_by, self.user)
        log = AuditLog.objects.get(action="create", object_id=str(p.pk), model="core.Project")
        self.assertEqual(log.user, self.user)

    def test_create_invalid(self):
        r = self.client.post(reverse("projects:create"), self.data(name="", start_date="2026-06-02", end_date="2026-06-01"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "field-error")
        self.assertContains(r, "종료일은 시작일 이후")
        self.assertFalse(Project.objects.exists())

    def test_edit(self):
        p = self.make_project()
        code = p.code
        r = self.client.post(reverse("projects:edit", args=[p.pk]), self.data(name="변경됨", contractor=self.contractor.pk))
        self.assertRedirects(r, reverse("projects:detail", args=[p.pk]))
        p.refresh_from_db()
        self.assertEqual((p.name, p.code, p.updated_by, p.contractor), ("변경됨", code, self.user, self.contractor))
        log = AuditLog.objects.get(action="update", object_id=str(p.pk))
        self.assertEqual(log.changes["name"]["new"], "변경됨")

    def test_customer_choices_only_customers(self):
        r = self.client.get(reverse("projects:create"))
        names = [c.name for c in r.context["form"].fields["customer"].queryset]
        self.assertEqual(names, ["테스트건설"])

    def test_detail(self):
        p = self.make_project()
        s = self.make_section(p)
        r = self.client.get(reverse("projects:detail", args=[p.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, reverse("bom:section", args=[s.pk]))
        self.assertContains(r, reverse("schedule:section", args=[s.pk]))
        self.assertContains(r, reverse("projects:order_items", args=[p.pk]))


class OrderItemTests(Base):
    def setUp(self):
        super().setUp()
        self.p = self.make_project()
        self.url = reverse("projects:order_items", args=[self.p.pk])

    def test_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_add_update_delete(self):
        d = mgmt("order_items", 2)
        d.update({"order_items-0-item_name": "품목A", "order_items-0-quantity": "10", "order_items-0-amount": "5000",
                  "order_items-1-item_name": "품목B", "order_items-1-quantity": "2", "order_items-1-amount": "300"})
        self.assertEqual(self.client.post(self.url, d).status_code, 302)
        items = list(self.p.order_items.all())
        self.assertEqual([(i.seq, i.item_name) for i in items], [(1, "품목A"), (2, "품목B")])
        self.assertEqual(self.p.order_amount, 5300)
        self.assertEqual(AuditLog.objects.filter(model="core.OrderItem", action="create").count(), 2)
        a, b = items
        d = mgmt("order_items", 2, initial=2)
        d.update({
            "order_items-0-id": a.pk, "order_items-0-project": self.p.pk, "order_items-0-item_name": "품목A", "order_items-0-quantity": "10",
            "order_items-0-amount": "9000", "order_items-0-DELETE": "",
            "order_items-1-id": b.pk, "order_items-1-project": self.p.pk, "order_items-1-item_name": "품목B", "order_items-1-quantity": "2",
            "order_items-1-amount": "300", "order_items-1-DELETE": "on",
        })
        self.assertEqual(self.client.post(self.url, d).status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.amount, 9000)
        self.assertEqual(a.updated_by, self.user)
        self.assertFalse(OrderItem.objects.filter(pk=b.pk).exists())
        self.assertTrue(AuditLog.objects.filter(model="core.OrderItem", action="update").exists())
        self.assertTrue(AuditLog.objects.filter(model="core.OrderItem", action="delete").exists())

    def test_validation_failure(self):
        d = mgmt("order_items", 1)
        d.update({"order_items-0-item_name": "", "order_items-0-quantity": "-1", "order_items-0-amount": "abc"})
        r = self.client.post(self.url, d)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "수량은 0 이상")
        self.assertContains(r, "field-error")
        self.assertEqual(self.p.order_items.count(), 0)


class PriceTests(Base):
    def setUp(self):
        super().setUp()
        self.p = self.make_project()
        self.url = reverse("projects:prices", args=[self.p.pk])

    def test_add_and_duplicate(self):
        d = mgmt("item_prices", 1)
        d.update({"item_prices-0-spec": "DH600", "item_prices-0-unit": "m", "item_prices-0-unit_price": "1500.50",
                  "item_prices-0-effective_from": "2026-01-01"})
        self.assertEqual(self.client.post(self.url, d).status_code, 302)
        pr = self.p.item_prices.get()
        self.assertEqual(str(pr.unit_price), "1500.50")
        d = mgmt("item_prices", 2)
        for i in range(2):
            d.update({f"item_prices-{i}-spec": "X", f"item_prices-{i}-unit_price": "1", f"item_prices-{i}-effective_from": "2026-02-01"})
        r = self.client.post(self.url, d)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "단가가 이미 있습니다")
        self.assertEqual(self.p.item_prices.count(), 1)

    def test_update_delete_and_invalid(self):
        pr = ItemPrice.objects.create(project=self.p, spec="A", unit_price=10)
        d = mgmt("item_prices", 1, 1)
        d.update({"item_prices-0-id": pr.pk, "item_prices-0-project": self.p.pk, "item_prices-0-spec": "A",
                  "item_prices-0-unit_price": "-5"})
        self.assertContains(self.client.post(self.url, d), "단가는 0 이상")
        d["item_prices-0-unit_price"] = "20"
        self.client.post(self.url, d)
        pr.refresh_from_db()
        self.assertEqual(pr.unit_price, 20)
        d["item_prices-0-DELETE"] = "on"
        self.client.post(self.url, d)
        self.assertFalse(ItemPrice.objects.exists())


class SectionTests(Base):
    def setUp(self):
        super().setUp()
        self.p = self.make_project()
        self.url = reverse("projects:sections", args=[self.p.pk])
        self.floor = Floor.objects.create(name="1F")

    def row(self, i, **kw):
        base = {"name": "A구간", "planned_ship_date": "2026-04-01", "make_type": "IN"}
        base.update(kw)
        return {f"sections-{i}-{k}": v for k, v in base.items()}

    def test_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_add_creates_activities_and_audit(self):
        d = mgmt("sections", 1)
        d.update(self.row(0, floor=self.floor.pk))
        self.assertEqual(self.client.post(self.url, d).status_code, 302)
        s = self.p.sections.get()
        self.assertEqual(s.created_by, self.user)
        self.assertTrue(SectionActivity.objects.filter(section=s).exists())
        self.assertTrue(AuditLog.objects.filter(model="core.Section", action="create").exists())

    def test_duplicate_with_empty_floor(self):
        d = mgmt("sections", 2)
        d.update(self.row(0))
        d.update(self.row(1))
        r = self.client.post(self.url, d)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "이미 있습니다")
        self.assertEqual(self.p.sections.count(), 0)

    def test_duplicate_against_existing_and_distinct_floor_ok(self):
        self.make_section(self.p, "A구간")
        existing = self.p.sections.get()
        d = mgmt("sections", 2, 1)
        d.update({"sections-0-id": existing.pk, "sections-0-project": self.p.pk, **{k: v for k, v in self.row(0).items()}})
        d.update(self.row(1))
        self.assertContains(self.client.post(self.url, d), "이미 있습니다")
        d.update(self.row(1, floor=self.floor.pk))
        self.assertEqual(self.client.post(self.url, d).status_code, 302)
        self.assertEqual(self.p.sections.count(), 2)

    def test_update_and_delete(self):
        s = self.make_section(self.p)
        d = mgmt("sections", 1, 1)
        d.update({"sections-0-id": s.pk, "sections-0-project": self.p.pk, **self.row(0, name="B구간", note="메모")})
        self.client.post(self.url, d)
        s.refresh_from_db()
        self.assertEqual(s.name, "B구간")
        d["sections-0-DELETE"] = "on"
        self.client.post(self.url, d)
        self.assertFalse(Section.objects.filter(pk=s.pk).exists())
        self.assertTrue(AuditLog.objects.filter(model="core.Section", action="delete").exists())

    def test_delete_blocked_with_bom(self):
        s = self.make_section(self.p)
        BomItem.objects.create(section=s, ass_mark="M1", profile_text="P", length_mm=100, quantity=1)
        d = mgmt("sections", 1, 1)
        d.update({"sections-0-id": s.pk, "sections-0-project": self.p.pk, **self.row(0), "sections-0-DELETE": "on"})
        r = self.client.post(self.url, d)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "BOM이 등록되어 있어 삭제할 수 없습니다")
        self.assertTrue(Section.objects.filter(pk=s.pk).exists())
        self.assertTrue(BomItem.objects.exists())

    def test_required_dates(self):
        d = mgmt("sections", 1)
        d.update(self.row(0, planned_ship_date=""))
        r = self.client.post(self.url, d)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "field-error")


class FileTests(Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        ov = override_settings(MEDIA_ROOT=self.tmp)
        ov.enable()
        self.addCleanup(ov.disable)
        self.p = self.make_project()
        self.s = self.make_section(self.p)
        self.url = reverse("projects:files", args=[self.p.pk])

    def upload(self, name="plan.pdf", **kw):
        d = {"file": SimpleUploadedFile(name, b"data"), "distributed_on": "2026-03-01", "section": self.s.pk}
        d.update(kw)
        return self.client.post(self.url, d)

    def test_upload_seq_and_default_date(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'name="distributed_on"')
        self.assertEqual(self.upload().status_code, 302)
        self.upload("two.pdf")
        seqs = list(self.p.files.values_list("seq", flat=True))
        self.assertEqual(seqs, [1, 2])
        f = self.p.files.first()
        self.assertEqual(f.created_by, self.user)
        self.assertTrue(AuditLog.objects.filter(model="core.SectionFile", action="create").count() == 2)

    def test_default_distributed_today(self):
        r = self.client.get(self.url)
        from django.utils import timezone
        self.assertEqual(r.context["form"]["distributed_on"].value(), timezone.localdate())

    def test_upload_requires_file(self):
        r = self.client.post(self.url, {"distributed_on": "2026-03-01"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "field-error")
        self.assertFalse(SectionFile.objects.exists())

    def test_section_must_belong_to_project(self):
        other = self.make_project("다른 현장")
        o = self.make_section(other, "Z")
        r = self.upload(section=o.pk)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(SectionFile.objects.exists())

    def test_delete_confirm_and_delete(self):
        self.upload()
        f = self.p.files.get()
        path = f.file.path
        url = reverse("projects:file_delete", args=[self.p.pk, f.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertTrue(SectionFile.objects.filter(pk=f.pk).exists())
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(SectionFile.objects.exists())
        import os
        self.assertFalse(os.path.exists(path))
        self.assertTrue(AuditLog.objects.filter(model="core.SectionFile", action="delete").exists())

    def test_delete_wrong_project_404(self):
        self.upload()
        f = self.p.files.get()
        other = self.make_project("다른 현장")
        r = self.client.post(reverse("projects:file_delete", args=[other.pk, f.pk]))
        self.assertEqual(r.status_code, 404)


class QuickAddTests(Base):
    def post(self, name, **kw):
        return self.client.post(reverse("projects:quick_partner"), {"name": name, **kw})

    def test_partner_success_sets_role(self):
        r = self.post("신규원청", role="contractor")
        data = r.json()
        self.assertTrue(data["ok"])
        p = Partner.objects.get(pk=data["id"])
        self.assertTrue(p.is_contractor)
        self.assertFalse(p.is_customer)
        self.assertEqual(p.created_by, self.user)
        self.assertTrue(AuditLog.objects.filter(model="core.Partner", action="create").exists())
        self.assertTrue(Partner.objects.get(pk=self.post("외주사", role="fabricator").json()["id"]).is_fabricator)

    def test_partner_duplicate_selects_existing_and_adds_role(self):
        data = self.post("테스트건설", role="contractor").json()
        self.assertTrue(data["ok"] and data["existing"])
        self.assertEqual(data["id"], self.customer.pk)
        self.assertIn("이미 등록", data["message"])
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_customer and self.customer.is_contractor)
        self.assertEqual(Partner.objects.filter(name="테스트건설").count(), 1)

    def test_partner_invalid(self):
        r = self.post("  ", role="customer")
        self.assertEqual(r.status_code, 400)
        self.assertIn("입력", r.json()["error"])
        self.assertEqual(self.post("x", role="bogus").status_code, 400)
        self.assertEqual(self.post("가" * 101, role="customer").status_code, 400)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("projects:quick_partner")).status_code, 405)

    def test_csrf_enforced(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.user)
        r = c.post(reverse("projects:quick_floor"), {"name": "3F"})
        self.assertEqual(r.status_code, 403)

    def test_floor(self):
        url = reverse("projects:quick_floor")
        d = self.client.post(url, {"name": "3F"}).json()
        self.assertTrue(d["ok"] and not d["existing"])
        self.assertTrue(Floor.objects.filter(name="3F").exists())
        d2 = self.client.post(url, {"name": "3F"}).json()
        self.assertTrue(d2["ok"] and d2["existing"])
        self.assertEqual(d2["id"], d["id"])
        self.assertEqual(Floor.objects.filter(name="3F").count(), 1)
        self.assertEqual(self.client.post(url, {"name": ""}).status_code, 400)


class UiTextTests(Base):
    def test_empty_options_are_korean(self):
        p = self.make_project()
        for url in (reverse("projects:create"), reverse("projects:sections", args=[p.pk]), reverse("projects:files", args=[p.pk])):
            r = self.client.get(url)
            self.assertNotContains(r, "Select an option")
            self.assertNotContains(r, "---------")
        self.assertContains(self.client.get(reverse("projects:create")), "선택하세요")
        self.assertContains(self.client.get(reverse("projects:sections", args=[p.pk])), "선택 안 함")

    def test_detail_has_mobile_cards_and_completed_next_to_confirmed(self):
        p = self.make_project()
        s = self.make_section(p)
        r = self.client.get(reverse("projects:detail", args=[p.pk]))
        self.assertContains(r, "card-actions")
        r = self.client.get(reverse("projects:sections", args=[p.pk]))
        self.assertContains(r, "field-pair")
