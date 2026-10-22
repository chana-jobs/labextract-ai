/**
 * EditableField — inline editable field.  SAVE ONLY — no approval here.
 *
 * Rules (Spec §7.2, Backend review.py):
 *  - Enter / blur → calls onSave(newValue).  The parent persists to the server.
 *  - Escape → reverts draft, no onSave called, no network request.
 *  - No change guard: if trimmed value equals current value, exit silently.
 *  - Saving spinner shown while parent is persisting (I-3: never shows success
 *    before server confirms).
 *  - saveError shown inline without clearing the field (I-4: no silent overwrite).
 *  - Approval is NEVER performed here.  The Approve button lives in FieldRow.
 *
 * Accessibility:
 *  - Input aria-label derived from fieldLabel.
 *  - Error linked via aria-describedby.
 *  - Edit ✎ button aria-pressed=false; commit ✓ / cancel ✕ tabIndex=-1 (keyboard
 *    users commit with Enter, cancel with Escape, not by tabbing to these buttons).
 */

import {
  useState,
  useRef,
  useEffect,
  useCallback,
  type KeyboardEvent,
} from "react";

// ----------------------------------------------------------------- types

export interface EditableFieldProps {
  /** Human-readable label for screen readers and titles */
  fieldLabel: string;
  /** Current persisted value (normalized, or raw as fallback) */
  value: string;
  /** Placeholder when value is empty */
  placeholder?: string;
  /** True while the parent is persisting to the server */
  saving?: boolean;
  /** Error message from the last failed save — shown without clearing the field */
  saveError?: string | null;
  /** Text direction: rtl for Hebrew names/sender, ltr for IDs/numbers/dates */
  dir?: "ltr" | "rtl";
  /** Prevent editing (e.g. while another save is in flight) */
  disabled?: boolean;
  /** Hide the value when a table cell provides its own source-selection control. */
  showValue?: boolean;
  /** Confirm changes to a previously saved value before persistence. */
  onBeforeSave?: (previousValue: string, nextValue: string) => boolean;
  /** Notify the parent when inline editing starts. */
  onEditStart?: () => void;
  /** Commit when the input loses focus (default). False keeps the draft open until Enter / ✓ / Escape. */
  commitOnBlur?: boolean;
  /** Reports whether an open editor holds a draft that differs from the persisted value. */
  onDirtyChange?: (dirty: boolean) => void;
  /**
   * Called when the user commits a new, different value (Enter or blur).
   * Never called on Escape or when the value is unchanged.
   * The parent is responsible for persisting; this component does NOT approve.
   */
  onSave: (newValue: string) => void;
  onCancel?: () => void;
}

// ----------------------------------------------------------------- component

export default function EditableField({
  fieldLabel,
  value,
  placeholder = "—",
  saving = false,
  saveError = null,
  dir = "ltr",
  disabled = false,
  showValue = true,
  onBeforeSave,
  onEditStart,
  commitOnBlur = true,
  onDirtyChange,
  onSave,
  onCancel,
}: EditableFieldProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const dirty = editing && draft.trim() !== value.trim();

  // Report only when `dirty` flips (or on unmount); the callback may be a new function every render.
  const dirtyCallback = useRef(onDirtyChange);
  dirtyCallback.current = onDirtyChange;
  useEffect(() => {
    dirtyCallback.current?.(dirty);
    return () => { if (dirty) dirtyCallback.current?.(false); };
  }, [dirty]);
  const inputRef = useRef<HTMLInputElement>(null);
  const wasSaving = useRef(saving);
  const savePending = useRef(false);
  const errorId = `ef-err-${fieldLabel.replace(/[\s/]/g, "-")}`;

  // Keep failed edits intact; close only after the parent confirms the save.
  useEffect(() => {
    if (wasSaving.current && !saving) {
      savePending.current = false;
      if (editing && !saveError) {
        setDraft(value);
        setEditing(false);
      }
    } else if (!editing && !saveError) {
      setDraft(value);
    }
    wasSaving.current = saving;
  }, [value, editing, saving, saveError]);

  // Auto-focus + select-all when editing starts
  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const startEdit = useCallback(() => {
    if (disabled || saving) return;
    setDraft(value);
    setEditing(true);
    onEditStart?.();
  }, [disabled, saving, value, onEditStart]);

  /** Commit: save if changed, else just close. Never approves. */
  const commit = useCallback(() => {
    if (savePending.current || saving) return;
    const trimmed = draft.trim();
    if (trimmed === value.trim()) {
      // No change — exit edit mode silently, no network request
      setEditing(false);
      return;
    }
    if (value.trim() && onBeforeSave && !onBeforeSave(value, trimmed)) {
      setDraft(value);
      setEditing(false);
      return;
    }
    savePending.current = true;
    onSave(trimmed); // parent persists; does NOT approve
  }, [draft, value, onSave, onBeforeSave, saving]);

  /** Cancel: revert draft, close without calling onSave */
  const cancel = useCallback(() => {
    setDraft(value);
    setEditing(false);
    onCancel?.();
  }, [value, onCancel]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commit();
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      }
    },
    [commit, cancel]
  );

  // ----------------------------------------------------------------- editing mode

  if (editing) {
    return (
      <span
        className="editable-field editable-field--editing"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
      >
        <input
          ref={inputRef}
          type="text"
          className="editable-field__input"
          value={draft}
          disabled={saving}
          dir={dir}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          onBlur={commitOnBlur ? commit : undefined}
          aria-label={`עריכת ${fieldLabel}`}
          aria-describedby={saveError ? errorId : undefined}
          aria-invalid={!!saveError}
          autoComplete="off"
          spellCheck={false}
        />
        {/* onMouseDown prevents blur from firing before the click */}
        <button
          type="button"
          className="editable-field__action editable-field__action--commit"
          onMouseDown={(e) => { e.preventDefault(); commit(); }}
          disabled={saving}
          aria-label={`שמור ${fieldLabel}`}
          tabIndex={-1}
          title="שמור (Enter)"
        >
          ✓
        </button>
        <button
          type="button"
          className="editable-field__action editable-field__action--cancel"
          onMouseDown={(e) => { e.preventDefault(); cancel(); }}
          disabled={saving}
          aria-label={`בטל עריכת ${fieldLabel}`}
          tabIndex={-1}
          title="בטל (Escape)"
        >
          ✕
        </button>
        {saveError && <span id={errorId} className="editable-field__error" role="alert">{saveError}</span>}
      </span>
    );
  }

  // ----------------------------------------------------------------- display mode

  return (
    <span
      className="editable-field"
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      {showValue && (
        <span
          className={`editable-field__value${!value ? " editable-field__value--empty" : ""}`}
          dir={dir}
          title={value ? `ערך נוכחי: ${value}` : undefined}
        >
          {value || (
            <span style={{ color: "var(--neutral-400)", fontStyle: "italic" }}>
              {placeholder}
            </span>
          )}
        </span>
      )}

      {saving ? (
        <span className="spinner editable-field__spinner" aria-label="שומר…" />
      ) : (
        !disabled && (
          <button
            type="button"
            className="editable-field__edit-btn"
            onClick={startEdit}
            aria-pressed={false}
            aria-label={`ערוך ${fieldLabel}`}
            title="ערוך ערך"
          >
            ✎
          </button>
        )
      )}

      {/* saveError is shown WITHOUT clearing the field — I-4 */}
      {saveError && (
        <span
          id={errorId}
          className="editable-field__error"
          role="alert"
          aria-live="polite"
        >
          {saveError}
        </span>
      )}
    </span>
  );
}
