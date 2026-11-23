from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import AuditLog, Carrier, Partner, Plant, VehicleType

User = get_user_model()

URLS = [
    ("shipping:plant_list", {}),
    ("shipping:plant_create", {}),
    ("shipping:carrier_list", {}),
    ("shipping:carrier_create", {}),
    ("shipping:vehicle_type_list", {}),
    ("shipping:vehicle_type_create", {}),
]


class MasterBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("tester", password="pw")
        cls.partner = Partner.objects.create(name="테스트외주사", is_fabricator=True)

    def setUp(self):
        self.client.force_login(self.user)


class MasterAccessTests(MasterBase):
    def test_anonymous_redirects_to_login(self):
        plant = Plant.objects.create(name="가제작장")
        urls = [reverse(n, kwargs=kw) for n, kw in URLS] + [reverse("shipping:plant_edit", kwargs={"pk": plant.pk})]
        self.client.logout()
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r["Location"], url)

    def test_all_screens_ok(self):
        plant = Plant.objects.create(name="가제작장")
        carrier = Carrier.objects.create(name="가운송")
        vtype = VehicleType.objects.create(name="가차종")
        urls = [reverse(n, kwargs=kw) for n, kw in URLS] + [
            reverse("shipping:plant_edit", kwargs={"pk": plant.pk}),
            reverse("shipping:carrier_edit", kwargs={"pk": carrier.pk}),
            reverse("shipping:vehicle_type_edit", kwargs={"pk": vtype.pk}),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_edit_unknown_pk_is_404(self):
        self.assertEqual(self.client.get(reverse("shipping:plant_edit", kwargs={"pk": 9999})).status_code, 404)


class PlantTests(MasterBase):
    def test_list_groups_by_site_and_shows_owner_wording(self):
        Plant.objects.create(name="본사1공장", site="본사 공장")
        Plant.objects.create(name="본사외주A", site="본사 공장", operator=self.partner)
        Plant.objects.create(name="따로공장", site="다른 곳")
        Plant.objects.create(name="위치없음")
        r = self.client.get(reverse("shipping:plant_list"))
        self.assertEqual([g["site"] for g in r.context["groups"]], ["다른 곳", "본사 공장", ""])
        html = r.content.decode()
        self.assertIn("본사 공장", html)
        self.assertIn("위치 미지정", html)
        self.assertIn("자사", html)
        self.assertIn("외주", html)
        self.assertIn("테스트외주사", html)

    def test_create_in_house_and_outsourced(self):
        r = self.client.post(reverse("shipping:plant_create"), {"name": "자사공장", "site": "A", "sort_order": 1, "is_active": "on"})
        self.assertRedirects(r, reverse("shipping:plant_list"))
        p = Plant.objects.get(name="자사공장")
        self.assertFalse(p.is_outsourced)
        self.assertEqual(p.created_by, self.user)
        self.client.post(
            reverse("shipping:plant_create"),
            {"name": "외주공장", "site": "A", "operator": self.partner.pk, "sort_order": 2, "is_active": "on"},
        )
        self.assertTrue(Plant.objects.get(name="외주공장").is_outsourced)
        self.assertEqual(AuditLog.objects.filter(model="core.Plant", action="create").count(), 2)

    def test_duplicate_name_error_next_to_field(self):
        Plant.objects.create(name="중복")
        r = self.client.post(reverse("shipping:plant_create"), {"name": "중복", "sort_order": 0, "is_active": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["form"].errors["name"])
        self.assertContains(r, "field-error")

    def test_name_required(self):
        r = self.client.post(reverse("shipping:plant_create"), {"name": "", "sort_order": 0})
        self.assertEqual(r.status_code, 200)
        self.assertIn("name", r.context["form"].errors)

    def test_edit_and_deactivate_instead_of_delete(self):
        p = Plant.objects.create(name="수정대상", site="A")
        r = self.client.post(reverse("shipping:plant_edit", kwargs={"pk": p.pk}), {"name": "수정됨", "site": "B", "sort_order": 3})
        self.assertRedirects(r, reverse("shipping:plant_list"))
        p.refresh_from_db()
        self.assertEqual((p.name, p.site, p.is_active), ("수정됨", "B", False))
        self.assertTrue(AuditLog.objects.filter(model="core.Plant", action="update", object_id=str(p.pk)).exists())
        self.assertContains(self.client.get(reverse("shipping:plant_list")), "사용 안 함")

    def test_next_return_and_unsafe_next_ignored(self):
        url = reverse("shipping:plant_create")
        r = self.client.post(url, {"name": "돌아감", "sort_order": 0, "is_active": "on", "next": "/shipping/new/?project=1"})
        self.assertRedirects(r, "/shipping/new/?project=1", fetch_redirect_response=False)
        r = self.client.post(url, {"name": "나쁜주소", "sort_order": 0, "is_active": "on", "next": "https://evil.example/x"})
        self.assertRedirects(r, reverse("shipping:plant_list"))

    def test_operator_choices_exclude_inactive_partner(self):
        Partner.objects.create(name="쉬는업체", is_active=False)
        r = self.client.get(reverse("shipping:plant_create"))
        names = [p.name for p in r.context["form"].fields["operator"].queryset]
        self.assertIn("테스트외주사", names)
        self.assertNotIn("쉬는업체", names)


class CarrierAndVehicleTests(MasterBase):
    def test_carrier_create_edit(self):
        r = self.client.post(reverse("shipping:carrier_create"), {"name": "가운송", "phone": "010-0000-0000", "is_active": "on"})
        self.assertRedirects(r, reverse("shipping:carrier_list"))
        c = Carrier.objects.get(name="가운송")
        self.client.post(reverse("shipping:carrier_edit", kwargs={"pk": c.pk}), {"name": "가운송", "phone": "02-111"})
        c.refresh_from_db()
        self.assertFalse(c.is_active)
        self.assertEqual(c.phone, "02-111")
        self.assertContains(self.client.get(reverse("shipping:carrier_list")), "가운송")

    def test_carrier_duplicate(self):
        Carrier.objects.create(name="중복")
        r = self.client.post(reverse("shipping:carrier_create"), {"name": "중복"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("name", r.context["form"].errors)

    def test_vehicle_type_create_edit_and_tabs(self):
        r = self.client.post(reverse("shipping:vehicle_type_create"), {"name": "특수차", "sort_order": 9, "is_active": "on"})
        self.assertRedirects(r, reverse("shipping:vehicle_type_list"))
        v = VehicleType.objects.get(name="특수차")
        self.client.post(reverse("shipping:vehicle_type_edit", kwargs={"pk": v.pk}), {"name": "특수차2", "sort_order": 1})
        v.refresh_from_db()
        self.assertEqual((v.name, v.is_active), ("특수차2", False))
        html = self.client.get(reverse("shipping:vehicle_type_list")).content.decode()
        for label in ("제작장", "운송회사", "차종"):
            self.assertIn(label, html)
        self.assertIn(reverse("shipping:plant_list"), html)
        self.assertIn(reverse("shipping:carrier_list"), html)

    def test_shipment_list_links_to_master(self):
        r = self.client.get(reverse("shipping:list"))
        self.assertContains(r, reverse("shipping:plant_list"))
        self.assertContains(r, "기준정보")
