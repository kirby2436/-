"""Fill an empty database with fictional demo data.

Idempotent: every record is looked up by its natural key (name / seq / spec /
section name) before being created, so running this command repeatedly does
not duplicate rows.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import (
    FormedPart,
    Floor,
    ItemPrice,
    OrderItem,
    Partner,
    Product,
    ProductComponent,
    Project,
    ProjectType,
    Section,
    SectionActivity,
)
from core.services.schedule import set_actual

PASSWORD_ADMIN = "admin1234"
PASSWORD_DEMO = "demo1234"

PARTNERS = [
    # name, flag
    ("가나건설", "is_customer"),
    ("다라종합건설", "is_customer"),
    ("바사시공", "is_contractor"),
    ("아자건설", "is_contractor"),
    ("마바철강", "is_fabricator"),
    ("사아산업", "is_fabricator"),
]

FLOORS = [("B2", 1), ("B1", 2), ("1F", 3), ("2F", 4), ("3F", 5)]

FORMED_PARTS = [
    (FormedPart.Kind.BOTTOM, "W300"),
    (FormedPart.Kind.BOTTOM, "W400"),
    (FormedPart.Kind.BOTTOM, "W500"),
    (FormedPart.Kind.BOTTOM, "W600"),
    (FormedPart.Kind.SIDE, "H450"),
    (FormedPart.Kind.SIDE, "H600"),
    (FormedPart.Kind.SIDE, "H700"),
]

# name -> (bottom_thickness, side_thickness, [(kind, slot, formed part name, qty)])
PRODUCTS = [
    (
        "DH600X2700",
        Decimal("1.00"),
        Decimal("1.00"),
        [
            (FormedPart.Kind.BOTTOM, 1, "W600", 1),
            (FormedPart.Kind.SIDE, 1, "H700", 2),
        ],
    ),
    (
        "DH800X2700",
        None,
        None,
        [
            (FormedPart.Kind.BOTTOM, 1, "W400", 2),
            (FormedPart.Kind.SIDE, 1, "H700", 2),
        ],
    ),
]

# Offsets are in calendar days from today.
PROJECTS = [
    {
        "name": "데모 A동 골조공사",
        "type": "무해체보거푸집",
        "status": Project.Status.ORDERED,
        "customer": "가나건설",
        "contractor": "바사시공",
        "manager": "demo1",
        "contract_offset": -7,
        "start_offset": 5,
        "end_offset": 90,
    },
    {
        "name": "데모 B동 DECK공사",
        "type": "DECK PLATE",
        "status": Project.Status.ACTIVE,
        "customer": "다라종합건설",
        "contractor": "아자건설",
        "manager": "demo2",
        "contract_offset": -60,
        "start_offset": -45,
        "end_offset": 60,
    },
    {
        "name": "데모 C동 거푸집공사",
        "type": "거푸집/DECK",
        "status": Project.Status.ACTIVE,
        "customer": "가나건설",
        "contractor": "아자건설",
        "manager": "demo1",
        "contract_offset": -30,
        "start_offset": -20,
        "end_offset": 120,
    },
]

# Order items and unit prices per project (same for all demo projects).
ORDER_ITEMS = [
    (1, "DH600X2700 성형품 세트", "EA", Decimal("120"), Decimal("36000000")),
    (2, "DH800X2700 성형품 세트", "EA", Decimal("80"), Decimal("28000000")),
]
ITEM_PRICES = [
    ("DH600X2700", "EA", Decimal("85000")),
    ("DH800X2700", "EA", Decimal("95000")),
]

# (section name, floor name, planned ship offset in days, make type, fabricator name)
SECTIONS = {
    "데모 A동 골조공사": [
        ("A-1구간", "2F", -10, Section.MakeType.IN, None),
        ("A-2구간", "3F", 12, Section.MakeType.OUT, "마바철강"),
        ("A-3구간", "1F", 40, Section.MakeType.OUT, "사아산업"),
    ],
    "데모 B동 DECK공사": [
        ("B-1구간", "B1", -3, Section.MakeType.OUT, "마바철강"),
        ("B-2구간", "1F", 8, Section.MakeType.IN, None),
    ],
    "데모 C동 거푸집공사": [
        ("C-1구간", "2F", 25, Section.MakeType.IN, None),
        ("C-2구간", "3F", -1, Section.MakeType.OUT, "사아산업"),
        ("C-3구간", "1F", 36, Section.MakeType.OUT, "마바철강"),
    ],
}


class Command(BaseCommand):
    help = "Fill an empty database with fictional demo data (idempotent)."

    def handle(self, *args, **options):
        today = timezone.localdate()
        self.counts = {}
        with transaction.atomic():
            self._users()
            partners = self._partners()
            floors = self._floors()
            self._formed_parts()
            products = self._products()
            projects = self._projects(today, partners)
            self._order_items_and_prices(projects)
            sections = self._sections(today, projects, floors, partners)
            self._actuals(today, sections)
        self._summary()

    # --- helpers -----------------------------------------------------------

    def _count(self, label, created):
        created_n, total = self.counts.get(label, (0, 0))
        self.counts[label] = (created_n + int(created), total + 1)

    def _users(self):
        User = get_user_model()
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser(username="admin", email="", password=PASSWORD_ADMIN)
            self._count("사용자", True)
        else:
            self._count("사용자", False)
        for username in ("demo1", "demo2"):
            user, created = User.objects.get_or_create(username=username)
            if created:
                user.set_password(PASSWORD_DEMO)
                user.save()
            self._count("사용자", created)

    def _partners(self):
        result = {}
        for name, flag in PARTNERS:
            obj, created = Partner.objects.get_or_create(name=name, defaults={flag: True})
            self._count("거래처", created)
            result[name] = obj
        return result

    def _floors(self):
        result = {}
        for name, order in FLOORS:
            obj, created = Floor.objects.get_or_create(name=name, defaults={"sort_order": order})
            self._count("층", created)
            result[name] = obj
        return result

    def _formed_parts(self):
        result = {}
        for kind, name in FORMED_PARTS:
            obj, created = FormedPart.objects.get_or_create(kind=kind, name=name)
            self._count("성형품 규격", created)
            result[name] = obj
        return result

    def _products(self):
        formed = {fp.name: fp for fp in FormedPart.objects.all()}
        result = {}
        for name, bottom, side, components in PRODUCTS:
            product, created = Product.objects.get_or_create(
                name=name, defaults={"bottom_thickness": bottom, "side_thickness": side}
            )
            self._count("품목", created)
            for kind, slot, part_name, qty in components:
                _, created = ProductComponent.objects.get_or_create(
                    product=product,
                    kind=kind,
                    slot=slot,
                    defaults={"formed_part": formed[part_name], "quantity": qty},
                )
                self._count("품목 구성", created)
            result[name] = product
        return result

    def _projects(self, today, partners):
        User = get_user_model()
        result = []
        for spec in PROJECTS:
            project_type = ProjectType.objects.get(name=spec["type"])
            project, created = Project.objects.get_or_create(
                name=spec["name"],
                defaults={
                    "project_type": project_type,
                    "status": spec["status"],
                    "contract_date": today + timedelta(days=spec["contract_offset"]),
                    "start_date": today + timedelta(days=spec["start_offset"]),
                    "end_date": today + timedelta(days=spec["end_offset"]),
                    "customer": partners[spec["customer"]],
                    "contractor": partners[spec["contractor"]],
                    "manager": User.objects.get(username=spec["manager"]),
                },
            )
            self._count("프로젝트", created)
            result.append(project)
        return result

    def _order_items_and_prices(self, projects):
        for project in projects:
            for seq, item_name, unit, qty, amount in ORDER_ITEMS:
                _, created = OrderItem.objects.get_or_create(
                    project=project,
                    seq=seq,
                    defaults={"item_name": item_name, "unit": unit, "quantity": qty, "amount": amount},
                )
                self._count("수주정보", created)
            for spec, unit, price in ITEM_PRICES:
                _, created = ItemPrice.objects.get_or_create(
                    project=project,
                    spec=spec,
                    defaults={"unit": unit, "unit_price": price, "effective_from": project.contract_date},
                )
                self._count("품목 단가", created)

    def _sections(self, today, projects, floors, partners):
        result = {}
        for project in projects:
            for name, floor_name, offset, make_type, fab_name in SECTIONS[project.name]:
                fabricator = partners[fab_name] if fab_name else None
                section, created = Section.objects.get_or_create(
                    project=project,
                    name=name,
                    floor=floors[floor_name],
                    defaults={
                        "planned_ship_date": today + timedelta(days=offset),
                        "make_type": make_type,
                        "fabricator": fabricator,
                    },
                )
                self._count("구간", created)
                result.setdefault(project.pk, []).append(section)
        return result

    def _actuals(self, today, sections):
        """Record some actual dates for sections shipping in the past or within 10 days.

        Every eligible section gets its P200 actual; the first section of each
        project also gets P400 and P500. The remaining activities stay empty so
        that some show as late.
        """
        limit = today + timedelta(days=10)
        for project_sections in sections.values():
            for index, section in enumerate(project_sections):
                if section.planned_ship_date > limit:
                    continue
                codes = ["P200"]
                if index == 0:
                    codes += ["P400", "P500"]
                for code in codes:
                    sa = SectionActivity.objects.select_related("activity_type").get(
                        section=section, activity_type__code=code
                    )
                    if sa.actual_date is None:
                        set_actual(section, code, sa.planned_date, None)

    def _summary(self):
        labels = [
            "사용자",
            "거래처",
            "층",
            "성형품 규격",
            "품목",
            "품목 구성",
            "프로젝트",
            "수주정보",
            "품목 단가",
            "구간",
        ]
        self.stdout.write("")
        self.stdout.write("=== 데모 데이터 시드 결과 ===")
        for label in labels:
            created_n, total = self.counts.get(label, (0, 0))
            self.stdout.write(f"  {label}: 신규 {created_n}건 / 확인 {total}건")
        self.stdout.write("")
        self.stdout.write("로그인 계정:")
        self.stdout.write(f"  admin (관리자) / {PASSWORD_ADMIN}")
        self.stdout.write(f"  demo1 / {PASSWORD_DEMO}")
        self.stdout.write(f"  demo2 / {PASSWORD_DEMO}")
        self.stdout.write("")
        self.stdout.write("※ 데모 비밀번호는 실제 사용 전에 반드시 변경하세요.")
