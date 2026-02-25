"""
URL configuration for dashboard_reports app.

This file defines URL patterns for the dashboard reports endpoints.
The dashboard endpoints are mounted at /api/v1/ for automation-reports integration.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.dashboard_reports.views import (
    DashboardReportViewSet,
    LabelsViewSet,
    OrganizationsViewSet,
    ProjectsViewSet,
    JobTemplatesViewSet
)

app_name = "dashboard_reports"

# Main dashboard router for report endpoints
dashboard_router = DefaultRouter()
dashboard_router.register(r"labels", LabelsViewSet, basename="labels")
dashboard_router.register(r"organization", OrganizationsViewSet, basename="organizations")
dashboard_router.register(r"projects", ProjectsViewSet, basename="projects")
dashboard_router.register(r"templates", JobTemplatesViewSet, basename="templates")
dashboard_router.register(r"report", DashboardReportViewSet, basename="dashboard-report")

urlpatterns = [
    # Dashboard report endpoints at /api/v1/
    path("api/v1/", include(dashboard_router.urls)),
]
