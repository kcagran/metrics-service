"""Integration tests for the dashboard report export endpoint URL routing and response contracts."""

import datetime

import pytest
from django.contrib.auth import get_user_model
from django.urls import resolve, reverse

from apps.dashboard_reports.models import JobData, JobStatusChoices, TemplateMetadata

User = get_user_model()


def _seed_job_data() -> None:
    """Insert minimal TemplateMetadata + JobData rows for integration tests."""
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
class TestExportURLRouting:
    """Verify the export endpoint is correctly routed."""

    def test_export_url_resolves(self):
        match = resolve("/api/v1/dashboard_reports/report/export/")
        assert match is not None

    def test_export_url_reverse(self):
        url = reverse("dashboard_reports:report-export")
        assert url == "/api/v1/dashboard_reports/report/export/"


@pytest.mark.integration
@pytest.mark.django_db
class TestExportEndpointAccess:
    """Verify RBAC and authentication on the export endpoint."""

    base_params = {"period": "last_7_days", "tz": "UTC", "report_type": "summary", "export_format": "csv"}

    def test_regular_user_returns_403(self, api_client):
        regular_user = User.objects.create_user(
            username="regularuser",
            email="regularuser@example.com",
            password="password123",  # noqa: S106
            is_superuser=False,
            is_staff=False,
        )
        api_client.force_authenticate(user=regular_user)
        response = api_client.get("/api/v1/dashboard_reports/report/export/", data=self.base_params)
        assert response.status_code == 403

    def test_superuser_returns_200(self, admin_client):
        response = admin_client.get("/api/v1/dashboard_reports/report/export/", data=self.base_params)
        assert response.status_code == 200

    def test_put_not_allowed(self, admin_client):
        response = admin_client.put("/api/v1/dashboard_reports/report/export/", data={})
        assert response.status_code == 405

    def test_delete_not_allowed(self, admin_client):
        response = admin_client.delete("/api/v1/dashboard_reports/report/export/")
        assert response.status_code == 405


@pytest.mark.integration
@pytest.mark.django_db
class TestExportEndpointResponseContract:
    """Verify response headers and content for each report_type."""

    @pytest.fixture(autouse=True)
    def setup(self, admin_client):
        _seed_job_data()
        self.client = admin_client

    def _get_export(self, report_type: str, **extra_params) -> object:
        params = {"period": "last_7_days", "tz": "UTC", "report_type": report_type, "export_format": "csv"}
        params.update(extra_params)
        return self.client.get("/api/v1/dashboard_reports/report/export/", data=params)

    # --- Content-Type ---

    def test_summary_content_type_is_csv(self):
        response = self._get_export("summary")
        assert response.status_code == 200
        assert "text/csv" in response["Content-Type"]

    def test_roi_content_type_is_csv(self):
        response = self._get_export("roi")
        assert response.status_code == 200
        assert "text/csv" in response["Content-Type"]

    def test_trends_content_type_is_csv(self):
        response = self._get_export("trends")
        assert response.status_code == 200
        assert "text/csv" in response["Content-Type"]

    # --- Content-Disposition ---

    def test_summary_content_disposition_is_attachment(self):
        response = self._get_export("summary")
        assert "attachment" in response["Content-Disposition"]
        assert "summary" in response["Content-Disposition"]

    def test_roi_content_disposition_is_attachment(self):
        response = self._get_export("roi")
        assert "attachment" in response["Content-Disposition"]
        assert "roi" in response["Content-Disposition"]

    def test_trends_content_disposition_is_attachment(self):
        response = self._get_export("trends")
        assert "attachment" in response["Content-Disposition"]
        assert "trends" in response["Content-Disposition"]

    # --- CSV has headers ---

    def test_summary_csv_has_headers(self):
        response = self._get_export("summary")
        assert response.status_code == 200
        first_line = response.content.decode("utf-8").splitlines()[0]
        assert "Name" in first_line

    def test_roi_csv_has_headers(self):
        response = self._get_export("roi")
        assert response.status_code == 200
        first_line = response.content.decode("utf-8").splitlines()[0]
        assert "Cost Savings" in first_line

    def test_trends_csv_has_headers(self):
        response = self._get_export("trends")
        assert response.status_code == 200
        first_line = response.content.decode("utf-8").splitlines()[0]
        assert "Date" in first_line

    # --- Data rows ---

    def test_summary_csv_has_data_row(self):
        response = self._get_export("summary")
        assert response.status_code == 200
        lines = [line for line in response.content.decode("utf-8").splitlines() if line.strip()]
        assert len(lines) >= 2  # header + at least 1 data row

    def test_roi_csv_has_exactly_one_data_row(self):
        response = self._get_export("roi")
        assert response.status_code == 200
        lines = [line for line in response.content.decode("utf-8").splitlines() if line.strip()]
        assert len(lines) == 2  # header + exactly 1 aggregate row

    def test_trends_csv_has_data_row(self):
        response = self._get_export("trends")
        assert response.status_code == 200
        lines = [line for line in response.content.decode("utf-8").splitlines() if line.strip()]
        assert len(lines) >= 2

    # --- Error cases ---

    def test_invalid_period_returns_400(self):
        response = self.client.get(
            "/api/v1/dashboard_reports/report/export/",
            data={"period": "last_999_days", "tz": "UTC", "report_type": "summary", "export_format": "csv"},
        )
        assert response.status_code == 400

    def test_invalid_report_type_returns_400(self):
        response = self.client.get(
            "/api/v1/dashboard_reports/report/export/",
            data={"period": "last_7_days", "tz": "UTC", "report_type": "invalid", "export_format": "csv"},
        )
        assert response.status_code == 400

    def test_invalid_format_returns_400(self):
        response = self.client.get(
            "/api/v1/dashboard_reports/report/export/",
            data={"period": "last_7_days", "tz": "UTC", "report_type": "summary", "export_format": "xml"},
        )
        assert response.status_code == 400

    def test_pdf_format_rejected(self):
        """pdf is no longer valid; html is the print-ready alternative."""
        response = self.client.get(
            "/api/v1/dashboard_reports/report/export/",
            data={"period": "last_7_days", "tz": "UTC", "report_type": "summary", "export_format": "pdf"},
        )
        assert response.status_code == 400

    # --- Organization filter ---

    def test_organization_filter_accepted(self):
        """organization filter should be accepted without error."""
        response = self._get_export("summary", organization=[1])
        assert response.status_code == 200

    def test_organization_filter_on_roi(self):
        response = self._get_export("roi", organization=[1])
        assert response.status_code == 200

    def test_organization_filter_on_trends(self):
        response = self._get_export("trends", organization=[1])
        assert response.status_code == 200
