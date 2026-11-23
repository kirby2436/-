from django.urls import path

from . import views_master, views_report, views_shipment, views_statement

app_name = "shipping"

urlpatterns = [
    # 출하 (T3)
    path("", views_shipment.shipment_list, name="list"),
    path("new/", views_shipment.shipment_create, name="create"),
    path("<int:pk>/", views_shipment.shipment_detail, name="detail"),
    path("<int:pk>/edit/", views_shipment.shipment_edit, name="edit"),
    path("<int:pk>/load/", views_shipment.shipment_load, name="load"),
    path("<int:pk>/confirm/", views_shipment.shipment_confirm, name="confirm"),
    path("<int:pk>/cancel/", views_shipment.shipment_cancel, name="cancel"),
    path("trucks/<int:pk>/edit/", views_shipment.truck_edit, name="truck_edit"),
    # 기준정보 (T3)
    path("masters/plants/", views_master.plant_list, name="plant_list"),
    path("masters/plants/new/", views_master.plant_create, name="plant_create"),
    path("masters/plants/<int:pk>/edit/", views_master.plant_edit, name="plant_edit"),
    path("masters/carriers/", views_master.carrier_list, name="carrier_list"),
    path("masters/carriers/new/", views_master.carrier_create, name="carrier_create"),
    path("masters/carriers/<int:pk>/edit/", views_master.carrier_edit, name="carrier_edit"),
    path("masters/vehicle-types/", views_master.vehicle_type_list, name="vehicle_type_list"),
    path("masters/vehicle-types/new/", views_master.vehicle_type_create, name="vehicle_type_create"),
    path("masters/vehicle-types/<int:pk>/edit/", views_master.vehicle_type_edit, name="vehicle_type_edit"),
    # 거래명세서 (T4)
    path("<int:pk>/statement/", views_statement.statement, name="statement"),
    path("<int:pk>/statement.xlsx", views_statement.statement_xlsx, name="statement_xlsx"),
    # 출고 실적 (T5)
    path("reports/performance/", views_report.performance, name="performance"),
    path("reports/lines/", views_report.lines, name="lines"),
    path("reports/lines.xlsx", views_report.lines_xlsx, name="lines_xlsx"),
    path("reports/unshipped/", views_report.unshipped, name="unshipped"),
]
