"""History queries; supersession and explicit disqualification are different operations."""

from sqlalchemy import exists, select

from .models import Evaluation, Promotion, SourceDisqualification, Submission


def source_blocked(source):
    return exists(
        select(SourceDisqualification.source_hash).where(
            SourceDisqualification.source_hash == source,
            SourceDisqualification.active,
        )
    )


def champion(session, board):
    return session.scalar(
        select(Promotion)
        .join(Evaluation, Evaluation.id == Promotion.evaluation_id)
        .join(Submission, Submission.id == Evaluation.submission_id)
        .where(
            ~source_blocked(Submission.source_sha256),
            Promotion.board == board,
            Promotion.disqualified_at.is_(None),
        )
        .order_by(Promotion.id.desc())
        .limit(1)
    )


def history(session, board, *, limit=None):
    query = (
        select(Promotion, Evaluation.submission_id, Submission.hotkey)
        .join(Evaluation, Evaluation.id == Promotion.evaluation_id)
        .join(Submission, Submission.id == Evaluation.submission_id)
        .where(
            Promotion.board == board,
            Promotion.disqualified_at.is_(None),
            ~source_blocked(Submission.source_sha256),
        )
        .order_by(Promotion.id.desc())
    )
    return session.execute(query.limit(limit) if limit else query).all()
