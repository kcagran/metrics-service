"""
Task groups configuration for organized task management.

This module defines task groups with feature enabled controls and provides
a centralized way to manage different categories of tasks.

Feature enabled settings are stored in the database using the Setting model, allowing
runtime configuration without code changes. Values fall back to Django
settings if not found in the database.
"""

import json
import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def get_feature_enabled_from_db(setting_name: str, default: bool = False) -> bool:
    """
    Get a feature enabled value from database settings.

    Args:
        setting_name: Name of the feature enabled setting
        default: Default value if not found in database

    Returns:
        bool: Feature enabled value from database or default
    """
    try:
        # Avoid circular import by importing here
        from apps.dynamic_settings.models import Setting

        setting = Setting.objects.filter(setting_key=setting_name).first()
        if setting and setting.current_value:
            try:
                # Parse JSON value from database
                value = json.loads(setting.current_value)
                return bool(value)
            except (json.JSONDecodeError, ValueError):
                # If not valid JSON, treat as string boolean
                return setting.current_value.lower() in ("true", "1", "yes", "on")

        # Fallback to Django settings
        feature_enabled = getattr(settings, "FEATURE_ENABLED", {})
        return feature_enabled.get(setting_name, default)

    except Exception as e:
        logger.warning(f"Error reading feature enabled setting {setting_name} from database: {e}")
        # Fallback to Django settings
        feature_enabled = getattr(settings, "FEATURE_ENABLED", {})
        return feature_enabled.get(setting_name, default)


class TaskGroup:
    """
    Represents a group of related tasks with feature enabled control.
    """

    def __init__(
        self,
        name: str,
        description: str,
        enabled_setting: str = None,
        default_enabled: bool = True,
        tasks: list[dict[str, Any]] = None,
    ):
        """
        Initialize a task group.

        Args:
            name: Unique name for the task group
            description: Human-readable description
            enabled_setting: Feature enabled setting name to control this group
            default_enabled: Default state if feature enabled setting not set
            tasks: List of task configurations in this group
        """
        self.name = name
        self.description = description
        self.enabled_setting = enabled_setting
        self.default_enabled = default_enabled
        self.tasks = tasks or []

    def is_enabled(self) -> bool:
        """
        Check if this task group is enabled based on feature enabled settings.

        Feature enabled settings are read from database first, with fallback to
        Django settings if not found in database.

        Returns:
            bool: True if group is enabled, False otherwise
        """
        if not self.enabled_setting:
            return self.default_enabled

        return get_feature_enabled_from_db(self.enabled_setting, self.default_enabled)

    def get_enabled_tasks(self) -> list[dict[str, Any]]:
        """
        Get all tasks in this group that should be active.

        Returns:
            List of task configurations if group is enabled, empty list otherwise
        """
        if not self.is_enabled():
            return []

        return [task for task in self.tasks if task.get("enabled", True)]


# System Tasks Group - Always enabled, core system maintenance
SYSTEM_TASKS_GROUP = TaskGroup(
    name="system_tasks",
    description="Core system maintenance tasks that are always enabled",
    enabled_setting=None,  # Always enabled
    default_enabled=True,
    tasks=[
        {
            "task_id": "daily_task_cleanup",
            "function": "cleanup_old_tasks",
            "cron": "0 2 * * *",  # Daily at 2 AM
            "args": {
                "days_old": 5,
                "dry_run": False,
                "include_executions": True,
                "preserve_recurring": True,
            },
            "enabled": True,
            "description": "Daily cleanup of old completed/failed tasks (preserves recurring tasks)",
            "category": "maintenance",
        },
        {
            "task_id": "weekly_data_cleanup",
            "function": "cleanup_old_data",
            "cron": "0 3 * * 0",  # Weekly on Sunday at 3 AM
            "args": {"days_old": 30, "data_types": ["logs", "temp_files", "cache"]},
            "enabled": True,
            "description": "Weekly cleanup of old system data and temporary files",
            "category": "maintenance",
        },
        {
            "task_id": "hourly_health_check",
            "function": "hello_world",
            "cron": "0 * * * *",  # Every hour
            "args": {},
            "enabled": True,
            "description": "Hourly system health check",
            "category": "monitoring",
        },
    ],
)

