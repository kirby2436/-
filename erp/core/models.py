from decimal import Decimal

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL


class Stamped(models.Model):
    created_at = models.DateTimeField("등록일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)
    created_by = models.ForeignKey(
        User, verbose_name="등록자", null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    updated_by = models.ForeignKey(
        User, verbose_name="수정자", null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )

    class Meta:
        abstract = True


class Partner(Stamped):
    name = models.CharField("거래처명", max_length=100, unique=True)
    biz_no = models.CharField("사업자번호", max_length=20, blank=True)
    is_customer = models.BooleanField("매출처", default=False)
    is_contractor = models.BooleanField("원청", default=False)
    is_fabricator = models.BooleanField("외주제작처", default=False)
    contact_name = models.CharField("담당자", max_length=50, blank=True)
    phone = models.CharField("연락처", max_length=30, blank=True)
    is_active = models.BooleanField("사용", default=True)

    class Meta:
        verbose_name = "거래처"
        verbose_name_plural = "거래처"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Floor(Stamped):
    name = models.CharField("층", max_length=30, unique=True)
    sort_order = models.PositiveIntegerField("정렬", default=0)

    class Meta:
        verbose_name = "층"
        verbose_name_plural = "층"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class ProjectType(Stamped):
    name = models.CharField("프로젝트구분", max_length=50, unique=True)
    sort_order = models.PositiveIntegerField("정렬", default=0)
    is_active = models.BooleanField("사용", default=True)

    class Meta:
        verbose_name = "프로젝트구분"
        verbose_name_plural = "프로젝트구분"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Project(Stamped):
    class Status(models.TextChoices):
        ORDERED = "ORDERED", "수주"
        ACTIVE = "ACTIVE", "진행"
        DONE = "DONE", "완료"
        FORCED = "FORCED", "강제완료"

    code = models.CharField("프로젝트코드", max_length=20, unique=True, editable=False)
    name = models.CharField("프로젝트명", max_length=200)
    project_type = models.ForeignKey(ProjectType, verbose_name="프로젝트구분", on_delete=models.PROTECT, related_name="projects")
    status = models.CharField("상태", max_length=10, choices=Status.choices, default=Status.ORDERED)
    start_date = models.DateField("시작일", null=True, blank=True)
    end_date = models.DateField("종료일", null=True, blank=True)
    contract_date = models.DateField("계약일", null=True, blank=True)
    customer = models.ForeignKey(Partner, verbose_name="매출처", on_delete=models.PROTECT, related_name="customer_projects")
    customer_contact = models.CharField("매출처 담당자", max_length=50, blank=True)
    contractor = models.ForeignKey(
        Partner, verbose_name="원청", null=True, blank=True, on_delete=models.PROTECT, related_name="contractor_projects"
    )
    contractor_contact = models.CharField("원청 담당자", max_length=50, blank=True)
    manager = models.ForeignKey(
        User, verbose_name="담당자", null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_projects"
    )
    note = models.TextField("비고", blank=True)

    class Meta:
        verbose_name = "프로젝트"
        verbose_name_plural = "프로젝트"
        ordering = ["-code"]

    def __str__(self):
        return f"{self.code} {self.name}"

    @property
    def order_amount(self):
        return self.order_items.aggregate(total=models.Sum("amount"))["total"] or 0

    def save(self, *args, **kwargs):
        if not self.code:
            from core.services.codes import next_project_code

            self.code = next_project_code(self.contract_date or timezone.localdate())
        super().save(*args, **kwargs)


class OrderItem(Stamped):
    project = models.ForeignKey(Project, verbose_name="프로젝트", on_delete=models.CASCADE, related_name="order_items")
    seq = models.PositiveIntegerField("순번", default=1)
    item_name = models.CharField("품명", max_length=200)
    unit = models.CharField("단위", max_length=20, blank=True)
    quantity = models.DecimalField("수량", max_digits=14, decimal_places=2, default=0)
    amount = models.DecimalField("금액", max_digits=16, decimal_places=0, default=0)
    note = models.CharField("비고", max_length=200, blank=True)

    class Meta:
        verbose_name = "수주정보"
        verbose_name_plural = "수주정보"
        ordering = ["project", "seq"]

    def __str__(self):
        return f"{self.project.code} #{self.seq} {self.item_name}"

    @property
    def unit_price(self):
        """Amount divided by quantity (0 when quantity is 0)."""
        if not self.quantity:
            return 0
        return self.amount / self.quantity


class ItemPrice(Stamped):
    project = models.ForeignKey(Project, verbose_name="프로젝트", on_delete=models.CASCADE, related_name="item_prices")
    spec = models.CharField("규격", max_length=100)
    unit = models.CharField("단위", max_length=20, blank=True)
    unit_price = models.DecimalField("단가", max_digits=14, decimal_places=2, default=0)
    effective_from = models.DateField("적용 시작일", null=True, blank=True)
    note = models.CharField("비고", max_length=200, blank=True)

    class Meta:
        verbose_name = "품목 단가"
        verbose_name_plural = "품목 단가"
        ordering = ["project", "spec", "effective_from"]

    def __str__(self):
        return f"{self.project.code} {self.spec} {self.unit_price}"


class Section(Stamped):
    class MakeType(models.TextChoices):
        IN = "IN", "사내"
        OUT = "OUT", "사외"

    project = models.ForeignKey(Project, verbose_name="프로젝트", on_delete=models.CASCADE, related_name="sections")
    name = models.CharField("구간", max_length=100)
    floor = models.ForeignKey(Floor, verbose_name="층", null=True, blank=True, on_delete=models.PROTECT, related_name="sections")
    planned_ship_date = models.DateField("출고예정일")
    confirmed_ship_date = models.DateField("출고확정일", null=True, blank=True)
    is_completed = models.BooleanField("완료", default=False)
    make_type = models.CharField("제작구분", max_length=3, choices=MakeType.choices, default=MakeType.IN)
    fabricator = models.ForeignKey(
        Partner, verbose_name="외주제작처", null=True, blank=True, on_delete=models.PROTECT, related_name="fabricated_sections"
    )
    fabricator_reviewed_on = models.DateField("외주검토일", null=True, blank=True)
    plant = models.ForeignKey(
        "Plant", verbose_name="제작장", null=True, blank=True, on_delete=models.PROTECT, related_name="sections"
    )
    note = models.TextField("비고", blank=True)

    class Meta:
        verbose_name = "구간"
        verbose_name_plural = "구간"
        ordering = ["project", "planned_ship_date", "name"]
        constraints = [
            models.UniqueConstraint(fields=["project", "name", "floor"], name="uniq_section_project_name_floor"),
        ]

    def __str__(self):
        floor = f" {self.floor}" if self.floor_id else ""
        return f"{self.project.name} {self.name}{floor}"


def section_file_path(instance, filename):
    return f"section_files/{instance.project_id}/{filename}"


class SectionFile(Stamped):
    project = models.ForeignKey(Project, verbose_name="프로젝트", on_delete=models.CASCADE, related_name="files")
    section = models.ForeignKey(
        Section, verbose_name="구간", null=True, blank=True, on_delete=models.SET_NULL, related_name="files"
    )
    seq = models.PositiveIntegerField("순번", default=1)
    distributed_on = models.DateField("배포일자", default=timezone.localdate)
    design_changed_on = models.DateField("설계변경일", null=True, blank=True)
    change_note = models.CharField("변경내용", max_length=200, blank=True)
    file = models.FileField("파일", upload_to=section_file_path)
    note = models.CharField("비고", max_length=200, blank=True)

    class Meta:
        verbose_name = "도면/BOM 파일"
        verbose_name_plural = "도면/BOM 파일"
        ordering = ["project", "seq"]

    def __str__(self):
        return f"{self.project.code} #{self.seq}"


class FormedPart(Stamped):
    class Kind(models.TextChoices):
        BOTTOM = "BOTTOM", "하판"
        SIDE = "SIDE", "측판"

    kind = models.CharField("구분", max_length=6, choices=Kind.choices)
    name = models.CharField("규격", max_length=50)

    class Meta:
        verbose_name = "성형품 규격"
        verbose_name_plural = "성형품 규격"
        ordering = ["kind", "name"]
        constraints = [models.UniqueConstraint(fields=["kind", "name"], name="uniq_formedpart_kind_name")]

    def __str__(self):
        return f"{self.get_kind_display()} {self.name}"


class Product(Stamped):
    name = models.CharField("품목(Profile)", max_length=100, unique=True)
    bottom_thickness = models.DecimalField("하판 두께", max_digits=5, decimal_places=2, null=True, blank=True)
    side_thickness = models.DecimalField("측판 두께", max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "완제품 품목"
        verbose_name_plural = "완제품 품목"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_configured(self):
        return self.components.exists()


class ProductComponent(Stamped):
    product = models.ForeignKey(Product, verbose_name="품목", on_delete=models.CASCADE, related_name="components")
    kind = models.CharField("구분", max_length=6, choices=FormedPart.Kind.choices)
    slot = models.PositiveSmallIntegerField("순서", validators=[MinValueValidator(1), MaxValueValidator(3)])
    formed_part = models.ForeignKey(FormedPart, verbose_name="성형품 규격", on_delete=models.PROTECT, related_name="components")
    quantity = models.PositiveIntegerField("수량", default=1)
    non_standard = models.BooleanField("비규격", default=False)

    class Meta:
        verbose_name = "품목 구성"
        verbose_name_plural = "품목 구성"
        ordering = ["product", "kind", "slot"]
        constraints = [models.UniqueConstraint(fields=["product", "kind", "slot"], name="uniq_component_product_kind_slot")]

    def __str__(self):
        return f"{self.product} {self.get_kind_display()}{self.slot} {self.formed_part.name}"


class BomUpload(Stamped):
    section = models.ForeignKey(Section, verbose_name="구간", on_delete=models.CASCADE, related_name="bom_uploads")
    filename = models.CharField("파일명", max_length=255, blank=True)
    is_additional = models.BooleanField("추가 등록", default=False)
    row_count = models.PositiveIntegerField("행 수", default=0)

    class Meta:
        verbose_name = "BOM 업로드"
        verbose_name_plural = "BOM 업로드"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.section} {self.filename or '붙여넣기'} ({self.row_count}건)"


class BomItem(Stamped):
    class MemberKind(models.TextChoices):
        BEAM = "BEAM", "BEAM"
        GIRDER = "GIRDER", "GIRDER"

    section = models.ForeignKey(Section, verbose_name="구간", on_delete=models.CASCADE, related_name="bom_items")
    upload = models.ForeignKey(BomUpload, verbose_name="업로드", null=True, blank=True, on_delete=models.SET_NULL, related_name="items")
    ass_mark = models.CharField("Ass Mark", max_length=100)
    profile_text = models.CharField("Profile", max_length=100)
    product = models.ForeignKey(
        Product, verbose_name="품목", null=True, blank=True, on_delete=models.SET_NULL, related_name="bom_items"
    )
    length_mm = models.DecimalField("Length(mm)", max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField("수량")
    finish = models.CharField("FINISH", max_length=100, blank=True)
    location = models.CharField("LOCATION", max_length=100, blank=True)
    note = models.CharField("비고", max_length=200, blank=True)
    bottom_width = models.PositiveIntegerField("하판 폭(mm)", null=True, blank=True)
    side1_width = models.PositiveIntegerField("측판1 폭(mm)", null=True, blank=True)
    side2_width = models.PositiveIntegerField("측판2 폭(mm)", null=True, blank=True)
    unit_area = models.DecimalField("면적(㎡/개)", max_digits=12, decimal_places=2, null=True, blank=True)
    member_kind = models.CharField("보 구분", max_length=6, choices=MemberKind.choices, blank=True)

    class Meta:
        verbose_name = "구간 BOM"
        verbose_name_plural = "구간 BOM"
        ordering = ["section", "id"]

    def __str__(self):
        return f"{self.ass_mark} {self.profile_text}"

    @property
    def shipped_qty(self):
        """Quantity on non-canceled shipments (planned ones included)."""
        if "shipped_total" in self.__dict__:  # annotated by shipping.annotate_remaining
            return self.__dict__["shipped_total"]
        from core.services import shipping

        return shipping.shipped_qty(self)

    @property
    def remaining_qty(self):
        return self.quantity - self.shipped_qty


class ActivityType(Stamped):
    class ActualSource(models.TextChoices):
        MANUAL = "MANUAL", "수기 입력"
        BOM_UPLOAD = "BOM_UPLOAD", "BOM 업로드"
        SHIP = "SHIP", "출고 완료"

    code = models.CharField("코드", max_length=10, unique=True)
    name = models.CharField("Activity", max_length=50)
    sort_order = models.PositiveIntegerField("정렬", default=0)
    lead_days = models.IntegerField("리드타임(영업일)", default=0)
    base_activity = models.ForeignKey(
        "self", verbose_name="기준 Activity", null=True, blank=True, on_delete=models.PROTECT, related_name="dependents"
    )
    mark = models.CharField("표시", max_length=5, blank=True)
    actual_source = models.CharField("실적 반영", max_length=12, choices=ActualSource.choices, default=ActualSource.MANUAL)

    class Meta:
        verbose_name = "Activity 종류"
        verbose_name_plural = "Activity 종류"
        ordering = ["sort_order", "code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class SectionActivity(Stamped):
    section = models.ForeignKey(Section, verbose_name="구간", on_delete=models.CASCADE, related_name="activities")
    activity_type = models.ForeignKey(ActivityType, verbose_name="Activity", on_delete=models.PROTECT, related_name="section_activities")
    planned_date = models.DateField("계획일")
    actual_date = models.DateField("실적일", null=True, blank=True)

    class Meta:
        verbose_name = "구간 Activity"
        verbose_name_plural = "구간 Activity"
        ordering = ["section", "activity_type__sort_order"]
        constraints = [
            models.UniqueConstraint(fields=["section", "activity_type"], name="uniq_sectionactivity_section_type"),
        ]

    def __str__(self):
        return f"{self.section} {self.activity_type.code}"


class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", "등록"
        UPDATE = "update", "수정"
        DELETE = "delete", "삭제"

    user = models.ForeignKey(User, verbose_name="사용자", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    at = models.DateTimeField("일시", default=timezone.now, db_index=True)
    action = models.CharField("작업", max_length=10, choices=Action.choices)
    model = models.CharField("대상", max_length=100)
    object_id = models.CharField("대상 ID", max_length=50, blank=True)
    object_repr = models.CharField("대상 내용", max_length=200, blank=True)
    changes = models.JSONField("변경내용", default=dict, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        verbose_name = "변경 기록"
        verbose_name_plural = "변경 기록"
        ordering = ["-at", "-id"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.get_action_display()} {self.object_repr}"


class Plant(Stamped):
    name = models.CharField("제작장", max_length=100, unique=True)
    site = models.CharField("위치", max_length=100, blank=True)
    operator = models.ForeignKey(
        Partner, verbose_name="운영 업체(외주)", null=True, blank=True, on_delete=models.PROTECT, related_name="operated_plants"
    )
    is_active = models.BooleanField("사용", default=True)
    sort_order = models.PositiveIntegerField("정렬", default=0)

    class Meta:
        verbose_name = "제작장"
        verbose_name_plural = "제작장"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def is_outsourced(self):
        return self.operator_id is not None


class Carrier(Stamped):
    name = models.CharField("운송회사", max_length=100, unique=True)
    phone = models.CharField("연락처", max_length=30, blank=True)
    is_active = models.BooleanField("사용", default=True)

    class Meta:
        verbose_name = "운송회사"
        verbose_name_plural = "운송회사"
        ordering = ["name"]

    def __str__(self):
        return self.name


class VehicleType(Stamped):
    name = models.CharField("차종", max_length=50, unique=True)
    sort_order = models.PositiveIntegerField("정렬", default=0)
    is_active = models.BooleanField("사용", default=True)

    class Meta:
        verbose_name = "차종"
        verbose_name_plural = "차종"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class SiteProfile(Stamped):
    project = models.OneToOneField(Project, verbose_name="프로젝트", on_delete=models.CASCADE, related_name="site_profile")
    address = models.CharField("현장 주소", max_length=200, blank=True)
    access_note = models.TextField("진입 조건", blank=True)
    unloading_contact_name = models.CharField("하차 담당자", max_length=50, blank=True)
    unloading_contact_phone = models.CharField("하차 담당자 연락처", max_length=30, blank=True)
    has_corner_plate = models.BooleanField("코너 플레이트 있음", default=False)
    allowed_vehicle_types = models.ManyToManyField(
        VehicleType, verbose_name="진입 가능 차량", blank=True, related_name="site_profiles"
    )
    note = models.TextField("비고", blank=True)

    class Meta:
        verbose_name = "현장개요"
        verbose_name_plural = "현장개요"

    def __str__(self):
        return f"{self.project.code} 현장개요"


class Shipment(Stamped):
    class Status(models.TextChoices):
        PLANNED = "PLANNED", "예정"
        SHIPPED = "SHIPPED", "출하"
        CANCELED = "CANCELED", "취소"

    project = models.ForeignKey(Project, verbose_name="프로젝트", on_delete=models.PROTECT, related_name="shipments")
    ship_date = models.DateField("출하일")
    arrival_date = models.DateField("도착일", null=True, blank=True)
    plant = models.ForeignKey(
        Plant, verbose_name="제작장", null=True, blank=True, on_delete=models.PROTECT, related_name="shipments"
    )
    status = models.CharField("상태", max_length=10, choices=Status.choices, default=Status.PLANNED)
    dispatcher = models.CharField("출하 담당", max_length=50, blank=True)
    note = models.TextField("비고", blank=True)
    canceled_reason = models.CharField("취소 사유", max_length=200, blank=True)

    class Meta:
        verbose_name = "출하"
        verbose_name_plural = "출하"
        ordering = ["-ship_date", "-id"]

    def __str__(self):
        return f"{self.ship_date:%Y-%m-%d} {self.project.name} ({self.get_status_display()})"

    def _all_lines(self):
        # Uses prefetched data when present: prefetch_related("trucks__lines__bom_item__section__floor").
        return [line for truck in self.trucks.all() for line in truck.lines.all()]

    @property
    def truck_count(self):
        return len(self.trucks.all())

    @property
    def piece_count(self):
        return sum(line.quantity for line in self._all_lines())

    @property
    def total_area(self):
        return sum((line.effective_area for line in self._all_lines()), Decimal("0"))

    @property
    def section_label(self):
        """Floor and section names of all included sections, joined by ', ' in first-use order."""
        labels = []
        for line in self._all_lines():
            sec = line.bom_item.section
            label = f"{sec.floor.name} {sec.name}" if sec.floor_id else sec.name
            if label not in labels:
                labels.append(label)
        return ", ".join(labels)

    @property
    def title(self):
        """One-line title: date, site, floor/section, N대, M부재, area, plant (empty parts skipped)."""
        parts = [
            f"{self.ship_date:%Y-%m-%d}",
            self.project.name,
            self.section_label,
            f"{self.truck_count}대",
            f"{self.piece_count}부재",
            f"{self.total_area:,.2f}㎡",
            self.plant.name if self.plant_id else "",
        ]
        return " ".join(p for p in parts if p)


class ShipmentTruck(Stamped):
    shipment = models.ForeignKey(Shipment, verbose_name="출하", on_delete=models.CASCADE, related_name="trucks")
    seq = models.PositiveIntegerField("호차", default=1)
    supply_no = models.CharField("공급번호", max_length=30, unique=True, null=True, blank=True, editable=False)
    carrier = models.ForeignKey(
        Carrier, verbose_name="운송회사", null=True, blank=True, on_delete=models.PROTECT, related_name="trucks"
    )
    vehicle_type = models.ForeignKey(
        VehicleType, verbose_name="차종", null=True, blank=True, on_delete=models.PROTECT, related_name="trucks"
    )
    plate_no = models.CharField("차량번호", max_length=30, blank=True)
    driver_name = models.CharField("기사", max_length=50, blank=True)
    driver_phone = models.CharField("기사 연락처", max_length=30, blank=True)
    load_note = models.CharField("상차 메모", max_length=200, blank=True)

    class Meta:
        verbose_name = "출하 차량"
        verbose_name_plural = "출하 차량"
        ordering = ["shipment", "seq"]
        constraints = [models.UniqueConstraint(fields=["shipment", "seq"], name="uniq_shipmenttruck_shipment_seq")]

    def __str__(self):
        return f"{self.shipment_id}-{self.seq}호차"

    @property
    def piece_count(self):
        return sum(line.quantity for line in self.lines.all())

    @property
    def total_area(self):
        return sum((line.effective_area for line in self.lines.all()), Decimal("0"))


class ShipmentLine(Stamped):
    truck = models.ForeignKey(ShipmentTruck, verbose_name="차량", on_delete=models.CASCADE, related_name="lines")
    bom_item = models.ForeignKey(BomItem, verbose_name="BOM 부재", on_delete=models.PROTECT, related_name="shipment_lines")
    quantity = models.PositiveIntegerField("수량")
    area = models.DecimalField("면적(㎡)", max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "상차 부재"
        verbose_name_plural = "상차 부재"
        ordering = ["truck", "id"]
        constraints = [models.UniqueConstraint(fields=["truck", "bom_item"], name="uniq_shipmentline_truck_bom_item")]

    def __str__(self):
        return f"{self.truck} {self.bom_item.ass_mark} x{self.quantity}"

    @property
    def effective_area(self):
        """Stored area (set at confirm); for planned lines computed from the BOM item."""
        if self.area is not None:
            return self.area
        from core.services.area import line_area

        return line_area(self.bom_item.unit_area, self.quantity) or Decimal("0")
