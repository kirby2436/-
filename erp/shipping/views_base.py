from django.shortcuts import render


def placeholder(request, title):
    """Temporary body for screens that are not built yet. Owners replace the whole view function."""
    return render(request, "shipping/_placeholder.html", {"title": title})
