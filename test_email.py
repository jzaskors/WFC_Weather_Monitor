#!/usr/bin/env python3
"""One-off email test. Sends a single email through the same sender setup as
real alerts (SMTP_HOST/PORT/USER/PASS, EMAIL_FROM, EMAIL_TO, and optionally
EMAIL_FROM_NAME / EMAIL_REPLY_TO). Run it from the Actions tab
(Test Email -> Run workflow) to confirm the relay works and to see how the
sender looks in your inbox.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

DEFAULT_FROM_NAME = "WFC Weather Monitor Alert System"

host = os.environ["SMTP_HOST"]
port = int(os.environ.get("SMTP_PORT") or 587)
user = os.environ["SMTP_USER"]
pw = os.environ["SMTP_PASS"]
name_in_from, frm = parseaddr(os.environ.get("EMAIL_FROM") or user)
frm = frm or user
display = (os.environ.get("EMAIL_FROM_NAME") or name_in_from or DEFAULT_FROM_NAME).strip()
reply_to = (os.environ.get("EMAIL_REPLY_TO") or "").strip()
to = [x.strip() for x in os.environ["EMAIL_TO"].split(",") if x.strip()]

msg = MIMEText("WFC weather monitor — test email. The relay is working.\n\n"
               f"Sender shown as: {display} <{frm}>\n"
               + (f"Replies go to: {reply_to}\n" if reply_to else "")
               + "\nIf this landed in spam, the sending domain still needs to be "
                 "authenticated in Brevo.")
msg["Subject"] = "WFC weather monitor — test email ✅"
msg["From"] = formataddr((display, frm))
msg["To"] = ", ".join(to)
if reply_to:
    msg["Reply-To"] = reply_to

if port == 465:
    server = smtplib.SMTP_SSL(host, port, timeout=30)
else:
    server = smtplib.SMTP(host, port, timeout=30)
    server.starttls()
with server:
    server.login(user, pw)
    server.sendmail(frm, to, msg.as_string())
print(f"Sent to {', '.join(to)} from {display} <{frm}>. Check your inbox (and spam).")