# Anonymized Data Collection Group - Controlled by feature enabled setting
ANONYMIZED_DATA_GROUP = TaskGroup(
    name="anonymized_data",
    description="Anonymized data collection tasks",
    enabled_setting="ANONYMIZED_DATA_COLLECTION",
    default_enabled=True,  # Default enabled but can be controlled
    tasks=[
        {
            "task_id": "full_process_anonymize",
            "function": "full_process_anonymize",
            "cron": "0 */12 * * *",  # Every 12 hours
            "args": {},
            "enabled": True,
            "description": "Collect anonymized metrics and send directly to Segment.com",
            "category": "anonymous_metrics",
        },
    ],
)

# Metrics Collection Group - Customer controlled
METRICS_COLLECTION_GROUP = TaskGroup(
    name="metrics_collection",
    description="Customer-controlled metrics collection tasks",
    enabled_setting="METRICS_COLLECTION_ENABLED",
    default_enabled=False,  # Customers must explicitly enable
    tasks=[
        {
            "task_id": "collect_all_metrics_daily",
            "function": "collect_metrics",  # Changed from collect_all_metrics to collect_metrics
            "cron": "0 1 * * *",  # Daily at 1 AM
            "args": {"collectors": ["anonymized_rollups", "config", "job_host_summary", "main_host", "main_jobevent"]},
            "enabled": True,
            "description": "Daily comprehensive metrics collection using all collectors",
            "category": "metrics_collection",
        },
    ],
)

# Hourly Metrics Collection Group - Customer controlled, hourly granularity with daily rollup
HOURLY_METRICS_GROUP = TaskGroup(
    name="hourly_metrics",
    description="Hourly metrics collection with daily rollup and anonymization",
    enabled_setting="METRICS_COLLECTION_ENABLED",
    default_enabled=False,  # Disabled by default, customer opt-in required
    tasks=[
        # Hourly Collection Tasks
        {
            "task_id": "hourly_job_host_summary",
            "function": "collect_job_host_summary_hourly",
            "cron": "5 * * * *",  # Every hour at XX:05
            "args": {},
            "enabled": True,
            "description": "Collect job host summary metrics every hour",
            "category": "hourly_collection",
        },
        {
            "task_id": "hourly_host_metrics",
            "function": "collect_host_metrics_hourly",
            "cron": "10 * * * *",  # Every hour at XX:10
            "args": {},
            "enabled": True,
            "description": "Collect host metrics every hour",
            "category": "hourly_collection",
        },
        {
            "task_id": "hourly_main_host",
            "function": "collect_main_host_hourly",
            "cron": "15 * * * *",  # Every hour at XX:15
            "args": {},
            "enabled": True,
            "description": "Collect main_host metrics every hour",
            "category": "hourly_collection",
        },
        # Daily Rollup Tasks
        {
            "task_id": "daily_metrics_rollup",
            "function": "daily_metrics_rollup",
            "cron": "0 2 * * *",  # Daily at 2:00 AM
            "args": {},
            "enabled": True,
            "description": "Create daily rollup from hourly collections",
            "category": "daily_rollup",
        },
        {
            "task_id": "daily_anonymize",
            "function": "daily_anonymize_and_prepare",
            "cron": "0 3 * * *",  # Daily at 3:00 AM
            "args": {},
            "enabled": True,
            "description": "Anonymize daily summary for Segment transmission",
            "category": "daily_anonymization",
        },
        {
            "task_id": "send_to_segment_daily",
            "function": "send_anonymized_to_segment",
            "cron": "30 3 * * *",  # Daily at 3:30 AM
            "args": {},
            "enabled": True,
            "description": "Send anonymized payloads to Segment",
            "category": "daily_send",
        },
        # Cleanup Task
        {
            "task_id": "cleanup_metrics_data",
            "function": "cleanup_metrics_data",
            "cron": "0 4 * * *",  # Daily at 4:00 AM
            "args": {
                "hourly_retention_days": 7,
                "daily_retention_days": 30,
                "payload_retention_days": 7,
            },
            "enabled": True,
            "description": "Clean up old metrics data based on retention policies",
            "category": "maintenance",
        },
    ],
)

# Dashboard Collection Group - automation-reports integration
DASHBOARD_COLLECTION_GROUP = TaskGroup(
    name="dashboard_collection",
    description="Automation-reports dashboard data collection (SQL-based, separate from anonymization)",
    enabled_setting="ENABLE_DASHBOARD_COLLECTION",
    default_enabled=False,  # Customer opt-in required
    tasks=[
        {
            "task_id": "daily_dashboard_collection",
            "function": "collect_dashboard_reports_data",
            "cron": "0 1 * * *",  # Daily at 1:00 AM
            "args": {"incremental": True},  # Uses incremental collection by default to minimize load
            "enabled": True,
            "description": "Daily dashboard report collection (last 30 days)",
            "category": "dashboard_collection",
        },
    ],
)

