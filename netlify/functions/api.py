from app import app
from serverless_wsgi import handle

def handler(event, context):
    return handle(app, event, context)
