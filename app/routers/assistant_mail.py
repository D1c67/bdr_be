"""Assistant-only live mail and approval-bound send/review routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.core.approval_bound import require_approval_bound
from app.core.deps import CurrentUser, require_internal
from app.services import assistant_mail as mail
from app.services import assistant_mail_review as review
from app.services import assistant_mail_send as send

router = APIRouter(prefix="/assistant/mail", tags=["assistant-mail"])


def _assistant(user: CurrentUser = Depends(require_internal)) -> CurrentUser:
    if not user.via_assistant_run_id:
        raise HTTPException(404, "Not Found")
    return user


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except mail.MailError as error:
        if error.code == "bid_recipient_not_authorized":
            raise HTTPException(409, {"code": "mail_recipient_policy",
                "message": "This draft can be sent only to authorized internal bid viewers."}) from None
        raise HTTPException(409, {"code": "mail_unavailable", "message": "Mail is unavailable."}) from None
    except Exception:
        # Provider, retention and policy errors have one sanitized public shape.
        raise HTTPException(409, {"code": "mail_unavailable", "message": "Mail is unavailable."}) from None


def _normal(user):
    from app.assistant import repo
    cid = repo.conversation_for_run(user.id, user.via_assistant_run_id)
    conversation = repo.get_conversation(user.id, cid) if cid else None
    if not conversation or conversation.get("mode") != "normal" or conversation.get("mailbox_tainted_at"):
        raise HTTPException(409, {"code": "mail_unavailable", "message": "Mail is unavailable."})


@router.get("/search")
def email_search(mailbox: str, query: str = "", window_days: int = 30,
                 limit: int = Query(20, ge=1, le=50), user: CurrentUser = Depends(_assistant)):
    _normal(user)
    return _call(mail.search, user.id, mailbox, query, window_days, limit)


@router.get("/read")
def email_read(mailbox: str, message_id: str, user: CurrentUser = Depends(_assistant)):
    _normal(user)
    from app.assistant import repo
    conversation = repo.conversation_for_run(user.id, user.via_assistant_run_id)
    return _call(mail.read, user.id, mailbox, message_id, conversation_id=conversation,
                 run_id=user.via_assistant_run_id)


class AttachmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mailbox: str
    message_id: str
    attachment_id: str


@router.post("/attachment")
def email_attachment(body: AttachmentIn, user: CurrentUser = Depends(_assistant)):
    _normal(user)
    return _call(mail.attachment_to_staging, user, body.mailbox, body.message_id, body.attachment_id)


@router.post("/authority")
def email_authority(draft: dict, user: CurrentUser = Depends(_assistant)):
    return _call(send.prepare, user.id, user.via_assistant_run_id, draft)


@router.post("/send")
def email_send(body: dict, approval_id: str = Depends(require_approval_bound),
               user: CurrentUser = Depends(_assistant)):
    return _call(send.dispatch, user.id, user.via_assistant_run_id, approval_id, body)


@router.get("/review-target")
def review_target(target_upn: str, user: CurrentUser = Depends(_assistant)):
    return _call(review.target, user.id, user.via_assistant_run_id, target_upn)


@router.post("/review")
def read_mailbox(body: dict, approval_id: str = Depends(require_approval_bound),
                 user: CurrentUser = Depends(_assistant)):
    return _call(review.review, user.id, user.via_assistant_run_id, approval_id, body)
