from django.shortcuts import render


def index(request):
    return render(request, "bom/stub.html", {"heading": "구간 BOM"})


def section_bom(request, pk):
    return render(request, "bom/stub.html", {"heading": "구간 BOM 상세"})
