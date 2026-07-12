"""WSGI entry point for hosts like PythonAnywhere.

Wraps the ASGI (FastAPI) app so classic WSGI servers can run it:

    from fie.wsgi import application
"""

from a2wsgi import ASGIMiddleware

from fie.api.app import create_app

application = ASGIMiddleware(create_app())