# Registry of all task groups
TASK_GROUPS = [
    SYSTEM_TASKS_GROUP,
    ANONYMIZED_DATA_GROUP,
    METRICS_COLLECTION_GROUP,
    HOURLY_METRICS_GROUP,
    DASHBOARD_COLLECTION_GROUP
]


def get_all_enabled_tasks() -> dict[str, dict[str, Any]]:
    """
    Get all enabled tasks from all groups.

    Returns:
        Dictionary mapping task_id to task configuration for all enabled tasks.
        Each task config includes the feature_flag from its group for runtime checking.
    """
    all_tasks = {}

    for group in TASK_GROUPS:
        enabled_tasks = group.get_enabled_tasks()
        for task in enabled_tasks:
            task_id = task["task_id"]
            task_config = task.copy()
            task_config["group"] = group.name
            task_config["group_description"] = group.description
            # Add feature flag for runtime checking
            task_config["feature_flag"] = group.enabled_setting
            all_tasks[task_id] = task_config

    return all_tasks


def get_task_group_status() -> dict[str, Any]:
    """
    Get status of all task groups including feature enabled setting source information.

    Returns:
        Dictionary with status information for each group
    """
    status = {}
    enabled_status = get_feature_enabled_status()

    for group in TASK_GROUPS:
        group_status = {
            "name": group.name,
            "description": group.description,
            "enabled": group.is_enabled(),
            "enabled_setting": group.enabled_setting,
            "total_tasks": len(group.tasks),
            "enabled_tasks": len(group.get_enabled_tasks()),
            "tasks": group.get_enabled_tasks() if group.is_enabled() else [],
        }

        # Add feature enabled setting details if available
        if group.enabled_setting and group.enabled_setting in enabled_status:
            setting_info = enabled_status[group.enabled_setting]
            group_status["setting_source"] = setting_info["source"]
            if "last_modified" in setting_info:
                group_status["setting_last_modified"] = setting_info["last_modified"]
            if "last_modified_by" in setting_info:
                group_status["setting_last_modified_by"] = setting_info["last_modified_by"]

        status[group.name] = group_status

    return status


def enable_task_group(group_name: str, user=None) -> bool:
    """
    Enable a task group by updating its feature enabled setting in the database.

    Args:
        group_name: Name of the group to enable
        user: User making the change (optional)

    Returns:
        bool: True if group was found and enabled successfully
    """
    group = next((g for g in TASK_GROUPS if g.name == group_name), None)
    if not group or not group.enabled_setting:
        return False

    try:
        from apps.dynamic_settings.models import Setting

        # Get current value for logging
        old_value = get_feature_enabled_from_db(group.enabled_setting, group.default_enabled)

        # Update or create the setting
        setting, created = Setting.objects.get_or_create(
            setting_key=group.enabled_setting,
            defaults={
                "current_value": json.dumps(True),
                "previous_value": json.dumps(old_value),
                "last_modified_by": user,
            },
        )

        if not created:
            setting.previous_value = setting.current_value
            setting.current_value = json.dumps(True)
            setting.last_modified_by = user
            setting.save()

        logger.info(f"Enabled task group: {group_name} via setting: {group.enabled_setting}")
        return True

    except Exception as e:
        logger.error(f"Failed to enable task group {group_name}: {e}")
        return False


def disable_task_group(group_name: str, user=None) -> bool:
    """
    Disable a task group by updating its feature enabled setting in the database.

    Args:
        group_name: Name of the group to disable
        user: User making the change (optional)

    Returns:
        bool: True if group was found and disabled successfully
    """
    group = next((g for g in TASK_GROUPS if g.name == group_name), None)
    if not group or not group.enabled_setting:
        return False

    # System tasks cannot be disabled
    if group.name == "system_tasks":
        logger.warning("Cannot disable system tasks group")
        return False

    try:
        from apps.dynamic_settings.models import Setting

        # Get current value for logging
        old_value = get_feature_enabled_from_db(group.enabled_setting, group.default_enabled)

        # Update or create the setting
        setting, created = Setting.objects.get_or_create(
            setting_key=group.enabled_setting,
            defaults={
                "current_value": json.dumps(False),
                "previous_value": json.dumps(old_value),
                "last_modified_by": user,
            },
        )

        if not created:
            setting.previous_value = setting.current_value
            setting.current_value = json.dumps(False)
            setting.last_modified_by = user
            setting.save()

        logger.info(f"Disabled task group: {group_name} via setting: {group.enabled_setting}")
        return True

    except Exception as e:
        logger.error(f"Failed to disable task group {group_name}: {e}")
        return False


