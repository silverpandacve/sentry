import 'echarts/lib/component/tooltip';

import {EChartOption} from 'echarts';
import moment from 'moment';

import BaseChart from 'sentry/components/charts/baseChart';
import {getFormattedDate, getTimeFormat} from 'sentry/utils/dates';

import {truncationFormatter} from '../utils';

type ChartProps = React.ComponentProps<typeof BaseChart>;

export function defaultFormatAxisLabel(
  value: number,
  isTimestamp: boolean,
  utc: boolean,
  showTimeInTooltip: boolean,
  addSecondsToTimeFormat: boolean,
  bucketSize?: number
) {
  if (!isTimestamp) {
    return value;
  }

  if (!bucketSize) {
    const format = `MMM D, YYYY ${
      showTimeInTooltip ? getTimeFormat({displaySeconds: addSecondsToTimeFormat}) : ''
    }`.trim();
    return getFormattedDate(value, format, {local: !utc});
  }

  const now = moment();
  const bucketStart = moment(value);
  const bucketEnd = moment(value + bucketSize);

  const showYear = now.year() !== bucketStart.year() || now.year() !== bucketEnd.year();
  const showEndDate = bucketStart.date() !== bucketEnd.date();

  const formatStart = `MMM D${showYear ? ', YYYY' : ''} ${
    showTimeInTooltip ? getTimeFormat({displaySeconds: addSecondsToTimeFormat}) : ''
  }`.trim();
  const formatEnd = `${showEndDate ? `MMM D${showYear ? ', YYYY' : ''} ` : ''}${
    showTimeInTooltip ? getTimeFormat({displaySeconds: addSecondsToTimeFormat}) : ''
  }`.trim();

  return `${getFormattedDate(bucketStart, formatStart, {
    local: !utc,
  })} — ${getFormattedDate(bucketEnd, formatEnd, {local: !utc})}`;
}

function defaultValueFormatter(value: string | number) {
  if (typeof value === 'number') {
    return value.toLocaleString();
  }

  return value;
}

function defaultNameFormatter(value: string) {
  return value;
}

function defaultMarkerFormatter(value: string) {
  return value;
}

function getSeriesValue(series: EChartOption.Tooltip.Format, offset: number) {
  if (!series.data) {
    return undefined;
  }
  if (Array.isArray(series.data)) {
    return series.data[offset];
  }
  if (Array.isArray(series.data.value)) {
    return series.data.value[offset];
  }

  return undefined;
}

type NeededChartProps = 'isGroupedByDate' | 'showTimeInTooltip' | 'utc' | 'bucketSize';

type TooltipFormatters =
  | 'truncate'
  | 'filter'
  | 'formatAxisLabel'
  | 'valueFormatter'
  | 'nameFormatter'
  | 'markerFormatter';

type FormatterOptions = Pick<NonNullable<ChartProps['tooltip']>, TooltipFormatters> &
  Pick<ChartProps, NeededChartProps> & {
    /**
     * Array containing seriesNames that need to be indented
     */
    indentLabels?: string[];
    /**
     * If true seconds will be added to the Axis label time format
     */
    addSecondsToTimeFormat?: boolean;
  };

