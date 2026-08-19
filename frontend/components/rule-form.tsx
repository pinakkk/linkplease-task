"use client";

import { useId, useState } from "react";
import { createRule } from "@/lib/api";

export interface RuleFormProps {
  /** Called after a 201 so the parent can refresh its rules poll. */
  onCreated?: () => void;
  className?: string;
}

const KEYWORD_MAX = 64;
const MESSAGE_MAX = 1000;

interface FieldErrors {
  keyword?: string;
  dm_message?: string;
}

function validate(keyword: string, message: string): FieldErrors {
  const errors: FieldErrors = {};
  const k = keyword.trim();
  const m = message.trim();

  if (!k) errors.keyword = "A keyword is required.";
  else if (k.length > KEYWORD_MAX)
    errors.keyword = `Keep it under ${KEYWORD_MAX} characters.`;

  if (!m) errors.dm_message = "The DM body is required.";
  else if (m.length > MESSAGE_MAX)
    errors.dm_message = `Keep it under ${MESSAGE_MAX} characters.`;

  return errors;
}

/**
 * Create-rule form. POSTs the graded `POST /rules` contract shape exactly —
 * `{keyword, dm_message}` — and surfaces the server's own error text rather
 * than a generic "something went wrong", because a 4xx from the backend is
 * usually the most useful thing on screen.
 */
export function RuleForm({ onCreated, className }: RuleFormProps) {
  const keywordId = useId();
  const messageId = useId();

  const [keyword, setKeyword] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [serverError, setServerError] = useState<string>();
  const [success, setSuccess] = useState<string>();

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    setServerError(undefined);
    setSuccess(undefined);

    const errors = validate(keyword, message);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    setSubmitting(true);
    const result = await createRule({
      keyword: keyword.trim(),
      dm_message: message.trim(),
    });
    setSubmitting(false);

    if (!result.ok) {
      setServerError(result.error.message);
      return;
    }

    setSuccess(
      `Rule ${result.data.rule_id} created. Comments containing “${result.data.keyword}” now match it.`,
    );
    setKeyword("");
    setMessage("");
    onCreated?.();
  }

  const inputClass =
    "w-full rounded-2xl border border-line bg-bg px-4 py-3 text-sm text-ink placeholder:text-ink-muted/70 transition-colors focus:border-accent focus:outline-none disabled:opacity-60";

  return (
    <form onSubmit={handleSubmit} className={className} noValidate>
      <div className="grid gap-5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.6fr)]">
        <div>
          <label
            htmlFor={keywordId}
            className="block text-xs font-semibold uppercase tracking-[0.16em] text-ink-muted"
          >
            Keyword
          </label>
          <input
            id={keywordId}
            name="keyword"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            disabled={submitting}
            placeholder="PRICE"
            maxLength={KEYWORD_MAX}
            aria-invalid={fieldErrors.keyword ? true : undefined}
            aria-describedby={
              fieldErrors.keyword ? `${keywordId}-error` : `${keywordId}-hint`
            }
            className={`mt-2 ${inputClass}`}
          />
          {fieldErrors.keyword ? (
            <p
              id={`${keywordId}-error`}
              className="mt-2 text-xs font-medium text-status-failed"
            >
              {fieldErrors.keyword}
            </p>
          ) : (
            <p id={`${keywordId}-hint`} className="mt-2 text-xs text-ink-muted">
              Case-insensitive, matched anywhere in the comment text.
            </p>
          )}
        </div>

        <div>
          <label
            htmlFor={messageId}
            className="block text-xs font-semibold uppercase tracking-[0.16em] text-ink-muted"
          >
            DM message
          </label>
          <textarea
            id={messageId}
            name="dm_message"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            disabled={submitting}
            rows={3}
            placeholder="Here's the price list: https://…"
            maxLength={MESSAGE_MAX}
            aria-invalid={fieldErrors.dm_message ? true : undefined}
            aria-describedby={
              fieldErrors.dm_message ? `${messageId}-error` : `${messageId}-hint`
            }
            className={`mt-2 resize-y ${inputClass}`}
          />
          {fieldErrors.dm_message ? (
            <p
              id={`${messageId}-error`}
              className="mt-2 text-xs font-medium text-status-failed"
            >
              {fieldErrors.dm_message}
            </p>
          ) : (
            <p id={`${messageId}-hint`} className="mt-2 text-xs text-ink-muted">
              Sent once per person per rule — the database constraint, not this
              form, is what enforces that.
            </p>
          )}
        </div>
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={submitting}
          className="inline-flex items-center gap-2 rounded-full bg-accent px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-accent/20 transition-all duration-200 hover:shadow-xl hover:shadow-accent/30 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60 disabled:active:scale-100 motion-reduce:transition-none motion-reduce:active:scale-100"
        >
          {submitting ? (
            <>
              <span
                aria-hidden="true"
                className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent motion-reduce:animate-none"
              />
              Creating…
            </>
          ) : (
            "Create rule"
          )}
        </button>

        <p aria-live="polite" className="text-sm">
          {serverError ? (
            <span className="font-medium text-status-failed">
              {serverError}
            </span>
          ) : success ? (
            <span className="font-medium text-status-sent">{success}</span>
          ) : null}
        </p>
      </div>
    </form>
  );
}

export default RuleForm;
