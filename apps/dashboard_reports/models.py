import decimal
import logging
from datetime import datetime
from typing import List, Any

from django.conf import settings
from django.db import models
from metrics_utility.library.collectors.dashboard import AWXJobType

# Import base classes, handling both DAB and simple fallbacks
try:
    from ansible_base.lib.abstract_models import CommonModel

    DAB_AVAILABLE = True
except ImportError:
    # Provide simple alternative when DAB is not available
    DAB_AVAILABLE = False


    class CommonModel(models.Model):
        created = models.DateTimeField(auto_now_add=True)
        modified = models.DateTimeField(auto_now=True)

        class Meta:
            abstract = True

logger = logging.getLogger(__name__)

# Default estimate for time taken to create automation (in minutes)
# This should be used from settings.
DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES = 60


class SubscriptionCost(CommonModel):
    """
    Stores subscription cost information for the AAP subscription, including monthly cost and average engineer hourly rate.
    This is used for cost calculations in the dashboard reports.

    There should typically only be one record in this table, which can be updated as needed when subscription costs change.
    """

    monthly_subscription_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        help_text="Monthly subscription cost for AAP subscription"
    )

    engineer_avg_hourly_rate = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        help_text='Average hourly rate for engineers performing manual tasks (used for cost calculations in reports)'
    )

    include_template_creation_time_in_costs = models.BooleanField(
        default=True,
        help_text='Include template creation time in cost calculations. If false, costs related to template creation time will be excluded.'
    )

    class Meta:
        db_table = 'dashboard_subscription_cost'
        verbose_name = 'Subscription Cost'
        verbose_name_plural = 'Subscription Costs'

    def __str__(self):
        return f"SubscriptionCost: Monthly={self.monthly_subscription_cost}, Engineer Hourly Rate={self.engineer_avg_hourly_rate}"

    def save(self, *args, **kwargs):
        """
        Save object to the database. Removes all other entries if there are any.
        """
        if self.monthly_subscription_cost < 0:
            raise ValueError("Monthly subscription cost cannot be negative.")
        if self.engineer_avg_hourly_rate < 0:
            raise ValueError("Engineer average hourly rate cannot be negative.")
        self.__class__.objects.exclude(id=self.id).delete()
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        """
        Returns the single SubscriptionCost instance, or creates a default one if it doesn't exist.
        """
        instance = cls.objects.first()
        if instance is None:
            # TODO - In the future,
            #  we may want to pull these default values from settings
            instance = cls.objects.create(
                monthly_subscription_cost=decimal.Decimal(5000.00),
                engineer_avg_hourly_rate=decimal.Decimal(60.00),
            )
            logger.info("Created default SubscriptionCost instance with zero values.")
        return instance


class FilterSet(CommonModel):
    """
       Saved filter configurations (saved views) for dashboard filtering.

       Allows users to save commonly-used filter combinations for quick access.
       Users can have multiple filter sets, but only one can be marked as default.

       Example:
           filter_set = FilterSet.objects.create(
               name="Last 30 days - Production",
               filters={'organizations': [1, 2], 'date_range': 'last_30_days'}
           )
       """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='filter_sets',
        help_text="User who created this filter set"
    )

    name = models.CharField(
        max_length=255,
        db_index=True,
        help_text='Display name for this filter set (e.g. "Last 30 days")'
    )

    filters = models.JSONField(
        help_text="Filter configuration: {organizations: [], projects: [], labels: [], date_range: {}}"
    )

    is_default = models.BooleanField(
        default=False,
        help_text="Whether this is the user's default filter set (only one allowed per user)"
    )

    class Meta:
        db_table = 'dashboard_filter_set'
        verbose_name = "Filter Set"
        verbose_name_plural = "Filter Sets"
        ordering = ['name']
        indexes = [
            models.Index(fields=['user', 'is_default'], name='dashboard_fs_user_default_idx'),
            models.Index(fields=['user', '-modified'], name='dashboard_fs_user_mod_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'is_default'],
                condition=models.Q(is_default=True),
                name='one_default_per_user',
                violation_error_message="User can only have one default filter set"
            )
        ]

    def __str__(self):
        return self.name


