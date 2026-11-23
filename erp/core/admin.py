from django.contrib import admin

from . import models


class StampedAdmin(admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(models.Partner)
class PartnerAdmin(StampedAdmin):
    list_display = ("name", "biz_no", "is_customer", "is_contractor", "is_fabricator", "contact_name", "phone", "is_active")
    list_filter = ("is_customer", "is_contractor", "is_fabricator", "is_active")
    search_fields = ("name", "biz_no", "contact_name")


@admin.register(models.Floor)
class FloorAdmin(StampedAdmin):
    list_display = ("name", "sort_order")
    list_editable = ("sort_order",)


@admin.register(models.ProjectType)
class ProjectTypeAdmin(StampedAdmin):
    list_display = ("name", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")


class OrderItemInline(admin.TabularInline):
    model = models.OrderItem
    extra = 0


class ItemPriceInline(admin.TabularInline):
    model = models.ItemPrice
    extra = 0


@admin.register(models.Project)
class ProjectAdmin(StampedAdmin):
    list_display = ("code", "name", "project_type", "status", "customer", "contractor", "manager", "start_date", "end_date")
    list_filter = ("status", "project_type")
    search_fields = ("code", "name", "customer__name", "contractor__name")
    readonly_fields = ("code",)
    inlines = [OrderItemInline, ItemPriceInline]


@admin.register(models.Section)
class SectionAdmin(StampedAdmin):
    list_display = ("project", "name", "floor", "planned_ship_date", "confirmed_ship_date", "is_completed", "make_type", "plant")
    list_filter = ("make_type", "is_completed", "plant")
    search_fields = ("name", "project__name", "project__code")


@admin.register(models.SectionFile)
class SectionFileAdmin(StampedAdmin):
    list_display = ("project", "seq", "section", "distributed_on", "design_changed_on", "file")
    search_fields = ("project__name", "project__code", "change_note")


@admin.register(models.FormedPart)
class FormedPartAdmin(StampedAdmin):
    list_display = ("kind", "name")
    list_filter = ("kind",)
    search_fields = ("name",)


class ProductComponentInline(admin.TabularInline):
    model = models.ProductComponent
    extra = 0


@admin.register(models.Product)
class ProductAdmin(StampedAdmin):
    list_display = ("name", "bottom_thickness", "side_thickness")
    search_fields = ("name",)
    inlines = [ProductComponentInline]


@admin.register(models.BomUpload)
class BomUploadAdmin(StampedAdmin):
    list_display = ("section", "filename", "is_additional", "row_count", "created_at")
    search_fields = ("filename", "section__name", "section__project__name")


@admin.register(models.BomItem)
class BomItemAdmin(StampedAdmin):
    list_display = ("section", "ass_mark", "profile_text", "product", "length_mm", "quantity", "unit_area", "member_kind", "finish", "location")
    list_filter = ("member_kind",)
    search_fields = ("ass_mark", "profile_text", "section__name", "section__project__name")
    raw_id_fields = ("section", "upload", "product")


@admin.register(models.Plant)
class PlantAdmin(StampedAdmin):
    list_display = ("name", "site", "operator", "is_active", "sort_order")
    list_filter = ("is_active",)
    list_editable = ("sort_order", "is_active")
    search_fields = ("name", "site", "operator__name")
    raw_id_fields = ("operator",)


@admin.register(models.Carrier)
class CarrierAdmin(StampedAdmin):
    list_display = ("name", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(models.VehicleType)
class VehicleTypeAdmin(StampedAdmin):
    list_display = ("name", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")


@admin.register(models.SiteProfile)
class SiteProfileAdmin(StampedAdmin):
    list_display = ("project", "address", "unloading_contact_name", "has_corner_plate")
    search_fields = ("project__name", "project__code", "address")
    raw_id_fields = ("project",)
    filter_horizontal = ("allowed_vehicle_types",)


class ShipmentTruckInline(admin.TabularInline):
    model = models.ShipmentTruck
    extra = 0
    readonly_fields = ("supply_no",)


@admin.register(models.Shipment)
class ShipmentAdmin(StampedAdmin):
    list_display = ("ship_date", "project", "plant", "status", "dispatcher")
    list_filter = ("status", "plant")
    search_fields = ("project__name", "project__code", "trucks__supply_no")
    raw_id_fields = ("project",)
    inlines = [ShipmentTruckInline]


class ShipmentLineInline(admin.TabularInline):
    model = models.ShipmentLine
    extra = 0
    raw_id_fields = ("bom_item",)


@admin.register(models.ShipmentTruck)
class ShipmentTruckAdmin(StampedAdmin):
    list_display = ("shipment", "seq", "supply_no", "carrier", "vehicle_type", "plate_no")
    search_fields = ("supply_no", "plate_no", "shipment__project__name")
    readonly_fields = ("supply_no",)
    raw_id_fields = ("shipment",)
    inlines = [ShipmentLineInline]


@admin.register(models.ActivityType)
class ActivityTypeAdmin(StampedAdmin):
    list_display = ("code", "name", "sort_order", "lead_days", "base_activity", "mark", "actual_source")
    list_editable = ("sort_order", "lead_days")


@admin.register(models.SectionActivity)
class SectionActivityAdmin(StampedAdmin):
    list_display = ("section", "activity_type", "planned_date", "actual_date")
    list_filter = ("activity_type",)
    search_fields = ("section__name", "section__project__name")
    raw_id_fields = ("section",)


@admin.register(models.AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("at", "user", "action", "model", "object_repr")
    list_filter = ("action", "model")
    search_fields = ("object_repr", "model", "user__username")
    readonly_fields = ("user", "at", "action", "model", "object_id", "object_repr", "changes")

    def has_add_permission(self, request):
        return False