def set_feature_enabled(setting_name: str, value: bool, user=None) -> bool:
    """
    Set a feature enabled value in the database.

    Args:
        setting_name: Name of the feature enabled setting
        value: Boolean value to set
        user: User making the change (optional)

    Returns:
        bool: True if successfully set, False otherwise
    """
    try:
        from apps.dynamic_settings.models import Setting

        # Get current value for logging
        old_value = get_feature_enabled_from_db(setting_name, False)

        # Update or create the setting
        setting, created = Setting.objects.get_or_create(
            setting_key=setting_name,
            defaults={
                "current_value": json.dumps(value),
                "previous_value": json.dumps(old_value),
                "last_modified_by": user,
            },
        )

        if not created:
            setting.previous_value = setting.current_value
            setting.current_value = json.dumps(value)
            setting.last_modified_by = user
            setting.save()

        logger.info(f"Set feature enabled setting {setting_name} to {value}")
        return True

    except Exception as e:
        logger.error(f"Failed to set feature enabled setting {setting_name}: {e}")
        return False


def get_feature_enabled_status() -> dict[str, Any]:
    """
    Get the status of all feature enabled settings used by task groups.

    Returns:
        dict: Status of each feature enabled setting including source (db/settings/default)
    """
    settings_status = {}

    for group in TASK_GROUPS:
        if group.enabled_setting:
            try:
                from apps.dynamic_settings.models import Setting

                # Check if setting exists in database
                db_setting = Setting.objects.filter(setting_key=group.enabled_setting).first()
                if db_setting and db_setting.current_value:
                    try:
                        db_value = json.loads(db_setting.current_value)
                        settings_status[group.enabled_setting] = {
                            "value": bool(db_value),
                            "source": "database",
                            "last_modified": db_setting.modified,
                            "last_modified_by": (
                                db_setting.last_modified_by.username if db_setting.last_modified_by else "System"
                            ),
                            "group": group.name,
                        }
                        continue
                    except (json.JSONDecodeError, ValueError):
                        pass

                # Check Django settings
                feature_enabled = getattr(settings, "FEATURE_ENABLED", {})
                if group.enabled_setting in feature_enabled:
                    settings_status[group.enabled_setting] = {
                        "value": feature_enabled[group.enabled_setting],
                        "source": "django_settings",
                        "group": group.name,
                    }
                else:
                    settings_status[group.enabled_setting] = {
                        "value": group.default_enabled,
                        "source": "default",
                        "group": group.name,
                    }

            except Exception as e:
                logger.warning(f"Error getting status for setting {group.enabled_setting}: {e}")
                settings_status[group.enabled_setting] = {
                    "value": group.default_enabled,
                    "source": "error_fallback",
                    "error": str(e),
                    "group": group.name,
                }

    return settings_status


def get_tasks_by_category(category: str) -> list[dict[str, Any]]:
    """
    Get all enabled tasks in a specific category.

    Args:
        category: Category name to filter by

    Returns:
        List of task configurations in the specified category
    """
    all_tasks = get_all_enabled_tasks()
    return [task for task in all_tasks.values() if task.get("category") == category]


def _validate_task_id(task_id: str | None, group_name: str, all_task_ids: list[str]) -> list[str]:
    """Validate a single task ID."""
    errors = []
    if not task_id:
        errors.append(f"Task in group {group_name} missing task_id")
        return errors

    if task_id in all_task_ids:
        errors.append(f"Duplicate task_id: {task_id}")

    return errors


def _validate_required_fields(task: dict, task_id: str) -> list[str]:
    """Validate required fields for a task."""
    errors = []
    required_fields = ["function", "cron", "description"]
    for field in required_fields:
        if not task.get(field):
            errors.append(f"Task {task_id} missing required field: {field}")
    return errors


def validate_task_groups() -> list[str]:
    """
    Validate all task groups and return any errors found.

    Returns:
        List of error messages, empty if no errors
    """
    errors = []
    all_task_ids = []

    for group in TASK_GROUPS:
        for task in group.tasks:
            task_id = task.get("task_id")

            # Validate task ID
            id_errors = _validate_task_id(task_id, group.name, all_task_ids)
            errors.extend(id_errors)

            if task_id and task_id not in all_task_ids:
                all_task_ids.append(task_id)

            # Validate required fields
            if task_id:
                errors.extend(_validate_required_fields(task, task_id))

    return errors
