import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from core.models import (
    AuditLog,
    BomItem,
    BomUpload,
    FormedPart,
    Partner,
    Product,
    ProductComponent,
    Project,
    ProjectType,
    Section,
)
from core.services.schedule import set_actual, sync_section_activities

from . import services

User = get_user_model()

HEADER = ["Ass Mark", "Profile", "Length(mm)", "Q'ty", "FINISH", "LOCATION"]


class ParsingTests(SimpleTestCase):
    def test_header_variants(self):
        for header in (
            ["ASS MARK", "profile", "length (mm)", "QTY", "finish", "location"],
            ["Ass  Mark", "Profile", "Length", "수량"],
            ["AssMark", "PROFILE", "LENGTH(MM)", "Q'ty"],
        ):
            idx, mapping = services.find_header([header])
            self.assertEqual(idx, 0)
            for f in services.REQUIRED_FIELDS:
                self.assertIn(f, mapping)

    def test_header_offset_and_missing(self):
        grid = [["제목"], [], ["x", "y"]] + [HEADER, ["Z-1", "DH600X2700", 630, 1, "3TB0", "X3-X4"]]
        rows = services.rows_from_grid(grid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["row_no"], 5)
        self.assertEqual(rows[0]["length"], "630.00")
        with self.assertRaises(services.BomParseError):
            services.rows_from_grid([["Ass Mark", "Profile", "Length"], ["a", "b", 1]])

    def test_header_beyond_30_rows_not_found(self):
        grid = [[""]] * 31 + [HEADER, ["A", "P", 1, 1]]
        with self.assertRaises(services.BomParseError):
            services.rows_from_grid(grid)

    def test_pasted_text_and_empty_rows_skipped(self):
        text = "Ass Mark\tProfile\tLength(mm)\tQ'ty\tFINISH\tLOCATION\nZ-1\tDH600\t630\t1\t3TB0\tX3\n\t\t\t\n\nZ-2\tDH700\t1,200.5\t2\t\t\n"
        rows = services.parse_text(text)
        self.assertEqual([r["ass_mark"] for r in rows], ["Z-1", "Z-2"])
        self.assertEqual(rows[1]["length"], "1200.50")
        self.assertFalse(any(r["errors"] for r in rows))

    def test_row_errors(self):
        bad = services.clean_row({"ass_mark": "", "profile": "P", "length": "abc", "qty": "1.5"})
        self.assertEqual(len(bad["errors"]), 3)
        zero = services.clean_row({"ass_mark": "A", "profile": "P", "length": 0, "qty": 0})
        self.assertEqual(len(zero["errors"]), 2)
        neg = services.clean_row({"ass_mark": "A", "profile": "P", "length": -5, "qty": 2})
        self.assertEqual(len(neg["errors"]), 1)
        ok = services.clean_row({"ass_mark": "A", "profile": "P", "length": 10.0, "qty": 2.0})
        self.assertEqual(ok["errors"], [])
        self.assertEqual(ok["qty"], 2)

    def test_duplicate_warnings(self):
        rows = [
            services.clean_row({"ass_mark": a, "profile": "P", "length": 1, "qty": 1}) for a in ("A", "A", "B", "C")
        ]
        services.annotate_duplicates(rows, ["C"])
        self.assertEqual([r["dup_file"] for r in rows], [True, True, False, False])
        self.assertEqual([r["dup_existing"] for r in rows], [False, False, False, True])
        self.assertFalse(any(r["errors"] for r in rows))

    def test_non_xlsx_and_garbage_rejected(self):
        with self.assertRaises(services.BomParseError):
            services.parse_xlsx(io.BytesIO(b"x"), "a.csv")
        with self.assertRaises(services.BomParseError):
            services.parse_xlsx(io.BytesIO(b"not a zip"), "a.xlsx")

    def test_row_cap(self):
        grid = [HEADER] + [[f"A{i}", "P", 1, 1] for i in range(services.MAX_ROWS + 1)]
        with self.assertRaises(services.BomParseError):
            services.rows_from_grid(grid)


