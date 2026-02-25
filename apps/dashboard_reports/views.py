import datetime
import decimal
import logging
from typing import Any

import pytz
from django.db import models
from django.db.models import F, Count, Q, Sum, QuerySet, Min, Max, OuterRef, Subquery, Value
from django.db.models.functions import Trunc, Coalesce
from django.http import HttpRequest
from django_generate_series.models import generate_series
from rest_framework import filters
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.core.permissions import DeveloperModeRequired
from apps.dashboard_reports.awx_queries import (
    fetch_labels,
    fetch_organizations,
    fetch_projects,
    fetch_templates
)
from apps.dashboard_reports.models import JobData, JobStatusChoices, JobHostSummary
from apps.dashboard_reports.serializers import (
    PaginatedFilterOptionsSerializer,
    FilterOptionWithIdSerializer,
    ReportSerializer,
    ReportDetailSerializer
)
from apps.tasks.api_utils import build_error_response
from apps.tasks.utils import get_db_connection
from .filters import CustomReportFilter, get_filter_options

logger = logging.getLogger(__name__)


class FilterOptionsViewSet(ReadOnlyModelViewSet):
    """
    Base ViewSet for AWX filter dropdowns (labels, organizations, projects, job templates).
    Handles pagination, search, error handling, and response formatting.
    """
    permission_classes = [DeveloperModeRequired]
    versioning_class = None  # Disable versioning for this viewset
    list_error_msg = "Failed to fetch records"
    retrieve_error_msg = "Failed to fetch record"

    def not_found_msg(self, pk):
        """Returns a formatted not found message for a missing record."""
        return f"Record with id {pk} not found"

    def get_queryset(self):
        """Override to disable queryset (not used for filter dropdowns)."""
        return None

    def search(self, request: HttpRequest) -> str | None:
        """Extracts search query from request parameters."""
        return request.query_params.get('search', '').strip() or None

    def paginate(self, request: HttpRequest, data: list[dict[str, Any]]) -> Response:
        """
        Paginates the filter dropdown data and builds pagination URLs.
        """
        # Parse pagination parameters
        try:
            page = int(request.query_params.get('page', 1))
            page_size = int(request.query_params.get('page_size', 10))
        except (ValueError, TypeError):
            page = 1
            page_size = 10

        search_query = self.search(request)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = data[start_idx:end_idx]

        # Build pagination URLs
        base_url = request.build_absolute_uri(request.path)
        next_url = None
        previous_url = None

        if end_idx < len(data):
            next_url = f"{base_url}?page={page + 1}&page_size={page_size}"
            if search_query:
                next_url += f"&search={search_query}"

        if page > 1:
            previous_url = f"{base_url}?page={page - 1}&page_size={page_size}"
            if search_query:
                previous_url += f"&search={search_query}"

        # Build response
        response_data = {
            'count': len(data),
            'next': next_url,
            'previous': previous_url,
            'results': paginated_data
        }

        serializer = PaginatedFilterOptionsSerializer(response_data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def retrieve_response(self, request: HttpRequest, data: dict[str, Any], error_msg: str) -> Response:
        """
        Returns a single filter dropdown item or error if not found.
        """
        if (not data) or len(data) == 0:
            error_response = build_error_response(error_msg, status_code=404)
            return Response(error_response, status=status.HTTP_404_NOT_FOUND)
        serializer = FilterOptionWithIdSerializer(data[0])
        return Response(serializer.data, status=status.HTTP_200_OK)

    def list(self, request: HttpRequest) -> Response:
        """
        Returns paginated filter dropdown data from AWX database.
        """

        try:
            db_connection = get_db_connection('awx')
            data = self.awx_query_function(db_connection=db_connection, search_str=self.search(request))
            return self.paginate(request, data)
        except Exception as e:
            logger.error(f"{self.list_error_msg}: {str(e)}")
            error_response = build_error_response(f"{self.list_error_msg}: {str(e)}", status_code=500)
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def retrieve(self, request, *args, **kwargs) -> Response:
        """
        Returns a single filter dropdown item by ID from AWX database.
        """
        try:
            pk = kwargs.get('pk')
            pk = int(pk) if pk and str(pk).isdigit() else None
            if pk is None:
                error_response = build_error_response("Invalid ID", status_code=400)
                return Response(error_response, status=status.HTTP_400_BAD_REQUEST)
            db_connection = get_db_connection('awx')
            data = self.awx_query_function(db_connection=db_connection, pk=pk)
            return self.retrieve_response(request, data, error_msg=self.not_found_msg(pk))
        except Exception as e:
            logger.error(f"{self.retrieve_error_msg}: {str(e)}")
            error_response = build_error_response(f"{self.retrieve_error_msg}: {str(e)}", status_code=500)
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LabelsViewSet(FilterOptionsViewSet):
    """
        ViewSet for retrieving labels from AWX database.

        Provides real-time label data for filter dropdowns with pagination support.

        Endpoints:
            GET /api/v1/labels/ - List all labels (paginated)
            GET /api/v1/labels/{id}/ - Get specific label

        Query Parameters:
            page (int): Page number (default: 1)
            page_size (int): Results per page (default: 10)
            search (str): Search by label name
        """
    awx_query_function = fetch_labels
    list_error_msg = "Failed to fetch labels"
    retrieve_error_msg = "Failed to fetch label"

    def not_found_msg(self, pk):
        return f"Label with id {pk} not found"


class OrganizationsViewSet(FilterOptionsViewSet):
    """
        ViewSet for retrieving organizations from AWX database.

        Provides real-time organization data for filter dropdowns with pagination support.

        Endpoints:
            GET /api/v1/organizations/ - List all organizations (paginated)
            GET /api/v1/organizations/{id}/ - Get specific organization

        Query Parameters:
            page (int): Page number (default: 1)
            page_size (int): Results per page (default: 10)
            search (str): Search by organization name
        """
    awx_query_function = fetch_organizations
    list_error_msg = "Failed to fetch organizations"
    retrieve_error_msg = "Failed to fetch organization"

    def not_found_msg(self, pk):
        return f"Organization with id {pk} not found"


class ProjectsViewSet(FilterOptionsViewSet):
    """
    ViewSet for retrieving projects from AWX database.

    Provides real-time project data for filter dropdowns with pagination support.

    Endpoints:
        GET /api/v1/projects/ - List all projects (paginated)
        GET /api/v1/projects/{id}/ - Get specific project

    Query Parameters:
        page (int): Page number (default: 1)
        page_size (int): Results per page (default: 10)
        search (str): Search by project name
    """
    awx_query_function = fetch_projects
    list_error_msg = "Failed to fetch projects"
    retrieve_error_msg = "Failed to fetch project"

    def not_found_msg(self, pk):
        return f"Project with id {pk} not found"


class JobTemplatesViewSet(FilterOptionsViewSet):
    """
    ViewSet for retrieving job templates from AWX database.

    Provides real-time job template data for filter dropdowns with pagination support.

    Endpoints:
        GET /api/v1/job_templates/ - List all job templates (paginated)
        GET /api/v1/job_templates/{id}/ - Get specific job template

    Query Parameters:
        page (int): Page number (default: 1)
        page_size (int): Results per page (default: 10)
        search (str): Search by job template name
    """
    awx_query_function = fetch_templates
    list_error_msg = "Failed to fetch job templates"
    retrieve_error_msg = "Failed to fetch job template"

    def not_found_msg(self, pk):
        return f"Job template with id {pk} not found"


class DashboardReportViewSet(ReadOnlyModelViewSet):
    """
    ViewSet for dashboard reporting and chart data aggregation.
    Provides endpoints for summary, chart, and top users/projects.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    versioning_class = None  # Disable versioning for this viewset

    serializer_class = ReportSerializer

    filter_backends = [CustomReportFilter, filters.OrderingFilter]

    ordering_fields = ["template_name", "successful_runs", "failed_runs",
                       "num_hosts", "elapsed", "manual_time", "manual_costs",
                       "automated_costs", "savings", "runs"]

    ordering = ["template_name"]

    def get_serializer_class(self):
        if self.action == "details":
            return ReportDetailSerializer
        return super().get_serializer_class()

    def get_queryset(self) -> QuerySet[JobData]:
        """
        Builds annotated queryset for dashboard reporting, including cost and time calculations.
        """
        average_cost_employee_minute = decimal.Decimal(
            1)  # This should be readed from a database in a real implementation
        monthly_aap_subscription_cost = decimal.Decimal(
            5000)  # This should be readed from a database in a real implementation

        employee_cost_per_second = average_cost_employee_minute / decimal.Decimal(60)
        aap_subscription_per_second = monthly_aap_subscription_cost / (
                decimal.Decimal(30) * decimal.Decimal(24) * decimal.Decimal(3600))

        enable_template_creation_time = True  # This should be readed from a database in a real implementation

        if enable_template_creation_time:
            automated_costs = (F('time_taken_create_automation_minutes') * average_cost_employee_minute) + (
                    F('elapsed') * aap_subscription_per_second)
            time_savings = (F("manual_time") - F("elapsed") - (
                    F("time_taken_create_automation_minutes") * decimal.Decimal(60)))
        else:
            automated_costs = (F('elapsed') * aap_subscription_per_second)
            time_savings = (F("manual_time") - F("elapsed"))

        manual_costs = (F("num_hosts") * F("time_taken_manually_execute_minutes") * average_cost_employee_minute)
        manual_time = (F("num_hosts") * (F("time_taken_manually_execute_minutes") * 60))

        qs = (JobData.objects
        .prefetch_related('template_metadata')
        .values(
            'template_name',
            'template_metadata_id',
            time_taken_manually_execute_minutes=F('template_metadata__time_taken_manually_execute_minutes'),
            time_taken_create_automation_minutes=F('template_metadata__time_taken_create_automation_minutes'),
        ).annotate(
            runs=Count('id'),
            successful_runs=Count('id', filter=Q(status=JobStatusChoices.SUCCESSFUL)),
            failed_runs=Count('id', filter=Q(status=JobStatusChoices.FAILED)),
            elapsed=Sum('elapsed'),
            num_hosts=Sum('num_hosts'),
            automated_costs=automated_costs,
            manual_costs=manual_costs,
            manual_time=manual_time,
            time_savings=time_savings,
            savings=(F("manual_costs") - F("automated_costs")),
        ))
        return qs

    def validate_date_param(self, date_str: str, param_name: str) -> bool:
        """
        Validates ISO date string for query parameters.
        """
        if not date_str:
            return True  # Consider empty value as valid (optional parameter)
        try:
            datetime.datetime.fromisoformat(date_str)
            return True
        except ValueError as e:
            logger.error(f"Invalid {param_name} format: {date_str}. Error: {str(e)}")
            return False

    def raise_invalid_date_param(self, msg: str) -> Response:
        """
        Returns error response for invalid date parameters.
        """
        error_response = build_error_response(msg, status_code=400)
        return Response(error_response, status=status.HTTP_400_BAD_REQUEST)

    def list(self, request, *args, **kwargs):
        """
        Returns paginated report data for dashboard.
        """
        start_date = request.query_params.get("start_date", None)
        end_date = request.query_params.get("end_date", None)

        start_date_valid = self.validate_date_param(start_date, "start_date")
        end_date_valid = self.validate_date_param(end_date, "end_date")

        if not start_date_valid:
            self.raise_invalid_date_param(f"Invalid start_date format: {start_date}. Error: {str(e)}")
        if not end_date_valid:
            self.raise_invalid_date_param(f"Invalid end_date format, {str(e)}.)")
        return super().list(request, *args, **kwargs)

    def _get_date_range_and_kind(self, filter_options: dict[str, Any] | None = None):
        """
        Determines the start and end date for chart data and the chart kind (hour, day, month, year).
        Returns: (start_date, end_date, kind)
        """
        qs = JobData.objects
        start_date = filter_options.get("start_date", None) if filter_options else None
        end_date = filter_options.get("end_date", None) if filter_options else None
        if start_date is None:
            start_date = JobData.objects.filter(finished__isnull=False).aggregate(Min('finished'))['finished__min']
        if end_date is None:
            end_date = JobData.objects.filter(finished__isnull=False).aggregate(Max('finished'))['finished__max']

        if start_date is None or end_date is None:
            return None, None, None

        start_date = start_date.astimezone(pytz.UTC)
        end_date = end_date.astimezone(pytz.UTC)
        diff = abs(end_date - start_date)

        if start_date.year != end_date.year and start_date.month <= end_date.month:
            kind = 'year'
            start_date = start_date.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end_date = end_date.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif diff.days <= 1:
            kind = 'hour'
            start_date = start_date.replace(minute=0, second=0, microsecond=0)
            end_date = end_date.replace(minute=0, second=0, microsecond=0)
        elif diff.days <= 45:
            kind = 'day'
            start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            kind = 'month'
            start_date = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end_date = end_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start_date, end_date, kind

    def _prepare_chart_querysets(self, qs, kind):
        """
        Prepares job and host chart querysets for the given kind.
        Returns: (job_chart_qs, host_chart_qs)
        """
        qs = qs.values(
            date=Trunc(
                expression="finished",
                kind=kind,
                output_field=models.DateTimeField())).filter(
            date=OuterRef("term")
        )
        job_chart_qs = qs.annotate(
            runs=Count("id")
        ).values("runs").order_by()
        host_chart_qs = qs.annotate(
            num_hosts=Sum("num_hosts")
        ).values("num_hosts").order_by()
        return job_chart_qs, host_chart_qs

    def _format_chart_result(self, date_sequence_queryset):
        """
        Formats the chart result from the date sequence queryset.
        Returns: dict with host_chart and job_chart.
        """
        result = {
            'host_chart': {
                'kind': '',
                'items': []
            },
            'job_chart': {
                'kind': '',
                'items': []
            }
        }
        for data in date_sequence_queryset:
            result['job_chart']['items'].append({'label': data.term, 'value': data.runs})
            result['host_chart']['items'].append({'label': data.term, 'value': data.hosts})
        return result

    def get_chart_data(self, filter_options: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Returns chart data for jobs and hosts over a time range, grouped by kind (hour, day, month, year).
        """
        qs = JobData.objects
        start_date, end_date, kind = self._get_date_range_and_kind(filter_options)
        if not start_date or not end_date or not kind:
            return {
                'host_chart': {'kind': '', 'items': []},
                'job_chart': {'kind': '', 'items': []}
            }
        job_chart_qs, host_chart_qs = self._prepare_chart_querysets(qs, kind)
        date_sequence_queryset = generate_series(
            start=start_date,
            stop=end_date,
            step=f'1 {kind}s',
            span=5,
            output_field=models.DateTimeField
        ).annotate(
            runs=Coalesce(Subquery(job_chart_qs), Value(0)),
            hosts=Coalesce(Subquery(host_chart_qs), Value(0)),
        )
        result = self._format_chart_result(date_sequence_queryset)
        result['host_chart']['kind'] = kind
        result['job_chart']['kind'] = kind
        return result

    @action(detail=False, methods=['get'], url_path='details')
    def details(self, request, *args, **kwargs):
        """
        Returns dashboard details including top users, top projects, aggregated metrics, chart data, and unique hosts count.
        """
        start_date = request.query_params.get("start_date", None)
        end_date = request.query_params.get("end_date", None)

        start_date_valid = self.validate_date_param(start_date, "start_date")
        end_date_valid = self.validate_date_param(end_date, "end_date")

        if not start_date_valid:
            self.raise_invalid_date_param(f"Invalid start_date format: {start_date}. Error: {str(e)}")
        if not end_date_valid:
            self.raise_invalid_date_param(f"Invalid end_date format, {str(e)}.)")

        filtered_qs = self.filter_queryset(JobData.objects.all())

        ### TOP USERS ###
        top_users_qs = (filtered_qs.filter(launched_by_id__isnull=False)
        .values('launched_by_id', 'launched_by_username')
        .annotate(count=Count('id'))
        .order_by('launched_by_id')
        .order_by('-count')[:5]
        )

        ### TOP PROJECTS ###
        top_projects_qs = (filtered_qs.filter(project_id__isnull=False)
        .values('project_id', 'project_name')
        .annotate(count=Count('id'))
        .order_by('project_id')
        .order_by('-count')[:5]
        )

        ### AGGREGATED DATA ###
        qs = self.filter_queryset(self.get_queryset())
        report_data_qs = qs.aggregate(
            total_runs=Sum("runs"),
            total_successful_runs=Sum("successful_runs"),
            total_failed_runs=Sum("failed_runs"),
            total_num_hosts=Sum("num_hosts"),
            total_elapsed=Sum("elapsed"),
            total_manual_time=Sum("manual_time"),
            total_manual_costs=Sum("manual_costs"),
            total_automated_costs=Sum("automated_costs"),
            total_savings=Sum("savings"),
            total_time_savings=Sum("time_savings"),
        )

        options = get_filter_options(request=request)
        ### CHART DATA ###
        chart_data = self.get_chart_data(options)

        ### Unique hosts count ###
        unique_hosts_count = JobHostSummary.unique_count(options)

        ### Serialize data ###
        report_data = self.get_serializer(
            {
                **report_data_qs,
                **{
                    'top_users': top_users_qs,
                    'top_projects': top_projects_qs,
                    'total_number_of_unique_hosts': unique_hosts_count
                },
                **chart_data
            }
        ).data

        return Response(report_data, status=status.HTTP_200_OK)
