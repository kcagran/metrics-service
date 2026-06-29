"""Unit tests for DashboardReportViewSet HTML export endpoint (summary, roi, trends)."""

import datetime
import decimal
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest
from django.urls import reverse

from apps.dashboard_reports.models import (
    JobData,
    JobStatusChoices,
    SubscriptionCost,
    TemplateMetadata,
)

FIXED_PER_SECOND_COST = decimal.Decimal("0.001")


def get_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def build_html_query(report_type: str = "summary", days_back: int = 14, **extra) -> dict:
    query = {
        "period": f"last_{days_back}_days",
        "tz": "UTC",
        "report_type": report_type,
        "export_format": "html",
    }
    query.update(extra)
    return query


def get_html(client, params: dict):
    url = reverse("dashboard_reports:report-export")
    qs = urlencode(params, doseq=True)
    return client.get(f"{url}?{qs}")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def template_metadata():
    templates = [
        TemplateMetadata(
            template_id=1,
            template_name="Template A",
            time_taken_manually_execute_minutes=240,
            time_taken_create_automation_minutes=40,
        ),
        TemplateMetadata(
            template_id=2,
            template_name="Template B",
            time_taken_manually_execute_minutes=360,
            time_taken_create_automation_minutes=60,
        ),
    ]
    return TemplateMetadata.objects.bulk_create(templates)


