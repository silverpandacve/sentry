from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from sentry.api.bases.project import ProjectEndpoint
from sentry.api.exceptions import ResourceDoesNotExist
from sentry.snuba.metrics import DATA_SOURCE, InvalidField, InvalidParams, QueryDefinition


class ProjectMetricsEndpoint(ProjectEndpoint):
    """ Get metric name, type, unit and tag names """

    def get(self, request, project):
        metrics = DATA_SOURCE.get_metrics(project)
        return Response(metrics, status=200)


class ProjectMetricDetailsEndpoint(ProjectEndpoint):
    """ Get metric name, type, unit and tag names """

    def get(self, request, project, metric_name):
        try:
            metric = DATA_SOURCE.get_single_metric(project, metric_name)
        except InvalidParams:
            raise ResourceDoesNotExist(detail=f"metric '{metric_name}'")

        return Response(metric, status=200)


class ProjectMetricsTagsEndpoint(ProjectEndpoint):
    """ Get all existing tag values for a metric """

    def get(self, request, project):

        try:
            tag_names = DATA_SOURCE.get_tag_names(project)
        except InvalidParams as exc:
            raise (ParseError(detail=str(exc)))

        return Response(tag_names, status=200)


class ProjectMetricsTagDetailsEndpoint(ProjectEndpoint):
    """ Get all existing tag values for a metric """

    def get(self, request, project, tag_name):

        try:
            tag_values = DATA_SOURCE.get_tag_values(project, tag_name)
        except InvalidParams as exc:
            raise (ParseError(detail=str(exc)))

        if not tag_values:
            # NOTE: this behavior might change once we have a true tag indexer
            raise ResourceDoesNotExist(f"tag '{tag_name}'")

        return Response(tag_values, status=200)


class ProjectMetricsDataEndpoint(ProjectEndpoint):
    """Get the time series data for one or more metrics.

    The data can be filtered and grouped by tags.
    Based on `OrganizationSessionsEndpoint`.
    """

    def get(self, request, project):

        try:
            query = QueryDefinition(request.GET, allow_minute_resolution=False)
            data = DATA_SOURCE.get_series(query)
        except (InvalidField, InvalidParams) as exc:
            raise (ParseError(detail=str(exc)))

        return Response(data, status=200)
