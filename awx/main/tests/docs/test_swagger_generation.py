import json
import yaml
import os
import re

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.functional import Promise
from django.utils.encoding import force_text

from coreapi.compat import force_bytes
from openapi_codec.encode import generate_swagger_object
import pytest

import awx
from awx.api.versioning import drf_reverse


config_dest = os.sep.join([
    os.path.realpath(os.path.dirname(awx.__file__)),
    'api', 'templates', 'swagger'
])
config_file = os.sep.join([config_dest, 'config.yml'])
description_file = os.sep.join([config_dest, 'description.md'])


class i18nEncoder(DjangoJSONEncoder):
    def default(self, obj):
        if isinstance(obj, Promise):
            return force_text(obj)
        return super(i18nEncoder, self).default(obj)


@pytest.mark.django_db
class TestSwaggerGeneration():
    """
    This class is used to generate a Swagger/OpenAPI document for the awx
    API.  A _prepare fixture generates a JSON blob containing OpenAPI data,
    individual tests have the ability modify the payload.

    Finally, the JSON content is written to a file, `swagger.json`, in the
    current working directory.

    $ py.test test_swagger_generation.py --version 3.3.0

    To customize the `info.description` in the generated OpenAPI document,
    modify the text in `awx.api.templates.swagger.description.md`
    """
    JSON = {}

    @pytest.fixture(autouse=True, scope='function')
    def _prepare(self, get, admin):
        if not self.__class__.JSON:
            url = drf_reverse('api:swagger_view') + '?format=openapi'
            response = get(url, user=admin)
            data = generate_swagger_object(response.data)
            if response.has_header('X-Deprecated-Paths'):
                data['deprecated_paths'] = json.loads(response['X-Deprecated-Paths'])
            data.update(response.accepted_renderer.get_customizations() or {})

            data['host'] = None
            data['schemes'] = ['https']
            data['consumes'] = ['application/json']

            # Inject a top-level description into the OpenAPI document
            if os.path.exists(description_file):
                with open(description_file, 'r') as f:
                    data['info']['description'] = f.read()

            # Write tags in the order we want them sorted
            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    config = yaml.load(f.read())
                    for category in config.get('categories', []):
                        tag = {'name': category['name']}
                        if 'description' in category:
                            tag['description'] = category['description']
                        data.setdefault('tags', []).append(tag)

            revised_paths = {}
            deprecated_paths = data.pop('deprecated_paths', [])
            for path, node in data['paths'].items():
                # change {version} in paths to the actual default API version (e.g., v2)
                revised_paths[path.replace(
                    '{version}',
                    settings.REST_FRAMEWORK['DEFAULT_VERSION']
                )] = node
                for method in node:
                    if path in deprecated_paths:
                        node[method]['deprecated'] = True
                    if 'description' in node[method]:
                        # Pop off the first line and use that as the summary
                        lines = node[method]['description'].splitlines()
                        node[method]['summary'] = lines.pop(0).strip('#:')
                        node[method]['description'] = '\n'.join(lines)

                    # remove the required `version` parameter
                    for param in node[method].get('parameters'):
                        if param['in'] == 'path' and param['name'] == 'version':
                            node[method]['parameters'].remove(param)
            data['paths'] = revised_paths
            self.__class__.JSON = data

    def test_sanity(self, release):
        JSON = self.__class__.JSON
        JSON['info']['version'] = release

        # Make some basic assertions about the rendered JSON so we can
        # be sure it doesn't break across DRF upgrades and view/serializer
        # changes.
        assert len(JSON['tags'])
        assert len(JSON['paths'])

        # The number of API endpoints changes over time, but let's just check
        # for a reasonable number here; if this test starts failing, raise/lower the bounds
        paths = JSON['paths']
        assert 250 < len(paths) < 300
        assert paths['/api/'].keys() == ['get']
        assert paths['/api/v2/'].keys() == ['get']
        assert sorted(
            paths['/api/v2/credentials/'].keys()
        ) == ['get', 'post']
        assert sorted(
            paths['/api/v2/credentials/{id}/'].keys()
        ) == ['delete', 'get', 'patch', 'put']
        assert paths['/api/v2/settings/'].keys() == ['get']
        assert paths['/api/v2/settings/{category_slug}/'].keys() == [
            'get', 'put', 'patch', 'delete'
        ]

        # Test deprecated paths
        assert paths['/api/v2/jobs/{id}/extra_credentials/']['get']['deprecated'] is True

    @pytest.mark.parametrize('path', [
        '/api/',
        '/api/v2/',
        '/api/v2/ping/',
        '/api/v2/config/',
    ])
    def test_basic_paths(self, path, get, admin):
        # hit a couple important endpoints so we always have example data
        get(path, user=admin, expect=200)

    def test_autogen_response_examples(self, swagger_autogen):
        for pattern, node in TestSwaggerGeneration.JSON['paths'].items():
            pattern = pattern.replace('{id}', '[0-9]+')
            pattern = pattern.replace('{category_slug}', '[a-zA-Z0-9\-]+')
            for path, result in swagger_autogen.items():
                if re.match('^{}$'.format(pattern), path):
                    for key, value in result.items():
                        method, status_code = key
                        content_type, resp, request_data = value
                        if method in node:
                            status_code = str(status_code)
                            if content_type:
                                produces = node[method].setdefault('produces', [])
                                if content_type not in produces:
                                    produces.append(content_type)
                            if request_data and status_code.startswith('2'):
                                # DRF builds a schema based on the serializer
                                # fields.  This is _pretty good_, but if we
                                # have _actual_ JSON examples, those are even
                                # better and we should use them instead
                                for param in node[method].get('parameters'):
                                    if param['in'] == 'body':
                                        node[method]['parameters'].remove(param)
                                node[method].setdefault('parameters', []).append({
                                    'name': 'data',
                                    'in': 'body',
                                    'schema': {'example': request_data},
                                })

                            # Build response examples
                            if resp:
                                if content_type.startswith('text/html'):
                                    continue
                                if content_type == 'application/json':
                                    resp = json.loads(resp)
                                node[method]['responses'].setdefault(status_code, {}).setdefault(
                                    'examples', {}
                                )[content_type] = resp

    @classmethod
    def teardown_class(cls):
        with open('swagger.json', 'w') as f:
            f.write(force_bytes(
                json.dumps(cls.JSON, cls=i18nEncoder)
            ))
