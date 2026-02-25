import logging
from datetime import datetime, timedelta
from typing import Any

import pytz
from django.db import transaction
from metrics_utility.library.collectors.dashboard import (
    dashboard_jobs,
    DashboardJobsResultType
)

from apps.dashboard_reports.models import JobData
from apps.tasks.utils import get_db_connection, create_task_result, log_task_execution, task, task_execution_wrapper

DEFAULT_DB_NAME = "awx"

logger = logging.getLogger(__name__)


def collect_jobs(db_connection, since: datetime, until: datetime) -> DashboardJobsResultType:
    return dashboard_jobs(since, until, db_connection)


@task(queue="metrics_collectors", decorate=False)
@task_execution_wrapper("collect_dashboard_reports_data")
def collect_dashboard_reports_data(**kwargs) -> dict[str, Any]:
    # Database connection
    db_name = kwargs.get('database', DEFAULT_DB_NAME)
    db_connection = get_db_connection(db_name)

    until = kwargs.get('until', None)
    since = kwargs.get('since', None)

    incremental = kwargs.get('incremental', False)

    if incremental:
        logger.debug("Incremental collection: using last timestamps from metadata tables")
        since = JobData.last_timestamp()
        until = datetime.now().astimezone(tz=pytz.UTC)
        if since is None:
            since = until - timedelta(days=30)
            since = since.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
            since = since.astimezone(tz=pytz.UTC)
    else:
        logger.debug("Non-incremental collection: using provided date range or defaults")
        if until is None:
            until = datetime.now().astimezone(tz=pytz.UTC)
        if since is None:
            since = until - timedelta(days=30)
            since = since.replace(hour=0, minute=0, second=0, microsecond=0)
            since = since.astimezone(tz=pytz.UTC)

    if since >= until:
        msg = f"Invalid date range: since ({since.isoformat()}) must be before until ({until.isoformat()})"
        logger.error(msg)
        return create_task_result('error', error=msg)

    start_str = since.isoformat()
    end_str = until.isoformat()
    log_task_execution(
        "collect_dashboard_reports",
        "processing",
        f"Collecting dashboard data for: {start_str} to {end_str}"
    )

    # Collect jobs
    log_task_execution("collect_dashboard_reports", "processing", "Collecting jobs")
    try:
        jobs = collect_jobs(db_connection, since=since, until=until)
    except Exception as e:
        logger.error(f"Error collecting jobs: {str(e)}")
        return create_task_result('error', error=f"Collecting jobs failed: {str(e)}")

    for job in jobs['results']:
        with transaction.atomic():
            try:
                JobData.create_or_update_from_awx(job)
            except Exception as e:
                transaction.set_rollback(True)
                logger.error(f"Error creating/updating JobData for job {job['id']}: {str(e)}")
                return create_task_result('error',
                                          error=f"Creating/updating JobData for job {job['id']} failed: {str(e)}")

    job_count = jobs['count']

    log_task_execution(
        "collect_dashboard_reports",
        "completed",
        f"Collected {job_count} jobs for {start_str} to {end_str}"
    )
    return create_task_result(
        'success',
        {
            'task_type': 'collect_dashboard_reports',
            'date_range': {
                'start': start_str,
                'end': end_str,
                'incremental': incremental,
            },
            'jobs_count': job_count
        }
    )