class TemplateMetadata(CommonModel):
    """
    Stores metadata for AWX job templates, including name, description, and time estimates.
    Used for reporting and cost calculations.
    """
    template_id = models.IntegerField(
        unique=True,
        db_index=True,
        help_text="AWX job template ID (from AWX database main_jobtemplate table)",
    )

    template_name = models.CharField(
        max_length=512,
        db_index=True,
        help_text="Template name for display (from AWX)"
    )

    time_taken_manually_execute_minutes = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="User override: Estimated time to perform this task manually (minutes)"
    )

    time_taken_create_automation_minutes = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="User override: Estimated time spent creating this automation (minutes)"
    )

    class Meta:
        db_table = 'dashboard_template_metadata'
        ordering = ['template_name']
        verbose_name = "Template Metadata"
        verbose_name_plural = "Template Metadata"
        indexes = [
            models.Index(fields=['template_id'], name='dashboard_tm_template_idx'),
        ]

    def __str__(self):
        """Return string representation of the template metadata."""
        return f"Metadata for {self.template_name} (ID: {self.template_id})"

    @classmethod
    def get_min_awx_id(cls) -> int:
        """
        Returns a negative integer for new AWX template IDs if none exist,
        otherwise returns the minimum existing template_id minus one.
        """
        min_id = cls.objects.aggregate(models.Min('template_id')).get("template_id__min", None)
        return min_id - 1 if min_id is not None and min_id < 0 else -1

    @classmethod
    def get_by_awx_id_or_name(cls, name: str, awx_id: int | None = None, elapsed: decimal.Decimal | None = None):
        """
        Retrieves TemplateMetadata by AWX ID or name. If not found, creates a new instance.
        Sets default manual and automation time estimates if not present.
        """
        instance = None

        if awx_id is not None:
            try:
                instance = cls.objects.get(template_id=awx_id)
            except cls.DoesNotExist:
                instance = None

        if instance is None:
            try:
                instance = cls.objects.get(template_name=name)
            except cls.DoesNotExist:
                instance = cls.objects.create(
                    template_name=name,
                    template_id=awx_id if awx_id is not None else cls.get_min_awx_id(),
                )
                logger.info(f"Created new TemplateMetadata '{instance}' from AWX data.")

        update_fields = []

        if instance.time_taken_manually_execute_minutes is None:
            # If we have elapsed time from AWX and no manual override, set a default estimate based on elapsed time
            # Default: 2x the elapsed time, with a minimum of 30 minutes
            estimated_manual_time = max(
                int(decimal.Decimal(elapsed / 60 * 2).quantize(decimal.Decimal(1), rounding=decimal.ROUND_UP)),
                30)
            if estimated_manual_time > 1000000:
                estimated_manual_time = 1000000  # Cap at a reasonable maximum to avoid overflow issues
            instance.time_taken_manually_execute_minutes = estimated_manual_time
            update_fields.append('time_taken_manually_execute_minutes')
            logger.debug(
                f"Set default manual execution time for TemplateMetadata '{instance}' to {estimated_manual_time} minutes based on elapsed time.")

        if instance.time_taken_create_automation_minutes is None:
            instance.time_taken_create_automation_minutes = DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES
        update_fields.append('time_taken_create_automation_minutes')
        logger.debug(
            f"Set default automation creation time for TemplateMetadata '{instance}' to {DEFAULT_TIME_TAKEN_TO_CREATE_AUTOMATION_MINUTES} minutes.")

        if len(update_fields) > 0:
            instance.save(update_fields=update_fields)

        return instance


class JobStatusChoices(models.TextChoices):
    NEW = "new", "New"
    PENDING = "pending", "Pending"
    WAITING = "waiting", "Waiting"
    RUNNING = "running", "Running"
    SUCCESSFUL = "successful", "Successful"
    FAILED = "failed", "Failed"
    ERROR = "error", "Error"
    CANCELED = "canceled", "Canceled"


class JobDataFilterMethods(object):

    def organization(self, ids: List[int] | None):
        if ids is not None and len(ids) > 0:
            return self.filter(organization_id__in=ids)
        return self

    def template(self, ids: List[int] | None):
        if ids is not None and len(ids) > 0:
            return self.filter(template_id__in=ids)
        return self

    def project(self, ids: List[int] | None):
        if ids is not None and len(ids) > 0:
            return self.filter(project_id__in=ids)
        return self

    def label(self, ids: List[int] | None):
        if ids is not None and len(ids) > 0:
            return self.filter(labels__label_id__in=ids).distinct()
        return self

    def before(self, dt: datetime | None):
        if dt is not None:
            return self.filter(finished__lte=dt)
        return self

    def after(self, dt: datetime | None):
        if dt is not None:
            return self.filter(finished__gte=dt)
        return self


class JobDataQuerySet(JobDataFilterMethods, models.QuerySet):
    pass


class JobDataManager(JobDataFilterMethods, models.Manager):
    use_for_related_objects = True

    def get_queryset(self):
        return JobDataQuerySet(self.model, using=self._db)


