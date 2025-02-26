import enum
import itertools
import random
import re
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from operator import itemgetter
from typing import (
    Any,
    Collection,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TypedDict,
    Union,
)

from snuba_sdk import (
    And,
    Column,
    Condition,
    Entity,
    Function,
    Granularity,
    Limit,
    Offset,
    Op,
    Or,
    Query,
)
from snuba_sdk.conditions import BooleanCondition
from snuba_sdk.orderby import Direction, OrderBy

from sentry.models import Project
from sentry.sentry_metrics import indexer
from sentry.snuba.dataset import Dataset, EntityKey
from sentry.snuba.sessions_v2 import (  # TODO: unite metrics and sessions_v2
    AllowedResolution,
    InvalidField,
    InvalidParams,
    finite_or_none,
    get_constrained_date_range,
)
from sentry.utils.snuba import parse_snuba_datetime, raw_snql_query

FIELD_REGEX = re.compile(r"^(\w+)\(((\w|\.|_)+)\)$")
TAG_REGEX = re.compile(r"^(\w|\.|_)+$")

_OPERATIONS_PERCENTILES = (
    "p50",
    "p75",
    "p90",
    "p95",
    "p99",
)

OPERATIONS = (
    "avg",
    "count_unique",
    "count",
    "max",
    "sum",
) + _OPERATIONS_PERCENTILES

#: Max number of data points per time series:
MAX_POINTS = 10000


TS_COL_QUERY = "timestamp"
TS_COL_GROUP = "bucketed_time"


def reverse_resolve(index: int) -> str:
    resolved = indexer.reverse_resolve(index)
    # If we cannot find a string for an integer index, that's a bug:
    assert resolved is not None, index

    return resolved


def parse_field(field: str) -> Tuple[str, str]:
    matches = FIELD_REGEX.match(field)
    try:
        if matches is None:
            raise TypeError
        operation = matches[1]
        metric_name = matches[2]
    except (IndexError, TypeError):
        raise InvalidField(f"Failed to parse '{field}'. Must be something like 'sum(my_metric)'.")
    else:
        if operation not in OPERATIONS:

            raise InvalidField(
                f"Invalid operation '{operation}'. Must be one of {', '.join(OPERATIONS)}"
            )

        return operation, metric_name


def verify_tag_name(name: str) -> str:

    if not TAG_REGEX.match(name):
        raise InvalidParams(f"Invalid tag name: '{name}'")

    return name


def parse_tag(tag_string: str) -> Tuple[str, str]:
    try:
        name, value = tag_string.split(":")
    except ValueError:
        raise InvalidParams(f"Expected something like 'foo:\"bar\"' for tag, got '{tag_string}'")

    return (verify_tag_name(name), value.strip('"'))


def parse_query(query_string: str) -> dict:
    return {
        "or": [
            {"and": [parse_tag(and_part) for and_part in or_part.split(" and ")]}
            for or_part in query_string.split(" or ")
        ]
    }