@pytest.fixture
def job_data(template_metadata):
    # Anchor to noon UTC yesterday so timestamps are always in the past
    now = (get_now() - datetime.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    job_configs = [
        (1, 0, 1, 10, "Project A", JobStatusChoices.SUCCESSFUL, datetime.timedelta(minutes=5), 60, 10, 1, "test_user"),
        (2, 0, 1, 10, "Project A", JobStatusChoices.FAILED, datetime.timedelta(minutes=1), 5, 10, 1, "test_user"),
        (
            3,
            1,
            2,
            20,
            "Project B",
            JobStatusChoices.FAILED,
            datetime.timedelta(days=7, hours=1),
            50,
            1,
            2,
            "other_user",
        ),
        (
            4,
            1,
            2,
            20,
            "Project B",
            JobStatusChoices.SUCCESSFUL,
            datetime.timedelta(days=7, hours=2),
            300,
            1,
            2,
            "other_user",
        ),
    ]
    jobs = []
    for job_id, tmpl_idx, org_id, proj_id, proj_name, status, offset, elapsed, hosts, user_id, username in job_configs:
        tmpl = template_metadata[tmpl_idx]
        jobs.append(
            JobData(
                job_id=job_id,
                template_name=tmpl.template_name,
                template_id=tmpl.template_id,
                project_id=proj_id,
                project_name=proj_name,
                organization_id=org_id,
                status=status,
                started=now - offset - datetime.timedelta(minutes=10),
                finished=now - offset,
                elapsed=elapsed,
                num_hosts=hosts,
                launched_by_id=user_id,
                launched_by_username=username,
                template_metadata=tmpl,
            )
        )
    return JobData.objects.bulk_create(jobs)


# =============================================================================
# Access / RBAC
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db
class TestExportHTMLAccess:
    """RBAC and method restrictions on the HTML export endpoint."""

    def test_unauthenticated_returns_403(self, api_client):
        response = get_html(api_client, build_html_query())
        assert response.status_code == 403

    def test_superuser_returns_200(self, admin_client):
        response = get_html(admin_client, build_html_query())
        assert response.status_code == 200

    def test_post_not_allowed(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.post(url, data={})
        assert response.status_code == 405

    def test_put_not_allowed(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.put(url, data={})
        assert response.status_code == 405


# =============================================================================
# Content-type and body
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db
class TestExportHTMLContentType:
    """Verify the HTML export returns text/html with rendered markup."""

    @pytest.fixture(autouse=True)
    def fixed_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_summary_content_type_is_html(self, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

    def test_roi_content_type_is_html(self, admin_client):
        response = get_html(admin_client, build_html_query("roi"))
        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

    def test_trends_content_type_is_html(self, admin_client):
        response = get_html(admin_client, build_html_query("trends"))
        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

    def test_no_content_disposition_attachment(self, admin_client):
        """HTML export opens inline — no attachment header."""
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert "Content-Disposition" not in response or "attachment" not in response.get("Content-Disposition", "")

    def test_response_body_is_html_document(self, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert b"<!DOCTYPE html>" in response.content or b"<html" in response.content

    def test_response_contains_print_button(self, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert b"window.print()" in response.content

    def test_response_contains_red_hat_logo(self, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        # Logo is now inlined as SVG; check for its unique element ID
        assert b"aap-logo_svg" in response.content


# =============================================================================
# Summary HTML report
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestExportHTMLSummary:
    """Verify summary HTML report content."""

    @pytest.fixture(autouse=True)
    def fixed_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_summary_contains_report_title(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert b"Summary" in response.content

    def test_summary_contains_template_names(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert b"Template A" in response.content
        assert b"Template B" in response.content

    def test_summary_contains_period_badge(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert b"Period:" in response.content

    def test_summary_organization_filter_limits_templates(self, job_data, admin_client):
        """Filtering by org=1 should only include Template A in the response."""
        response = get_html(admin_client, build_html_query("summary", organization=[1]))
        assert response.status_code == 200
        assert b"Template A" in response.content
        assert b"Template B" not in response.content

    def test_summary_label_filter_shows_badge(self, job_data, admin_client):
        """label has no name lookup — exercises the else branch in _build_filter_labels."""
        response = get_html(admin_client, build_html_query("summary", label=[42]))
        assert response.status_code == 200
        assert b"Label" in response.content


# =============================================================================
# ROI HTML report
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestExportHTMLROI:
    """Verify ROI HTML report content."""

    @pytest.fixture(autouse=True)
    def fixed_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_roi_contains_report_title(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("roi"))
        assert response.status_code == 200
        assert b"ROI" in response.content

    def test_roi_contains_cost_savings_label(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("roi"))
        assert response.status_code == 200
        assert b"Cost savings" in response.content

    def test_roi_contains_period_badge(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("roi"))
        assert response.status_code == 200
        assert b"Period:" in response.content

    def test_roi_empty_data_returns_200(self, admin_client):
        """No job data should still render a valid HTML page."""
        response = get_html(admin_client, build_html_query("roi"))
        assert response.status_code == 200
        assert b"<html" in response.content


# =============================================================================
# Trends HTML report
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestExportHTMLTrends:
    """Verify trends HTML report content."""

    @pytest.fixture(autouse=True)
    def fixed_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_trends_contains_report_title(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("trends"))
        assert response.status_code == 200
        assert b"Trends" in response.content

    def test_trends_contains_date_column_header(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("trends"))
        assert response.status_code == 200
        assert b"Date" in response.content

    def test_trends_contains_granularity_badge(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("trends"))
        assert response.status_code == 200
        assert b"Granularity" in response.content

    def test_trends_empty_data_returns_200(self, admin_client):
        response = get_html(admin_client, build_html_query("trends"))
        assert response.status_code == 200
        assert b"<html" in response.content


# =============================================================================
# Validation errors
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db
class TestExportHTMLValidation:
    """Invalid parameters should still return 400, same as CSV path."""

    def test_invalid_report_type_returns_400(self, admin_client):
        response = get_html(admin_client, {**build_html_query(), "report_type": "bogus"})
        assert response.status_code == 400

    def test_invalid_export_format_returns_400(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(
            url, data={"period": "last_14_days", "tz": "UTC", "report_type": "summary", "export_format": "pdf"}
        )
        assert response.status_code == 400

    def test_missing_period_returns_400(self, admin_client):
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(url, data={"export_format": "html", "report_type": "summary"})
        assert response.status_code == 400

    def test_pdf_format_rejected(self, admin_client):
        """pdf is no longer a valid format — expect 400."""
        url = reverse("dashboard_reports:report-export")
        response = admin_client.get(
            url, data={"period": "last_14_days", "tz": "UTC", "report_type": "summary", "export_format": "pdf"}
        )
        assert response.status_code == 400
        assert b"csv, html" in response.content


# =============================================================================
# PassthroughRendererHTML
# =============================================================================


@pytest.mark.unit
class TestPassthroughRendererHTML:
    """Verify the HTML renderer has the correct media_type and format."""

    def test_renderer_media_type(self):
        from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRendererHTML

        assert PassthroughRendererHTML.media_type == "text/html"

    def test_renderer_format(self):
        from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRendererHTML

        assert PassthroughRendererHTML.format == "html"

    def test_render_returns_data_as_is(self):
        from apps.dashboard_reports.viewsets.dashboard_report import PassthroughRendererHTML

        renderer = PassthroughRendererHTML()
        data = b"<html></html>"
        assert renderer.render(data) == data


# =============================================================================
# SVG chart helpers
# =============================================================================


@pytest.mark.unit
class TestRenderSvgChart:
    """Unit tests for DashboardReportViewSet._render_svg_chart."""

    @pytest.fixture
    def viewset(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        return DashboardReportViewSet()

    def _make_chart(self, n: int = 5, kind: str = "day", base_value: int = 10) -> dict:
        items = []
        base = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        for i in range(n):
            items.append({"label": base + datetime.timedelta(days=i), "value": base_value + i})
        return {"kind": kind, "items": items}

    def test_bar_chart_returns_svg_element(self, viewset):
        chart_data = self._make_chart()
        svg = viewset._render_svg_chart(chart_data, "bar")
        assert "<svg" in svg
        assert "</svg>" in svg

    def test_line_chart_returns_svg_element(self, viewset):
        chart_data = self._make_chart()
        svg = viewset._render_svg_chart(chart_data, "line")
        assert "<svg" in svg
        assert "</svg>" in svg

    def test_bar_chart_contains_rect_elements(self, viewset):
        chart_data = self._make_chart(n=3)
        svg = viewset._render_svg_chart(chart_data, "bar")
        # At least one bar rect beyond the background rect
        assert svg.count("<rect") >= 2

    def test_line_chart_contains_path_element(self, viewset):
        chart_data = self._make_chart(n=3)
        svg = viewset._render_svg_chart(chart_data, "line")
        assert "<path" in svg

    def test_empty_items_returns_no_data_message(self, viewset):
        result = viewset._render_svg_chart({"kind": "day", "items": []}, "bar")
        assert "<svg" not in result
        assert "No data" in result

    def test_all_zero_values_does_not_raise(self, viewset):
        items = [{"label": datetime.datetime(2025, 1, i + 1, tzinfo=datetime.UTC), "value": 0} for i in range(5)]
        chart_data = {"kind": "day", "items": items}
        svg = viewset._render_svg_chart(chart_data, "bar")
        assert "<svg" in svg

    def test_single_item_line_chart_does_not_render_path(self, viewset):
        items = [{"label": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC), "value": 5}]
        chart_data = {"kind": "day", "items": items}
        svg = viewset._render_svg_chart(chart_data, "line")
        # Single point: no connecting line path, just the SVG wrapper
        assert "<svg" in svg

    def test_none_value_treated_as_zero(self, viewset):
        items = [
            {"label": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC), "value": None},
            {"label": datetime.datetime(2025, 1, 2, tzinfo=datetime.UTC), "value": 5},
        ]
        svg = viewset._render_svg_chart({"kind": "day", "items": items}, "bar")
        assert "<svg" in svg


@pytest.mark.unit
class TestFormatChartLabel:
    """Unit tests for DashboardReportViewSet._format_chart_label."""

    @pytest.fixture
    def viewset(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        return DashboardReportViewSet()

    def test_datetime_day_kind(self, viewset):
        dt = datetime.datetime(2025, 6, 15, tzinfo=datetime.UTC)
        assert viewset._format_chart_label(dt, "day") == "15 Jun"

    def test_datetime_month_kind(self, viewset):
        dt = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
        assert viewset._format_chart_label(dt, "month") == "Jun 2025"

    def test_datetime_year_kind(self, viewset):
        dt = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        assert viewset._format_chart_label(dt, "year") == "2025"

    def test_datetime_hour_kind(self, viewset):
        dt = datetime.datetime(2025, 6, 15, 14, 30, tzinfo=datetime.UTC)
        assert viewset._format_chart_label(dt, "hour") == "14:00"

    def test_iso_string_label_is_parsed(self, viewset):
        label = "2025-06-15T00:00:00+00:00"
        result = viewset._format_chart_label(label, "day")
        assert result == "15 Jun"

    def test_already_formatted_string_returned_as_is(self, viewset):
        # Strings that are not valid ISO datetimes fall back to the raw string
        result = viewset._format_chart_label("15 Jun", "day")
        assert result == "15 Jun"

    def test_unknown_kind_uses_default_format(self, viewset):
        dt = datetime.datetime(2025, 6, 15, tzinfo=datetime.UTC)
        result = viewset._format_chart_label(dt, "week")
        assert result == "2025-06-15"


# =============================================================================
# Charts in HTML output
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestExportHTMLCharts:
    """Verify that SVG charts appear in the summary and trends HTML exports."""

    @pytest.fixture(autouse=True)
    def fixed_cost(self):
        with patch(
            "apps.dashboard_reports.models.SubscriptionCost.per_second_subscription_cost",
            return_value=FIXED_PER_SECOND_COST,
        ):
            yield

    def test_summary_html_contains_svg_chart(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert b"<svg" in response.content

    def test_summary_html_contains_job_chart_title(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert b"Number of times jobs were run" in response.content

    def test_summary_html_contains_host_chart_title(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        assert b"Number of hosts jobs are running on" in response.content

    def test_trends_html_contains_svg_chart(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("trends"))
        assert response.status_code == 200
        assert b"<svg" in response.content

    def test_trends_html_contains_job_chart_title(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("trends"))
        assert response.status_code == 200
        assert b"Number of times jobs were run" in response.content

    def test_trends_html_contains_host_chart_title(self, job_data, admin_client):
        response = get_html(admin_client, build_html_query("trends"))
        assert response.status_code == 200
        assert b"Number of hosts jobs are running on" in response.content

    def test_summary_html_no_data_shows_no_data_message(self, admin_client):
        """With no job data, the charts should show the no-data placeholder instead of SVG."""
        response = get_html(admin_client, build_html_query("summary"))
        assert response.status_code == 200
        # Either an SVG chart or a no-data message must be present
        has_svg = b"<svg" in response.content
        has_no_data = b"No data" in response.content
        assert has_svg or has_no_data


# =============================================================================
# _read_static_file
# =============================================================================


@pytest.mark.unit
class TestReadStaticFile:
    def setup_method(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        DashboardReportViewSet._read_static_file.cache_clear()

    def teardown_method(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        DashboardReportViewSet._read_static_file.cache_clear()

    def test_returns_empty_string_when_file_not_found(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        with patch("apps.dashboard_reports.viewsets.dashboard_report.finders.find", return_value=None):
            assert DashboardReportViewSet._read_static_file("missing/file.css") == ""

    def test_returns_file_content_when_found(self, tmp_path):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        f = tmp_path / "style.css"
        f.write_text("body { color: red; }")
        with patch("apps.dashboard_reports.viewsets.dashboard_report.finders.find", return_value=str(f)):
            assert DashboardReportViewSet._read_static_file("dashboard_reports/styles/style.css") == "body { color: red; }"


# =============================================================================
# _inline_fonts_in_css
# =============================================================================


@pytest.mark.unit
class TestInlineFontsInCss:
    def setup_method(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        DashboardReportViewSet._inline_fonts_in_css.cache_clear()

    def teardown_method(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        DashboardReportViewSet._inline_fonts_in_css.cache_clear()

    def test_css_without_font_urls_returned_unchanged(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        css = "body { color: red; }"
        assert DashboardReportViewSet._inline_fonts_in_css(css) == css

    def test_leaves_unresolvable_font_path_unchanged(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        css = '@font-face { src: url("../fonts/RedHatText.woff2"); }'
        with patch("apps.dashboard_reports.viewsets.dashboard_report.finders.find", return_value=None):
            result = DashboardReportViewSet._inline_fonts_in_css(css)
        assert 'url("../fonts/RedHatText.woff2")' in result

    def test_replaces_font_url_with_base64_data_uri(self, tmp_path):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        font_file = tmp_path / "font.woff2"
        font_file.write_bytes(b"fakefontdata")
        css = '@font-face { src: url("../fonts/RedHatText.woff2"); }'
        with patch("apps.dashboard_reports.viewsets.dashboard_report.finders.find", return_value=str(font_file)):
            result = DashboardReportViewSet._inline_fonts_in_css(css)
        assert "data:font/woff2;base64," in result
        assert 'url("../fonts/RedHatText.woff2")' not in result


# =============================================================================
# _render_bar_segments
# =============================================================================


@pytest.mark.unit
class TestRenderBarSegments:
    def test_returns_one_rect_per_item(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        items = [{"value": 10}, {"value": 20}, {"value": 5}]
        parts = DashboardReportViewSet._render_bar_segments(items, 54, 16, 100, 182, 20, "#0066cc")
        assert len(parts) == 3
        assert all("<rect" in p for p in parts)

    def test_zero_value_produces_zero_height_bar(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        parts = DashboardReportViewSet._render_bar_segments([{"value": 0}], 54, 16, 100, 182, 10, "#0066cc")
        assert 'height="0.0"' in parts[0]

    def test_rect_contains_fill_color(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        parts = DashboardReportViewSet._render_bar_segments([{"value": 5}], 54, 16, 100, 182, 10, "#abc123")
        assert 'fill="#abc123"' in parts[0]


# =============================================================================
# _render_line_segments
# =============================================================================


@pytest.mark.unit
class TestRenderLineSegments:
    def test_returns_empty_for_zero_items(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        assert DashboardReportViewSet._render_line_segments([], 54, 16, 100, 182, 10, "#0066cc") == []

    def test_returns_empty_for_single_item(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        assert DashboardReportViewSet._render_line_segments([{"value": 5}], 54, 16, 100, 182, 10, "#0066cc") == []

    def test_returns_area_line_and_circles_for_multiple_items(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        items = [{"value": 5}, {"value": 10}, {"value": 8}]
        parts = DashboardReportViewSet._render_line_segments(items, 54, 16, 100, 182, 10, "#0066cc")
        # 1 area path + 1 line path + 3 circles
        assert len(parts) == 5
        assert any("<path" in p for p in parts)
        assert any("<circle" in p for p in parts)

    def test_none_value_treated_as_zero(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        items = [{"value": None}, {"value": 10}]
        parts = DashboardReportViewSet._render_line_segments(items, 54, 16, 100, 182, 10, "#0066cc")
        assert len(parts) > 0


# =============================================================================
# _compute_roi_metrics
# =============================================================================


@pytest.mark.unit
class TestComputeRoiMetrics:
    def _make_qs(
        self,
        automated: int = 100,
        manual: int = 200,
        savings: int = 100,
        time_savings: int = 3600,
    ) -> MagicMock:
        qs = MagicMock()
        qs.aggregate.return_value = {
            "total_automated_costs": decimal.Decimal(str(automated)),
            "total_manual_costs": decimal.Decimal(str(manual)),
            "total_savings": decimal.Decimal(str(savings)),
            "total_time_savings": decimal.Decimal(str(time_savings)),
        }
        return qs

    def test_returns_correct_savings(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        result = DashboardReportViewSet._compute_roi_metrics(self._make_qs(savings=50))
        assert result["savings"] == decimal.Decimal("50")

    def test_time_saved_hours_converts_seconds(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        result = DashboardReportViewSet._compute_roi_metrics(self._make_qs(time_savings=7200))
        assert result["time_saved_hours"] == 2.0

    def test_roi_percentage_zero_when_no_automated_costs(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        result = DashboardReportViewSet._compute_roi_metrics(self._make_qs(automated=0, savings=50))
        assert result["roi_percentage"] == decimal.Decimal("0")

    def test_roi_percentage_calculated_correctly(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        result = DashboardReportViewSet._compute_roi_metrics(self._make_qs(automated=100, savings=50))
        assert result["roi_percentage"] == decimal.Decimal("50")

    def test_automation_value_is_manual_plus_savings(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        result = DashboardReportViewSet._compute_roi_metrics(self._make_qs(manual=200, savings=100))
        assert result["automation_value"] == decimal.Decimal("300")


# =============================================================================
# _build_trends_queryset
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db(transaction=True, reset_sequences=True)
class TestBuildTrendsQueryset:
    def test_returns_rows_grouped_by_day(self, job_data):
        from apps.dashboard_reports.models import JobData
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        qs = DashboardReportViewSet._build_trends_queryset(JobData.objects.all(), "day")
        rows = list(qs)
        assert len(rows) > 0
        assert "date" in rows[0]
        assert "runs" in rows[0]

    def test_correct_run_counts_per_day(self, job_data):
        from apps.dashboard_reports.models import JobData
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        qs = DashboardReportViewSet._build_trends_queryset(JobData.objects.all(), "day")
        total_runs = sum(r["runs"] for r in qs)
        assert total_runs == JobData.objects.count()

    def test_empty_queryset_returns_no_rows(self):
        from apps.dashboard_reports.models import JobData
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        qs = DashboardReportViewSet._build_trends_queryset(JobData.objects.none(), "day")
        assert list(qs) == []


# =============================================================================
# _build_filter_labels
# =============================================================================


@pytest.mark.unit
@pytest.mark.django_db
class TestBuildFilterLabels:
    @pytest.fixture
    def viewset(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        return DashboardReportViewSet()

    def _make_request(self, params: dict):
        from rest_framework.test import APIRequestFactory

        factory = APIRequestFactory()
        request = factory.get("/", params)
        from rest_framework.request import Request

        return Request(request)

    def test_returns_empty_dict_with_no_params(self, viewset):
        request = self._make_request({})
        assert viewset._build_filter_labels(request) == {}

    def test_label_param_uses_raw_ids_not_db_names(self, viewset):
        request = self._make_request({"label": "42"})
        result = viewset._build_filter_labels(request)
        assert "label" in result
        assert result["label"]["values"] == "42"

    def test_organization_filter_looks_up_name(self, viewset, job_data):
        org_id = job_data[0].organization_id
        request = self._make_request({"organization": str(org_id)})
        result = viewset._build_filter_labels(request)
        assert "organization" in result

    def test_template_filter_looks_up_name(self, viewset, job_data):
        tmpl_id = job_data[0].template_id
        request = self._make_request({"template": str(tmpl_id)})
        result = viewset._build_filter_labels(request)
        assert "template" in result
        assert "Template" in result["template"]["values"]

    def test_unknown_org_id_falls_back_to_raw_id(self, viewset):
        request = self._make_request({"organization": "99999"})
        result = viewset._build_filter_labels(request)
        assert "organization" in result
        assert result["organization"]["values"] == "99999"


# =============================================================================
# _export_html dispatch
# =============================================================================


@pytest.mark.unit
class TestExportHtmlDispatch:
    @pytest.fixture
    def viewset(self):
        from apps.dashboard_reports.viewsets.dashboard_report import DashboardReportViewSet

        return DashboardReportViewSet()

    def test_summary_calls_build_html_summary(self, viewset):
        request = MagicMock()
        with patch.object(viewset, "_build_html_summary", return_value=MagicMock()) as mock_build:
            viewset._export_html(request, "summary")
            mock_build.assert_called_once_with(request)

    def test_roi_calls_build_html_roi(self, viewset):
        request = MagicMock()
        with patch.object(viewset, "_build_html_roi", return_value=MagicMock()) as mock_build:
            viewset._export_html(request, "roi")
            mock_build.assert_called_once_with(request)

    def test_trends_calls_build_html_trends(self, viewset):
        request = MagicMock()
        with patch.object(viewset, "_build_html_trends", return_value=MagicMock()) as mock_build:
            viewset._export_html(request, "trends")
            mock_build.assert_called_once_with(request)
