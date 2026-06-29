"""Integration tests for the dashboard report HTML export endpoint."""

import datetime

import pytest
from django.contrib.auth import get_user_model
from django.urls import resolve, reverse

from apps.dashboard_reports.models import JobData, JobStatusChoices, TemplateMetadata

User = get_user_model()


def _seed_job_data() -> None:
    tm = TemplateMetadata.objects.create(
        template_id=1,
        template_name="Integration Template",
        time_taken_manually_execute_minutes=30,
        time_taken_create_automation_minutes=10,
    )
    now = datetime.datetime.now(datetime.UTC)
    JobData.objects.create(
        job_id=1,
        template_id=1,
        template_name="Integration Template",
        organization_id=1,
        project_id=1,
        project_name="Integration Project",
        status=JobStatusChoices.SUCCESSFUL,
        started=now - datetime.timedelta(hours=2),
        finished=now - datetime.timedelta(hours=1),
        elapsed=120,
        num_hosts=5,
        template_metadata=tm,
    )


@pytest.mark.integration
class TestExportHTMLURLRouting:
    """Verify the HTML export endpoint uses the same URL as CSV."""

    def test_export_url_resolves(self):
        match = resolve("/api/v1/dashboard_reports/report/export/")
        assert match is not None

    def test_export_url_reverse(self):
        url = reverse("dashboard_reports:report-export")
        assert url == "/api/v1/dashboard_reports/report/export/"


@pytest.mark.integration
@pytest.mark.django_db
class TestExportHTMLAccess:
    """Verify RBAC and method restrictions on HTML export."""

    base_params = {"period": "last_7_days", "tz": "UTC", "report_type": "summary", "export_format": "html"}

    def test_unauthenticated_returns_403(self, api_client):
        response = api_client.get(reverse("dashboard_reports:report-export"), data=self.base_params)
        assert response.status_code == 403

    def test_regular_user_returns_403(self, api_client):
        regular_user = User.objects.create_user(
            username="regularhtml",
            email="regularhtml@example.com",
            password="password123",  # noqa: S106
            is_superuser=False,
            is_staff=False,
        )
        api_client.force_authenticate(user=regular_user)
        response = api_client.get(reverse("dashboard_reports:report-export"), data=self.base_params)
        assert response.status_code == 403

    def test_superuser_returns_200(self, admin_client):
        response = admin_client.get(reverse("dashboard_reports:report-export"), data=self.base_params)
        assert response.status_code == 200

    def test_put_not_allowed(self, admin_client):
        response = admin_client.put(reverse("dashboard_reports:report-export"), data={})
        assert response.status_code == 405

    def test_delete_not_allowed(self, admin_client):
        response = admin_client.delete(reverse("dashboard_reports:report-export"))
        assert response.status_code == 405


@pytest.mark.integration
@pytest.mark.django_db
class TestExportHTMLResponseContract:
    """Verify response headers and rendered content for all report types."""

    @pytest.fixture(autouse=True)
    def setup(self, admin_client):
        _seed_job_data()
        self.client = admin_client

    def _get(self, report_type: str, **extra) -> object:
        params = {"period": "last_7_days", "tz": "UTC", "report_type": report_type, "export_format": "html"}
        params.update(extra)
        return self.client.get(reverse("dashboard_reports:report-export"), data=params)

    # --- Content-Type ---

    def test_summary_content_type_is_html(self):
        response = self._get("summary")
        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

    def test_roi_content_type_is_html(self):
        response = self._get("roi")
        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

    def test_trends_content_type_is_html(self):
        response = self._get("trends")
        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

    # --- No attachment header ---

    def test_summary_no_content_disposition_attachment(self):
        response = self._get("summary")
        assert response.status_code == 200
        assert "attachment" not in response.get("Content-Disposition", "")

    # --- HTML structure ---

    def test_summary_body_is_html(self):
        response = self._get("summary")
        assert b"<html" in response.content

    def test_roi_body_is_html(self):
        response = self._get("roi")
        assert b"<html" in response.content

    def test_trends_body_is_html(self):
        response = self._get("trends")
        assert b"<html" in response.content

    def test_summary_contains_print_button(self):
        response = self._get("summary")
        assert b"window.print()" in response.content

    def test_summary_contains_inline_styles(self):
        response = self._get("summary")
        assert b"<style>" in response.content

    def test_summary_contains_template_name(self):
        response = self._get("summary")
        assert b"Integration Template" in response.content

    def test_roi_contains_cost_label(self):
        response = self._get("roi")
        assert b"Cost savings" in response.content

    def test_trends_contains_date_column(self):
        response = self._get("trends")
        assert b"Date" in response.content

    # --- Filters ---

    def test_organization_filter_accepted(self):
        response = self._get("summary", organization=[1])
        assert response.status_code == 200

    def test_organization_filter_on_roi(self):
        response = self._get("roi", organization=[1])
        assert response.status_code == 200

    def test_organization_filter_on_trends(self):
        response = self._get("trends", organization=[1])
        assert response.status_code == 200

    # --- Error cases ---

    def test_invalid_period_returns_400(self):
        response = self.client.get(
            reverse("dashboard_reports:report-export"),
            data={"period": "last_999_days", "tz": "UTC", "report_type": "summary", "export_format": "html"},
        )
        assert response.status_code == 400

    def test_invalid_report_type_returns_400(self):
        response = self.client.get(
            reverse("dashboard_reports:report-export"),
            data={"period": "last_7_days", "tz": "UTC", "report_type": "bogus", "export_format": "html"},
        )
        assert response.status_code == 400

    def test_pdf_format_rejected(self):
        """pdf is no longer a valid export_format."""
        response = self.client.get(
            reverse("dashboard_reports:report-export"),
            data={"period": "last_7_days", "tz": "UTC", "report_type": "summary", "export_format": "pdf"},
        )
        assert response.status_code == 400
