from alembic import context
from sqlalchemy import create_engine

from heatmon.db.models import Base, UtcDateTime

config = context.config
connection = config.attributes.get("connection")


def render_item(type_, obj, autogen_context):
    # Keep migrations free of imports from model code.
    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.String(length=25)"
    return False


def run(conn):
    context.configure(
        connection=conn,
        target_metadata=Base.metadata,
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


if connection is not None:
    run(connection)
else:  # `alembic revision --autogenerate` from the command line
    with create_engine(config.get_main_option("sqlalchemy.url")).connect() as conn:
        run(conn)