class QueryDefinition:
    """
    This is the definition of the query the user wants to execute.
    This is constructed out of the request params, and also contains a list of
    `fields` and `groupby` definitions as [`ColumnDefinition`] objects.

    Adapted from [`sentry.snuba.sessions_v2`].

    """

    def __init__(self, query_params):

        self.query = query_params.get("query", "")
        self.parsed_query = parse_query(self.query) if self.query else None
        raw_fields = query_params.getlist("field", [])
        self.groupby = query_params.getlist("groupBy", [])

        if len(raw_fields) == 0:
            raise InvalidField('Request is missing a "field"')

        self.fields = {key: parse_field(key) for key in raw_fields}

        self.orderby = self._parse_orderby(query_params)
        self.limit = self._parse_limit(query_params)

        start, end, rollup = get_constrained_date_range(
            query_params, AllowedResolution.ten_seconds, max_points=MAX_POINTS
        )
        self.rollup = rollup
        self.start = start
        self.end = end

    def _parse_orderby(self, query_params):
        orderby = query_params.getlist("orderBy", [])
        if not orderby:
            return None
        elif len(orderby) > 1:
            raise InvalidParams("Only one 'orderBy' is supported")

        if len(self.fields) != 1:
            # If we were to allow multiple fields when `orderBy` is set,
            # we would require two snuba queries: one to get the sorted metric,
            # And one to get the fields that we are not currently sorting by.
            #
            # For example, the query
            #
            #   ?field=sum(foo)&field=sum(bar)&groupBy=tag1&orderBy=sum(foo)&limit=1
            #
            # with snuba entries (simplified)
            #
            #   | metric | tag1 | sum(value) |
            #   |----------------------------|
            #   | foo    | val1 |          2 |
            #   | foo    | val2 |          1 |
            #   | bar    | val1 |          3 |
            #   | bar    | val2 |          4 |
            #
            # Would require a query (simplified)
            #
            #   SELECT sum(value) BY tag1 WHERE metric = foo ORDER BY sum(value)
            #
            # ->
            #
            #   {tag1: val2, sum(value): 1}
            #
            # and then
            #
            #   SELECT sum(value) BY metric, tag WHERE metric in [bar] and tag1 in [val2]
            #
            # to get the values for the other requested field(s).
            #
            # Since we do not have a requirement for ordered multi-field results (yet),
            # let's keep it simple and only allow a single field when `orderBy` is set.
            #
            raise InvalidParams("Cannot provide multiple 'field's when 'orderBy' is given")
        orderby = orderby[0]
        direction = Direction.ASC
        if orderby[0] == "-":
            orderby = orderby[1:]
            direction = Direction.DESC
        try:
            op, metric_name = self.fields[orderby]
        except KeyError:
            # orderBy one of the group by fields may be supported in the future
            raise InvalidParams("'orderBy' must be one of the provided 'fields'")

        if op in _OPERATIONS_PERCENTILES:
            # NOTE(jjbayer): This should work, will fix later
            raise InvalidParams("'orderBy' percentiles is not yet supported")

        return (op, metric_name), direction

    def _parse_limit(self, query_params):
        limit = query_params.get("limit", None)
        if not self.orderby and limit:
            raise InvalidParams("'limit' is only supported in combination with 'orderBy'")

        if limit is not None:
            try:
                limit = int(limit)
                if limit < 1:
                    raise ValueError
            except (ValueError, TypeError):
                raise InvalidParams("'limit' must be integer >= 1")

        return limit


class TimeRange(Protocol):
    start: datetime
    end: datetime
    rollup: int


def get_intervals(query: TimeRange):
    start = query.start
    end = query.end
    delta = timedelta(seconds=query.rollup)
    while start < end:
        yield start
        start += delta


#: The type of metric, which determines the snuba entity to query
MetricType = Literal["counter", "set", "distribution"]

#: A function that can be applied to a metric
MetricOperation = Literal["avg", "count", "max", "min", "p50", "p75", "p90", "p95", "p99"]

MetricUnit = Literal["seconds"]


METRIC_TYPE_TO_ENTITY: Mapping[MetricType, EntityKey] = {
    "counter": EntityKey.MetricsCounters,
    "set": EntityKey.MetricsSets,
    "distribution": EntityKey.MetricsDistributions,
}


class MetricMeta(TypedDict):
    name: str
    type: MetricType
    operations: Collection[MetricOperation]
    unit: Optional[MetricUnit]


class Tag(TypedDict):
    key: str  # Called key here to be consistent with JS type


class TagValue(TypedDict):
    key: str
    value: str


class MetricMetaWithTagKeys(MetricMeta):
    tags: Sequence[Tag]


class DataSource(ABC):
    """Base class for metrics data sources"""

    @abstractmethod
    def get_metrics(self, projects: Sequence[Project]) -> Sequence[MetricMeta]:
        """Get metrics metadata, without tags"""

    @abstractmethod
    def get_single_metric(
        self, projects: Sequence[Project], metric_name: str
    ) -> MetricMetaWithTagKeys:
        """Get metadata for a single metric, without tag values"""

    @abstractmethod
    def get_series(self, projects: Sequence[Project], query: QueryDefinition) -> dict:
        """Get time series for the given query"""

    @abstractmethod
    def get_tags(self, projects: Sequence[Project], metric_names=None) -> Sequence[Tag]:
        """Get all available tag names for this project

        If ``metric_names`` is provided, the list of available tag names will
        only contain tags that appear in *all* these metrics.
        """

    @abstractmethod
    def get_tag_values(
        self, projects: Sequence[Project], tag_name: str, metric_names=None
    ) -> Sequence[TagValue]:
        """Get all known values for a specific tag"""


