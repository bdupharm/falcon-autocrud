from datetime import date, datetime, time
from decimal import Decimal
import falcon
import falcon.errors
import json
import sqlalchemy.exc
import sqlalchemy.orm.exc
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.properties import ColumnProperty
from sqlalchemy.inspection import inspect
from sqlalchemy.orm.session import make_transient
import sqlalchemy.sql.sqltypes
import uuid
import logging
import sys

from .db_session import session_scope


def identify(req, resp, resource, params):
    identifiers = getattr(resource, '__identifiers__', {})
    if req.method in identifiers:
        Identifier = identifiers[req.method]
        Identifier().identify(req, resp, resource, params)


def authorize(req, resp, resource, params):
    authorizers = getattr(resource, '__authorizers__', {})
    if req.method in authorizers:
        Authorizer = authorizers[req.method]
        Authorizer().authorize(req, resp, resource, params)


def update_resource(resource, attributes):
    '''Update `resource` w/ `attributes`.'''
    for key, value in attributes.items():
        setattr(resource, key, value)


def get_pk(attributes):
    '''Get pk from a dictionary of attributes or throw error.'''
    try:
        return int(attributes.pop('pk'))
    except KeyError:
        raise falcon.errors.HTTPBadRequest('Invalid request', 'No primary key provided for related object.')


def identify_pk(resource_class):
    '''Find the primary key of a resource class.'''
    primary_key, = [
        attr
        for attr in inspect(resource_class).attrs.values()
        if isinstance(attr, ColumnProperty) and attr.columns[0].primary_key
    ]
    return primary_key.key


def add_included(instance, req, res, data):
    '''Add included objects to a data dictionary.'''
    if '__included' in req.params:
        allowed_included = getattr(instance, 'allowed_included', [])
        for included in req.get_param_as_list('__included'):
            if included not in allowed_included:
                raise falcon.errors.HTTPBadRequest('Invalid parameter', 'The "__included" parameter includes invalid entities')
            # Get secondary/tertiary objects
            if '.' in included:
                attrs = included.split('.')
                included_resources = res
                for attr in attrs:
                    included_resources = getattr(included_resources, attr)
            else:
                included_resources = getattr(res, included)

            # Store the related resource underneath the table name as a key
            if isinstance(included_resources, list):
                data['attributes'][included_resource.__tablename__] = []
                for included_resource in included_resources:
                    primary_key = identify_pk(included_resource.__class__)
                    attributes = instance.serialize(included_resource, getattr(included_resource, 'response_fields', None), getattr(included_resource, 'geometry_axes', {}))
                    data['attributes'][included_resource.__tablename__].append(attributes)
            elif included_resources is not None:
                primary_key = identify_pk(included_resources.__class__)
                attributes = instance.serialize(included_resources, getattr(included_resources, 'response_fields', None), getattr(included_resources, 'geometry_axes', {}))
                data['attributes'][included_resources.__tablename__] = attributes


class UnsupportedGeometryType(Exception):
    pass

try:
    import geoalchemy2.shape
    from geoalchemy2.elements import WKBElement
    from geoalchemy2.types import Geometry
    from shapely.geometry import Point, LineString, Polygon
    support_geo = True
except ImportError:
    support_geo = False


