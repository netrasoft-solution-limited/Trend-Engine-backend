"""URL patterns for the output builder, mounted at `output/` by config/urls_ops.py.

In this app rather than `apps.outputs` because the screens drive publish,
withdraw and deliver — the gate — which is a layer above outputs.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.OutputListView.as_view(), name="ops-outputs"),
    path("<int:pk>/", views.OutputDetailView.as_view(), name="ops-output"),
    path("<int:pk>/signoff/", views.OutputSignoffView.as_view(), name="ops-output-signoff"),
    path("<int:pk>/withdraw/", views.OutputWithdrawView.as_view(), name="ops-output-withdraw"),
    path("<int:pk>/deliver/", views.OutputDeliverView.as_view(), name="ops-output-deliver"),
    path(
        "<int:pk>/v<int:number>/approve/",
        views.OutputApproveView.as_view(),
        name="ops-output-approve",
    ),
    path(
        "<int:pk>/v<int:number>/publish/",
        views.OutputPublishView.as_view(),
        name="ops-output-publish",
    ),
    # A GET, because a download is a read and `<a href>` is how you offer one
    # without JavaScript. See the note in views.py.
    path(
        "<int:pk>/v<int:number>/export/<str:fmt>/",
        views.OutputExportView.as_view(),
        name="ops-output-export",
    ),
]