@dataclass
class SnubaField:
    snuba_function: Literal[
        "sum", "uniq", "avg", "count", "max", "min", "quantiles(0.5,0.75,0.9,0.95,0.99)"
    ]
    snuba_alias: Literal["value", "avg", "count", "max", "min", "percentiles"]


_OP_TO_FIELD = {
    "metrics_counters": {"sum": SnubaField("sum", "value")},
    "metrics_distributions": {
        "avg": SnubaField("avg", "avg"),
        "count": SnubaField("count", "count"),
        "max": SnubaField("max", "max"),
        "min": SnubaField("min", "min"),
        "p50": SnubaField("quantiles(0.5,0.75,0.9,0.95,0.99)", "percentiles"),
        "p75": SnubaField("quantiles(0.5,0.75,0.9,0.95,0.99)", "percentiles"),
        "p90": SnubaField("quantiles(0.5,0.75,0.9,0.95,0.99)", "percentiles"),
        "p95": SnubaField("quantiles(0.5,0.75,0.9,0.95,0.99)", "percentiles"),
        "p99": SnubaField("quantiles(0.5,0.75,0.9,0.95,0.99)", "percentiles"),
    },
    "metrics_sets": {"count_unique": SnubaField("uniq", "value")},
}

_AVAILABLE_OPERATIONS = {type_: sorted(mapping.keys()) for type_, mapping in _OP_TO_FIELD.items()}


_BASE_TAGS = {
    "environment": [
        "production",
        "staging",
    ],
    "release": [],
}

_SESSION_TAGS = dict(
    _BASE_TAGS,
    **{
        "session.status": [
            "abnormal",
            "crashed",
            "errored",
            "healthy",
        ],
    },
)

_MEASUREMENT_TAGS = dict(
    _BASE_TAGS,
    **{
        "measurement_rating": ["good", "meh", "poor"],
        "transaction": ["/foo/:ordId/", "/bar/:ordId/"],
    },
)

_METRICS = {
    "session": {
        "type": "counter",
        "operations": _AVAILABLE_OPERATIONS["metrics_counters"],
        "tags": _SESSION_TAGS,
    },
    "user": {
        "type": "set",
        "operations": _AVAILABLE_OPERATIONS["metrics_sets"],
        "tags": _SESSION_TAGS,
    },
    "session.duration": {
        "type": "distribution",
        "operations": _AVAILABLE_OPERATIONS["metrics_distributions"],
        "tags": _SESSION_TAGS,
        "unit": "seconds",
    },
    "session.error": {
        "type": "set",
        "operations": _AVAILABLE_OPERATIONS["metrics_sets"],
        "tags": _SESSION_TAGS,
    },
}

_METRICS.update(
    {
        f"measurement.{web_vital}": {
            "type": "distribution",
            "operations": _AVAILABLE_OPERATIONS["metrics_distributions"],
            "tags": _MEASUREMENT_TAGS,
        }
        for web_vital in ("lcp", "fcp", "fid", "cls")
    }
)


def _get_metric(metric_name: str) -> dict:
    try:
        metric = _METRICS[metric_name]
    except KeyError:
        raise InvalidParams(f"Unknown metric '{metric_name}'")

    return metric


