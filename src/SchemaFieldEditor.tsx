import { RotateCcw, Trash2, X } from 'lucide-react'
import type { CanonicalSchemaField } from './api'

type Changes = { name?: string; original_type?: string; nullable?: boolean; comment?: string }
type Props = {
  mode: 'synthetic' | 'evolution'; field: CanonicalSchemaField; status?: string
  removed?: boolean; disabled?: boolean; complex?: boolean
  onChange: (changes: Changes) => void; onRemove: () => void; onRestore?: () => void
}

const syntheticTypes = ['STRING', 'TINYINT', 'SMALLINT', 'INT', 'INTEGER', 'BIGINT', 'FLOAT', 'DOUBLE', 'BOOLEAN', 'DATE', 'TIMESTAMP', 'DECIMAL(18,2)']

/** Shared column editing controls; callers own immutable draft state and persistence. */
export function SchemaFieldEditor({ mode, field, status, removed = false, disabled = false, complex = false, onChange, onRemove, onRestore }: Props) {
  if (mode === 'synthetic') return <div className="synthetic-rule-row">
    <input aria-label={`Column name ${field.name}`} value={field.name} onChange={(event) => onChange({ name: event.target.value })} />
    <select aria-label={`Type for ${field.name}`} value={field.original_type} onChange={(event) => onChange({ original_type: event.target.value })}>
      {!syntheticTypes.includes(field.original_type.toUpperCase()) && <option value={field.original_type}>{field.original_type} · source type</option>}
      {syntheticTypes.map((type) => <option key={type}>{type}</option>)}
    </select>
    <select aria-label={`Nullability for ${field.name}`} value={field.nullable === false ? 'required' : 'nullable'} onChange={(event) => onChange({ nullable: event.target.value === 'nullable' })}>
      <option value="nullable">Nullable</option><option value="required">Required</option>
    </select>
    <button className="icon-button subtle" aria-label={`Remove ${field.name}`} onClick={onRemove}><X size={13} /></button>
  </div>

  return <tr className={`${removed ? 'removed' : status === 'Added' ? 'added' : ''} ${status === 'Differs from PROD' || status === 'DEV-only' ? 'reference-different' : ''}`}>
    <td><input aria-label={`Column name ${field.name}`} value={field.name} disabled={disabled || removed || complex} onChange={(event) => onChange({ name: event.target.value })} /></td>
    <td><input aria-label={`Column type ${field.name}`} value={field.original_type} disabled={disabled || removed || complex} onChange={(event) => onChange({ original_type: event.target.value })} /></td>
    <td><select aria-label={`Nullability ${field.name}`} value={field.nullable === false ? 'required' : 'nullable'} disabled={disabled || removed || complex} onChange={(event) => onChange({ nullable: event.target.value === 'nullable' })}><option value="nullable">Nullable</option><option value="required">NOT NULL</option></select></td>
    <td><input aria-label={`Comment ${field.name}`} value={String(field.technology_properties?.comment ?? '')} disabled={disabled || removed || complex} onChange={(event) => onChange({ comment: event.target.value })} /></td>
    <td>{removed ? 'Removed' : status ?? 'Unchanged'}</td>
    <td>{removed ? <button className="icon-button subtle" aria-label={`Restore ${field.name}`} onClick={onRestore}><RotateCcw size={14} /></button> : <button className="icon-button subtle" aria-label={`Remove ${field.name}`} onClick={onRemove}><Trash2 size={14} /></button>}</td>
  </tr>
}
