"""
CountyWatch — mailer.py
Sends the digest as a multipart HTML/plain-text email via Gmail SMTP.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown as md


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 15px;
    line-height: 1.6;
    color: #1a1a1a;
    max-width: 720px;
    margin: 0 auto;
    padding: 24px 16px;
    background: #fff;
  }}
  h1 {{ font-size: 1.4em; border-bottom: 2px solid #1a1a1a; padding-bottom: 6px; }}
  h2 {{ font-size: 1.15em; margin-top: 28px; color: #111; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  h3 {{ font-size: 1em; margin-top: 20px; color: #333; }}
  blockquote {{
    margin: 4px 0 8px 0;
    padding: 6px 12px;
    border-left: 3px solid #0066cc;
    background: #f4f8ff;
    border-radius: 0 4px 4px 0;
  }}
  blockquote a {{ color: #0055bb; text-decoration: none; }}
  blockquote a:hover {{ text-decoration: underline; }}
  a {{ color: #0066cc; }}
  code {{
    background: #f0f0f0;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 0.88em;
  }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 20px 0; }}
  em {{ color: #555; }}
  ul {{ padding-left: 20px; }}
  li {{ margin-bottom: 6px; }}
  p {{ margin: 6px 0 10px; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def send_digest(subject: str, body: str, to: str) -> None:
    addr = os.environ["GMAIL_ADDRESS"]
    pw   = os.environ["GMAIL_APP_PASSWORD"]

    html_body = md.markdown(
        body,
        extensions=["extra", "nl2br"],
    )
    html_full = _HTML_TEMPLATE.format(body=html_body)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = addr
    msg["To"]      = to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html_full, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(addr, pw)
        smtp.send_message(msg)
