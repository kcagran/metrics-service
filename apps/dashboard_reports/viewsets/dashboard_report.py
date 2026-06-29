"""ViewSet for the main dashboard report endpoint, providing job run metrics and cost analytics."""

import base64
import csv
import decimal
import html as html_module
import logging
import re
from datetime import UTC, datetime
from functools import cache, wraps
from typing import Any

from ansible_base.rbac.api.permissions import IsSystemAdminOrAuditor
from dateutil.relativedelta import relativedelta
from django.contrib.staticfiles import finders
from django.db import models
from django.db.models import Case, Count, F, OuterRef, Q, QuerySet, Subquery, Sum, Value, When
from django.db.models.functions import Cast, Coalesce, Trunc
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django_generate_series.models import generate_series  # PostgreSQL-only; revisit if other DB support is added
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import filters, serializers, status
from rest_framework.decorators import action
from rest_framework.renderers import BaseRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.dashboard_reports.awx_queries import fetch_templates, fetch_organizations, fetch_projects, fetch_labels
from apps.dashboard_reports.filters import (
    CustomReportFilter,
    DateFilter,
    validate_custom_period_dates,
    get_filter_options,
    get_or_filter_options,
)
from apps.dashboard_reports.models import JobData, JobHostSummary, JobLabel, JobStatusChoices, SubscriptionCost
from apps.dashboard_reports.serializers import (
    ReportDetailSerializer,
    ReportSerializer,
)
from apps.dashboard_reports.utils import sec2time
from apps.tasks.api_utils import build_error_response
from apps.tasks.utils import get_db_connection

logger = logging.getLogger(__name__)


PAGINATION_QUERY_PARAMETERS = [
    OpenApiParameter(
        name="page",
        type=OpenApiTypes.INT,
        default=1,
        location=OpenApiParameter.QUERY,
        required=True,
        description="Page number (default: 1)",
    ),
    OpenApiParameter(
        name="page_size",
        type=OpenApiTypes.INT,
        default=10,
        location=OpenApiParameter.QUERY,
        required=True,
        description="Results per page (default: 10)",
    ),
]


def make_passthrough_renderer(media_type: str, fmt: str) -> type:
    return type(
        "PassthroughRenderer",
        (BaseRenderer,),
        {
            "media_type": media_type,
            "format": fmt,
            "render": lambda self, data, *a, **kw: data,
        },
    )


PassthroughRendererHTML = make_passthrough_renderer(media_type="text/html", fmt="html")
PassthroughRenderer = make_passthrough_renderer(media_type="text/csv", fmt="csv")
HTML_CONTENT_TYPE = "text/html; charset=UTF-8"


def parse_period_param(
    period_str: str | None, param_name: str, timezone_str: str, start_date_str: str | None, end_date_str: str | None
) -> tuple[datetime | None, datetime | None, str]:
    """
    Validates ISO date string for query parameters.
    """
    if not period_str:
        return None, None, f"{param_name} is required"
    if period_str not in DateFilter.to_list():
        return None, None, f"Invalid {param_name}: {period_str}. Must be one of: {DateFilter.to_list()}"

    try:
        if period_str == DateFilter.CUSTOM.value:
            validate_custom_period_dates(start_date_str, end_date_str)
            start_date, end_date = DateFilter.custom_range_to_start_date_end_date(
                start_date_str=start_date_str,
                end_date_str=end_date_str,
                tz_string=timezone_str,
            )
        else:
            start_date, end_date = DateFilter.to_start_date_end_date(value=period_str, tz_string=timezone_str)
    except ValueError as e:
        if period_str == DateFilter.CUSTOM.value:
            msg = f"Invalid start_date or end_date: {str(e)}"
        else:
            msg = f"Invalid {param_name}: {period_str}. Error: {str(e)}"
        logger.error(msg)
        return None, None, msg

    return start_date, end_date, ""


def require_date_range(view_func):
    """Decorator that validates and injects start_date/end_date into view kwargs from the period query parameter."""

    @wraps(view_func)
    def wrapper(view, *args, **kwargs) -> Response:
        """Validate date range and delegate to the wrapped view function."""
        # Use request.GET for query parameters (works for DRF and Django)
        period = view.request.GET.get("period", None)
        timezone_str = view.request.GET.get("tz", "UTC") or "UTC"
        start_date_str = view.request.GET.get("start_date", None)
        end_date_str = view.request.GET.get("end_date", None)

        start_date, end_date, period_err_msg = parse_period_param(
            period_str=period,
            param_name="period",
            timezone_str=timezone_str,
            start_date_str=start_date_str,
            end_date_str=end_date_str,
        )
        if start_date is None or end_date is None:
            error_response = build_error_response(period_err_msg, status_code=400)
            return Response(error_response, status=status.HTTP_400_BAD_REQUEST)

        if start_date > end_date:
            error_response = build_error_response(
                f"Invalid date range: start_date ({start_date.isoformat()}) must be before end_date ({end_date.isoformat()}).",
                status_code=400,
            )
            return Response(error_response, status=status.HTTP_400_BAD_REQUEST)

        # Inject parsed dates into kwargs for downstream use
        view.kwargs["start_date"] = start_date
        view.kwargs["end_date"] = end_date
        view.kwargs["period"] = period
        view.kwargs["tz"] = timezone_str

        return view_func(view, *args, **kwargs)

    return wrapper


