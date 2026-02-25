from apps.dashboard_reports.models import JobData
from dateutil.relativedelta import relativedelta
from rest_framework import serializers


def sec2time(sec: int) -> str:
    """
    This function converts a number of seconds into a human-readable string format,
    displaying hours, minutes, and seconds.
    It uses `relativedelta` to break down the total seconds and combines days into hours for the output.
    If the total time is less than one hour, it omits the hours part for brevity.
    """
    rd = relativedelta(seconds=sec)
    hours = rd.hours + (24 * rd.days)
    seconds = round(rd.seconds)
    return (
        f"{hours}h {rd.minutes}min {seconds}sec"
        if hours > 0
        else f"{rd.minutes}min {seconds}sec"
    )


class FilterOptionWithIdSerializer(serializers.Serializer):
    id = serializers.IntegerField(
        help_text="Option ID"
    )
    name = serializers.CharField(
        help_text="Option display name"
    )


class PaginatedFilterOptionsSerializer(serializers.Serializer):
    """
    Paginated response serializer for filter options.

    Matches the OptionsResponse TypeScript interface from automation-reports:
        interface OptionsResponse {
            count: number;
            next: string | null;
            previous: string | null;
            results: FilterOptionWithId[];
        }
    """

    count = serializers.IntegerField(
        help_text="Total number of options available"
    )
    next = serializers.CharField(
        allow_null=True,
        help_text="URL to next page (null if last page)"
    )
    previous = serializers.CharField(
        allow_null=True,
        help_text="URL to previous page (null if first page)"
    )
    results = FilterOptionWithIdSerializer(
        many=True,
        help_text="Array of filter options in {key, value, cluster_id} format"
    )


class ReportSerializer(serializers.ModelSerializer[JobData]):
    time_taken_manually_execute_minutes = serializers.IntegerField(read_only=True)
    time_taken_create_automation_minutes = serializers.IntegerField(read_only=True)
    runs = serializers.IntegerField(read_only=True)
    successful_runs = serializers.IntegerField(read_only=True)
    failed_runs = serializers.IntegerField(read_only=True)
    elapsed = serializers.DecimalField(max_digits=10, decimal_places=2)
    elapsed_str = serializers.SerializerMethodField()
    automated_costs = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    manual_costs = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    time_savings = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    time_savings_str = serializers.SerializerMethodField()
    savings = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)

    class Meta:
        model = JobData
        fields = ('template_name', 'template_metadata_id', 'time_taken_manually_execute_minutes',
                  'time_taken_create_automation_minutes', 'runs', 'successful_runs', 'failed_runs',
                  'elapsed', 'elapsed_str', 'automated_costs', 'manual_costs', 'time_savings', 'time_savings_str',
                  'savings')

    def get_elapsed_str(self, obj):
        return sec2time(obj["elapsed"]) if obj["elapsed"] is not None else ""

    def get_time_savings_str(self, obj):
        return sec2time(obj["time_savings"]) if obj["time_savings"] is not None else ""


class TopUserSerializer(serializers.Serializer):
    id = serializers.IntegerField(
        read_only=True,
        source='launched_by_id',
        help_text='ID of the user who executed the job'
    )

    user_name = serializers.CharField(
        read_only=True,
        source='launched_by_username',
        help_text='Username of the user who executed the job'
    )

    execution_count = serializers.IntegerField(
        read_only=True,
        source='count',
        help_text='Number of times this user executed a job'
    )


class TopProjectSerializer(serializers.Serializer):
    id = serializers.IntegerField(
        read_only=True,
        source='project_id',
        help_text='ID of the project associated with the job'
    )

    project_name = serializers.CharField(
        read_only=True,
        help_text='Name of the project associated with the job'
    )

    execution_count = serializers.IntegerField(
        read_only=True,
        source='count',
        help_text='Number of times jobs associated with this project were executed'
    )


class ChartDataItemSerializer(serializers.Serializer):
    label = serializers.DateTimeField(read_only=True, help_text='Label for the data point (e.g. timestamp)')
    value = serializers.IntegerField(read_only=True, help_text='Value for the data point (e.g. number of job runs)')


class ReportChartSerializer(serializers.Serializer):
    kind = serializers.CharField(
        read_only=True,
        help_text='Type fo date range for the series (e.g. hour, day, month, year)'
    )
    items = ChartDataItemSerializer(
        many=True,
        read_only=True,
        help_text='Data points for the chart series'
    )


class ReportDetailSerializer(serializers.Serializer):
    total_number_of_job_runs = serializers.IntegerField(
        read_only=True,
        source='total_runs',
        help_text='Total number of job runs'
    )

    total_number_of_successful_jobs = serializers.IntegerField(
        read_only=True,
        source='total_successful_runs',
        help_text='Total number of successful job runs'
    )

    total_number_of_failed_jobs = serializers.IntegerField(
        read_only=True,
        source='total_failed_runs',
        help_text='Total number of failed job runs'
    )

    total_number_of_host_job_runs = serializers.IntegerField(
        read_only=True,
        source='total_num_hosts',
        help_text='Total number of host job runs (sum of all hosts across all jobs)'
    )

    total_number_of_unique_hosts = serializers.IntegerField(
        read_only=True,
        help_text='Total number of unique hosts across all job runs'
    )

    total_hours_of_automation = serializers.SerializerMethodField(help_text='Total hours of automation')
    cost_of_automated_execution = serializers.SerializerMethodField(help_text='Total cost of automated execution')
    cost_of_manual_automation = serializers.SerializerMethodField(help_text='Total cost of manual execution')
    total_saving = serializers.SerializerMethodField(
        help_text='Total savings from automation (manual costs - automated costs)')
    total_time_saving = serializers.SerializerMethodField(help_text='Total time savings from automation (in hours)')

    top_users = TopUserSerializer(
        many=True,
        read_only=True,
        help_text='List of top users who executed the most jobs'
    )

    top_projects = TopProjectSerializer(
        many=True,
        read_only=True,
        help_text='List of top projects associated with the most job executions'
    )

    job_chart = ReportChartSerializer(
        read_only=True,
        help_text='Chart data showing job executions over time'
    )

    host_chart = ReportChartSerializer(
        read_only=True,
        help_text='Chart data showing host job runs over time'
    )

    def get_total_hours_of_automation(self, obj):
        return round((obj['total_elapsed'] / 3600), 2) if obj['total_elapsed'] is not None else 0

    def get_cost_of_automated_execution(self, obj):
        return round(obj['total_automated_costs'], 2) if obj['total_automated_costs'] is not None else 0

    def get_cost_of_manual_automation(self, obj):
        return round(obj['total_manual_costs'], 2) if obj['total_manual_costs'] is not None else 0

    def get_total_saving(self, obj):
        return round(obj['total_savings'], 2) if obj['total_savings'] is not None else 0

    def get_total_time_saving(self, obj):
        return round((obj['total_time_savings'] / 3600), 2) if obj['total_time_savings'] is not None else 0
