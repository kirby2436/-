"""Fill an empty database with fictional demo data.

Idempotent: every record is looked up by its natural key (name / seq / spec /
section name / Ass Mark) before being created, so running this command
repeatedly does not duplicate rows. Shipments are created only when the
database has no shipment yet.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import (
    Carrier,
    BomItem,
    FormedPart,
    Floor,
    ItemPrice,
    OrderItem,
    Partner,
    Plant,
    Product,
    ProductComponent,
    Project,
    ProjectType,
    Section,
    SectionActivity,
    Shipment,
    SiteProfile,
    VehicleType,
)
from core.services import shipping
from core.services.area import fill_bom_item
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

# Production plants: name, location, operator (outsourced partner or None), sort order.
PLANTS = [
    ("1공장 가동", "1공장", None, 1),
    ("1공장 나동", "1공장", None, 2),
    ("1공장 다동", "1공장", "마바철강", 3),
    ("외주 라제작", "외부", "사아산업", 4),
]

# Section name -> plant name.
SECTION_PLANTS = {
    "A-1구간": "1공장 가동",
    "A-2구간": "1공장 다동",
    "A-3구간": "외주 라제작",
    "B-1구간": "1공장 나동",
    "B-2구간": "1공장 가동",
    "C-1구간": "1공장 가동",
    "C-2구간": "1공장 나동",
    "C-3구간": "외주 라제작",
}

CARRIERS = [
    ("데모운수", "010-0000-0001"),
    ("데모물류", "010-0000-0002"),
]

# Site profiles for two projects. vehicle_count: first N active vehicle types are allowed.
SITE_PROFILES = {
    "데모 A동 골조공사": {
        "address": "가상시 데모구 예시로 00 (가짜 주소)",
        "access_note": "정문 경비실 호출 후 진입합니다. 진입로 폭이 좁아 소형 차량을 우선 배차합니다.",
        "contact_name": "데모 하차담당A",
        "contact_phone": "010-0000-0003",
        "corner_plate": False,
        "vehicle_count": 2,
    },
    "데모 B동 DECK공사": {
        "address": "가상시 샘플구 테스트로 00 (가짜 주소)",
        "access_note": "지하주차장 진입 높이 제한이 있습니다. 오전 8시 이전에만 상차 차량을 진입시킵니다.",
        "contact_name": "데모 하차담당B",
        "contact_phone": "010-0000-0004",
        "corner_plate": True,
        "vehicle_count": 3,
    },
}

# (project name, section name) -> (Ass Mark prefix, item count, has one unreadable Profile)
BOM_PLAN = {
    ("데모 A동 골조공사", "A-1구간"): ("A1G", 16, False),
    ("데모 A동 골조공사", "A-2구간"): ("A2G", 44, True),  # 44 items: two-page statement
    ("데모 B동 DECK공사", "B-1구간"): ("B1G", 14, True),
    ("데모 B동 DECK공사", "B-2구간"): ("B2G", 12, False),
}
BOM_PROFILES = ["NB400X450", "NB500X600", "NB300X450", "NBL400X450"]
UNREADABLE_PROFILE = "SPECIAL-1"

PROJECT_A = "데모 A동 골조공사"
PROJECT_B = "데모 B동 DECK공사"


def _bom_spec(prefix, index, count, has_unreadable):
    """Deterministic fictional BOM row for position `index` of a section."""
    if has_unreadable and index == count - 1:
        profile = UNREADABLE_PROFILE
    else:
        profile = BOM_PROFILES[index % len(BOM_PROFILES)]
    return {
        "ass_mark": f"{prefix}-{101 + index}",
        "profile_text": profile,
        "length_mm": Decimal(2000 + (index * 370) % 6000),
        "quantity": 3 if index == 0 else 1 + index % 3,
        "location": f"X{1 + index % 6}-X{2 + index % 6}/Y{1 + index % 4}-Y{2 + index % 4}",
        "member_kind": BomItem.MemberKind.BEAM if index % 2 == 0 else BomItem.MemberKind.GIRDER,
    }


def _full(items, start=None, stop=None):
    """(bom item, full quantity) pairs for a slice of items."""
    return [(item, item.quantity) for item in items[start:stop]]


def _truck(carrier, vehicle, plate, driver, phone, lines):
    return {
        "carrier": carrier,
        "vehicle": vehicle,
        "plate": plate,
        "driver": driver,
        "phone": phone,
        "lines": lines,
    }


class Command(BaseCommand):
    help = "Fill an empty database with fictional demo data (idempotent)."

    def handle(self, *args, **options):
        today = timezone.localdate()
        self.counts = {}
        self.ship_status = {}
        self.shipments_skipped = False
        self.project_by_name = {}
        self.section_by_key = {}
        self.carriers = {}
        self.vtypes = []
        with transaction.atomic():
            self._users()
            partners = self._partners()
            floors = self._floors()
            self._formed_parts()
            self._products()
            projects = self._projects(today, partners)
            self._order_items_and_prices(projects)
            sections = self._sections(today, projects, floors, partners)
            plants = self._plants(partners)
            self._assign_section_plants(plants)
            self._carriers()
            self.vtypes = list(VehicleType.objects.filter(is_active=True).order_by("sort_order", "name"))
            self._site_profiles()
            bom = self._bom_items()
            self._actuals(today, sections)
            self._shipments(today, plants, bom)
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
            self.project_by_name[spec["name"]] = project
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
                self.section_by_key[(project.name, name)] = section
                result.setdefault(project.pk, []).append(section)
        return result

    def _plants(self, partners):
        result = {}
        for name, site, operator_name, order in PLANTS:
            operator = partners[operator_name] if operator_name else None
            obj, created = Plant.objects.get_or_create(
                name=name, defaults={"site": site, "operator": operator, "sort_order": order}
            )
            self._count("제작장", created)
            result[name] = obj
        return result

    def _assign_section_plants(self, plants):
        """Set the plant on sections that have none yet (existing plant choices are kept)."""
        for (_, section_name), section in self.section_by_key.items():
            plant_name = SECTION_PLANTS.get(section_name)
            if plant_name:
                Section.objects.filter(pk=section.pk, plant__isnull=True).update(plant=plants[plant_name])

    def _carriers(self):
        for name, phone in CARRIERS:
            obj, created = Carrier.objects.get_or_create(name=name, defaults={"phone": phone})
            self._count("운송회사", created)
            self.carriers[name] = obj

    def _site_profiles(self):
        for project_name, spec in SITE_PROFILES.items():
            project = self.project_by_name[project_name]
            site, created = SiteProfile.objects.get_or_create(
                project=project,
                defaults={
                    "address": spec["address"],
                    "access_note": spec["access_note"],
                    "unloading_contact_name": spec["contact_name"],
                    "unloading_contact_phone": spec["contact_phone"],
                    "has_corner_plate": spec["corner_plate"],
                },
            )
            self._count("현장개요", created)
            site.allowed_vehicle_types.set(self.vtypes[: spec["vehicle_count"]])

    def _bom_items(self):
        """Create BOM rows (natural key: section + Ass Mark) and fill their areas."""
        result = {}
        for (project_name, section_name), (prefix, count, has_unreadable) in BOM_PLAN.items():
            section = self.section_by_key[(project_name, section_name)]
            items = []
            for index in range(count):
                spec = _bom_spec(prefix, index, count, has_unreadable)
                item, created = BomItem.objects.get_or_create(
                    section=section,
                    ass_mark=spec["ass_mark"],
                    defaults={
                        "profile_text": spec["profile_text"],
                        "product": Product.objects.filter(name=spec["profile_text"]).first(),
                        "length_mm": spec["length_mm"],
                        "quantity": spec["quantity"],
                        "location": spec["location"],
                        "member_kind": spec["member_kind"],
                    },
                )
                fill_bom_item(item)
                self._count("구간 BOM", created)
                items.append(item)
            result[(project_name, section_name)] = items
        return result

    def _vehicle(self, index):
        if not self.vtypes:
            return None
        return self.vtypes[index % len(self.vtypes)]

    def _load_truck(self, shipment, user, spec):
        truck = shipping.add_truck(shipment, user)
        truck.carrier = self.carriers[spec["carrier"]]
        truck.vehicle_type = self._vehicle(spec["vehicle"])
        truck.plate_no = spec["plate"]
        truck.driver_name = spec["driver"]
        truck.driver_phone = spec["phone"]
        truck.updated_by = user
        truck.save()
        for item, qty in spec["lines"]:
            shipping.add_line(truck, item, qty, user)
        return truck

    def _create_shipment(self, user, plants, project_name, plant_name, ship_date, trucks, action, reason=""):
        shipment = Shipment.objects.create(
            project=self.project_by_name[project_name],
            plant=plants[plant_name],
            ship_date=ship_date,
            dispatcher="데모 출하담당",
            created_by=user,
            updated_by=user,
        )
        for spec in trucks:
            self._load_truck(shipment, user, spec)
        if action == "confirm":
            shipping.confirm(shipment, user)
        elif action == "cancel":
            shipping.cancel(shipment, user, reason)
        status = Shipment.objects.get(pk=shipment.pk).status
        self.ship_status[status] = self.ship_status.get(status, 0) + 1
        self._count("출하", True)

    def _shipments(self, today, plants, bom):
        if Shipment.objects.exists():
            self.shipments_skipped = True
            return
        user = get_user_model().objects.get(username="admin")
        a1 = bom[(PROJECT_A, "A-1구간")]
        a2 = bom[(PROJECT_A, "A-2구간")]
        b1 = bom[(PROJECT_B, "B-1구간")]
        b2 = bom[(PROJECT_B, "B-2구간")]
        first = today.replace(day=1)
        monday = today - timedelta(days=today.weekday())

        # 1) Last month, plant 1공장 가동: all of A-1 (completes the section), two trucks.
        self._create_shipment(
            user, plants, PROJECT_A, "1공장 가동", first - timedelta(days=15),
            [
                _truck("데모운수", 0, "00가0001", "데모기사A", "010-0000-0101", _full(a1, 0, 8)),
                _truck("데모물류", 1, "00가0002", "데모기사B", "010-0000-0102", _full(a1, 8, 16)),
            ],
            "confirm",
        )
        # 2) Last month, plant 1공장 나동: several sections in one shipment (B-1 and B-2 partly).
        self._create_shipment(
            user, plants, PROJECT_B, "1공장 나동", first - timedelta(days=6),
            [
                _truck("데모운수", 2, "00가0003", "데모기사C", "010-0000-0103", _full(b1, 0, 4)),
                _truck("데모물류", 1, "00가0004", "데모기사D", "010-0000-0104", _full(b2, 0, 4)),
            ],
            "confirm",
        )
        # 3) This month, plant 1공장 다동: one A-2 piece split across two trucks.
        split = a2[0]  # quantity 3: 2 pieces on truck 1, 1 piece on truck 2
        self._create_shipment(
            user, plants, PROJECT_A, "1공장 다동", min(first + timedelta(days=2), today),
            [
                _truck("데모운수", 0, "00가0005", "데모기사E", "010-0000-0105",
                       [(split, 2), (a2[41], a2[41].quantity)]),
                _truck("데모물류", 2, "00가0006", "데모기사F", "010-0000-0106", [(split, 1)]),
            ],
            "confirm",
        )
        # 4) This month, plant 외주 라제작: one truck with 35 BOM rows (A-2, readable Profiles only).
        self._create_shipment(
            user, plants, PROJECT_A, "외주 라제작", min(first + timedelta(days=6), today),
            [_truck("데모운수", 1, "00가0007", "데모기사G", "010-0000-0107", _full(a2, 1, 36))],
            "confirm",
        )
        # 5) Planned this week (A-2 remaining rows).
        self._create_shipment(
            user, plants, PROJECT_A, "1공장 가동", monday + timedelta(days=4),
            [_truck("데모운수", 0, "00가0008", "데모기사H", "010-0000-0108", _full(a2, 36, 41))],
            "plan",
        )
        # 6) Planned next week (B-1 remaining rows).
        self._create_shipment(
            user, plants, PROJECT_B, "1공장 가동", monday + timedelta(days=11),
            [_truck("데모운수", 2, "00가0009", "데모기사I", "010-0000-0109", _full(b1, 4, 10))],
            "plan",
        )
        # 7) Canceled with reason.
        self._create_shipment(
            user, plants, PROJECT_B, "외주 라제작", monday + timedelta(days=4),
            [_truck("데모물류", 0, "00가0010", "데모기사J", "010-0000-0110", _full(b2, 4, 8))],
            "cancel",
            reason="데모: 발주 일정 변경으로 취소",
        )

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
            "제작장",
            "운송회사",
            "현장개요",
            "구간 BOM",
            "출하",
        ]
        self.stdout.write("")
        self.stdout.write("=== 데모 데이터 시드 결과 ===")
        for label in labels:
            created_n, total = self.counts.get(label, (0, 0))
            self.stdout.write(f"  {label}: 신규 {created_n}건 / 확인 {total}건")
        if self.shipments_skipped:
            self.stdout.write("  출하: 이미 출하 데이터가 있어 생성 단계를 건너뛰었습니다.")
        else:
            status_names = [("PLANNED", "예정"), ("SHIPPED", "출하"), ("CANCELED", "취소")]
            detail = " / ".join(f"{name} {self.ship_status.get(code, 0)}건" for code, name in status_names)
            self.stdout.write(f"  출하 상태별 신규: {detail}")
        self.stdout.write("")
        self.stdout.write("로그인 계정:")
        self.stdout.write(f"  admin (관리자) / {PASSWORD_ADMIN}")
        self.stdout.write(f"  demo1 / {PASSWORD_DEMO}")
        self.stdout.write(f"  demo2 / {PASSWORD_DEMO}")
        self.stdout.write("")
        self.stdout.write("※ 데모 비밀번호는 실제 사용 전에 반드시 변경하세요.")
