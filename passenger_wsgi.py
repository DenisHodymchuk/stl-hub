import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

def application(environ, start_response):
    from a2wsgi import ASGIMiddleware
    from app import app
    if not hasattr(application, "_wsgi"):
        application._wsgi = ASGIMiddleware(app)
    return application._wsgi(environ, start_response)