class IndexMockingDataSource(DataSource):
    def get_metrics(self, projects: Sequence[Project]) -> Sequence[MetricMeta]:
        """Get metrics metadata, without tags"""
        return [
            MetricMeta(
                name=name,
                **{key: value for key, value in metric.items() if key != "tags"},
            )
            for name, metric in _METRICS.items()
        ]

    def get_single_metric(
        self, projects: Sequence[Project], metric_name: str
    ) -> MetricMetaWithTagKeys:
        """Get metadata for a single metric, without tag values"""
        try:
            metric = _METRICS[metric_name]
        except KeyError:
            raise InvalidParams()

        return dict(
            name=metric_name,
            **{
                # Only return tag names
                key: (sorted(value.keys()) if key == "tags" else value)
                for key, value in metric.items()
            },
        )

    @classmethod
    def _validate_metric_names(cls, metric_names):
        unknown_metric_names = set(metric_names) - _METRICS.keys()
        if unknown_metric_names:
            raise InvalidParams(f"Unknown metrics '{', '.join(unknown_metric_names)}'")

        return metric_names

    def get_tags(self, projects: Sequence[Project], metric_names=None) -> Sequence[Tag]:
        """Get all available tag names for this project

        If ``metric_names`` is provided, the list of available tag names will
        only contain tags that appear in *all* these metrics.
        """
        if metric_names is None:
            tag_names = sorted(
                {tag_name for metric in _METRICS.values() for tag_name in metric["tags"]}
            )
        else:
            metric_names = self._validate_metric_names(metric_names)
            key_sets = [set(_METRICS[metric_name]["tags"].keys()) for metric_name in metric_names]
            tag_names = sorted(set.intersection(*key_sets))

        return [{"key": tag_name} for tag_name in tag_names]

    @classmethod
    def _get_tag_values(cls, metric_name: str, tag_name: str) -> List[str]:
        metric = _get_metric(metric_name)
        try:
            tags = metric["tags"][tag_name]
        except KeyError:
            raise InvalidParams(f"Unknown tag '{tag_name}'")

        return tags

    def get_tag_values(
        self, projects: Sequence[Project], tag_name: str, metric_names=None
    ) -> Sequence[TagValue]:
        if metric_names is None:
            tag_values = sorted(
                {
                    tag_value
                    for metric in _METRICS.values()
                    for tag_value in metric["tags"].get(
                        tag_name, []
                    )  # TODO: validation of tag name
                }
            )
        else:
            metric_names = self._validate_metric_names(metric_names)
            value_sets = [
                set(self._get_tag_values(metric_name, tag_name)) for metric_name in metric_names
            ]
            tag_values = sorted(set.intersection(*value_sets))

        return [{"key": tag_name, "value": tag_value} for tag_value in tag_values]


class MockDataSource(IndexMockingDataSource):
    """Mocks metadata and time series"""

    #: Used to compute totals from series
    #: NOTE: Not mathematically correct but plausible mock
    _operations = {
        "avg": lambda values: sum(values) / len(values),
        "count_unique": lambda values: 3 * sum(values) // len(values),
        "count": sum,
        "max": max,
        "p50": lambda values: values[int(0.50 * len(values))],
        "p75": lambda values: values[int(0.75 * len(values))],
        "p90": lambda values: values[int(0.90 * len(values))],
        "p95": lambda values: values[int(0.95 * len(values))],
        "p99": lambda values: values[int(0.99 * len(values))],
        "sum": sum,
    }

    def _generate_series(self, fields: dict, intervals: List[datetime]) -> dict:
        series = {}
        totals = {}
        for field, (operation, metric_name) in fields.items():

            metric = _get_metric(metric_name)

            if operation not in metric["operations"]:
                raise InvalidParams(f"Invalid operation '{operation}' for metric '{metric_name}'")

            mu = 1000 * random.random()
            series[field] = [random.normalvariate(mu, 50) for _ in intervals]

            if operation == "count_unique":
                series[field] = list(map(int, series[field]))

            totals[field] = self._operations[operation](series[field])

        return {
            "totals": totals,
            "series": series,
        }

    def get_series(self, projects: Sequence[Project], query: QueryDefinition) -> dict:
        """Get time series for the given query"""

        intervals = list(get_intervals(query))

        tags = [
            {
                (tag_name, tag_value)
                for metric in _METRICS.values()
                for tag_value in metric["tags"].get(tag_name, [])
            }
            for tag_name in query.groupby
        ]

        return {
            "start": query.start,
            "end": query.end,
            "query": query.query,
            "intervals": intervals,
            "groups": [
                dict(
                    by={tag_name: tag_value for tag_name, tag_value in combination},
                    **self._generate_series(query.fields, intervals),
                )
                for combination in itertools.product(*tags)
            ]
            if tags
            else [dict(by={}, **self._generate_series(query.fields, intervals))],
        }


