#!/usr/bin/env python3
"""One-off email test. Sends a single email using your SMTP relay secrets
(SMTP_HOST/PORT/USER/PASS, EMAIL_FROM, EMAIL_TO). Run from the Actions tab
(Test Email -> Run workflow) to confirm the relay is wired up. Safe to delete after.
"""
import os
import smtplib
from email.mime.text import MIMEText

host = os.environ["SMTP_HOST"]
port = int(os.environ.get("SMTP_PORT", "587"))
user = os.environ["SMTP_USER"]
pw = os.environ["SMTP_PASS"]
frm = os.environ.get("EMAIL_FROM", user)
to = [x.strip() for x in os.environ["EMAIL_TO"].split(",") if x.strip()]

msg = MIMEText("WFC weather monitor — test email. The relay is working. "
               "If you can read this in your inbox, email alerts are good to go.")
msg["Subject"] = "WFC weather monitor — test email ✅"
msg["From"] = frm
msg["To"] = ", ".join(to)

with smtplib.SMTP(host, port) as server:
    server.starttls()
    server.login(user, pw)
    server.sendmail(frm, to, msg.as_string())
print(f"Sent to {', '.join(to)}. Check your inbox (and spam folder, just in case).")
