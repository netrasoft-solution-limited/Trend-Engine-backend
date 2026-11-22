from django.urls import path

from . import views_cycles

urlpatterns = [
    path("<slug:slug>/", views_cycles.CycleView.as_view(), name="ops-cycle"),
    path("<slug:slug>/setup/", views_cycles.SetupView.as_view(), name="ops-cycle-setup"),
]