@extend_schema_view(
    list=extend_schema(
        summary="Get a list of report data for dashboard (with pagination).",
        description="Returns a paginated report data for dashboard.",
    ),
    retrieve=extend_schema(
        summary="Not supported for aggregated dashboard report data.",
        description="This endpoint is intentionally not supported and returns 405.",
        responses={
            405: inline_serializer(
                name="DashboardReportRetrieveNotAllowed",
                fields={"detail": serializers.CharField()},
            )
        },
    ),
)
class DashboardReportViewSet(ReadOnlyModelViewSet):
    """
    ViewSet for dashboard reporting and chart data aggregation.
    Provides endpoints for report data, summary, chart, and top users/projects.

    Endpoints:
        GET /api/v1/dashboard_reports/report/ - List all data (paginated)
        GET /api/v1/dashboard_reports/report/details/ - Get  report summary, chart data, and top users/projects
        GET /api/v1/dashboard_reports/report/export/ - Export report data as CSV

    Query Parameters:
        page (int): Page number (default: 1)
        page_size (int): Results per page (default: 10)
        period (string): Filter for report start date. Options: 'last_7_days', 'last_14_days', 'last_30_days', 'last_60_days', 'last_90_days' (required)
        tz (string): Timezone string (default: UTC)
        organization (int): Filter by organization ID (multiple allowed)
        template (int): Filter by job template ID (multiple allowed)
        label (int): Filter by label ID (multiple allowed)
        project (int): Filter by project ID (multiple allowed)
        ordering (str): Field to order by (e.g. "template_metadata__template_name", "successful_runs", "savings", etc.)
        report_type (string): Type of report to export. Options: 'summary', 'roi', 'trends' (default: 'summary', used for export endpoint)
        export_format (string): Export file format. Options: 'csv' (default: 'csv', used for export endpoint)
    """

    permission_classes = [IsSystemAdminOrAuditor]

    detail_query_parameters = [
        *PAGINATION_QUERY_PARAMETERS,
        OpenApiParameter(
            name="period",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=True,
            enum=DateFilter.to_list(),
            description="Filter for report period.",
        ),
        OpenApiParameter(
            name="tz",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            default="UTC",
            required=True,
            description="Timezone string (default: UTC)",
        ),
        OpenApiParameter(
            name="start_date",
            type=OpenApiTypes.DATE,
            location=OpenApiParameter.QUERY,
            required=False,
            description="Start date in 'YYYY-MM-DD' format. Required when period is 'custom'.",
        ),
        OpenApiParameter(
            name="end_date",
            type=OpenApiTypes.DATE,
            location=OpenApiParameter.QUERY,
            required=False,
            description="End date in 'YYYY-MM-DD' format. Required when period is 'custom'.",
        ),
        OpenApiParameter(
            name="organization",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="Filter by organization ID (multiple allowed)",
        ),
        OpenApiParameter(
            name="template",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="Filter by template ID (multiple allowed)",
        ),
        OpenApiParameter(
            name="label",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="Filter by label ID (multiple allowed)",
        ),
        OpenApiParameter(
            name="project",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="Filter by project ID (multiple allowed)",
        ),
        OpenApiParameter(
            name="or__organization",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="OR filter by organization ID (multiple allowed)",
        ),
        OpenApiParameter(
            name="or__template",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="OR filter by template ID (multiple allowed)",
        ),
        OpenApiParameter(
            name="or__label",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="OR filter by label ID (multiple allowed)",
        ),
        OpenApiParameter(
            name="or__project",
            type=OpenApiTypes.INT,
            location=OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="OR filter by project ID (multiple allowed)",
        ),
        OpenApiParameter(
            name="ordering",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description="Field to order by (e.g. 'template_metadata__template_name', 'successful_runs', 'savings', etc.)",
        ),
    ]

    export_query_parameters = [
        *[p for p in detail_query_parameters if p not in PAGINATION_QUERY_PARAMETERS],
        OpenApiParameter(
            name="report_type",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            default="summary",
            enum=["summary", "roi", "trends"],
            description="Type of report to export. Options: 'summary', 'roi', 'trends'.",
        ),
        OpenApiParameter(
            name="export_format",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            default="csv",
            enum=["csv"],
            description="Export file format. Options: 'csv'.",
        ),
    ]

    versioning_class = None  # Disable versioning for this viewset
    serializer_class = ReportSerializer

    filter_backends = [CustomReportFilter, filters.OrderingFilter]

    # Maximum number of top users/projects to return in details endpoint
    # TODO: Consider moving to a Django setting if UIs need to configure this value.
    TOP_RESULTS_LIMIT = 5
    TRENDS_DATE_FORMATS: dict[str, str] = {
        "hour": "%Y-%m-%d %H:00",
        "day": "%Y-%m-%d",
        "week": "%Y-%m-%d",
        "month": "%Y-%m",
        "year": "%Y",
    }

    ordering_fields: list[str] = [
        "template_metadata__template_name",
        "successful_runs",
        "failed_runs",
        "num_hosts",
        "elapsed",
        "manual_time",
        "manual_costs",
        "automated_costs",
        "savings",
        "runs",
    ]

    ordering = ["template_metadata__template_name"]

    def get_serializer_class(self) -> type:
        """Return ReportDetailSerializer for the details action, otherwise the default serializer."""
        if self.action == "details":
            return ReportDetailSerializer
        return super().get_serializer_class()

    def _build_aggregated_queryset(self, base_qs: QuerySet[JobData]) -> QuerySet:
        """
        Applies grouping, cost, and time annotations to a pre-filtered JobData queryset.
        Must be called after all CustomReportFilter filtering has been applied to base_qs,
        because the custom filter methods (after_date, organizations, etc.) are only available
        on JobDataQuerySet, not on the ValuesQuerySet this method returns.
        """
        subscription_cost = SubscriptionCost.get()
        average_cost_employee_minute = subscription_cost.cost_employee_per_minute
        start_date = self.kwargs.get("start_date")
        end_date = self.kwargs.get("end_date")

        aap_subscription_per_second = subscription_cost.per_second_subscription_cost(start_date, end_date)
        enable_template_creation_time = subscription_cost.include_template_creation_time_in_costs

        coalesced_manual_minutes = Coalesce(F("time_taken_manually_execute_minutes"), Value(0))
        coalesced_create_minutes = Coalesce(F("time_taken_create_automation_minutes"), Value(0))

        if enable_template_creation_time:
            automated_costs = (coalesced_create_minutes * average_cost_employee_minute) + (
                F("elapsed") * aap_subscription_per_second
            )
            time_savings = F("manual_time") - F("elapsed") - (coalesced_create_minutes * decimal.Decimal(60))
        else:
            automated_costs = F("elapsed") * aap_subscription_per_second
            time_savings = F("manual_time") - F("elapsed")

        # Manual minutes are a per-job-run estimate (see TemplateMetadata defaults); scale by runs,
        # not Sum(num_hosts), which would mis-apply one run's minute estimate across all hosts.
        manual_costs = F("runs") * coalesced_manual_minutes * average_cost_employee_minute
        manual_time = F("runs") * (coalesced_manual_minutes * 60)

        return (
            # Exclude rows without template_metadata: ReportSerializer.id sources from
            # template_metadata_id, so null rows would cause serialization to fail.
            base_qs.filter(template_metadata_id__isnull=False)
            .values(
                "template_metadata_id",
                "template_metadata__template_name",
                time_taken_manually_execute_minutes=F("template_metadata__time_taken_manually_execute_minutes"),
                time_taken_create_automation_minutes=F("template_metadata__time_taken_create_automation_minutes"),
            )
            .annotate(
                runs=Count("id"),
                successful_runs=Count("id", filter=Q(status=JobStatusChoices.SUCCESSFUL)),
                failed_runs=Count("id", filter=Q(status=JobStatusChoices.FAILED)),
                elapsed=Sum("elapsed"),
                num_hosts=Sum("num_hosts"),
            )
            .annotate(
                automated_costs=automated_costs,
                manual_costs=manual_costs,
                manual_time=manual_time,
            )
            .annotate(
                time_savings=time_savings,
                savings=(F("manual_costs") - F("automated_costs")),
            )
        )

    def get_queryset(self) -> QuerySet:
        """
        Returns the filtered, grouped, and annotated queryset for dashboard reporting.
        CustomReportFilter is applied here on the raw JobDataQuerySet (before .values()),
        so its custom manager methods (after_date, organizations, etc.) are available.
        filter_queryset() then only applies OrderingFilter to the resulting ValuesQuerySet.
        """
        base_qs = self._filter_raw_jobdata_queryset(JobData.objects.all())
        return self._build_aggregated_queryset(base_qs)

    def filter_queryset(self, queryset: QuerySet) -> QuerySet:
        """
        Applies only OrderingFilter to the aggregated queryset.
        CustomReportFilter is intentionally excluded here because it has already been applied
        to the raw JobDataQuerySet inside get_queryset(), before grouping and aggregation.
        Calling it again on the ValuesQuerySet would cause an AttributeError since the custom
        manager methods (after_date, organizations, etc.) are not available post-.values().
        """
        for backend in self.filter_backends:
            if backend is filters.OrderingFilter:
                queryset = backend().filter_queryset(self.request, queryset, self)
        return queryset

    def _inject_label_ids(self, rows: list[dict], base_qs: QuerySet[JobData]) -> None:
        """Add a label_ids list to each aggregated row by querying JobLabel.

        Runs a single batch query scoped to the same filtered JobData queryset used
        to build the report, so labels only reflect jobs actually included in the
        current date range / org / project / label filters. Mutates each row dict in
        place. Avoids annotating the ValuesQuerySet (which would inflate aggregates
        via the FK JOIN).
        """
        label_map: dict[int, list[int]] = {}
        for item in (
            JobLabel.objects.filter(job_data__in=base_qs, label_id__isnull=False)
            .values("job_data__template_metadata_id", "label_id")
            .distinct()
        ):
            tid = item["job_data__template_metadata_id"]
            if tid is not None:
                label_map.setdefault(tid, []).append(item["label_id"])
        for row in rows:
            row["label_ids"] = label_map.get(row.get("template_metadata_id"), [])

    @require_date_range
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Returns paginated report data for dashboard, including label IDs per template row.
        """
        base_qs = self._filter_raw_jobdata_queryset(JobData.objects.all())
        queryset = self.filter_queryset(self._build_aggregated_queryset(base_qs))
        page = self.paginate_queryset(queryset)
        if page is not None:
            self._inject_label_ids(page, base_qs)
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        rows = list(queryset)
        self._inject_label_ids(rows, base_qs)
        serializer = self.get_serializer(rows, many=True)
        return Response(serializer.data)

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Not supported: this viewset returns aggregated-per-template data, not individual JobData records.
        Inheriting retrieve from ReadOnlyModelViewSet would cause a PostgreSQL error because get_object()
        appends .filter(pk=...) to a ValuesQuerySet grouped by template_metadata_id, and the job id
        column is absent from the GROUP BY clause.
        """
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def _get_date_range_and_kind(self) -> tuple[datetime | None, datetime | None, str | None]:
        """
        Determine the chart time granularity (hour/day/month/year) from the injected date range.

        Normalizes start_date and end_date to UTC and snaps start_date to the appropriate boundary
        (e.g. hour=0 for day-level, day=1 for month-level). end_date is advanced to the start of
        the *next* period boundary so that generate_series always emits a bucket for the final
        period — truncating end_date backward would omit data that falls after the truncated
        boundary (e.g. a "year" end of Dec 2025 truncated to Jan 1 2025 could lose the 2025 bucket
        when generate_series stops exactly there). Returns (start_date, end_date, kind).
        """
        start_date = self.kwargs.get("start_date")
        end_date = self.kwargs.get("end_date")
        if start_date is None or end_date is None:
            return None, None, None

        start_date = start_date.astimezone(UTC)
        end_date = end_date.astimezone(UTC)

        diff = abs(end_date - start_date)

        diff_relative = relativedelta(end_date, start_date)
        total_months = diff_relative.months + diff_relative.years * 12

        # Trigger 'year' kind for any range spanning different years
        if total_months > 12:
            kind = "year"
            start_date = start_date.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            # Advance to Jan 1 of the *next* year so the current year's bucket is included.
            end_date = end_date.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0) + relativedelta(
                years=1
            )
        elif diff.days < 1:
            # Use hourly bucketing only for ranges UNDER 24 hours (< 1 day).
            # Exactly 24-hour ranges (diff.days == 1) fall through to daily bucketing below.
            # This is intentional: a 24h range like "June 15 10:00 → June 16 10:00" spans
            # 2 calendar days, so daily bucketing prevents data misattribution.
            kind = "hour"
            start_date = start_date.replace(minute=0, second=0, microsecond=0)
            # Advance to the start of the *next* hour so the current hour's bucket is included.
            end_date = end_date.replace(minute=0, second=0, microsecond=0) + relativedelta(hours=1)
        elif diff.days <= 45:
            kind = "day"
            start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
            # Advance to midnight of the *next* day so today's bucket is included.
            end_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0) + relativedelta(days=1)
        else:
            kind = "month"
            start_date = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # Advance to the 1st of the *next* month so the current month's bucket is included.
            end_date = end_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + relativedelta(months=1)
        return start_date, end_date, kind

    def _filter_raw_jobdata_queryset(self, queryset: QuerySet[JobData]) -> QuerySet[JobData]:
        """Apply all filter backends except OrderingFilter to the raw JobDataQuerySet."""
        for backend in self.filter_backends:
            if backend is filters.OrderingFilter:
                continue
            queryset = backend().filter_queryset(self.request, queryset, self)
        return queryset

    def _prepare_chart_querysets(self, kind: str) -> tuple[QuerySet, QuerySet]:
        """
        Prepares job and host chart querysets for the given kind.
        Returns: (job_chart_qs, host_chart_qs)
        """
        qs = self._filter_raw_jobdata_queryset(JobData.objects.all())
        qs = qs.values(date=Trunc(expression="finished", kind=kind, output_field=models.DateTimeField())).filter(
            date=OuterRef("term")
        )
        job_chart_qs = qs.annotate(runs=Count("id")).values("runs").order_by()
        host_chart_qs = qs.annotate(num_hosts=Sum("num_hosts")).values("num_hosts").order_by()
        return job_chart_qs, host_chart_qs

    def _format_chart_result(self, date_sequence_queryset: QuerySet) -> dict[str, Any]:
        """
        Formats the chart result from the date sequence queryset.
        Returns: dict with host_chart and job_chart.
        """
        result = {"host_chart": {"kind": "", "items": []}, "job_chart": {"kind": "", "items": []}}
        for row in date_sequence_queryset:
            result["job_chart"]["items"].append({"label": row.term, "value": row.runs})
            result["host_chart"]["items"].append({"label": row.term, "value": row.hosts})
        return result

    def get_chart_data(self) -> dict[str, Any]:
        """
        Returns chart data for jobs and hosts over a time range, grouped by kind (hour, day, month, year).
        """
        start_date, end_date, kind = self._get_date_range_and_kind()
        if not start_date or not end_date or not kind:
            return {"host_chart": {"kind": "", "items": []}, "job_chart": {"kind": "", "items": []}}

        job_chart_qs, host_chart_qs = self._prepare_chart_querysets(kind)

        # Generate a time series from start_date to end_date using PostgreSQL's generate_series function.
        # - start/stop: Define the date range boundaries
        # - step: Interval between each point (e.g., "1 hours", "1 days", "1 months", "1 years")
        # - span: Number of intervals to include beyond the stop date (buffer for edge cases)
        # - output_field: Ensures the series generates DateTimeField values
        # The resulting queryset contains a 'term' column with each timestamp in the series.
        date_sequence_queryset = generate_series(
            start=start_date, stop=end_date, step=f"1 {kind}s", span=5, output_field=models.DateTimeField
        ).annotate(
            # For each time point in the series, fetch the count of job runs (or 0 if none)
            runs=Coalesce(Subquery(job_chart_qs), Value(0)),
            # For each time point in the series, fetch the sum of hosts (or 0 if none)
            hosts=Coalesce(Subquery(host_chart_qs), Value(0)),
        )

        result = self._format_chart_result(date_sequence_queryset)
        result["host_chart"]["kind"] = kind
        result["job_chart"]["kind"] = kind
        return result

    @staticmethod
    @cache
    def _read_static_file(relative_path: str) -> str:
        """Return the content of a static file by its relative static path, or '' if not found.
        Results are cached for the process lifetime — static files never change at runtime."""
        abs_path = finders.find(relative_path)
        if not abs_path:
            return ""
        with open(abs_path, encoding="utf-8") as fh:
            return fh.read()

    @staticmethod
    @cache
    def _inline_fonts_in_css(css: str) -> str:
        """
        Replace url('../fonts/<path>') references with base64 woff2 data URIs so the
        exported HTML is fully self-contained and renders correctly when saved offline.
        Unresolvable font paths are left unchanged (graceful fallback to system fonts).
        """

        def _font_data_uri(match: re.Match) -> str:
            font_path = match.group(1)
            abs_path = finders.find(f"dashboard_reports/fonts/{font_path}")
            if not abs_path:
                return match.group(0)
            with open(abs_path, "rb") as fh:
                encoded = base64.b64encode(fh.read()).decode("ascii")
            return f'url("data:font/woff2;base64,{encoded}")'

        return re.sub(r'url\("\.\./fonts/([^"]+)"\)', _font_data_uri, css)

    def _build_inline_context(self) -> dict:
        """
        Return context entries for a fully self-contained HTML export:
          inline_css  — style.css with @font-face url() replaced by base64 data URIs
          inline_logo — RedHatLogo.svg content for direct <svg> embedding
        """
        css = self._read_static_file("dashboard_reports/styles/style.css")
        if css:
            css = self._inline_fonts_in_css(css)
        logo = self._read_static_file("dashboard_reports/images/RedHatLogo.svg")
        # mark_safe: both values are read from trusted static files on disk — no user input reaches these strings.
        return {
            "inline_css": mark_safe(css),  # noqa: S308
            "inline_logo": mark_safe(logo),  # noqa: S308
        }

    @staticmethod
    def _format_chart_label(label: Any, kind: str) -> str:
        """Format a chart label (datetime or string) for display on a chart axis."""
        date_formats = {
            "year": "%Y",
            "month": "%b %Y",
            "day": "%d %b",
            "hour": "%H:00",
        }
        fmt = date_formats.get(kind, "%Y-%m-%d")
        try:
            if isinstance(label, datetime):
                return label.strftime(fmt)
            parsed = datetime.fromisoformat(str(label).replace("Z", "+00:00"))
            return parsed.strftime(fmt)
        except (ValueError, TypeError, AttributeError):
            return str(label)

    @staticmethod
    def _render_bar_segments(
        items: list, pad_l: float, pad_t: float, slot_w: float, plot_h: float, max_val: int, color: str
    ) -> list:
        """Return SVG <rect> elements for a bar chart."""
        bar_w = max(slot_w * 0.62, 2.0)
        parts = []
        for i, item in enumerate(items):
            val = int(item.get("value") or 0)
            bh = (val / max_val) * plot_h
            bx = pad_l + i * slot_w + (slot_w - bar_w) / 2
            by = pad_t + plot_h - bh
            parts.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" fill="{color}" opacity="0.82" rx="2"/>'
            )
        return parts

    @staticmethod
    def _render_line_segments(
        items: list, pad_l: float, pad_t: float, slot_w: float, plot_h: float, max_val: int, color: str
    ) -> list:
        """Return SVG path and circle elements for a line chart with a filled area underneath."""
        pts = [
            (pad_l + i * slot_w + slot_w / 2, pad_t + plot_h - (int(item.get("value") or 0) / max_val) * plot_h)
            for i, item in enumerate(items)
        ]
        if len(pts) <= 1:
            return []
        area = (
            f"M {pts[0][0]:.1f},{pad_t + plot_h} "
            + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts)
            + f" L {pts[-1][0]:.1f},{pad_t + plot_h} Z"
        )
        line_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        return [
            f'<path d="{area}" fill="{color}" opacity="0.10"/>',
            f'<path d="{line_d}" fill="none" stroke="{color}" stroke-width="2"/>',
            *[f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{color}"/>' for cx, cy in pts],
        ]

    def _render_svg_chart(self, chart_data: dict, chart_type: str) -> str:
        """
        Render chart data as an inline SVG bar or line chart.
        Mirrors the two chart cards shown on the automation-dashboard UI:
          - chart_type='bar'  → job runs per period (blue bars)
          - chart_type='line' → host runs per period (blue line + filled area)
        Returns an empty-state message string when there is no data.
        """
        items = chart_data.get("items", [])
        kind = chart_data.get("kind", "day")

        if not items:
            # mark_safe: static string with no user input.
            return mark_safe(
                '<p style="color:#888;font-style:italic;text-align:center;margin:24px 0">No data available for this period.</p>'
            )

        width, height = 700, 250
        pad_l, pad_r, pad_t, pad_b = 54, 16, 16, 52
        plot_w = width - pad_l - pad_r
        plot_h = height - pad_t - pad_b

        values = [int(item.get("value") or 0) for item in items]
        max_val = max(values) if values else 1
        if max_val == 0:
            max_val = 1

        n = len(items)
        slot_w = plot_w / n

        svg = []
        svg.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" style="width:100%;display:block">'
        )
        svg.append(f'<rect width="{width}" height="{height}" fill="#fff" rx="3"/>')

        # Horizontal grid lines
        steps = 4
        for i in range(steps + 1):
            y = pad_t + plot_h - (i / steps) * plot_h
            label_val = int(max_val * i / steps)
            svg.append(
                f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" stroke="#e8e8e8" stroke-width="1"/>'
            )
            svg.append(
                f'<text x="{pad_l - 5}" y="{y + 4:.1f}" text-anchor="end" '
                f'font-family="inherit" font-size="9" fill="#888">{label_val:,}</text>'
            )

        color = "#0066cc"

        if chart_type == "bar":
            svg.extend(self._render_bar_segments(items, pad_l, pad_t, slot_w, plot_h, max_val, color))
        else:
            svg.extend(self._render_line_segments(items, pad_l, pad_t, slot_w, plot_h, max_val, color))

        # X axis labels — show at most 8 to avoid crowding
        tick_step = max(1, n // 8)
        for i in range(0, n, tick_step):
            label = html_module.escape(self._format_chart_label(items[i].get("label", ""), kind))
            cx = pad_l + i * slot_w + slot_w / 2
            by = pad_t + plot_h
            svg.append(
                f'<text x="{cx:.1f}" y="{by + 20}" text-anchor="middle" '
                f'font-family="inherit" font-size="8" fill="#888" '
                f'transform="rotate(-30 {cx:.1f} {by + 20})">{label}</text>'
            )

        # Axes
        svg.append(
            f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" stroke="#ccc" stroke-width="1.5"/>'
        )
        svg.append(
            f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" y2="{pad_t + plot_h}" stroke="#ccc" stroke-width="1.5"/>'
        )

        svg.append("</svg>")
        # mark_safe: SVG is built entirely from server-side Python — no user data is interpolated into the markup.
        return mark_safe("\n".join(svg))  # noqa: S308

    @staticmethod
    def _csv_safe(value: Any) -> Any:
        """
        Prepend a single quote to any string value that starts with special
        characters to prevent CSV injection.
        """
        if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return f"'{value}"
        return value

    def _write_summary_csv(self, response: HttpResponse, qs: QuerySet[JobData]) -> None:
        """
        Write summary data to CSV, including template name, number of job executions,
        hosts executed on, time taken to execute manually, time taken to create
        automation (if enabled), running time, automated costs, manual costs, and savings.
        """
        enable_template_creation_time = SubscriptionCost.get().include_template_creation_time_in_costs

        writer = csv.writer(response)
        headers = [
            "Name",
            "Number of job executions",
            "Hosts executions",
            "Time taken to manually execute (minutes)",
        ]

        if enable_template_creation_time:
            headers.append("Time taken to create automation (minutes)")

        headers += [
            "Running time (seconds)",
            "Running time",
            "Automated costs",
            "Manual costs",
            "Savings",
        ]

        writer.writerow(headers)
        for job in qs:
            row = [
                self._csv_safe(
                    job["template_metadata__template_name"]
                ),  # Escape template name to prevent CSV injection
                job["runs"],
                job["num_hosts"],
                job["time_taken_manually_execute_minutes"],
            ]
            if enable_template_creation_time:
                row.append(job["time_taken_create_automation_minutes"])
            row += [
                job["elapsed"],
                sec2time(job["elapsed"]),
                round(job["automated_costs"], 2),
                round(job["manual_costs"], 2),
                round(job["savings"], 2),
            ]
            writer.writerow(row)

    @staticmethod
    def _compute_roi_metrics(qs: QuerySet) -> dict:
        """Aggregate ROI metrics from a queryset of JobData."""
        aggregate = qs.aggregate(
            total_automated_costs=Coalesce(Sum("automated_costs"), Value(decimal.Decimal("0"))),
            total_manual_costs=Coalesce(Sum("manual_costs"), Value(decimal.Decimal("0"))),
            total_savings=Coalesce(Sum("savings"), Value(decimal.Decimal("0"))),
            total_time_savings=Coalesce(Sum("time_savings"), Value(decimal.Decimal("0"))),
        )
        automated_costs = aggregate["total_automated_costs"]
        manual_costs = aggregate["total_manual_costs"]
        savings = aggregate["total_savings"]
        return {
            "automated_costs": automated_costs,
            "manual_costs": manual_costs,
            "savings": savings,
            "time_saved_hours": round(aggregate["total_time_savings"] / 3600, 2),
            "roi_percentage": round((savings / automated_costs) * 100, 2) if automated_costs else decimal.Decimal("0"),
            "automation_value": manual_costs + savings,
        }

    def _write_roi_csv(self, response: HttpResponse, qs: QuerySet[JobData]) -> None:
        """
        Write ROI data to CSV, including total cost savings, time savings, automation
        cost, manual cost equivalent, and ROI percentage.
        """
        m = self._compute_roi_metrics(qs)

        writer = csv.writer(response)
        writer.writerow(
            [
                "Cost Savings",
                "Time Saved (hours)",
                "Automation Cost",
                "Manual Cost Equivalent",
                "ROI Percentage",
                "Automation Value",
            ]
        )
        writer.writerow(
            [
                round(m["savings"], 2),
                m["time_saved_hours"],
                round(m["automated_costs"], 2),
                round(m["manual_costs"], 2),
                m["roi_percentage"],
                round(m["automation_value"], 2),
            ]
        )

    @staticmethod
    def _build_trends_queryset(base_qs: QuerySet, kind: str) -> QuerySet:
        """Return a queryset of job trends grouped by the given time granularity."""
        return (
            base_qs.values(date=Trunc(expression="finished", kind=kind, output_field=models.DateTimeField()))
            .annotate(
                runs=Count("id"),
                successful_runs=Count("id", filter=Q(status=JobStatusChoices.SUCCESSFUL)),
                failed_runs=Count("id", filter=Q(status=JobStatusChoices.FAILED)),
                total_elapsed=Sum("elapsed"),
                total_hosts=Sum("num_hosts"),
            )
            .order_by("date")
        )

    def _write_trends_csv(self, response: HttpResponse, base_qs: QuerySet[JobData]) -> None:
        """Write trends data to CSV, grouped by the time granularity derived from the date range."""
        _, _, kind = self._get_date_range_and_kind()
        if not kind:
            logger.warning("Could not determine time granularity for trends report; defaulting to 'day'.")
            kind = "day"

        trends_qs = self._build_trends_queryset(base_qs, kind)

        writer = csv.writer(response)
        writer.writerow(
            [
                "Date",
                "Number of Job Executions",
                "Successful Runs",
                "Failed Runs",
                "Total Elapsed (seconds)",
                "Total Hosts",
            ]
        )
        for row in trends_qs:
            writer.writerow(
                [
                    row["date"].strftime(self.TRENDS_DATE_FORMATS[kind]),
                    row["runs"],
                    row["successful_runs"],
                    row["failed_runs"],
                    row["total_elapsed"],
                    row["total_hosts"],
                ]
            )

    def _build_filter_labels(self, request: Request) -> dict:
        """Build active filter labels for display in HTML report headers."""
        filters_data: dict[str, dict] = {}
        filter_map = [
            ("organization", "Organization"),
            ("template", "Template"),
            ("project", "Project"),
            ("label", "Label"),
        ]
        # Filter names are resolved from the AWX database — a filtered entity may not have
        # any corresponding records in JobData (e.g. no jobs ran for that org/template).
        name_queries = {
            "organization": fetch_organizations,
            "template": fetch_templates,
            "project": fetch_projects,
            "label": fetch_labels,
        }
        filter_options = get_filter_options(request)
        or_options = get_or_filter_options(request=request)
        options = {**filter_options, **or_options}
        try:
            db_connection = get_db_connection("awx")
            for param_key, label in filter_map:
                ids = options.get(param_key)
                if not ids:
                    continue
                items, _ = name_queries[param_key](db_connection=db_connection, ids=ids)
                if items:
                    filters_data[param_key] = {"name": label, "values": ", ".join(str(i["name"]) for i in items)}
        except Exception:  # noqa: BLE001 — any DB/connection error means AWX is unavailable; degrade gracefully
            logger.exception("Failed to resolve filter labels from AWX database; filters will not be shown in export.")
        return filters_data

    def _build_html_report_base(self, request: Request) -> tuple:
        """Return (start_date, end_date, currency, filtered_agg_qs) shared by HTML report builders."""
        start_date = self.kwargs["start_date"]
        end_date = self.kwargs["end_date"]
        currency = request.query_params.get("currency", "$")
        base_qs = self._filter_raw_jobdata_queryset(JobData.objects.all())
        qs = self.filter_queryset(self._build_aggregated_queryset(base_qs))
        return start_date, end_date, currency, qs

    def _build_html_summary(self, request: Request) -> HttpResponse:
        """Render the summary report as an HTML response."""
        start_date, end_date, currency, full_agg_qs = self._build_html_report_base(request)

        report_data_qs = full_agg_qs.aggregate(
            total_runs=Coalesce(Sum("runs"), Value(0)),
            total_successful_runs=Coalesce(Sum("successful_runs"), Value(0)),
            total_failed_runs=Coalesce(Sum("failed_runs"), Value(0)),
            total_num_hosts=Coalesce(Sum("num_hosts"), Value(0)),
            total_elapsed=Coalesce(Sum("elapsed"), Value(decimal.Decimal("0"))),
            total_manual_costs=Coalesce(Sum("manual_costs"), Value(decimal.Decimal("0"))),
            total_automated_costs=Coalesce(Sum("automated_costs"), Value(decimal.Decimal("0"))),
            total_savings=Coalesce(Sum("savings"), Value(decimal.Decimal("0"))),
            total_time_savings=Coalesce(Sum("time_savings"), Value(decimal.Decimal("0"))),
        )

        chart_data = self.get_chart_data()

        context = {
            **self._build_inline_context(),
            "report_type": "summary",
            "table_data": ReportSerializer(full_agg_qs, many=True).data,
            "details": ReportDetailSerializer(
                {
                    **report_data_qs,
                    "top_users": [],
                    "top_projects": [],
                    "total_number_of_unique_hosts": 0,
                    **chart_data,
                }
            ).data,
            "enable_template_creation_time": SubscriptionCost.get().include_template_creation_time_in_costs,
            "currency": currency,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "filters": self._build_filter_labels(request),
            "job_chart_svg": self._render_svg_chart(chart_data["job_chart"], "bar"),
            "host_chart_svg": self._render_svg_chart(chart_data["host_chart"], "line"),
        }

        html = render_to_string("dashboard_reports/report_summary.html", context, request=request)
        return HttpResponse(html, content_type=HTML_CONTENT_TYPE)

    def _build_html_roi(self, request: Request) -> HttpResponse:
        """Render the ROI report as an HTML response."""
        start_date, end_date, currency, qs = self._build_html_report_base(request)

        m = self._compute_roi_metrics(qs)

        context = {
            **self._build_inline_context(),
            "report_type": "roi",
            "currency": currency,
            "cost_savings": round(m["savings"], 2),
            "time_saved_hours": m["time_saved_hours"],
            "automation_cost": round(m["automated_costs"], 2),
            "manual_cost_equivalent": round(m["manual_costs"], 2),
            "roi_percentage": m["roi_percentage"],
            "automation_value": round(m["automation_value"], 2),
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "filters": self._build_filter_labels(request),
        }

        html = render_to_string("dashboard_reports/report_roi.html", context, request=request)
        return HttpResponse(html, content_type=HTML_CONTENT_TYPE)

    def _build_html_trends(self, request: Request) -> HttpResponse:
        """Render the trends report as an HTML response."""
        start_date = self.kwargs["start_date"]
        end_date = self.kwargs["end_date"]

        base_qs = self._filter_raw_jobdata_queryset(JobData.objects.all())

        _, _, kind = self._get_date_range_and_kind()
        if not kind:
            kind = "day"

        trends_qs = self._build_trends_queryset(base_qs, kind)

        rows = [
            {
                "date": row["date"].strftime(self.TRENDS_DATE_FORMATS[kind]),
                "runs": row["runs"],
                "successful_runs": row["successful_runs"],
                "failed_runs": row["failed_runs"],
                "total_elapsed": row["total_elapsed"],
                "total_elapsed_str": sec2time(row["total_elapsed"]) if row["total_elapsed"] else "0min 0sec",
                "total_hosts": row["total_hosts"],
            }
            for row in trends_qs
        ]

        job_chart = {
            "kind": kind,
            "items": [{"label": row["date"], "value": row["runs"]} for row in rows],
        }
        host_chart = {
            "kind": kind,
            "items": [{"label": row["date"], "value": row["total_hosts"]} for row in rows],
        }

        context = {
            **self._build_inline_context(),
            "report_type": "trends",
            "granularity": kind,
            "rows": rows,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "filters": self._build_filter_labels(request),
            "job_chart_svg": self._render_svg_chart(job_chart, "bar"),
            "host_chart_svg": self._render_svg_chart(host_chart, "line"),
        }

        html = render_to_string("dashboard_reports/report_trends.html", context, request=request)
        return HttpResponse(html, content_type=HTML_CONTENT_TYPE)

    def _export_csv(
        self, request: Request, report_type: str, filename: str, base_qs: QuerySet, qs: QuerySet
    ) -> HttpResponse:
        """Handle CSV export dispatch."""
        csv_response = HttpResponse(
            content_type="text/csv; charset=UTF-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )
        if report_type == "summary":
            self._write_summary_csv(csv_response, qs)
        elif report_type == "roi":
            self._write_roi_csv(csv_response, qs)
        elif report_type == "trends":
            self._write_trends_csv(csv_response, base_qs)
        return csv_response

    def _export_html(self, request: Request, report_type: str) -> HttpResponse:
        """Handle HTML export dispatch."""
        if report_type == "summary":
            return self._build_html_summary(request)
        if report_type == "roi":
            return self._build_html_roi(request)
        return self._build_html_trends(request)

    @extend_schema(
        summary="Returns summary, chart data, and top users/projects for dashboard",
        description="Returns summary, chart data, and top users/projects for dashboard",
        parameters=detail_query_parameters,
    )
    @action(detail=False, methods=["get"], url_path="details")
    @require_date_range
    def details(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """
        Returns summary, chart data, and top users/projects for dashboard.
         - Summary includes total runs, successful runs, failed runs, total time savings, and total cost savings.
         - Chart data provides aggregated metrics by job template for visualizations.
         - Top users and projects identify the most active users and projects in the given date range.
         All data is filtered by the provided date range and any additional filters (organization, template, label, project).
        """
        filtered_qs = self._filter_raw_jobdata_queryset(JobData.objects.all())

        ### TOP USERS ###
        top_users_qs = (
            filtered_qs.filter(launched_by_id__isnull=False)
            .values("launched_by_id", "launched_by_username")
            .annotate(count=Count("id"))
            .order_by("-count", "launched_by_id")[: self.TOP_RESULTS_LIMIT]
        )

        ### TOP PROJECTS ###
        top_projects_qs = (
            filtered_qs.filter(project_id__isnull=False)
            .values("project_id", "project_name")
            .annotate(count=Count("id"))
            .order_by("-count", "project_id")[: self.TOP_RESULTS_LIMIT]
        )

        ### AGGREGATED DATA ###
        # Build the aggregated queryset from the already-filtered raw queryset so that
        # CustomReportFilter's custom manager methods are not called on a ValuesQuerySet.
        qs = self._build_aggregated_queryset(filtered_qs)
        report_data_qs = qs.aggregate(
            total_runs=Coalesce(Sum("runs"), Value(0)),
            total_successful_runs=Coalesce(Sum("successful_runs"), Value(0)),
            total_failed_runs=Coalesce(Sum("failed_runs"), Value(0)),
            total_num_hosts=Coalesce(Sum("num_hosts"), Value(0)),
            total_elapsed=Coalesce(Sum("elapsed"), Value(decimal.Decimal("0"))),
            total_manual_time=Coalesce(Sum("manual_time"), Value(0)),
            total_manual_costs=Coalesce(Sum("manual_costs"), Value(decimal.Decimal("0"))),
            total_automated_costs=Coalesce(Sum("automated_costs"), Value(decimal.Decimal("0"))),
            total_savings=Coalesce(Sum("savings"), Value(decimal.Decimal("0"))),
            total_time_savings=Coalesce(Sum("time_savings"), Value(decimal.Decimal("0"))),
        )

        ### Unique hosts count ###
        # Use a stable surrogate key that prefers host_id (cast to text) when present and
        # falls back to host_name — matching JobHostSummary.unique_count() — so that two
        # hosts sharing a name aren't collapsed into one, and a renamed host isn't double-counted.
        unique_hosts_count = (
            JobHostSummary.objects.filter(job_data__in=filtered_qs)
            .annotate(
                stable_host=Case(
                    When(host_id__isnull=False, then=Cast(F("host_id"), output_field=models.TextField())),
                    default=F("host_name"),
                    output_field=models.TextField(),
                )
            )
            .values("stable_host")
            .distinct()
            .count()
        )

        ### CHART DATA ###
        chart_data = self.get_chart_data()

        ### Serialize data ###
        report_data = self.get_serializer(
            {
                **report_data_qs,
                "top_users": top_users_qs,
                "top_projects": top_projects_qs,
                "total_number_of_unique_hosts": unique_hosts_count,
                **chart_data,
            }
        ).data

        return Response(report_data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Exports report data as CSV or HTML",
        description="Exports report data as CSV or HTML based on the specified report type and format query parameters. The HTML format returns a print-ready page with a browser print button for saving as PDF.",
        parameters=export_query_parameters,
        responses={
            (200, "text/csv; charset=UTF-8"): str,
            (200, "text/html; charset=UTF-8"): str,
            400: inline_serializer(
                name="DashboardReportExportErrorSerializer",
                fields={
                    "detail": serializers.CharField(required=False),
                    "error": serializers.CharField(required=False),
                },
            ),
        },
    )
    @action(
        methods=["get"],
        detail=False,
        url_path="export",
        renderer_classes=[PassthroughRenderer, PassthroughRendererHTML],
    )
    @require_date_range
    def export(self, request: Request, *args: Any, **kwargs: Any) -> HttpResponse | JsonResponse:
        """Exports report data as CSV or print-ready HTML based on the specified report type and format."""
        report_type = request.query_params.get("report_type", "summary")
        export_format = request.query_params.get("export_format", "csv")

        if export_format not in ("csv", "html"):
            return JsonResponse(
                {"detail": f"Invalid export format: {export_format}. Must be one of: csv, html."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if report_type not in ("summary", "roi", "trends"):
            return JsonResponse(
                {"detail": f"Invalid report_type: {report_type}. Must be one of: summary, roi, trends."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        end_date = self.kwargs.get("end_date")
        filename = f"automation-dashboard-{report_type}-{end_date.date().isoformat()}"
        base_qs = self._filter_raw_jobdata_queryset(JobData.objects.all())
        qs = self.filter_queryset(self._build_aggregated_queryset(base_qs))

        if export_format == "csv":
            return self._export_csv(request, report_type, filename, base_qs, qs)

        return self._export_html(request, report_type)