PERCENTILE_INDEX = {}


class Percentile(enum.Enum):
    p50 = 0
    p75 = 1
    p90 = 2
    p95 = 3
    p99 = 4


class SnubaQueryBuilder:

    #: Datasets actually implemented in snuba:
    _implemented_datasets = {
        "metrics_counters",
        "metrics_distributions",
        "metrics_sets",
    }

    def __init__(self, projects: Sequence[Project], query_definition: QueryDefinition):
        self._projects = projects
        self._queries = self._build_queries(query_definition)

    def _build_logical(self, operator, operands) -> Optional[BooleanCondition]:
        """Snuba only accepts And and Or if they have 2 elements or more"""
        operands = [operand for operand in operands if operand is not None]
        if not operands:
            return None
        if len(operands) == 1:
            return operands[0]

        return operator(operands)

    def _build_filter(self, query_definition: QueryDefinition) -> Optional[BooleanCondition]:
        filter_ = query_definition.parsed_query
        if filter_ is None:
            return None

        def to_int(string):
            try:
                return indexer.resolve(string)
            except KeyError:
                return None

        return self._build_logical(
            Or,
            [
                self._build_logical(
                    And,
                    [
                        Condition(
                            Column(f"tags[{to_int(tag)}]"),
                            Op.EQ,
                            to_int(value),
                        )
                        for tag, value in or_operand["and"]
                    ],
                )
                for or_operand in filter_["or"]
            ],
        )

    def _build_where(
        self, query_definition: QueryDefinition
    ) -> List[Union[BooleanCondition, Condition]]:
        assert self._projects
        org_id = self._projects[0].organization_id
        where: List[Union[BooleanCondition, Condition]] = [
            Condition(Column("org_id"), Op.EQ, org_id),
            Condition(Column("project_id"), Op.IN, [p.id for p in self._projects]),
            Condition(
                Column("metric_id"),
                Op.IN,
                [indexer.resolve(name) for _, name in query_definition.fields.values()],
            ),
            Condition(Column(TS_COL_QUERY), Op.GTE, query_definition.start),
            Condition(Column(TS_COL_QUERY), Op.LT, query_definition.end),
        ]
        filter_ = self._build_filter(query_definition)
        if filter_:
            where.append(filter_)

        return where

    def _build_groupby(self, query_definition: QueryDefinition) -> List[Column]:
        return [Column("metric_id")] + [
            Column(f"tags[{indexer.resolve(field)}]") for field in query_definition.groupby
        ]

    def _build_orderby(
        self, query_definition: QueryDefinition, entity: str
    ) -> Optional[List[OrderBy]]:
        if query_definition.orderby is None:
            return None
        (op, metric_name), direction = query_definition.orderby
        snuba_field = _OP_TO_FIELD[entity][op]

        return [OrderBy(Column(snuba_field.snuba_alias), direction)]

    def _build_queries(self, query_definition):
        queries_by_entity = OrderedDict()
        for op, metric_name in query_definition.fields.values():
            type_ = _get_metric(metric_name)["type"]
            entity = self._get_entity(type_)
            queries_by_entity.setdefault(entity, []).append((op, metric_name))

        where = self._build_where(query_definition)
        groupby = self._build_groupby(query_definition)

        return {
            entity: self._build_queries_for_entity(query_definition, entity, fields, where, groupby)
            for entity, fields in queries_by_entity.items()
        }

    @staticmethod
    def _build_select(entity, fields):
        for op, _ in fields:
            field = _OP_TO_FIELD[entity][op]
            yield Function(field.snuba_function, [Column("value")], field.snuba_alias)

    def _build_queries_for_entity(self, query_definition, entity, fields, where, groupby):
        totals_query = Query(
            dataset=Dataset.Metrics.value,
            match=Entity(entity),
            groupby=groupby,
            select=list(self._build_select(entity, fields)),
            where=where,
            limit=Limit(query_definition.limit or MAX_POINTS),
            offset=Offset(0),
            granularity=Granularity(query_definition.rollup),
            orderby=self._build_orderby(query_definition, entity),
        )

        if totals_query.orderby is None:
            series_query = totals_query.set_groupby(
                (totals_query.groupby or []) + [Column(TS_COL_GROUP)]
            )
        else:
            series_query = None

        return {
            "totals": totals_query,
            "series": series_query,
        }

    def get_snuba_queries(self):
        return self._queries

    def _get_entity(self, metric_type: MetricType) -> str:

        entity = METRIC_TYPE_TO_ENTITY[metric_type].value

        if entity not in self._implemented_datasets:
            raise NotImplementedError(f"Dataset not yet implemented: {entity}")

        return entity


