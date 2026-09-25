# Migration runner. Carries Alembic plus the SQLAlchemy models the migrations are
# checked against, and nothing else -- no gate, no toolchain. BUILD FROM THE REPO ROOT
# (compose sets `context: .`) so validator/ and the alembic tree are in scope.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/validator

WORKDIR /app

# Dependency layer, cached across source edits. Only what alembic/env.py imports:
# alembic itself, SQLAlchemy (the models) and the driver.
RUN pip install --no-cache-dir \
      "alembic>=1.13" "sqlalchemy>=2.0.30" "psycopg[binary]>=3.2"

# The models are the schema's source of truth, so autogenerate and the migration
# tests have something to compare against.
COPY validator/db ./validator/db

# Alembic config and revisions are ops artifacts, not part of any package.
COPY deploy/migrate/alembic.ini ./alembic.ini
COPY deploy/migrate/alembic ./alembic

CMD ["alembic", "upgrade", "head"]
