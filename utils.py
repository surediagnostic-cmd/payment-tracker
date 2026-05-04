from flask import current_app
from flask_mail import Message
from app import mail


def send_email(to, subject, body):
    try:
        msg = Message(subject=subject, recipients=[to], body=body)
        mail.send(msg)
    except Exception as e:
        current_app.logger.warning(f"Email failed to {to}: {e}")


def format_naira(amount):
    if amount is None:
        return "—"
    return f"₦{float(amount):,.2f}"
