import { useEffect, useState } from 'react'
import { RotateCcw, Trash2, X } from 'lucide-react'
import type { CanonicalSchemaField } from './api'

type Changes = { name?: string; original_type?: string; nullable?: boolean; comment?: string }
type Props = {
  mode: 'synthetic' | 'evolution'; field: CanonicalSchemaField; status?: string; origin?: 'dev' | 'prod' | 'manual'; modified?: boolean
  removed?: boolean; disabled?: boolean; complex?: boolean; added?: boolean
  onChange: (changes: Changes) => void; onRemove: () => void; onRestore?: () => void
}

const databricksTypes = ['ARRAY<STRING>', 'BINARY', 'BOOLEAN', 'CHAR(10)', 'DATE', 'DECIMAL(18,2)', 'DOUBLE', 'FLOAT', 'GEOGRAPHY', 'GEOMETRY', 'INTEGER', 'BIGINT', 'MAP<STRING,STRING>', 'SMALLINT', 'STRING', 'STRUCT<field:STRING>', 'TIME', 'TIMESTAMP', 'TIMESTAMP_NTZ', 'TINYINT', 'VARIANT', 'VARCHAR(255)']
const typeAliases: Record<string, string> = { BYTE: 'TINYINT', SHORT: 'SMALLINT', INT: 'INTEGER', LONG: 'BIGINT', REAL: 'FLOAT', NUMERIC: 'DECIMAL' }

export function DatabricksTypeSelector({ fieldName, value, disabled, loading = false, onChange }: { fieldName: string; value: string; disabled?: boolean; loading?: boolean; onChange: (value: string) => void }) {
  const decimal = /^\s*(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*$/i.exec(value)
  const family = value.split(/[<(]/)[0].trim().toUpperCase()
  const normalizedFamily = typeAliases[family] ?? family
  const known = databricksTypes.find(type => {
    const optionFamily = type.split(/[<(]/)[0]
    if (['ARRAY', 'MAP', 'STRUCT', 'CHAR', 'VARCHAR'].includes(optionFamily)) return type.toUpperCase() === value.trim().toUpperCase()
    return optionFamily === normalizedFamily
  })
  const [custom, setCustom] = useState(!known && !decimal)
  const [precision, setPrecision] = useState(decimal?.[1] ?? '18')
  const [scale, setScale] = useState(decimal?.[2] ?? '2')
  useEffect(() => {
    if (decimal) { setPrecision(decimal[1]); setScale(decimal[2]) }
    setCustom(!known && !decimal)
  }, [value, known, decimal?.[1], decimal?.[2]])
  const validDecimal = Number(precision) >= 1 && Number(precision) <= 38 && scale !== '' && Number(scale) >= 0 && Number(scale) <= Number(precision)
  const updateDecimal = (p: string, s: string) => { setPrecision(p); setScale(s); if (p && s && Number(p) >= 1 && Number(p) <= 38 && Number(s) >= 0 && Number(s) <= Number(p)) onChange(`DECIMAL(${p},${s})`) }
  return <div className="databricks-type-selector">
    <select aria-label={`Type for ${fieldName}`} disabled={disabled || loading} value={custom ? 'custom' : decimal ? 'DECIMAL(18,2)' : known ?? 'custom'} onChange={(event) => { const next = event.target.value; setCustom(next === 'custom'); if (next !== 'custom') onChange(next) }}>
      {databricksTypes.map(type => <option key={type} value={type}>{type === 'DECIMAL(18,2)' ? 'DECIMAL' : type.split(/[<(]/)[0]}</option>)}
      <option value="custom">Custom / existing type</option>
    </select>
    {decimal && !custom ? <div className="type-parameters"><label>Precision<input type="number" aria-label={`Precision for ${fieldName}`} min={1} max={38} disabled={disabled} value={precision} onChange={event => updateDecimal(event.target.value, scale)} /></label><label>Scale<input type="number" aria-label={`Scale for ${fieldName}`} min={0} max={Number(precision)} disabled={disabled} value={scale} onChange={event => updateDecimal(precision, event.target.value)} /></label>{!validDecimal && <small role="alert">Precision must be 1–38; scale must be 0–precision.</small>}</div>
      : custom && <input aria-label={`Type declaration for ${fieldName}`} value={value} disabled={disabled} onChange={event => onChange(event.target.value)} />}
  </div>
}

/** Shared column editing controls; callers own immutable draft state and persistence. */
export function SchemaFieldEditor({ mode, field, status, origin, modified = false, removed = false, disabled = false, complex = false, added = false, onChange, onRemove, onRestore }: Props) {
  if (mode === 'synthetic') return <div className="synthetic-rule-row">
    <input aria-label={`Column name ${field.name}`} value={field.name} disabled={disabled} onChange={(event) => onChange({ name: event.target.value })} />
    <DatabricksTypeSelector fieldName={field.name} value={field.original_type} disabled={disabled} onChange={(original_type) => onChange({ original_type })} />
    <select aria-label={`Nullability for ${field.name}`} disabled={disabled} value={field.nullable === false ? 'required' : 'nullable'} onChange={(event) => onChange({ nullable: event.target.value === 'nullable' })}>
      <option value="nullable">Nullable</option><option value="required">Required</option>
    </select>
    <button type="button" className="icon-button subtle" aria-label={`Remove ${field.name}`} title={disabled && mode === 'synthetic' ? 'Append and Replace preserve the existing DEV schema. Choose a strategy that allows schema changes.' : undefined} disabled={disabled} onClick={onRemove}><X size={13} /></button>
  </div>

  return <tr data-origin={origin} data-edit-state={removed ? 'removed' : added ? 'added' : modified ? 'modified' : 'unchanged'} className={removed ? 'removed' : origin === 'prod' ? 'added-from-prod' : origin === 'manual' ? 'added-manually' : modified ? 'modified' : ''}>
    <td><input aria-label={`Column name ${field.name}`} value={field.name} disabled={disabled || removed || complex} onChange={(event) => onChange({ name: event.target.value })} /></td>
    <td><DatabricksTypeSelector fieldName={field.name} value={field.original_type} disabled={disabled || removed} onChange={(original_type) => onChange({ original_type })} /></td>
    <td><select aria-label={`Nullability ${field.name}`} value={field.nullable === false ? 'required' : 'nullable'} disabled={disabled || removed || complex} onChange={(event) => onChange({ nullable: event.target.value === 'nullable' })}><option value="nullable">Nullable</option><option value="required">NOT NULL</option></select></td>
    <td><input aria-label={`Comment ${field.name}`} value={String(field.technology_properties?.comment ?? '')} disabled={disabled || removed || complex} onChange={(event) => onChange({ comment: event.target.value })} /></td>
    <td><span className="schema-draft-label">{origin === 'prod' ? 'Added from PROD' : origin === 'manual' ? 'Added manually' : 'Original DEV'}</span>{(removed || added || modified) && <small>{removed ? 'Removed' : added ? 'Pending addition' : 'Modified'}</small>}{status && status !== 'Matches PROD' && <small className="schema-physical-comparison">{status}</small>}</td>
    <td>{removed ? <button type="button" className="icon-button subtle" aria-label={`Restore ${field.name}`} onClick={onRestore}><RotateCcw size={14} /></button> : <button type="button" className="icon-button subtle" aria-label={`Remove ${field.name}`} onClick={onRemove}><Trash2 size={14} /></button>}</td>
  </tr>
}
