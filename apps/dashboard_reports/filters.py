from curses.ascii import isdigit

from pandas.core.dtypes.inference import is_number
from rest_framework import filters
from rest_framework.request import Request
from datetime import datetime

def get_filter_options(request: Request):
    filter_fields = [
        "organization",
        "template",
        "label",
        "project",
    ]
    filter_options = {}
    for field in filter_fields:
        values = request.query_params.getlist(field)
        if len(values) > 0:
            values = sorted([int(value) for value in values if value and value.isnumeric()], key=int)
            filter_options[field] = values
    start_date = request.query_params.get("start_date", None)
    end_date = request.query_params.get("end_date", None)
    if start_date:
        filter_options["start_date"] = datetime.fromisoformat(start_date)
    if end_date:
        filter_options["end_date"] = datetime.fromisoformat(end_date)

    return filter_options

class CustomReportFilter(filters.BaseFilterBackend):

    def filter_queryset(self, request, queryset, view):
        options = get_filter_options(request)
        queryset = queryset.organization(options.get("organization", None))
        queryset = queryset.project(options.get("project", None))
        queryset = queryset.template(options.get("template", None))
        queryset = queryset.label(options.get("label", None))
        queryset = queryset.after(options.get("start_date", None))
        queryset = queryset.before(options.get("end_date", None))
        return queryset