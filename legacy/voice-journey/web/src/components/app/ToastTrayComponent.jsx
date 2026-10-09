import React from "react";
import { useDispatch, useSelector } from "react-redux";
import { selectToasts, toastDismissed } from "@scshafe/ui/state";

// Fixed bottom-right toast tray over the Toasts slice (MC's tray is RTK-coupled and
// stays in MC; this is voice-journey's own small renderer over the same slice shape).
export function ToastTrayComponent() {
  const dispatch = useDispatch();
  const toasts = useSelector(selectToasts);
  if (toasts.length === 0) return null;
  return (
    <div className="vj-toast-tray" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <button
          key={toast.id}
          type="button"
          className="vj-toast"
          data-kind={toast.kind}
          onClick={() => dispatch(toastDismissed({ id: toast.id }))}
        >
          {toast.message}
        </button>
      ))}
    </div>
  );
}