class BaseResource(object):
    def __init__(self, db_engine, logger=None, sessionmaker_=sessionmaker, sessionmaker_kwargs={}):
        self.db_engine = db_engine
        self.sessionmaker = sessionmaker_
        self.sessionmaker_kwargs = sessionmaker_kwargs
        if logger is None:
            logger = logging.getLogger('autocrud')
        self.logger = logger

    def filter_by_params(self, resources, params):
        for filter_key, value in params.items():
            if filter_key.startswith('__'):
                # Not a filtering parameter
                continue
            filter_parts = filter_key.split('__')
            key = filter_parts[0]
            if len(filter_parts) == 1:
                comparison = '='
            elif len(filter_parts) == 2:
                comparison = filter_parts[1]
            else:
                raise falcon.errors.HTTPBadRequest('Invalid attribute', 'An attribute provided for filtering is invalid')

            attr = getattr(self.model, key, None)
            if attr is None or not isinstance(inspect(self.model).attrs[key], ColumnProperty):
                self.logger.warn('An attribute ({0}) provided for filtering is invalid'.format(key))
                raise falcon.errors.HTTPBadRequest('Invalid attribute', 'An attribute provided for filtering is invalid')
            if comparison == '=':
                resources = resources.filter(attr == value)
            elif comparison == 'in':
                resources = resources.filter(attr.in_(value))
            elif comparison == 'null':
                if value != '0':
                    resources = resources.filter(attr.is_(None))
                else:
                    resources = resources.filter(attr.isnot(None))
            elif comparison == 'startswith':
                resources = resources.filter(attr.ilike('{0}%'.format(value)))
            elif comparison == 'contains':
                resources = resources.filter(attr.ilike('%{0}%'.format(value)))
            elif comparison == 'lt':
                resources = resources.filter(attr < value)
            elif comparison == 'lte':
                resources = resources.filter(attr <= value)
            elif comparison == 'gt':
                resources = resources.filter(attr > value)
            elif comparison == 'gte':
                resources = resources.filter(attr >= value)
            else:
                raise falcon.errors.HTTPBadRequest('Invalid attribute', 'An attribute provided for filtering is invalid')
        return resources

    def serialize(self, resource, response_fields=None, geometry_axes=None):
        attrs           = inspect(resource.__class__).attrs
        naive_datetimes = getattr(self, 'naive_datetimes', [])
        def _serialize_value(name, value):
            if isinstance(value, uuid.UUID):
                return value.hex
            if isinstance(value, datetime):
                if name in naive_datetimes:
                    return value.strftime('%Y-%m-%dT%H:%M:%S')
                else:
                    return value.strftime('%Y-%m-%dT%H:%M:%SZ')
            elif isinstance(value, date):
                return value.strftime('%Y-%m-%d')
            elif isinstance(value, time):
                return value.isoformat()
            elif isinstance(value, Decimal):
                return float(value)
            elif support_geo and isinstance(value, WKBElement):
                value = geoalchemy2.shape.to_shape(value)
                if isinstance(value, Point):
                    axes = (geometry_axes or {}).get(name, ['x', 'y'])
                    return {axes[0]: value.x, axes[1]: value.y}
                elif isinstance(value, LineString):
                    axes = (geometry_axes or {}).get(name, ['x', 'y'])
                    return [
                        {axes[0]: point[0], axes[1]: point[1]}
                        for point in list(value.coords)
                    ]
                elif isinstance(value, Polygon):
                    axes = (geometry_axes or {}).get(name, ['x', 'y'])
                    return [
                        {axes[0]: point[0], axes[1]: point[1]}
                        for point in list(value.boundary.coords)
                    ]
                else:
                    raise UnsupportedGeometryType('Unsupported geometry type {0}'.format(value.geometryType()))
            else:
                return value
        if response_fields is None:
            response_fields = attrs.keys()
        return {
            attr: _serialize_value(attr, getattr(resource, attr)) for attr in response_fields if isinstance(attrs[attr], ColumnProperty)
        }

    def apply_arg_filter(self, req, resp, resources, kwargs):
        for key, value in kwargs.items():
            key = getattr(self, 'attr_map', {}).get(key, key)
            if callable(key):
                resources = key(req, resp, resources, **kwargs)
            else:
                attr = getattr(self.model, key, None)
                if attr is None or not isinstance(inspect(self.model).attrs[key], ColumnProperty):
                    self.logger.error("Programming error: {0}.attr_map['{1}'] does not exist or is not a column".format(self.model, key))
                    raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')
                resources = resources.filter(attr == value)
        return resources

    def apply_default_attributes(self, defaults_type, req, resp, attributes):
        defaults = getattr(self, defaults_type, {})
        for key, setter in defaults.items():
            if key not in attributes:
                attributes[key] = setter(req, resp, attributes)

