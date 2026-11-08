import { useId } from 'react'
import { RotateCcw, Trash2, X } from 'lucide-react'
import type { CanonicalSchemaField } from './api'

type Changes = { name?: string; original_type?: string; nullable?: boolean; comment?: string }
type Props = {
  mode: 'synthetic' | 'evolution'; field: CanonicalSchemaField; status?: string; origin?: 'dev' | 'prod' | 'manual'; modified?: boolean
  removed?: boolean; disabled?: boolean; complex?: boolean
  onChange: (changes: Changes) => void; onRemove: () => void; onRestore?: () => void
}

const databricksTypes = ['ARRAY<STRING>', 'BINARY', 'BOOLEAN', 'BYTE', 'CHAR(10)', 'DATE', 'DECIMAL(18,2)', 'DOUBLE', 'FLOAT', 'GEOGRAPHY', 'GEOMETRY', 'INT', 'LONG', 'MAP<STRING,STRING>', 'SMALLINT', 'STRING', 'STRUCT<field:STRING>', 'TIME', 'TIMESTAMP', 'TIMESTAMP_NTZ', 'TINYINT', 'VARIANT', 'VARCHAR(255)']

export function DatabricksTypeSelector({ fieldName, value, disabled, onChange }: { fieldName: string; value: string; disabled?: boolean; onChange: (value: string) => void }) {
  const listId = useId()
  return <>
    <input aria-label={`Type for ${fieldName}`} list={listId} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)} placeholder="Search or enter a Databricks type" />
    <datalist id={listId}>{databricksTypes.map((type) => <option key={type} value={type} />)}</datalist>
  </>
}

/** Shared column editing controls; callers own immutable draft state and persistence. */
export function SchemaFieldEditor({ mode, field, status, origin, modified = false, removed = false, disabled = false, complex = false, onChange, onRemove, onRestore }: Props) {
  if (mode === 'synthetic') return <div className="synthetic-rule-row">
    <input aria-label={`Column name ${field.name}`} value={field.name} disabled={disabled} onChange={(event) => onChange({ name: event.target.value })} />
    <DatabricksTypeSelector fieldName={field.name} value={field.original_type} disabled={disabled} onChange={(original_type) => onChange({ original_type })} />
    <select aria-label={`Nullability for ${field.name}`} disabled={disabled} value={field.nullable === false ? 'required' : 'nullable'} onChange={(event) => onChange({ nullable: event.target.value === 'nullable' })}>
      <option value="nullable">Nullable</option><option value="required">Required</option>
    </select>
    <button className="icon-button subtle" aria-label={`Remove ${field.name}`} disabled={disabled} onClick={onRemove}><X size={13} /></button>
  </div>

  return <tr data-origin={origin} className={removed ? 'removed' : modified ? 'modified' : origin === 'prod' ? 'added-from-prod' : origin === 'manual' ? 'added-manually' : ''}>
    <td><input aria-label={`Column name ${field.name}`} value={field.name} disabled={disabled || removed || complex} onChange={(event) => onChange({ name: event.target.value })} /></td>
    <td><DatabricksTypeSelector fieldName={field.name} value={field.original_type} disabled={disabled || removed} onChange={(original_type) => onChange({ original_type })} /></td>
    <td><select aria-label={`Nullability ${field.name}`} value={field.nullable === false ? 'required' : 'nullable'} disabled={disabled || removed || complex} onChange={(event) => onChange({ nullable: event.target.value === 'nullable' })}><option value="nullable">Nullable</option><option value="required">NOT NULL</option></select></td>
    <td><input aria-label={`Comment ${field.name}`} value={String(field.technology_properties?.comment ?? '')} disabled={disabled || removed || complex} onChange={(event) => onChange({ comment: event.target.value })} /></td>
    <td><span className="schema-draft-label">{removed ? 'Removed' : modified ? 'Modified' : origin === 'prod' ? 'Added from PROD' : origin === 'manual' ? 'Added manually' : 'Original DEV'}</span>{(modified || removed) && origin && origin !== 'dev' && <small>{origin === 'prod' ? 'Added from PROD' : 'Added manually'}</small>}{status && <small>{status}</small>}</td>
    <td>{removed ? <button className="icon-button subtle" aria-label={`Restore ${field.name}`} onClick={onRestore}><RotateCcw size={14} /></button> : <button className="icon-button subtle" aria-label={`Remove ${field.name}`} onClick={onRemove}><Trash2 size={14} /></button>}</td>
  </tr>
}
