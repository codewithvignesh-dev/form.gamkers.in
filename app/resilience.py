"""Keep the public site usable when a dependency (the MySQL server) is down.

Database failures are expected operating conditions, not bugs: they never reach
the visitor as a stack trace. Pages that can degrade (the landing page) do so,
and every other page answers with a friendly 503 and a Retry-After header.
"""

from flask import current_app, g, make_response, render_template
from sqlalchemy.exc import (
    DisconnectionError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.extensions import db

# Connection refused / lost server / pool exhausted. Constraint and syntax errors
# are deliberately not here: those are application bugs or handled business cases.
DATABASE_ERRORS = (OperationalError, InterfaceError, DisconnectionError, PoolTimeoutError)

RETRY_AFTER_SECONDS = "30"


def database_down():
    """True once this request has already seen the database fail."""
    return bool(g.get("db_unavailable"))


def mark_database_down(error):
    """Remember the outage for the rest of this request and release the session."""
    g.db_unavailable = True
    current_app.logger.error(
        "Database unavailable (%s): %s", type(error).__name__, getattr(error, "orig", error)
    )
    try:
        db.session.rollback()
    except Exception:  # the connection is already gone; nothing is left to undo
        pass


def service_unavailable_response():
    response = make_response(render_template("errors/503.html"), 503)
    response.headers["Retry-After"] = RETRY_AFTER_SECONDS
    response.headers["Cache-Control"] = "no-store"
    return response


def server_error_response():
    response = make_response(render_template("errors/500.html"), 500)
    response.headers["Cache-Control"] = "no-store"
    return response


def register_error_handlers(app):
    def database_unavailable(error):
        mark_database_down(error)
        return service_unavailable_response()

    for error_class in DATABASE_ERRORS:
        app.register_error_handler(error_class, database_unavailable)

    @app.errorhandler(503)
    def unavailable(_error):
        return service_unavailable_response()

    @app.errorhandler(500)
    def internal_error(_error):
        # Flask has already logged the traceback; the visitor only sees a plain page.
        try:
            db.session.rollback()
        except Exception:
            pass
        return server_error_response()
