from django.urls import path

from . import views

app_name = "bom"

urlpatterns = [
    path("", views.index, name="index"),
    path("sections/<int:pk>/", views.section_bom, name="section"),
    path("sections/<int:pk>/upload/", views.upload, name="upload"),
    path("sections/<int:pk>/resolve/", views.resolve, name="resolve"),
    path("items/<int:pk>/delete/", views.item_delete, name="item_delete"),
    path("items/<int:pk>/spec/", views.item_spec, name="item_spec"),
    path("items/<int:pk>/note/", views.item_note, name="item_note"),
]