_DEFAULT_AGGREGATES = {
    "avg": None,
    "count_unique": 0,
    "count": 0,
    "max": None,
    "p50": None,
    "p75": None,
    "p90": None,
    "p95": None,
    "p99": None,
    "sum": 0,
}


class SnubaResultConverter:
    """Interpret a Snuba result and convert it to API format"""

    def __init__(
        self,
        organization_id: int,
        query_definition: QueryDefinition,
        intervals: List[datetime],
        results,
    ):
        self._organization_id = organization_id
        self._query_definition = query_definition
        self._intervals = intervals
        self._results = results

        self._ops_by_metric = ops_by_metric = {}
        for op, metric in query_definition.fields.values():
            ops_by_metric.setdefault(metric, []).append(op)

        self._timestamp_index = {timestamp: index for index, timestamp in enumerate(intervals)}

    def _parse_tag(self, tag_string: str) -> str:
        tag_key = int(tag_string.replace("tags[", "").replace("]", ""))
        return reverse_resolve(tag_key)

    def _extract_data(self, entity, data, groups):
        tags = tuple((key, data[key]) for key in sorted(data.keys()) if key.startswith("tags["))

        metric_name = reverse_resolve(data["metric_id"])
        ops = self._ops_by_metric[metric_name]

        tag_data = groups.setdefault(
            tags,
            {
                "totals": {},
            },
        )

        timestamp = data.pop(TS_COL_GROUP, None)
        if timestamp is not None:
            timestamp = parse_snuba_datetime(timestamp)

        for op in ops:
            key = f"{op}({metric_name})"

            field = _OP_TO_FIELD[entity][op].snuba_alias
            value = data[field]
            if field == "percentiles":
                value = value[Percentile[op].value]

            # If this is time series data, add it to the appropriate series.
            # Else, add to totals
            if timestamp is None:
                tag_data["totals"][key] = finite_or_none(value)
            else:
                series = tag_data.setdefault("series", {}).setdefault(
                    key, len(self._intervals) * [_DEFAULT_AGGREGATES[op]]
                )
                series_index = self._timestamp_index[timestamp]
                series[series_index] = finite_or_none(value)

    def translate_results(self):
        groups = {}

        for entity, subresults in self._results.items():
            totals = subresults["totals"]["data"]
            for data in totals:
                self._extract_data(entity, data, groups)

            if "series" in subresults:
                series = subresults["series"]["data"]
                for data in series:
                    self._extract_data(entity, data, groups)

        groups = [
            dict(
                by={self._parse_tag(key): reverse_resolve(value) for key, value in tags},
                **data,
            )
            for tags, data in groups.items()
        ]

        return groups


