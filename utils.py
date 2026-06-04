import threading
from flask import current_app
from flask_mail import Message
from app import mail


def send_email(to, subject, body):
    """Send an email in a background thread so it never blocks the HTTP response.

    If MAIL_USERNAME is not configured the call is silently skipped.
    All SMTP errors are caught and logged — they never propagate to callers.
    """
    # Resolve the real app object while we're still inside the request context
    try:
        if not current_app.config.get("MAIL_USERNAME"):
            print(f"[email] Skipped (MAIL_USERNAME not set): {subject}", flush=True)
            return
        app = current_app._get_current_object()
    except RuntimeError:
        # No application context — nothing we can do
        return

    def _send():
        with app.app_context():
            try:
                msg = Message(subject=subject, recipients=[to], body=body)
                mail.send(msg)
                print(f"[email] Sent to {to}: {subject}", flush=True)
            except Exception as e:
                print(f"[email] Failed to {to}: {e}", flush=True)

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def format_naira(amount):
    if amount is None:
        return "—"
    return f"₦{float(amount):,.2f}"