def make_xlsx(rows, header=HEADER, offset=0):
    wb = Workbook()
    ws = wb.active
    for _ in range(offset):
        ws.append(["제목"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class BomBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("tester", password="pw")
        customer = Partner.objects.create(name="테스트 매출처", is_customer=True)
        cls.project = Project.objects.create(
            name="테스트 현장", project_type=ProjectType.objects.first(), customer=customer
        )
        cls.section = Section.objects.create(project=cls.project, name="A구간", planned_ship_date=date(2026, 3, 18))
        sync_section_activities(cls.section)

    def setUp(self):
        self.client.force_login(self.user)

    def p100(self):
        return self.section.activities.get(activity_type__code="P100").actual_date

    def upload_xlsx(self, rows, **kw):
        f = SimpleUploadedFile("bom.xlsx", make_xlsx(rows, **kw))
        return self.client.post(reverse("bom:upload", args=[self.section.pk]), {"step": "preview", "file": f})

    def confirm(self, resp, mode="append", **extra):
        data = {"step": "confirm", "payload": resp.context["token"], "mode": mode}
        data.update(extra)
        return self.client.post(reverse("bom:upload", args=[self.section.pk]), data)


class UploadFlowTests(BomBase):
    def test_anonymous_redirects(self):
        self.client.logout()
        for name, args in (("bom:index", []), ("bom:section", [self.section.pk]), ("bom:upload", [self.section.pk]), ("bom:resolve", [self.section.pk])):
            r = self.client.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 302)
            self.assertIn("/accounts/login/", r["Location"])

    def test_pages_render(self):
        self.assertEqual(self.client.get(reverse("bom:index")).status_code, 200)
        self.assertEqual(self.client.get(reverse("bom:section", args=[self.section.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("bom:upload", args=[self.section.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("bom:resolve", args=[self.section.pk])).status_code, 200)

    def test_end_to_end_with_linking_and_p100(self):
        Product.objects.create(name="DH600X2700")
        resp = self.upload_xlsx(
            [["Z-3B201", "DH600X2700", 630, 1, "3TB0", "X3-X4/Y3-Y4"], ["Z-3B202", "XX1", 100.5, 3, None, None]],
            offset=2,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_rows"], 2)
        self.assertEqual(resp.context["error_rows"], 0)
        self.assertEqual(BomItem.objects.count(), 0)
        self.assertIsNone(self.p100())
        self.assertEqual(resp.context["mode"], "replace")
        resp = self.confirm(resp, mode="replace")
        self.assertEqual(resp.status_code, 302)
        items = {i.ass_mark: i for i in BomItem.objects.all()}
        self.assertEqual(items["Z-3B201"].product.name, "DH600X2700")
        self.assertIsNone(items["Z-3B202"].product)
        self.assertEqual(items["Z-3B202"].length_mm, Decimal("100.50"))
        self.assertEqual(items["Z-3B201"].created_by, self.user)
        self.assertEqual(self.p100(), timezone.localdate())
        up = BomUpload.objects.get()
        self.assertEqual((up.row_count, up.is_additional, up.filename), (2, False, "bom.xlsx"))
        self.assertTrue(AuditLog.objects.filter(model="core.BomUpload", object_id=str(up.pk)).exists())

    def test_text_paste_flow(self):
        text = "Ass Mark\tProfile\tLength(mm)\tQ'ty\nA\tP1\t100\t2\n"
        r = self.client.post(reverse("bom:upload", args=[self.section.pk]), {"step": "preview", "text": text})
        self.assertEqual(r.context["total_rows"], 1)
        self.confirm(r)
        self.assertEqual(BomItem.objects.get().quantity, 2)

    def test_errors_block_save(self):
        resp = self.upload_xlsx([["", "P", 10, 1], ["B", "P", "x", 0], ["C", "P", 5, 1]])
        self.assertEqual(resp.context["error_rows"], 2)
        resp = self.confirm(resp)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BomItem.objects.count(), 0)

    def test_bad_files_rejected(self):
        url = reverse("bom:upload", args=[self.section.pk])
        r = self.client.post(url, {"step": "preview", "file": SimpleUploadedFile("a.csv", b"a,b")})
        self.assertContains(r, ".xlsx 파일만")
        r = self.client.post(url, {"step": "preview", "file": SimpleUploadedFile("a.xlsx", b"junk")})
        self.assertContains(r, "읽을 수 없습니다")
        r = self.client.post(url, {"step": "preview"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(BomItem.objects.count(), 0)

    def test_replace_vs_append(self):
        r = self.upload_xlsx([["A", "P", 10, 1], ["B", "P", 10, 1]])
        self.confirm(r)
        r = self.upload_xlsx([["C", "P", 10, 1]])
        self.confirm(r, mode="append")
        self.assertEqual(BomItem.objects.count(), 3)
        self.assertTrue(BomUpload.objects.filter(is_additional=True).exists())
        r = self.upload_xlsx([["D", "P", 10, 1]])
        self.assertEqual(r.context["existing_count"], 3)
        # replace without the explicit confirmation is refused
        self.confirm(r, mode="replace")
        self.assertEqual(BomItem.objects.count(), 3)
        resp = self.confirm(r, mode="replace", confirm_replace="on")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(list(BomItem.objects.values_list("ass_mark", flat=True)), ["D"])

    def test_duplicate_against_existing_is_warning_only(self):
        self.confirm(self.upload_xlsx([["A", "P", 10, 1]]))
        r = self.upload_xlsx([["A", "P", 10, 1], ["A", "P", 10, 1]])
        self.assertEqual(r.context["warn_rows"], 2)
        self.assertEqual(r.context["error_rows"], 0)
        self.confirm(r)
        self.assertEqual(BomItem.objects.count(), 3)

    def test_tampered_payload_rejected(self):
        r = self.upload_xlsx([["A", "P", 10, 1]])
        token = r.context["token"]
        bad = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        resp = self.client.post(
            reverse("bom:upload", args=[self.section.pk]), {"step": "confirm", "payload": bad, "mode": "append"}
        )
        self.assertContains(resp, "올바르지 않습니다")
        self.assertEqual(BomItem.objects.count(), 0)
        resp = self.client.post(
            reverse("bom:upload", args=[self.section.pk]), {"step": "confirm", "payload": "{}", "mode": "append"}
        )
        self.assertEqual(BomItem.objects.count(), 0)

    def test_payload_bound_to_section(self):
        other = Section.objects.create(project=self.project, name="B구간", planned_ship_date=date(2026, 4, 1))
        r = self.upload_xlsx([["A", "P", 10, 1]])
        self.client.post(
            reverse("bom:upload", args=[other.pk]), {"step": "confirm", "payload": r.context["token"], "mode": "append"}
        )
        self.assertEqual(BomItem.objects.count(), 0)

    def test_signed_payload_revalidated(self):
        token = services.sign_rows(self.section.pk, self.user.pk, "x", [
            {"row_no": 2, "ass_mark": "", "profile": "P", "length": "10", "qty": 1, "finish": "", "location": ""}
        ])
        self.client.post(
            reverse("bom:upload", args=[self.section.pk]), {"step": "confirm", "payload": token, "mode": "append"}
        )
        self.assertEqual(BomItem.objects.count(), 0)


class DeleteAndP100Tests(BomBase):
    def test_delete_rows_and_clear_p100(self):
        self.confirm(self.upload_xlsx([["A", "P", 10, 1], ["B", "P", 10, 1]]))
        a, b = BomItem.objects.order_by("id")
        self.client.post(reverse("bom:item_delete", args=[a.pk]))
        self.assertEqual(BomItem.objects.count(), 1)
        self.assertIsNotNone(self.p100())
        self.client.post(reverse("bom:item_delete", args=[b.pk]))
        self.assertEqual(BomItem.objects.count(), 0)
        self.assertIsNone(self.p100())
        self.assertEqual(AuditLog.objects.filter(action="delete", model="core.BomItem").count(), 2)

    def test_delete_requires_post(self):
        self.confirm(self.upload_xlsx([["A", "P", 10, 1]]))
        item = BomItem.objects.get()
        self.assertEqual(self.client.get(reverse("bom:item_delete", args=[item.pk])).status_code, 405)

    def test_existing_p100_not_overwritten(self):
        set_actual(self.section, "P100", date(2026, 1, 5), self.user)
        self.confirm(self.upload_xlsx([["A", "P", 10, 1]]))
        self.assertEqual(self.p100(), date(2026, 1, 5))

    def test_note_edit(self):
        self.confirm(self.upload_xlsx([["A", "P", 10, 1]]))
        item = BomItem.objects.get()
        self.client.post(reverse("bom:item_note", args=[item.pk]), {"note": "확인 필요"})
        item.refresh_from_db()
        self.assertEqual(item.note, "확인 필요")
        self.assertEqual(item.updated_by, self.user)


def resolve_post(profile, **fields):
    data = {"profile": profile}
    data.update({f"f0-{k}": v for k, v in fields.items()})
    return data


class ResolveTests(BomBase):
    def setUp(self):
        super().setUp()
        self.other = Section.objects.create(project=self.project, name="B구간", planned_ship_date=date(2026, 4, 1))
        BomItem.objects.create(section=self.section, ass_mark="A", profile_text="DH600", length_mm=1000, quantity=2)
        BomItem.objects.create(section=self.other, ass_mark="B", profile_text="DH600", length_mm=500, quantity=1)

    def test_lists_unresolved(self):
        r = self.client.get(reverse("bom:resolve", args=[self.section.pk]))
        self.assertContains(r, "DH600")
        self.assertEqual(len(r.context["forms"]), 1)

    def test_resolve_creates_product_and_links_all_sections(self):
        url = reverse("bom:resolve", args=[self.section.pk])
        r = self.client.post(
            url,
            resolve_post(
                "DH600",
                bottom_thickness="1.6",
                side_thickness="2.3",
                **{
                    "BOTTOM1-name": "B-100",
                    "BOTTOM1-qty": "2",
                    "SIDE1-name": "S-50",
                    "SIDE1-qty": "1",
                    "SIDE1-ns": "on",
                    "SIDE2-name": "",
                },
            ),
        )
        self.assertEqual(r.status_code, 302)
        p = Product.objects.get(name="DH600")
        self.assertEqual(p.bottom_thickness, Decimal("1.60"))
        comps = {(c.kind, c.slot): c for c in p.components.all()}
        self.assertEqual(set(comps), {("BOTTOM", 1), ("SIDE", 1)})
        self.assertEqual(comps[("BOTTOM", 1)].quantity, 2)
        self.assertTrue(comps[("SIDE", 1)].non_standard)
        self.assertTrue(FormedPart.objects.filter(kind="BOTTOM", name="B-100").exists())
        self.assertEqual(BomItem.objects.filter(product=p).count(), 2)
        self.assertTrue(AuditLog.objects.filter(model="core.Product").exists())

    def test_requires_component(self):
        r = self.client.post(reverse("bom:resolve", args=[self.section.pk]), resolve_post("DH600", bottom_thickness="1.6"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Product.objects.exists())
        self.assertContains(r, "구성을 한 개 이상")

    def test_blank_form_skipped(self):
        r = self.client.post(reverse("bom:resolve", args=[self.section.pk]), resolve_post("DH600"))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Product.objects.exists())

    def test_existing_unconfigured_product(self):
        p = Product.objects.create(name="DH600")
        BomItem.objects.update(product=p)
        self.assertEqual([e["profile"] for e in services.unresolved_profiles(self.section)], ["DH600"])
        self.client.post(
            reverse("bom:resolve", args=[self.section.pk]), resolve_post("DH600", **{"BOTTOM1-name": "B-1", "BOTTOM1-qty": "1"})
        )
        self.assertEqual(services.unresolved_profiles(self.section), [])


class RequirementTests(BomBase):
    def test_summary_math(self):
        # Product X: bottom 1.6 -> B1 x2, B2 x1 ; side 2.3 -> S1 x1
        # Product Y: bottom 1.2 -> B1 x1 ; side 2.3 -> S1 x3
        x = Product.objects.create(name="X", bottom_thickness=Decimal("1.6"), side_thickness=Decimal("2.3"))
        y = Product.objects.create(name="Y", bottom_thickness=Decimal("1.2"), side_thickness=Decimal("2.3"))
        b1 = FormedPart.objects.create(kind="BOTTOM", name="B1")
        b2 = FormedPart.objects.create(kind="BOTTOM", name="B2")
        s1 = FormedPart.objects.create(kind="SIDE", name="S1")
        for prod, kind, slot, part, q in (
            (x, "BOTTOM", 1, b1, 2), (x, "BOTTOM", 2, b2, 1), (x, "SIDE", 1, s1, 1),
            (y, "BOTTOM", 1, b1, 1), (y, "SIDE", 1, s1, 3),
        ):
            ProductComponent.objects.create(product=prod, kind=kind, slot=slot, formed_part=part, quantity=q)
        z = Product.objects.create(name="Z")  # no components -> excluded
        for mark, prof, prod, length, qty in (
            ("1", "X", x, 1000, 2), ("2", "X", x, 500, 1), ("3", "Y", y, 200, 4), ("4", "Z", z, 100, 1), ("5", "W", None, 100, 1),
        ):
            BomItem.objects.create(section=self.section, ass_mark=mark, profile_text=prof, product=prod, length_mm=length, quantity=qty)
        rows, excluded = services.summarize_requirements(self.section)
        self.assertEqual(excluded, 2)
        got = {(r["kind"], r["name"], r["thickness"]): (r["count"], r["total_length"]) for r in rows}
        # X: qty 3, sum length*qty = 2500 ; Y: qty 4, sum = 800
        self.assertEqual(got[("BOTTOM", "B1", Decimal("1.60"))], (6, Decimal("5000")))
        self.assertEqual(got[("BOTTOM", "B2", Decimal("1.60"))], (3, Decimal("2500")))
        self.assertEqual(got[("BOTTOM", "B1", Decimal("1.20"))], (4, Decimal("800")))
        self.assertEqual(got[("SIDE", "S1", Decimal("2.30"))], (3 + 12, Decimal("2500") + Decimal("2400")))
        self.assertEqual(len(got), 4)
        r = self.client.get(reverse("bom:section", args=[self.section.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "집계에서 제외")

    def test_index_counts_and_search(self):
        BomItem.objects.create(section=self.section, ass_mark="1", profile_text="W", length_mm=1, quantity=1)
        with self.assertNumQueries(4):
            r = self.client.get(reverse("bom:index"), {"q": "A구간"})
        s = r.context["page"].object_list[0]
        self.assertEqual((s.bom_count, s.unresolved_count), (1, 1))
        r = self.client.get(reverse("bom:index"), {"q": "없는이름"})
        self.assertEqual(len(r.context["page"].object_list), 0)
