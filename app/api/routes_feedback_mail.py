"""GET /feedback/mail-account: the link the Jonah app reads to learn which mail account to send feedback reports through.

The app sends each report straight to Gmail's mail server itself (from the phone to the team's inbox); this route only hands it the login, so
the login can be changed here, on Render, without releasing a new app. The values come from the server's ENVIRONMENT, never from the code
or the repository:

  FEEDBACK_SMTP_USER       The Gmail address that logs in and sends.
  FEEDBACK_SMTP_PASSWORD   Its Gmail app password (the spaces Google shows in it do not matter).
  FEEDBACK_TO_EMAIL        Where reports arrive. Default: cogniaistudios@gmail.com.

Use a Gmail account made ONLY for sending, not the team's main inbox: whatever this route returns can be read by anyone who can call it (the
key the app sends is inside the app), and an app password gives full access to its mailbox. The key check and the rate limit below only keep
casual visitors out.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import guard

router = APIRouter(tags=["feedback"])

DEFAULT_TO = "cogniaistudios@gmail.com"
NO_STORE = {"Cache-Control": "no-store"}


@router.get("/feedback/mail-account", include_in_schema=False)
async def mail_account(_client: str = Depends(guard)):
    user = os.environ.get("FEEDBACK_SMTP_USER", "").strip()
    password = "".join(os.environ.get("FEEDBACK_SMTP_PASSWORD", "").split())
    to = os.environ.get("FEEDBACK_TO_EMAIL", "").strip() or DEFAULT_TO
    if not user or not password:
        return JSONResponse(status_code=503, content={"error": {"message": "Feedback mail is not set up on this server"}}, headers=NO_STORE)
    return JSONResponse(content={"user": user, "password": password, "to": to}, headers=NO_STORE)
