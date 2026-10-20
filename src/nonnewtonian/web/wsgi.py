"""gunicorn entrypoint. Production mode is a property of this module, not
of an env flag someone can forget: deploy/gunicorn.conf.py targets
nonnewtonian.web.wsgi:app."""
from . import create_app

app = create_app(production=True)