class JobData(CommonModel):
    """
    Stores AWX job execution data for reporting, including status, timing, host counts, and related template/project/org info.
    """
    job_id = models.IntegerField(
        unique=True,
        db_index=True,
        help_text="AWX job ID (from AWX database main_unifiedjob table)",
    )

    template_name = models.CharField(
        max_length=512,
        help_text="Job template name (from AWX)"
    )

    template_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="AWX template ID",
    )

    project_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="AWX project ID",
    )

    project_name = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Project name for display (from AWX)"
    )

    organization_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="AWX organization ID",
    )

    status = models.CharField(
        choices=JobStatusChoices.choices,
        default=JobStatusChoices.SUCCESSFUL,
        max_length=25, db_index=True
    )

    started = models.DateTimeField(
        null=True,
        default=None,
        help_text="Job start timestamp (from AWX database main_unifiedjob table)",
    )
    finished = models.DateTimeField(
        null=True,
        default=None,
        db_index=True,
        help_text="Job finish timestamp (from AWX database main_unifiedjob table)",
    )
    elapsed = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        help_text="Job elapsed time in seconds (from AWX database main_unifiedjob table)",
    )

    num_hosts = models.PositiveIntegerField(
        default=0,
        help_text="Number of hosts involved in the job (calculated from AWX job host summaries)"
    )

    launched_by_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="AWX user ID of the user who launched the job (from AWX database main_unifiedjob table)"
    )

    launched_by_username = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="AWX username of the user who launched the job (from AWX database main_unifiedjob table)"
    )

    template_metadata = models.ForeignKey(
        TemplateMetadata,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='jobs',
        help_text="Reference to TemplateMetadata for this job's template"
    )

    awx_created = models.DateTimeField(help_text="Creation timestamp from AWX", null=True, blank=True)
    awx_modified = models.DateTimeField(help_text="Modification timestamp from AWX", null=True, blank=True)

    class Meta:
        db_table = 'dashboard_job_data'
        ordering = ['-started']
        verbose_name = "Job Data"
        verbose_name_plural = "Jobs Data"
        indexes = [
            models.Index(fields=['template_id'], name='dashboard_jd_template_idx'),
            models.Index(fields=['project_id'], name='dashboard_jd_project_idx'),
            models.Index(fields=['organization_id'], name='dashboard_jd_organization_idx'),
        ]

    objects = JobDataManager()

    def __str__(self):
        return f"Job {self.job_id} - Template: {self.template_name} - Status: {self.status}"

    @classmethod
    def last_timestamp(cls) -> datetime | None:
        # Returns the largest awx_modified timestamp among all records, or None if there are no records.
        latest_awx_modified = cls.objects.filter(awx_modified__isnull=False).aggregate(models.Max('awx_modified'))[
            'awx_modified__max']
        return latest_awx_modified

    @classmethod
    def create_or_update_from_awx(cls, awx_job: AWXJobType):
        """
        Creates or updates a JobData instance from AWX job, label, and host summary data.
        Updates related JobLabel and JobHostSummary records.
        """
        template_metadata = TemplateMetadata.get_by_awx_id_or_name(name=awx_job['name'],
                                                                   awx_id=awx_job['unified_job_template_id'],
                                                                   elapsed=awx_job['elapsed'])

        labels = awx_job.get('labels', [])
        host_summaries = awx_job.get('host_summaries', [])

        model, created = cls.objects.update_or_create(
            job_id=awx_job['id'],
            defaults={
                'template_name': awx_job['name'],
                'template_id': awx_job['unified_job_template_id'],
                'project_id': awx_job['project_id'],
                'project_name': awx_job['project_name'],
                'organization_id': awx_job['organization_id'],
                'status': awx_job['status'],
                'started': awx_job['started'],
                'finished': awx_job['finished'],
                'elapsed': awx_job['elapsed'],
                'launched_by_id': awx_job['launched_by_id'],
                'launched_by_username': awx_job['launched_by_username'],
                'template_metadata': template_metadata,
                'awx_created': awx_job['created'],
                'awx_modified': awx_job['modified'],
                'num_hosts': awx_job['num_hosts'],
            }
        )

        if created:
            logger.info(f"Created new JobData {model.__str__()}")
            labels_dict = {}
            host_summaries_dict = {}
        else:
            labels_dict = {l.label_id: l for l in JobLabel.objects.filter(job_data=model).all()}
            host_summaries_dict = {s.host_summary_id: s for s in JobHostSummary.objects.filter(job_data=model).all()}
            logger.info(f"Updated JobData {model.__str__()}")

        if len(labels) > 0:
            labels_for_create = []
            for label_id in labels:
                label = labels_dict.pop(label_id, None)
                if label is None:
                    labels_for_create.append(JobLabel(job_data=model, label_id=label_id))
            if len(labels_for_create) > 0:
                JobLabel.objects.bulk_create(labels_for_create)
                logger.info(f"Created {len(labels_for_create)} new JobLabel records for JobData {model.__str__()}")
        for label in labels_dict.values():
            label.delete()
            logger.info(f"Deleted JobLabel with label_id {label.label_id} for JobData {model.__str__()}")

        if len(host_summaries) > 0:
            host_summaries_for_create = []
            for awx_host_summary in host_summaries:
                host_summary_model = host_summaries_dict.pop(awx_host_summary['id'], None)
                if host_summary_model is not None:
                    host_summary_model.host_id = awx_host_summary['host_id']
                    host_summary_model.host_name = awx_host_summary['host_name']
                    host_summary_model.save()
                    logger.info(
                        f"Updated JobHostSummary for host '{host_summary_model.host_name}' (ID: {host_summary_model.host_id}) in JobData {model.__str__()}")
                else:
                    host_summaries_for_create.append(
                        JobHostSummary(
                            job_data=model,
                            host_id=awx_host_summary['host_id'],
                            host_name=awx_host_summary['host_name'],
                            host_summary_id=awx_host_summary['id'],
                        )
                    )
            if len(host_summaries_for_create) > 0:
                JobHostSummary.objects.bulk_create(host_summaries_for_create)
                logger.info(
                    f"Created {len(host_summaries_for_create)} new JobHostSummary records for JobData {model.__str__()}")
        for host_summary in host_summaries_dict.values():
            host_summary.delete()
            logger.info(
                f"Deleted JobHostSummary with host_summary_id {host_summary.host_summary_id} for JobData {model.__str__()}")