class MetaFromSnuba:
    """Fetch metrics metadata (metric names, tag names, tag values, ...) from snuba.
    This is not intended for production use, but rather as an intermediate solution
    until we have a proper metadata store set up.

    To keep things simple, and hopefully reasonably efficient, we only look at
    the past 24 hours.
    """

    _granularity = 24 * 60 * 60  # coarsest granularity

    def __init__(self, projects: Sequence[Project]):
        assert projects
        self._org_id = projects[0].organization_id
        self._projects = projects

    def _get_data(
        self,
        *,
        entity_key: EntityKey,
        select: List[Column],
        where: List[Condition],
        groupby: List[Column],
        referrer: str,
    ) -> Mapping[str, Any]:
        # Round timestamp to minute to get cache efficiency:
        now = datetime.now().replace(second=0, microsecond=0)

        query = Query(
            dataset=Dataset.Metrics.value,
            match=Entity(entity_key.value),
            select=select,
            groupby=groupby,
            where=[
                Condition(Column("org_id"), Op.EQ, self._org_id),
                Condition(Column("project_id"), Op.IN, [p.id for p in self._projects]),
                Condition(Column(TS_COL_QUERY), Op.GTE, now - timedelta(hours=24)),
                Condition(Column(TS_COL_QUERY), Op.LT, now),
            ]
            + where,
            granularity=Granularity(self._granularity),
        )
        result = raw_snql_query(query, referrer, use_cache=True)
        return result["data"]

    def _get_metrics_for_entity(self, entity_key: EntityKey) -> Mapping[str, Any]:
        return self._get_data(
            entity_key=entity_key,
            select=[Column("metric_id")],
            groupby=[Column("metric_id")],
            where=[],
            referrer="snuba.metrics.get_metrics_names_for_entity",
        )

    def get_metrics(self) -> Sequence[MetricMeta]:
        metric_names = (
            (metric_type, row)
            for metric_type in ("counter", "set", "distribution")
            for row in self._get_metrics_for_entity(METRIC_TYPE_TO_ENTITY[metric_type])
        )

        return [
            MetricMeta(
                name=reverse_resolve(row["metric_id"]),
                type=metric_type,
                operations=_AVAILABLE_OPERATIONS[METRIC_TYPE_TO_ENTITY[metric_type].value],
                unit=None,  # snuba does not know the unit
            )
            for metric_type, row in metric_names
        ]

    def get_single_metric(self, metric_name: str) -> MetricMetaWithTagKeys:
        """Get metadata for a single metric, without tag values"""
        metric_id = indexer.resolve(metric_name)
        if metric_id is None:
            raise InvalidParams

        for metric_type in ("counter", "set", "distribution"):
            # TODO: What if metric_id exists for multiple types / units?
            entity_key = METRIC_TYPE_TO_ENTITY[metric_type]
            data = self._get_data(
                entity_key=entity_key,
                select=[Column("metric_id"), Column("tags.key")],
                where=[Condition(Column("metric_id"), Op.EQ, metric_id)],
                groupby=[Column("metric_id"), Column("tags.key")],
                referrer="snuba.metrics.meta.get_single_metric",
            )
            if data:
                tag_ids = {tag_id for row in data for tag_id in row["tags.key"]}
                return {
                    "name": metric_name,
                    "type": metric_type,
                    "operations": _AVAILABLE_OPERATIONS[entity_key.value],
                    "tags": sorted(
                        ({"key": reverse_resolve(tag_id)} for tag_id in tag_ids),
                        key=itemgetter("key"),
                    ),
                    "unit": None,
                }

        raise InvalidParams

    def _get_metrics_filter(
        self, metric_names: Optional[Sequence[str]]
    ) -> Optional[List[Condition]]:
        """Add a condition to filter by metrics. Return None if a name cannot be resolved."""
        where = []
        if metric_names is not None:
            metric_ids = []
            for name in metric_names:
                resolved = indexer.resolve(name)
                if resolved is None:
                    # We are looking for tags that appear in all given metrics.
                    # A tag cannot appear in a metric if the metric is not even indexed.
                    return None
                metric_ids.append(resolved)
            where.append(Condition(Column("metric_id"), Op.IN, metric_ids))

        return where

    def get_tags(self, metric_names: Optional[Sequence[str]]) -> Sequence[Tag]:
        """Get all metric tags for the given projects and metric_names"""
        where = self._get_metrics_filter(metric_names)
        if where is None:
            return []

        tag_ids_per_metric_id = defaultdict(list)

        for metric_type in ("counter", "set", "distribution"):
            # TODO: What if metric_id exists for multiple types / units?
            entity_key = METRIC_TYPE_TO_ENTITY[metric_type]
            rows = self._get_data(
                entity_key=entity_key,
                select=[Column("metric_id"), Column("tags.key")],
                where=where,
                groupby=[Column("metric_id"), Column("tags.key")],
                referrer="snuba.metrics.meta.get_tags",
            )
            for row in rows:
                tag_ids_per_metric_id[row["metric_id"]].extend(row["tags.key"])

        tag_id_lists = tag_ids_per_metric_id.values()
        if metric_names is not None:
            # Only return tags that occur in all metrics
            tag_ids = set.intersection(*map(set, tag_id_lists))
        else:
            tag_ids = {tag_id for ids in tag_id_lists for tag_id in ids}

        tags = [{"key": reverse_resolve(tag_id)} for tag_id in tag_ids]
        tags.sort(key=itemgetter("key"))

        return tags

    def get_tag_values(
        self, tag_name: str, metric_names: Optional[Sequence[str]]
    ) -> Sequence[TagValue]:
        """Get all known values for a specific tag"""
        tag_id = indexer.resolve(tag_name)
        if tag_id is None:
            raise InvalidParams

        where = self._get_metrics_filter(metric_names)
        if where is None:
            return []

        tags = defaultdict(list)

        column_name = f"tags[{tag_id}]"
        for metric_type in ("counter", "set", "distribution"):
            # TODO: What if metric_id exists for multiple types / units?
            entity_key = METRIC_TYPE_TO_ENTITY[metric_type]
            rows = self._get_data(
                entity_key=entity_key,
                select=[Column("metric_id"), Column(column_name)],
                where=where,
                groupby=[Column("metric_id"), Column(column_name)],
                referrer="snuba.metrics.meta.get_tag_values",
            )
            for row in rows:
                value_id = row[column_name]
                if value_id > 0:
                    metric_id = row["metric_id"]
                    tags[metric_id].append(value_id)

        value_id_lists = tags.values()
        if metric_names is not None:
            # Only return tags that occur in all metrics
            value_ids = set.intersection(*[set(ids) for ids in value_id_lists])
        else:
            value_ids = {value_id for ids in value_id_lists for value_id in ids}

        tags = [{"key": tag_name, "value": reverse_resolve(value_id)} for value_id in value_ids]
        tags.sort(key=itemgetter("key"))

        return tags


