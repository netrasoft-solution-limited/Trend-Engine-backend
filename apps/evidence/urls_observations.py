"""Observation screens, mounted at `observations/` by config/urls_ops.py."""
from django.urls import path

from . import views_observations as views

urlpatterns = [
    path("", views.ObservationListView.as_view(), name="ops-observations"),
    path("template.csv", views.ObservationTemplateView.as_view(), name="ops-observations-template"),
    path("import/", views.ObservationImportView.as_view(), name="ops-observations-import"),
    path("new/", views.ObservationEditView.as_view(), name="ops-observation-new"),
    path("<int:pk>/", views.ObservationEditView.as_view(), name="ops-observation"),
]
