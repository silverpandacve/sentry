import React from 'react';
import {components, OptionProps} from 'react-select';
import styled from '@emotion/styled';

import SelectControl from 'app/components/forms/selectControl';
import Highlight from 'app/components/highlight';
import Tooltip from 'app/components/tooltip';
import {t} from 'app/locale';
import SelectField from 'app/views/settings/components/forms/selectField';

import {MetricMeta} from './types';

type Props = {
  metricMetas: MetricMeta[];
  onChange: <F extends keyof Pick<Props, 'metricMeta' | 'aggregation'>>(
    field: F,
    value: Props[F]
  ) => void;
  aggregation?: MetricMeta['operations'][0];
  metricMeta?: MetricMeta;
};

function MetricSelectField({metricMetas, metricMeta, aggregation, onChange}: Props) {
  const operations = metricMeta?.operations ?? [];
  return (
    <Wrapper>
      <StyledSelectField
        name="metric"
        choices={metricMetas.map(m => [m.name, m.name])}
        placeholder={t('Select metric')}
        onChange={v => {
          const newMetric = metricMetas.find(m => m.name === v);
          onChange('metricMeta', newMetric);
        }}
        value={metricMeta?.name}
        components={{
          Option: ({
            label,
            ...optionProps
          }: OptionProps<{
            label: string;
            value: string;
          }>) => {
            const {selectProps} = optionProps;
            const {inputValue} = selectProps;

            return (
              <components.Option label={label} {...optionProps}>
                <Highlight text={inputValue ?? ''}>{label}</Highlight>
              </components.Option>
            );
          },
        }}
        styles={{
          control: provided => ({
            ...provided,
            borderTopRightRadius: 0,
            borderBottomRightRadius: 0,
            borderRight: 'none',
            boxShadow: 'none',
          }),
        }}
        inline={false}
        flexibleControlStateSize
        stacked
        allowClear
      />
      <Tooltip
        disabled={!!operations.length}
        title={t('Please select a metric to enable this field')}
      >
        <SelectControl
          name="aggregation"
          placeholder={t('Aggr')}
          disabled={!operations.length}
          options={operations.map(operation => ({
            label: operation === 'count_unique' ? 'unique' : operation,
            value: operation,
          }))}
          value={aggregation ?? ''}
          onChange={v => onChange('aggregation', v)}
          styles={{
            control: provided => ({
              ...provided,
              borderTopLeftRadius: 0,
              borderBottomLeftRadius: 0,
              boxShadow: 'none',
            }),
          }}
        />
      </Tooltip>
    </Wrapper>
  );
}

export default MetricSelectField;

const StyledSelectField = styled(SelectField)`
  padding-right: 0;
  padding-bottom: 0;
`;

const Wrapper = styled('div')`
  display: grid;
  grid-template-columns: 1fr 0.3fr;
`;
