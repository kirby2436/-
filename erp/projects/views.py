from django.shortcuts import render


def project_list(request):
    return render(request, "projects/stub.html", {"heading": "프로젝트"})


def project_detail(request, pk):
    return render(request, "projects/stub.html", {"heading": "프로젝트 상세"})
