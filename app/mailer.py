"""Sending email through Amazon SES (sign-in links, "Save my work" codes).

Settings (Settings & keys, or .env): SES_ACCESS_KEY_ID, SES_SECRET_ACCESS_KEY, SES_REGION, MAIL_FROM. Without them,
email features say so instead of pretending to send. boto3 is only imported when an email is actually sent.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("agent_api.mailer")
SENT: list[dict[str, Any]] = []        # tests replace send() or read this


def configured(s: Any) -> bool:
    return bool(s.ses_access_key_id and s.ses_secret_access_key and s.mail_from)


def _send_sync(s: Any, to: str, subject: str, text: str, html: str) -> None:
    import boto3  # noqa: PLC0415 - optional dependency, only needed when email is set up
    client = boto3.client("sesv2", region_name=s.ses_region or "eu-west-2",
                          aws_access_key_id=s.ses_access_key_id, aws_secret_access_key=s.ses_secret_access_key)
    client.send_email(FromEmailAddress=s.mail_from, Destination={"ToAddresses": [to]}, Content={"Simple": {
        "Subject": {"Data": subject, "Charset": "UTF-8"},
        "Body": {"Text": {"Data": text, "Charset": "UTF-8"}, "Html": {"Data": html, "Charset": "UTF-8"}}}})


async def send(s: Any, to: str, subject: str, text: str, html: str) -> None:
    """Raises RuntimeError with a readable reason when sending fails."""
    if not configured(s):
        raise RuntimeError("Email isn't set up yet: add the AWS SES keys on Settings & keys")
    try:
        await asyncio.to_thread(_send_sync, s, to, subject, text, html)
    except ImportError as e:
        raise RuntimeError("boto3 isn't installed in agent-api (pip install boto3)") from e
    except Exception as e:  # noqa: BLE001 - SES errors vary; say what happened without the keys
        log.warning("SES send to %s failed: %s", to, e)
        raise RuntimeError(f"Couldn't send the email ({e.__class__.__name__}): check the SES settings") from e
    SENT.append({"to": to, "subject": subject})