function getFormatter({
  filter,
  isGroupedByDate,
  showTimeInTooltip,
  truncate,
  formatAxisLabel,
  utc,
  bucketSize,
  valueFormatter = defaultValueFormatter,
  nameFormatter = defaultNameFormatter,
  markerFormatter = defaultMarkerFormatter,
  indentLabels = [],
  addSecondsToTimeFormat = false,
}: FormatterOptions) {
  const getFilter = (seriesParam: EChartOption.Tooltip.Format) => {
    // Series do not necessarily have `data` defined, e.g. releases don't have `data`, but rather
    // has a series using strictly `markLine`s.
    // However, real series will have `data` as a tuple of (label, value) or be
    // an object with value/label keys.
    const value = getSeriesValue(seriesParam, 0);
    if (typeof filter === 'function') {
      return filter(value, seriesParam);
    }

    return true;
  };

  const formatter: EChartOption.Tooltip['formatter'] = seriesParamsOrParam => {
    // If this is a tooltip for the axis, it will include all series for that axis item.
    // In this case seriesParamsOrParam will be of type `Object[]`
    //
    // Otherwise, it will be an `Object`, and is a tooltip for a single item
    const axisFormatterOrDefault = formatAxisLabel || defaultFormatAxisLabel;

    // Special tooltip if component is a `markPoint`
    if (
      !Array.isArray(seriesParamsOrParam) &&
      // TODO(ts): The EChart types suggest that this can _only_ be `series`,
      //           but assuming this code is correct (which I have not
      //           verified) their types may be wrong.
      (seriesParamsOrParam.componentType as unknown) === 'markPoint'
    ) {
      const timestamp = seriesParamsOrParam.data.coord[0] as number;
      const label = axisFormatterOrDefault(
        timestamp,
        !!isGroupedByDate,
        !!utc,
        !!showTimeInTooltip,
        addSecondsToTimeFormat,
        bucketSize,
        seriesParamsOrParam
      );
      // eCharts sets seriesName as null when `componentType` !== 'series'
      const truncatedName = truncationFormatter(
        seriesParamsOrParam.data.labelForValue,
        truncate
      );
      const formattedValue = valueFormatter(
        seriesParamsOrParam.data.coord[1],
        seriesParamsOrParam.name
      );

      const className = indentLabels.includes(seriesParamsOrParam.name ?? '')
        ? 'tooltip-label tooltip-label-indent'
        : 'tooltip-label';

      return [
        '<div class="tooltip-series">',
        `<div>
          <span class="${className}"><strong>${seriesParamsOrParam.name}</strong></span>
          ${truncatedName}: ${formattedValue}
        </div>`,
        '</div>',
        `<div class="tooltip-date">${label}</div>`,
        '</div>',
      ].join('');
    }

    const seriesParams = Array.isArray(seriesParamsOrParam)
      ? seriesParamsOrParam
      : [seriesParamsOrParam];

    // If axis, timestamp comes from axis, otherwise for a single item it is defined in the data attribute.
    // The data attribute is usually a list of [name, value] but can also be an object of {name, value} when
    // there is item specific formatting being used.
    const timestamp = Array.isArray(seriesParamsOrParam)
      ? seriesParams[0].axisValue
      : getSeriesValue(seriesParams[0], 0);

    const date =
      seriesParams.length &&
      axisFormatterOrDefault(
        timestamp,
        !!isGroupedByDate,
        !!utc,
        !!showTimeInTooltip,
        addSecondsToTimeFormat,
        bucketSize,
        seriesParamsOrParam
      );

    const tooltipContent = [
      '<div class="tooltip-series">',
      seriesParams
        .filter(getFilter)
        .map(s => {
          const formattedLabel = nameFormatter(
            truncationFormatter(s.seriesName ?? '', truncate)
          );
          const value = valueFormatter(getSeriesValue(s, 1), s.seriesName, s);

          const marker = markerFormatter(s.marker ?? '', s.seriesName);

          const className = indentLabels.includes(formattedLabel)
            ? 'tooltip-label tooltip-label-indent'
            : 'tooltip-label';

          return `<div><span class="${className}">${marker} <strong>${formattedLabel}</strong></span> ${value}</div>`;
        })
        .join(''),
      '</div>',
      `<div class="tooltip-date">${date}</div>`,
      `<div class="tooltip-arrow"></div>`,
    ].join('');

    return `<div class="tooltip-container">${tooltipContent}</div>`;
  };

  return formatter;
}

type Props = ChartProps['tooltip'] &
  Pick<ChartProps, NeededChartProps> &
  Pick<FormatterOptions, 'addSecondsToTimeFormat'>;

export default function Tooltip({
  filter,
  isGroupedByDate,
  showTimeInTooltip,
  addSecondsToTimeFormat,
  formatter,
  truncate,
  utc,
  bucketSize,
  formatAxisLabel,
  valueFormatter,
  nameFormatter,
  markerFormatter,
  hideDelay,
  indentLabels,
  ...props
}: Props = {}): EChartOption.Tooltip {
  formatter =
    formatter ||
    getFormatter({
      filter,
      isGroupedByDate,
      showTimeInTooltip,
      addSecondsToTimeFormat,
      truncate,
      utc,
      bucketSize,
      formatAxisLabel,
      valueFormatter,
      nameFormatter,
      markerFormatter,
      indentLabels,
    });

  return {
    show: true,
    trigger: 'item',
    backgroundColor: 'transparent',
    transitionDuration: 0,
    padding: 0,
    // Default hideDelay in echarts docs is 100ms
    hideDelay: hideDelay || 100,
    position(pos, _params, dom, _rec, _size) {
      // Center the tooltip slightly above the cursor.
      const tipWidth = dom.clientWidth;
      const tipHeight = dom.clientHeight;

      // Get the left offset of the tip container (the chart)
      // so that we can estimate overflows
      const chartLeft =
        dom.parentNode instanceof Element
          ? dom.parentNode.getBoundingClientRect().left
          : 0;

      // Determine the new left edge.
      let leftPos = Number(pos[0]) - tipWidth / 2;
      let arrowPosition = '50%';

      // And the right edge taking into account the chart left offset
      const rightEdge = chartLeft + Number(pos[0]) + tipWidth / 2;

      // If the tooltip would leave viewport on the right, pin it.
      // and adjust the arrow position.
      if (rightEdge >= window.innerWidth - 20) {
        leftPos -= rightEdge - window.innerWidth + 20;
        arrowPosition = `${Number(pos[0]) - leftPos}px`;
      }

      // If the tooltip would leave viewport on the left, pin it.
      if (leftPos + chartLeft - 20 <= 0) {
        leftPos = chartLeft * -1 + 20;
        arrowPosition = `${Number(pos[0]) - leftPos}px`;
      }

      // Reposition the arrow.
      const arrow = dom.querySelector<HTMLDivElement>('.tooltip-arrow');
      if (arrow) {
        arrow.style.left = arrowPosition;
      }

      return {left: leftPos, top: Number(pos[1]) - tipHeight - 20};
    },
    formatter,
    ...props,
  };
}
