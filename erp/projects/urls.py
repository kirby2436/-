from django.urls import path

from . import views

app_name = "projects"

urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_create, name="create"),
    path("quick-add/partner/", views.quick_add_partner, name="quick_partner"),
    path("quick-add/floor/", views.quick_add_floor, name="quick_floor"),
    path("<int:pk>/", views.project_detail, name="detail"),
    path("<int:pk>/edit/", views.project_edit, name="edit"),
    path("<int:pk>/site-profile/", views.site_profile, name="site_profile"),
    path("<int:pk>/order-items/", views.order_items_edit, name="order_items"),
    path("<int:pk>/prices/", views.prices_edit, name="prices"),
    path("<int:pk>/sections/", views.sections_edit, name="sections"),
    path("<int:pk>/files/", views.files, name="files"),
    path("<int:pk>/files/<int:file_pk>/delete/", views.file_delete, name="file_delete"),
]