class CollectionResource(BaseResource):
    """
    Provides CRUD facilities for a resource collection.
    """
    def deserialize(self, model, path_data, body_data, allow_recursion=False):
        mapper          = inspect(model)
        attributes      = {}
        naive_datetimes = getattr(self, 'naive_datetimes', [])

        for key, value in path_data.items():
            key = getattr(self, 'attr_map', {}).get(key, key)
            if getattr(model, key, None) is None or not isinstance(inspect(model).attrs[key], ColumnProperty):
                self.logger.error("Programming error: {0}.attr_map['{1}'] does not exist or is not a column".format(model, key))
                raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')
            attributes[key] = value

        deserialized = [attributes, {}]

        for key, value in body_data.items():
            if isinstance(getattr(model, key, None), property):
                # Value is set using a function, so we cannot tell what type it will be
                attributes[key] = value
                continue
            try:
                column = mapper.columns[key]
            except KeyError:
                if not allow_recursion:
                    # Assume programmer has done their job of filtering out invalid
                    # columns, and that they are going to use this field for some
                    # custom purpose
                    continue
                try:
                    relationship = mapper.relationships[key]
                    if relationship.uselist:
                        for entity in value:
                            deserialized[1][key] = [self.deserialize(relationship.mapper.entity, {}, entity, False)[0] for entity in value]
                    else:
                        deserialized[1][key] = self.deserialize(relationship.mapper.entity, {}, value, False)[0]
                    continue
                except KeyError:
                    # Assume programmer has done their job of filtering out invalid
                    # columns, and that they are going to use this field for some
                    # custom purpose
                    continue
            if isinstance(column.type, sqlalchemy.sql.sqltypes.DateTime):
                if value is None:
                    attributes[key] = None
                elif key in naive_datetimes:
                    attributes[key] = datetime.strptime(value, '%Y-%m-%dT%H:%M:%S')
                else:
                    attributes[key] = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
            elif isinstance(column.type, sqlalchemy.sql.sqltypes.Date):
                attributes[key] = datetime.strptime(value, '%Y-%m-%d').date() if value is not None else None
            elif isinstance(column.type, sqlalchemy.sql.sqltypes.Time):
                if value is not None:
                    hour, minute, second = value.split(':')
                    attributes[key] = time(int(hour), int(minute), int(second))
                else:
                    attributes[key] = None
            elif support_geo and isinstance(column.type, Geometry) and column.type.geometry_type == 'POINT':
                axes    = getattr(self, 'geometry_axes', {}).get(key, ['x', 'y'])
                point   = Point(value[axes[0]], value[axes[1]])
                # geoalchemy2.shape.from_shape uses buffer() which causes INSERT to fail
                attributes[key] = WKBElement(point.wkb, srid=4326)
            elif support_geo and isinstance(column.type, Geometry) and column.type.geometry_type == 'LINESTRING':
                axes    = getattr(self, 'geometry_axes', {}).get(key, ['x', 'y'])
                line    = LineString([point[axes[0]], point[axes[1]]] for point in value)
                # geoalchemy2.shape.from_shape uses buffer() which causes INSERT to fail
                attributes[key] = WKBElement(line.wkb, srid=4326)
            elif support_geo and isinstance(column.type, Geometry) and column.type.geometry_type == 'POLYGON':
                axes    = getattr(self, 'geometry_axes', {}).get(key, ['x', 'y'])
                polygon = Polygon([point[axes[0]], point[axes[1]]] for point in value)
                # geoalchemy2.shape.from_shape uses buffer() which causes INSERT to fail
                attributes[key] = WKBElement(polygon.wkb, srid=4326)
            else:
                attributes[key] = value
        return deserialized

    def get_filter(self, req, resp, query, *args, **kwargs):
        return query

    @falcon.before(identify)
    @falcon.before(authorize)
    def on_get(self, req, resp, *args, **kwargs):
        """
        Return a collection of items.
        """
        if 'GET' not in getattr(self, 'methods', ['GET', 'POST', 'PATCH']):
            raise falcon.errors.HTTPMethodNotAllowed(getattr(self, 'methods', ['GET', 'POST', 'PATCH']))

        with session_scope(self.db_engine, sessionmaker_=self.sessionmaker, **self.sessionmaker_kwargs) as db_session:
            resources = self.apply_arg_filter(req, resp, db_session.query(self.model), kwargs)

            resources = self.filter_by_params(
                self.get_filter(
                    req, resp,
                    resources,
                    *args, **kwargs
                ),
                req.params
            )

            sort                = getattr(self, 'default_sort', None)
            using_default_sort  = True
            if '__sort' in req.params:
                using_default_sort = False
                sort = req.get_param_as_list('__sort')
            if sort is not None:
                order_fields = []
                for field_name in sort:
                    reverse = False
                    if field_name[0] == '-':
                        field_name = field_name[1:]
                        reverse = True
                    attr = getattr(self.model, field_name, None)
                    if attr is None or not isinstance(inspect(self.model).attrs[field_name], ColumnProperty):
                        if using_default_sort:
                            self.logger.error("Programming error: Sort field {0}.{1} does not exist or is not a column".format(self.model, field_name))
                            raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')
                        else:
                            raise falcon.errors.HTTPBadRequest('Invalid attribute', 'An attribute provided for sorting is invalid')
                    if reverse:
                        order_fields.append(attr.desc())
                    else:
                        order_fields.append(attr)
                resources = resources.order_by(*order_fields)

            count = None
            page = req.get_param_as_int('__page')
            page_size = req.get_param_as_int('__page_size')
            if page and page_size:
                # count before filtering
                count     = resources.count()
                resources = resources.offset((page - 1) * page_size)
                resources = resources.limit(page_size)

            resp.status = falcon.HTTP_OK
            result = {
                'data': [],
            }
            for resource in resources:
                primary_key = identify_pk(resource.__class__)
                instance = {
                    'pk':           getattr(resource, primary_key),
                    'type':         resource.__tablename__,
                    'attributes':   self.serialize(resource, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {})),
                }
                add_included(self, req, resource, instance)
                result['data'].append(instance)

            if page is not None and page_size is not None:
                result['meta'] = {'total': count}
                result['meta']['page'] = page
                result['meta']['page_size'] = page_size
            req.context['result'] = result

            after_get = getattr(self, 'after_get', None)
            if after_get is not None:
                after_get(req, resp, resources, *args, **kwargs)

    @falcon.before(identify)
    @falcon.before(authorize)
    def on_post(self, req, resp, *args, **kwargs):
        """
        Add an item to the collection.
        """
        if 'POST' not in getattr(self, 'methods', ['GET', 'POST', 'PATCH']):
            raise falcon.errors.HTTPMethodNotAllowed(getattr(self, 'methods', ['GET', 'POST', 'PATCH']))

        attributes, linked = self.deserialize(self.model, kwargs, req.context['doc'] if 'doc' in req.context else None, getattr(self, 'allow_subresources', True))

        with session_scope(self.db_engine, sessionmaker_=self.sessionmaker, **self.sessionmaker_kwargs) as db_session:
            self.apply_default_attributes('post_defaults', req, resp, attributes)

            resource = self.model(**attributes)

            before_post = getattr(self, 'before_post', None)
            if before_post is not None:
                self.before_post(req, resp, db_session, resource, *args, **kwargs)

            db_session.add(resource)
            try:
                # Begin a nested transaction to create subresources
                db_session.begin_nested()

                # Add related objects now
                mapper = inspect(self.model)
                subresources_created = []
                for key, value in linked.items():
                    relationship = mapper.relationships[key]
                    resource_class = relationship.mapper.entity
                    if relationship.uselist:
                        for attributes in value:
                            subresource = resource_class(**attributes)
                            db_session.add(subresource)
                            subresources_created.append((key, 'onetomany', subresource))
                            getattr(resource, key).append(subresource)
                    else:
                        subresource = resource_class(**value)
                        db_session.add(subresource)
                        subresources_created.append((key, 'onetoone', subresource))
                        setattr(resource, key, subresource)

                # Inner commit (subresources)
                db_session.commit()

                # Now that subresources have ids, assign the correct reference ids
                for _, relationship_type, subresource in subresources_created:
                    if relationship_type == 'onetoone':
                        # Resource should have a referencee to subresource
                        subresource_id = subresource.__tablename__ + '_id'
                        if hasattr(resource, subresource_id):
                            setattr(resource, subresource_id, getattr(subresource, subresource_id))
                        # Subresource MAY have a reference to resource (Less likely)
                        resource_id = resource.__tablename__ + '_id'
                        if hasattr(subresource, resource_id):
                            setattr(subresource, resource_id, getattr(resource, resource_id))
                    elif relationship_type == 'onetomany':
                        # Subresource should have a reference to resource
                        resource_id = resource.__tablename__ + '_id'
                        if hasattr(subresource, resource_id):
                            setattr(subresource, resource_id, getattr(resource, resource_id))
                        # TODO: Maybe resource has a list of subresource ids?

                # Outer commit (resource)
                db_session.commit()
            except sqlalchemy.exc.IntegrityError as err:
                # Cases such as unallowed NULL value should have been checked
                # before we got here (e.g. validate against schema
                # using the middleware) - therefore assume this is a UNIQUE
                # constraint violation
                db_session.rollback()
                raise falcon.errors.HTTPConflict('Conflict', 'Unique constraint violated')
            except sqlalchemy.exc.ProgrammingError as err:
                db_session.rollback()
                if err.orig.args[1] == '23505':
                    raise falcon.errors.HTTPConflict('Conflict', 'Unique constraint violated')
                else:
                    raise
            except:
                db_session.rollback()
                raise

            resp.status = falcon.HTTP_CREATED
            req.context['result'] = {
                'data': self.serialize(resource, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {})),
            }
            # Add subresources created to response
            for relationship_key, relationship_type, subresource in subresources_created:
                req.context['result']['data'][relationship_key] = self.serialize(subresource, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {}))

            after_post = getattr(self, 'after_post', None)
            if after_post is not None:
                after_post(req, resp, resource)

    @falcon.before(identify)
    @falcon.before(authorize)
    def on_patch(self, req, resp, *args, **kwargs):
        """
        Update a collection.

        For now, it only supports adding entities to the collection, like this:

        {
            'patches': [
                {'op': 'add', 'path': '/', 'value': {'name': 'Jim', 'age', 25}},
                {'op': 'add', 'path': '/', 'value': {'name': 'Bob', 'age', 28}}
            ]
        }

        """
        if 'PATCH' not in getattr(self, 'methods', ['GET', 'POST', 'PATCH']):
            raise falcon.errors.HTTPMethodNotAllowed(getattr(self, 'methods', ['GET', 'POST', 'PATCH']))

        patch_paths = getattr(self, 'patch_paths', {})
        if len(patch_paths) == 0:
            patch_paths['/'] = self.model
        patch_lookups = {
            path: {
                'model':    model,
                'mapper':   inspect(model),
            } for path, model in patch_paths.items()
        }
        patches = req.context['doc']['patches']

        with session_scope(self.db_engine, sessionmaker_=self.sessionmaker, **self.sessionmaker_kwargs) as db_session:
            for index, patch in enumerate(patches):
                # Only support adding entities in a collection patch, for now
                if 'op' not in patch or patch['op'] not in ['add']:
                    raise falcon.errors.HTTPBadRequest('Invalid patch', 'Patch {0} is not valid'.format(index))
                if patch['op'] == 'add':
                    if 'path' not in patch or patch['path'] not in patch_paths:
                        raise falcon.errors.HTTPBadRequest('Invalid patch', 'Patch {0} is not valid for op {1}'.format(index, patch['op']))

                    model   = patch_lookups[patch['path']]['model']
                    mapper  = patch_lookups[patch['path']]['mapper']

                    try:
                        patch_value = patch['value']
                    except KeyError:
                        raise falcon.errors.HTTPBadRequest('Invalid patch', 'Patch {0} is not valid for op {1}'.format(index, patch['op']))
                    args = {}
                    for key, value in kwargs.items():
                        key = getattr(self, 'attr_map', {}).get(key, key)
                        if getattr(model, key, None) is None or not isinstance(inspect(model).attrs[key], ColumnProperty):
                            self.logger.error("Programming error: {0}.attr_map['{1}'] does not exist or is not a column".format(model, key))
                            raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')
                        args[key] = value
                    for key, value in patch_value.items():
                        if isinstance(mapper.columns[key].type, sqlalchemy.sql.sqltypes.DateTime):
                            args[key] = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
                        else:
                            args[key] = value
                    resource = model(**args)
                    db_session.add(resource)

            try:
                db_session.commit()
            except sqlalchemy.exc.IntegrityError as err:
                # Cases such as unallowed NULL value should have been checked
                # before we got here (e.g. validate against schema
                # using the middleware) - therefore assume this is a UNIQUE
                # constraint violation
                db_session.rollback()
                raise falcon.errors.HTTPConflict('Conflict', 'Unique constraint violated')
            except sqlalchemy.exc.ProgrammingError as err:
                db_session.rollback()
                if err.orig.args[1] == '23505':
                    raise falcon.errors.HTTPConflict('Conflict', 'Unique constraint violated')
                else:
                    raise
            except:
                db_session.rollback()
                raise

        resp.status = falcon.HTTP_OK
        req.context['result'] = {}

        after_patch = getattr(self, 'after_patch', None)
        if after_patch is not None:
            after_patch(req, resp, *args, **kwargs)


