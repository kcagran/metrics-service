"""
Top level settings file for all apps.

The settings here overrides any setting previously loaded
from the `metrics_service.settings`.
"""

from dynaconf import Dynaconf, post_hook

# Extra applications added after PSF templating
extra_applications = [
    "django_prometheus",
    "django_extensions",
]

# Default DAB applications layd out from PSF, add/remove according to the project needs,
# adjust `pyproject` dab extra dependencies acording to apps added/removed here
dab_applications = [
    "ansible_base.activitystream",
    "ansible_base.api_documentation",
    "ansible_base.feature_flags",
    "ansible_base.jwt_consumer",
    "ansible_base.rbac",
    "ansible_base.resource_registry",
    "ansible_base.rest_filters",
    "ansible_base.rest_pagination",
]

# List of applications from the apps/ folder
project_applications = [
    "apps.core",
    "apps.dynamic_settings",
    "apps.tasks",
    "apps.dashboard",
    "apps.dashboard_reports", # Dashboard data for automation-reports integration
]

# Final state of the INSTALLED_APPS that will merge with the rest of the settings
INSTALLED_APPS = [
    "dynaconf_merge_unique",  # DO NOT REMOVE THIS
    *dab_applications,
    *project_applications,
    *extra_applications,
    "django_generate_series" # Dashboard data for automation-reports integration
]

# Enable debug mode
DEBUG = False

# REST framework settings
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "UNAUTHENTICATED_USER": None,
    "UNAUTHENTICATED_TOKEN": None,
    "DEFAULT_FILTER_BACKENDS": [
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "apps.core.renderers.ServiceBrowsableAPIRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.NamespaceVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
}

# Title of Swagger the API documentation
SPECTACULAR_SETTINGS__TITLE = "metrics_service API"
# Description of Swagger the API documentation
SPECTACULAR_SETTINGS__DESCRIPTION = "API documentation for the metrics_service"
# Version of Swagger the API documentation
SPECTACULAR_SETTINGS__VERSION = "v1"
# Split components into request and response for generating clients
SPECTACULAR_SETTINGS__COMPONENT_SPLIT_REQUEST = True

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "default",
    },
}
CSRF_TRUSTED_ORIGINS = []

# Databases settings, using PostgreSQL by default
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "",  # require to be set at runtime
        "PORT": "5432",
        "USER": "metrics_service",
        "PASSWORD": "",  # require to be set at runtime
        "NAME": "metrics_service",
        "OPTIONS": {
            "sslmode": "prefer",
        },
    },
    # AWX database for metrics-utility collector integration
    # Override with METRICS_SERVICE_DATABASES__awx__HOST, etc.
    "awx": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "",  # require to be set at runtime
        "PORT": "5432",
        "USER": "myuser",
        "PASSWORD": "",  # require to be set at runtime
        "NAME": "awx",
        "OPTIONS": {
            "sslmode": "prefer",
        },
    },
}

# Feature flags
# TODO: convert to DAB feature flags
FEATURE_ENABLED = {
    "ANONYMIZED_DATA_COLLECTION": True,
    "METRICS_COLLECTION_ENABLED": False,
    "ENABLE_DASHBOARD_COLLECTION": False,  # automation-reports integration (customer opt-in)
}

# Used when generating API URLs in views, example "/api/metrics/"; None means "/api/"
URL_PREFIX = None


@post_hook
def load_prometheus_middlewares(settings: Dynaconf) -> dict:
    """Defer to execute after all settings are loaded."""
    middleware = settings.get("MIDDLEWARE", [])
    if "django_prometheus" in " ".join(middleware):
        return {}
    new = [
        "django_prometheus.middleware.PrometheusBeforeMiddleware",
        *middleware,
        "django_prometheus.middleware.PrometheusAfterMiddleware",
    ]
    return {"MIDDLEWARE": new}