class JobLabel(CommonModel):
    """
    Stores label associations for a JobData instance (AWX job).
    """
    job_data = models.ForeignKey(JobData, on_delete=models.CASCADE, related_name='labels')
    label_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="AWX label ID",
    )

    class Meta:
        db_table = 'dashboard_job_data_label'
        verbose_name = "Job Data Label"
        verbose_name_plural = "Job Data Labels"
        indexes = [
            models.Index(fields=['job_data', 'label_id'], name='dashboard_jl_job_label_idx'),
        ]

    def __str__(self):
        return f'{self.label_meta_data.label_name}: {self.job_data.template_name}'


class JobHostSummaryFilterMethods(object):

    def organization(self, ids: List[int] | None):
        if ids is not None and len(ids) > 0:
            return self.filter(job_data__organization_id__in=ids)
        return self

    def template(self, ids: List[int] | None):
        if ids is not None and len(ids) > 0:
            return self.filter(job_data__template_id__in=ids)
        return self

    def project(self, ids: List[int] | None):
        if ids is not None and len(ids) > 0:
            return self.filter(job_data__project_id__in=ids)
        return self

    def label(self, ids: List[int] | None):
        if ids is not None and len(ids) > 0:
            return self.filter(job_data__labels__label_id__in=ids).distinct()
        return self

    def before(self, dt: datetime | None):
        if dt is not None:
            return self.filter(job_data__finished__lte=dt)
        return self

    def after(self, dt: datetime | None):
        if dt is not None:
            return self.filter(job_data__finished__gte=dt)
        return self


class JobHostSummaryQuerySet(JobHostSummaryFilterMethods, models.QuerySet):
    pass


class JobHostSummaryManager(JobHostSummaryFilterMethods, models.Manager):
    use_for_related_objects = True

    def get_queryset(self):
        return JobHostSummaryQuerySet(self.model, using=self._db)


class JobHostSummary(CommonModel):
    """
    Stores host summary statistics for a JobData instance (AWX job).
    Used for host-level reporting and unique host counts.
    """
    job_data = models.ForeignKey(JobData, on_delete=models.CASCADE, related_name='host_summaries')
    host_summary_id = models.IntegerField(
        unique=True,
        null=True,
        blank=True,
        help_text="AWX host summary ID (from AWX database main_hostsummary table)",
    )
    host_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="AWX host ID (from AWX database main_host table)",
    )

    host_name = models.TextField(
        max_length=512,
        db_index=True,
        help_text="Host name for display (from AWX)")

    objects = JobHostSummaryManager()

    class Meta:
        db_table = 'dashboard_job_data_host_summary'
        verbose_name = "Job Data Host Summary"
        verbose_name_plural = "Job Data Host Summaries"
        indexes = [
            models.Index(fields=['job_data', 'host_id'], name='dashboard_jhs_job_host_idx'),
        ]

    def __str__(self):
        return f'{self.host_name}: {self.job_data.template_name}'

    @classmethod
    def unique_count(cls, options: dict[str, Any]) -> int:
        """
        Returns the count of unique hosts matching filter options (organization, project, template, label, date range).
        """
        queryset = cls.objects
        queryset = queryset.organization(options.get("organization", None))
        queryset = queryset.project(options.get("project", None))
        queryset = queryset.template(options.get("template", None))
        queryset = queryset.label(options.get("label", None))
        queryset = queryset.after(options.get("start_date", None))
        queryset = queryset.before(options.get("end_date", None))
        return queryset.values('host_name').distinct().count()
