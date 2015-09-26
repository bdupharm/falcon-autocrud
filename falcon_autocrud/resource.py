import falcon
import json
from sqlalchemy.inspection import inspect


class CollectionResource(object):
    """
    Provides CRUD facilities for a resource collection.
    """
    def __init__(self, db_session):
        self.db_session = db_session

    def serialize(self, resource):
        return {
            attr: getattr(resource, attr) for attr in inspect(self.model).attrs.keys()
        }

    def on_get(self, req, resp, *args, **kwargs):
        """
        Return a collection of items.
        """
        resources = self.db_session.query(self.model)

        resp.status = falcon.HTTP_OK
        req.context['result'] = {
            'data': [
                self.serialize(resource) for resource in resources
            ],
        }

class SingleResource(object):
    """
    Provides CRUD facilities for a single resource.
    """
    def __init__(self, db_session):
        self.db_session = db_session