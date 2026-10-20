"""History queries; supersession and explicit disqualification are different operations."""

from sqlalchemy import select

from .models import Evaluation, Promotion, Submission


def champion(session, board):
    return session.scalar(
        select(Promotion)
        .where(Promotion.board == board, Promotion.disqualified_at.is_(None))
        .order_by(Promotion.id.desc())
        .limit(1)
    )


def history(session, board, *, limit=None):
    query = (
        select(Promotion, Evaluation.submission_id, Submission.hotkey)
        .join(Evaluation, Evaluation.id == Promotion.evaluation_id)
        .join(Submission, Submission.id == Evaluation.submission_id)
        .where(Promotion.board == board, Promotion.disqualified_at.is_(None))
        .order_by(Promotion.id.desc())
    )
    return session.execute(query.limit(limit) if limit else query).all()
