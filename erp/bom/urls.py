from django.urls import path

from . import views

app_name = "bom"

urlpatterns = [
    path("", views.index, name="index"),
    path("sections/<int:pk>/", views.section_bom, name="section"),
]