class SnubaDataSource(DataSource):
    """Get both metadata and time series from Snuba"""

    def get_metrics(self, projects: Sequence[Project]) -> Sequence[MetricMeta]:
        meta = MetaFromSnuba(projects)
        return meta.get_metrics()

    def get_single_metric(
        self, projects: Sequence[Project], metric_name: str
    ) -> MetricMetaWithTagKeys:
        """Get metadata for a single metric, without tag values"""
        meta = MetaFromSnuba(projects)
        return meta.get_single_metric(metric_name)

    def get_tags(self, projects: Sequence[Project], metric_names=None) -> Sequence[Tag]:
        """Get all available tag names for this project

        If ``metric_names`` is provided, the list of available tag names will
        only contain tags that appear in *all* these metrics.
        """
        meta = MetaFromSnuba(projects)
        return meta.get_tags(metric_names)

    def get_tag_values(
        self, projects: Sequence[Project], tag_name: str, metric_names=None
    ) -> Sequence[TagValue]:
        """Get all known values for a specific tag"""
        meta = MetaFromSnuba(projects)
        return meta.get_tag_values(tag_name, metric_names)

    def get_series(self, projects: Sequence[Project], query: QueryDefinition) -> dict:
        """Get time series for the given query"""
        intervals = list(get_intervals(query))

        snuba_queries = SnubaQueryBuilder(projects, query).get_snuba_queries()
        results = {
            entity: {
                # TODO: Should we use cache?
                key: raw_snql_query(query, use_cache=False, referrer=f"api.metrics.{key}")
                for key, query in queries.items()
                if query is not None
            }
            for entity, queries in snuba_queries.items()
        }

        assert projects
        converter = SnubaResultConverter(projects[0].organization_id, query, intervals, results)

        return {
            "start": query.start,
            "end": query.end,
            "query": query.query,
            "intervals": intervals,
            "groups": converter.translate_results(),
        }
