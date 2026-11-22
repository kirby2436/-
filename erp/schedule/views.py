from django.shortcuts import render


def home(request):
    return render(request, "schedule/stub.html", {"heading": "홈"})


def board(request):
    return render(request, "schedule/stub.html", {"heading": "진행현황"})


def section_schedule(request, pk):
    return render(request, "schedule/stub.html", {"heading": "구간 일정"})
