import decimal
import logging
from datetime import datetime
from typing import List, Any

from django.db import models
from metrics_utility.library.collectors.dashboard import AwxJobTemplateType, AWXJobType, AWXJobHostSummaryType

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


class CommonAwxModel(models.Model):
    awx_created = models.DateTimeField(help_text="Creation timestamp from AWX", null=True, blank=True)
    awx_modified = models.DateTimeField(help_text="Modification timestamp from AWX", null=True, blank=True)

    class Meta:
        abstract = True

    @classmethod
    def last_timestamp(cls) -> datetime | None:
        # Returns the largest awx_modified timestamp among all records, or None if there are no records.
        latest_awx_modified = cls.objects.filter(awx_modified__isnull=False).aggregate(models.Max('awx_modified'))[
            'awx_modified__max']
        return latest_awx_modified


class TemplateMetadata(CommonModel, CommonAwxModel):
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

    template_description = models.TextField(
        blank=True,
        help_text="Template description for display (from AWX)"
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
    def create_or_update_from_awx(cls, awx_template: AwxJobTemplateType):
        """
        Creates or updates a TemplateMetadata instance from AWX job template data.
        Updates fields if template_id or template_name matches.
        """

        awx_template_id = awx_template.get('id', None)
        awx_template_name = awx_template['name']
        awx_template_description = awx_template.get('description', '')
        awx_template_created = awx_template.get('created', None)
        awx_template_modified = awx_template.get('modified', None)

        instance = None

        if awx_template_id is not None and awx_template_id > 0:
            try:
                instance = cls.objects.get(template_id=awx_template_id)
            except cls.DoesNotExist:
                instance = None

        if instance is None:
            instance = cls.objects.filter(template_name=awx_template_name).first()

        if instance is not None:
            instance.template_name = awx_template_name
            instance.template_description = awx_template_description
            instance.awx_created = awx_template_created if awx_template_created is not None else instance.awx_created
            instance.awx_modified = awx_template_modified if awx_template_modified is not None else instance.awx_modified
            instance.template_id = awx_template_id if awx_template_id is not None else instance.template_id
            instance.save()
            logger.info(f"Updated TemplateMetadata '{instance}' from AWX data.")
        else:
            instance = cls.objects.create(
                template_name=awx_template_name,
                template_description=awx_template_description,
                template_id=awx_template_id if awx_template_id is not None else cls.get_min_awx_id(),
                awx_created=awx_template_created,
                awx_modified=awx_template_modified
            )
            logger.info(f"Created new TemplateMetadata '{instance}' from AWX data.")

        return instance

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
                instance = cls.create_or_update_from_awx(
                    {
                        'id': awx_id if awx_id else -1,
                        'name': name,
                        'description': '',
                        'created': None,
                        'modified': None
                    }
                )

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


class JobData(CommonModel, CommonAwxModel):
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

    changed_hosts_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of hosts that were changed during the job (calculated from AWX job host summaries)"
    )

    dark_hosts_count = models.PositiveIntegerField(default=0)
    failures_hosts_count = models.PositiveIntegerField(default=0)
    ok_hosts_count = models.PositiveIntegerField(default=0)
    processed_hosts_count = models.PositiveIntegerField(default=0)
    skipped_hosts_count = models.PositiveIntegerField(default=0)
    failed_hosts_count = models.PositiveIntegerField(default=0)
    ignored_hosts_count = models.PositiveIntegerField(default=0)
    rescued_hosts_count = models.PositiveIntegerField(default=0)

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
    def create_or_update_from_awx(cls, awx_job: AWXJobType, labels: List[int],
                                  host_summaries: List[AWXJobHostSummaryType]):
        """
        Creates or updates a JobData instance from AWX job, label, and host summary data.
        Updates related JobLabel and JobHostSummary records.
        """
        template_metadata = TemplateMetadata.get_by_awx_id_or_name(name=awx_job['name'],
                                                                   awx_id=awx_job['unified_job_template_id'],
                                                                   elapsed=awx_job['elapsed'])

        num_hosts = 0
        changed_hosts_count = 0
        dark_hosts_count = 0
        failures_hosts_count = 0
        ok_hosts_count = 0
        processed_hosts_count = 0
        skipped_hosts_count = 0
        failed_hosts_count = 0
        ignored_hosts_count = 0
        rescued_hosts_count = 0

        for host_summary in host_summaries:
            num_hosts += 1
            changed_hosts_count += host_summary['changed']
            dark_hosts_count += host_summary['dark']
            failures_hosts_count += host_summary['failures']
            ok_hosts_count += host_summary['ok']
            processed_hosts_count += host_summary['processed']
            skipped_hosts_count += host_summary['skipped']
            failed_hosts_count += 1 if host_summary['failed'] else 0
            ignored_hosts_count += host_summary['ignored']
            rescued_hosts_count += host_summary['rescued']

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
                'num_hosts': num_hosts,
                'changed_hosts_count': changed_hosts_count,
                'dark_hosts_count': dark_hosts_count,
                'failures_hosts_count': failures_hosts_count,
                'ok_hosts_count': ok_hosts_count,
                'processed_hosts_count': processed_hosts_count,
                'skipped_hosts_count': skipped_hosts_count,
                'failed_hosts_count': failed_hosts_count,
                'ignored_hosts_count': ignored_hosts_count,
                'rescued_hosts_count': rescued_hosts_count,
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
                    host_summary_model.changed = awx_host_summary['changed']
                    host_summary_model.dark = awx_host_summary['dark']
                    host_summary_model.failures = awx_host_summary['failures']
                    host_summary_model.ok = awx_host_summary['ok']
                    host_summary_model.processed = awx_host_summary['processed']
                    host_summary_model.skipped = awx_host_summary['skipped']
                    host_summary_model.failed = awx_host_summary['failed']
                    host_summary_model.ignored = awx_host_summary['ignored']
                    host_summary_model.rescued = awx_host_summary['rescued']
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
                            changed=awx_host_summary['changed'],
                            dark=awx_host_summary['dark'],
                            failures=awx_host_summary['failures'],
                            ok=awx_host_summary['ok'],
                            processed=awx_host_summary['processed'],
                            skipped=awx_host_summary['skipped'],
                            failed=awx_host_summary['failed'],
                            ignored=awx_host_summary['ignored'],
                            rescued=awx_host_summary['rescued']
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

    changed = models.PositiveIntegerField(default=0)
    dark = models.PositiveIntegerField(default=0)
    failures = models.PositiveIntegerField(default=0)
    ok = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    ignored = models.PositiveIntegerField(default=0)
    rescued = models.PositiveIntegerField(default=0)
    failed = models.BooleanField(default=False)

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