class SingleResource(BaseResource):
    """
    Provides CRUD facilities for a single resource.
    """
    def deserialize(self, data, allow_recursion=False, model=None):
        if model is None:
            model = self.model
        mapper          = inspect(model)
        attributes      = {}
        naive_datetimes = getattr(self, 'naive_datetimes', [])

        deserialized = [attributes, {}]

        for key, value in data.items():
            # Explicitly allow deserialization of an PK (primary key) field
            if key == "pk":
                attributes[key] = value
                continue
            if isinstance(getattr(self.model, key, None), property):
                # Value is set using a function, so we cannot tell what type it will be
                attributes[key] = value
                continue
            try:
                column = mapper.columns[key]
            except KeyError:
                if not allow_recursion:
                    # Assume programmer has done their job of filtering out invalid
                    # columns, and that they are going to use this field for some
                    # custom purpose
                    continue
                try:
                    relationship = mapper.relationships[key]
                    if relationship.uselist:
                        for entity in value:
                            deserialized[1][key] = [self.deserialize(entity, False, relationship.mapper.entity)[0] for entity in value]
                    else:
                        deserialized[1][key] = self.deserialize(value, False, relationship.mapper.entity)[0]
                    continue
                except KeyError:
                    # Assume programmer has done their job of filtering out invalid
                    # columns, and that they are going to use this field for some
                    # custom purpose
                    continue
            if isinstance(column.type, sqlalchemy.sql.sqltypes.DateTime):
                if value is None:
                    attributes[key] = None
                elif key in naive_datetimes:
                    attributes[key] = datetime.strptime(value, '%Y-%m-%dT%H:%M:%S')
                else:
                    attributes[key] = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
            elif isinstance(column.type, sqlalchemy.sql.sqltypes.Date):
                attributes[key] = datetime.strptime(value, '%Y-%m-%d').date() if value is not None else None
            elif isinstance(column.type, sqlalchemy.sql.sqltypes.Time):
                if value is not None:
                    hour, minute, second = value.split(':')
                    attributes[key] = time(int(hour), int(minute), int(second))
                else:
                    attributes[key] = None
            elif support_geo and isinstance(column.type, Geometry) and column.type.geometry_type == 'POINT':
                axes    = getattr(self, 'geometry_axes', {}).get(key, ['x', 'y'])
                point   = Point(value[axes[0]], value[axes[1]])
                # geoalchemy2.shape.from_shape uses buffer() which causes INSERT to fail
                attributes[key] = WKBElement(point.wkb, srid=4326)
            elif support_geo and isinstance(column.type, Geometry) and column.type.geometry_type == 'LINESTRING':
                axes    = getattr(self, 'geometry_axes', {}).get(key, ['x', 'y'])
                line    = LineString([point[axes[0]], point[axes[1]]] for point in value)
                # geoalchemy2.shape.from_shape uses buffer() which causes INSERT to fail
                attributes[key] = WKBElement(line.wkb, srid=4326)
            elif support_geo and isinstance(column.type, Geometry) and column.type.geometry_type == 'POLYGON':
                axes    = getattr(self, 'geometry_axes', {}).get(key, ['x', 'y'])
                polygon = Polygon([point[axes[0]], point[axes[1]]] for point in value)
                # geoalchemy2.shape.from_shape uses buffer() which causes INSERT to fail
                attributes[key] = WKBElement(polygon.wkb, srid=4326)
            else:
                attributes[key] = value

        return deserialized

    def get_filter(self, req, resp, query, *args, **kwargs):
        return query

    @falcon.before(identify)
    @falcon.before(authorize)
    def on_get(self, req, resp, *args, **kwargs):
        """
        Return a single item.
        """
        if 'GET' not in getattr(self, 'methods', ['GET', 'PUT', 'PATCH', 'DELETE']):
            raise falcon.errors.HTTPMethodNotAllowed(getattr(self, 'methods', ['GET', 'PUT', 'PATCH', 'DELETE']))

        with session_scope(self.db_engine, sessionmaker_=self.sessionmaker, **self.sessionmaker_kwargs) as db_session:
            resources = self.apply_arg_filter(req, resp, db_session.query(self.model), kwargs)

            resources = self.get_filter(req, resp, resources, *args, **kwargs)

            try:
                resource = resources.one()
            except sqlalchemy.orm.exc.NoResultFound:
                raise falcon.errors.HTTPNotFound()
            except sqlalchemy.orm.exc.MultipleResultsFound:
                self.logger.error('Programming error: multiple results found for get of model {0}'.format(self.model))
                raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')

            resp.status = falcon.HTTP_OK
            primary_key = identify_pk(resource.__class__)
            result = {
                'data': {
                    'pk':           getattr(resource, primary_key),
                    'type':         resource.__tablename__,
                    'attributes':   self.serialize(resource, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {})),
                }
            }
            add_included(self, req, resource, result['data'])
            req.context['result'] = result

            after_get = getattr(self, 'after_get', None)
            if after_get is not None:
                after_get(req, resp, resource, *args, **kwargs)

    def delete_precondition(self, req, resp, query, *args, **kwargs):
        return query

    @falcon.before(identify)
    @falcon.before(authorize)
    def on_delete(self, req, resp, *args, **kwargs):
        """
        Delete a single item.
        """
        if 'DELETE' not in getattr(self, 'methods', ['GET', 'PUT', 'PATCH', 'DELETE']):
            raise falcon.errors.HTTPMethodNotAllowed(getattr(self, 'methods', ['GET', 'PUT', 'PATCH', 'DELETE']))

        with session_scope(self.db_engine, sessionmaker_=self.sessionmaker, **self.sessionmaker_kwargs) as db_session:
            resources = self.apply_arg_filter(req, resp, db_session.query(self.model), kwargs)

            try:
                resource = resources.one()
            except sqlalchemy.orm.exc.NoResultFound:
                raise falcon.errors.HTTPNotFound()
            except sqlalchemy.orm.exc.MultipleResultsFound:
                self.logger.error('Programming error: multiple results found for patch of model {0}'.format(self.model))
                raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')

            resources = self.delete_precondition(
                req, resp,
                self.filter_by_params(resources, req.params),
                *args, **kwargs
            )

            try:
                resource = resources.one()
            except sqlalchemy.orm.exc.NoResultFound:
                raise falcon.errors.HTTPConflict('Conflict', 'Resource found but conditions violated')
            except sqlalchemy.orm.exc.MultipleResultsFound:
                self.logger.error('Programming error: multiple results found for delete of model {0}'.format(self.model))
                raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')

            try:
                mark_deleted = getattr(self, 'mark_deleted', None)
                if mark_deleted is not None:
                    mark_deleted(req, resp, resource, *args, **kwargs)
                    db_session.add(resource)
                else:
                    make_transient(resource)
                    resources.delete()
                db_session.commit()
            except sqlalchemy.exc.IntegrityError as err:
                # As far we I know, this should only be caused by foreign key constraint being violated
                db_session.rollback()
                raise falcon.errors.HTTPConflict('Conflict', 'Other content links to this')
            except sqlalchemy.exc.ProgrammingError as err:
                db_session.rollback()
                if err.orig.args[1] == '23503':
                    raise falcon.errors.HTTPConflict('Conflict', 'Other content links to this')
                else:
                    raise

            resp.status = falcon.HTTP_OK
            req.context['result'] = {
                'data': self.serialize(resource, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {})),
            }

            after_delete = getattr(self, 'after_delete', None)
            if after_delete is not None:
                after_delete(req, resp, resource, *args, **kwargs)


    @falcon.before(identify)
    @falcon.before(authorize)
    def on_put(self, req, resp, *args, **kwargs):
        """
        Update an item in the collection.
        """
        if 'PUT' not in getattr(self, 'methods', ['GET', 'PUT', 'PATCH', 'DELETE']):
            raise falcon.errors.HTTPMethodNotAllowed(getattr(self, 'methods', ['GET', 'PUT', 'PATCH', 'DELETE']))

        with session_scope(self.db_engine, sessionmaker_=self.sessionmaker, **self.sessionmaker_kwargs) as db_session:
            resources = self.apply_arg_filter(req, resp, db_session.query(self.model), kwargs)

            try:
                resource = resources.one()
            except sqlalchemy.orm.exc.NoResultFound:
                raise falcon.errors.HTTPNotFound()
            except sqlalchemy.orm.exc.MultipleResultsFound:
                self.logger.error('Programming error: multiple results found for put of model {0}'.format(self.model))
                raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')

            attributes = self.deserialize(req.context['doc'])

            self.apply_default_attributes('put_defaults', req, resp, attributes)

            for key, value in attributes.items():
                setattr(resource, key, value)

            db_session.add(resource)
            try:
                db_session.commit()
            except sqlalchemy.exc.IntegrityError as err:
                # Cases such as unallowed NULL value should have been checked
                # before we got here (e.g. validate against schema
                # using the middleware) - therefore assume this is a UNIQUE
                # constraint violation
                db_session.rollback()
                raise falcon.errors.HTTPConflict('Conflict', 'Unique constraint violated')
            except sqlalchemy.exc.ProgrammingError as err:
                db_session.rollback()
                if err.orig.args[1] == '23505':
                    raise falcon.errors.HTTPConflict('Conflict', 'Unique constraint violated')
                else:
                    raise
            except:
                db_session.rollback()
                raise

            resp.status = falcon.HTTP_OK
            req.context['result'] = {
                'data': self.serialize(resource, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {})),
            }

            after_put = getattr(self, 'after_put', None)
            if after_put is not None:
                after_put(req, resp, resource, *args, **kwargs)

    def patch_precondition(self, req, resp, query, *args, **kwargs):
        return query

    def modify_patch(self, req, resp, resource, *args, **kwargs):
        pass

    @falcon.before(identify)
    @falcon.before(authorize)
    def on_patch(self, req, resp, *args, **kwargs):
        """
        Update part of an item in the collection.
        """
        if 'PATCH' not in getattr(self, 'methods', ['GET', 'PUT', 'PATCH', 'DELETE']):
            raise falcon.errors.HTTPMethodNotAllowed(getattr(self, 'methods', ['GET', 'PUT', 'PATCH', 'DELETE']))

        with session_scope(self.db_engine, sessionmaker_=self.sessionmaker, **self.sessionmaker_kwargs) as db_session:
            resources = self.apply_arg_filter(req, resp, db_session.query(self.model), kwargs)

            try:
                resource = resources.one()
            except sqlalchemy.orm.exc.NoResultFound:
                raise falcon.errors.HTTPNotFound()
            except sqlalchemy.orm.exc.MultipleResultsFound:
                self.logger.error('Programming error: multiple results found for patch of model {0}'.format(self.model))
                raise falcon.errors.HTTPInternalServerError('Internal Server Error', 'An internal server error occurred')

            resources = self.patch_precondition(
                req, resp,
                self.filter_by_params(resources, req.params),
                *args, **kwargs
            )

            try:
                resource = resources.one()
            except sqlalchemy.orm.exc.NoResultFound:
                raise falcon.errors.HTTPConflict('Conflict', 'Resource found but conditions violated')

            attributes, linked = self.deserialize(req.context['doc'], allow_recursion=getattr(self, 'allow_subresources', True))

            self.apply_default_attributes('patch_defaults', req, resp, attributes)

            update_resource(resource, attributes)

            self.modify_patch(req, resp, resource, *args, **kwargs)

            before_patch = getattr(self, 'before_patch', None)
            if before_patch is not None:
                self.before_patch(req, resp, db_session, resource, *args, **kwargs)

            db_session.add(resource)
            # Patch related
            mapper = inspect(self.model)
            # Store updated subresources to return in the response
            updated_subresources = {}
            for key, value in linked.items():
                relationship = mapper.relationships[key]
                resource_class = relationship.mapper.entity
                subresource_pk = identify_pk(resource_class)
                if relationship.uselist:
                    subresources = getattr(resource, key)
                    subresources_pks = [getattr(sr, subresource_pk) for sr in subresources]
                    updated_subresources[key] = []
                    for attributes in value:
                        lookup_pk = get_pk(attributes)
                        if lookup_pk not in subresources_pks:
                            raise falcon.errors.HTTPBadRequest('Invalid request', 'Primary key not found in related resources.')
                        update_resource(subresource, attributes)
                        updated_subresources[key].append(subresource)
                else:
                    subresource = getattr(resource, key)
                    if subresource is None:
                        raise falcon.errors.HTTPBadRequest('Invalid request', 'Related resource does not exist.')
                    lookup_pk = get_pk(value)
                    if lookup_pk != getattr(subresource, subresource_pk):
                        raise falcon.errors.HTTPBadRequest('Invalid request', 'Primary key does not match related resource.')
                    update_resource(subresource, value)
                    updated_subresources[key] = subresource
            try:
                db_session.commit()
            except sqlalchemy.exc.IntegrityError as err:
                # Cases such as unallowed NULL value should have been checked
                # before we got here (e.g. validate against schema
                # using the middleware) - therefore assume this is a UNIQUE
                # constraint violation
                db_session.rollback()
                raise falcon.errors.HTTPConflict('Conflict', 'Unique constraint violated')
            except sqlalchemy.exc.ProgrammingError as err:
                db_session.rollback()
                if err.orig.args[1] == '23505':
                    raise falcon.errors.HTTPConflict('Conflict', 'Unique constraint violated')
                else:
                    raise
            except:
                db_session.rollback()
                raise

            resp.status = falcon.HTTP_OK
            req.context['result'] = {
                'data': self.serialize(resource, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {})),
            }
            for key, value in updated_subresources.items():
                if isinstance(value, list):
                    req.context['result']['data'][key] = [
                        self.serialize(resource, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {}))
                        for resource in
                        value
                    ]
                else:
                    req.context['result']['data'][key] = self.serialize(value, getattr(self, 'response_fields', None), getattr(self, 'geometry_axes', {}))

            after_patch = getattr(self, 'after_patch', None)
            if after_patch is not None:
                after_patch(req, resp, resource, *args, **kwargs)
