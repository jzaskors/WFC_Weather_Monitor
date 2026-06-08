#!/usr/bin/env python3
"""One-off Twilio test. Sends a single text to every number in ALERT_SMS_TO.
Run it from the Actions tab (Test SMS -> Run workflow) to confirm your
GitHub secrets + Twilio are wired up correctly. Safe to delete afterward.
"""
import os
from twilio.rest import Client

sid = os.environ["TWILIO_SID"]
token = os.environ["TWILIO_TOKEN"]
frm = os.environ["TWILIO_FROM"]
to = [x.strip() for x in os.environ["ALERT_SMS_TO"].split(",") if x.strip()]

client = Client(sid, token)
for num in to:
    msg = client.messages.create(
        body="WFC weather monitor — test message. Twilio is working ✅",
        from_=frm, to=num,
    )
    print(f"Sent to {num}  (message SID {msg.sid})")
print("Done. If you didn't get a text, check the recipient is a VERIFIED number in Twilio.")
