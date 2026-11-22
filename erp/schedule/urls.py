from django.urls import path

from . import views

app_name = "schedule"

urlpatterns = [
    path("", views.home, name="home"),
    path("schedule/", views.board, name="board"),
    path("schedule/sections/<int:pk>/", views.section_schedule, name="section"),
]
