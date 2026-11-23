"""Recalculate widths, unit area and member kind of every BOM item."""
from django.core.management.base import BaseCommand

from core.models import BomItem
from core.services.area import fill_bom_item


class Command(BaseCommand):
    help = "기존 BOM 전체의 폭, 면적, 보 구분을 다시 계산한다."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="저장하지 않고 건수만 센다.")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        total = changed = missing = 0
        for item in BomItem.objects.all().iterator():
            before = (item.bottom_width, item.side1_width, item.side2_width, item.unit_area, item.member_kind)
            fill_bom_item(item, save=False)
            after = (item.bottom_width, item.side1_width, item.side2_width, item.unit_area, item.member_kind)
            total += 1
            if item.unit_area is None:
                missing += 1
            if before != after:
                changed += 1
                if not dry:
                    fill_bom_item(item, save=True)
        suffix = " (저장 안 함)" if dry else ""
        self.stdout.write(f"전체 {total}건, 변경 {changed}건, 면적 미계산 {missing}건{suffix}")
