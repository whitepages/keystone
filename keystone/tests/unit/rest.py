# Copyright 2013 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_serialization import jsonutils
from six.moves import http_client
import webtest

from keystone.auth import controllers as auth_controllers
from keystone.policy.backends import rules
from keystone.tests import unit
from keystone.tests.unit import default_fixtures
from keystone.tests.unit.ksfixtures import database


class RestfulTestCase(unit.TestCase):
    """Performs restful tests against the WSGI app over HTTP.

    This class launches public & admin WSGI servers for every test, which can
    be accessed by calling ``public_request()`` or ``admin_request()``,
    respectfully.

    ``restful_request()`` and ``request()`` methods are also exposed if you
    need to bypass restful conventions or access HTTP details in your test
    implementation.

    Three new asserts are provided:

    * ``assertResponseSuccessful``: called automatically for every request
        unless an ``expected_status`` is provided
    * ``assertResponseStatus``: called instead of ``assertResponseSuccessful``,
        if an ``expected_status`` is provided
    * ``assertValidResponseHeaders``: validates that the response headers
        appear as expected

    Requests are automatically serialized according to the defined
    ``content_type``. Responses are automatically deserialized as well, and
    available in the ``response.body`` attribute. The original body content is
    available in the ``response.raw`` attribute.

    """

    # default content type to test
    content_type = 'json'

    def get_extensions(self):
        return None

    def setUp(self, app_conf='keystone'):
        super(RestfulTestCase, self).setUp()

        # Will need to reset the plug-ins
        self.addCleanup(setattr, auth_controllers, 'AUTH_METHODS', {})

        self.useFixture(database.Database(self.sql_driver_version_overrides))
        self.load_backends()
        self.load_fixtures(default_fixtures)

        self.public_app = webtest.TestApp(
            self.loadapp(app_conf, name='main'))
        self.addCleanup(delattr, self, 'public_app')
        self.admin_app = webtest.TestApp(
            self.loadapp(app_conf, name='admin'))
        self.addCleanup(delattr, self, 'admin_app')
        # Initialize the policy engine and allow us to write to a temp
        # file in each test to create the policies
        rules.reset()

        # drop the policy rules
        self.addCleanup(rules.reset)

    def request(self, app, path, body=None, headers=None, token=None,
                expected_status=None, **kwargs):
        if headers:
            headers = {str(k): str(v) for k, v in headers.items()}
        else:
            headers = {}

        if token:
            headers['X-Auth-Token'] = str(token)

        # sets environ['REMOTE_ADDR']
        kwargs.setdefault('remote_addr', 'localhost')

        response = app.request(path, headers=headers,
                               status=expected_status, body=body,
                               **kwargs)

        return response

    def assertResponseSuccessful(self, response):
        """Asserts that a status code lies inside the 2xx range.

        :param response: :py:class:`httplib.HTTPResponse` to be
          verified to have a status code between 200 and 299.

        example::

             self.assertResponseSuccessful(response)
        """
        self.assertTrue(
            response.status_code >= 200 and response.status_code <= 299,
            'Status code %d is outside of the expected range (2xx)\n\n%s' %
            (response.status, response.body))

    def assertResponseStatus(self, response, expected_status):
        """Asserts a specific status code on the response.

        :param response: :py:class:`httplib.HTTPResponse`
        :param expected_status: The specific ``status`` result expected

        example::

            self.assertResponseStatus(response, http_client.NO_CONTENT)
        """
        self.assertEqual(
            expected_status, response.status_code,
            'Status code %s is not %s, as expected\n\n%s' %
            (response.status_code, expected_status, response.body))

    def assertValidResponseHeaders(self, response):
        """Ensures that response headers appear as expected."""
        self.assertIn('X-Auth-Token', response.headers.get('Vary'))

    def assertValidErrorResponse(self, response,
                                 expected_status=http_client.BAD_REQUEST):
        """Verify that the error response is valid.

        Subclasses can override this function based on the expected response.

        """
        self.assertEqual(expected_status, response.status_code)
        error = response.result['error']
        self.assertEqual(response.status_code, error['code'])
        self.assertIsNotNone(error.get('title'))

    def _to_content_type(self, body, headers, content_type=None):
        """Attempt to encode JSON and XML automatically."""
        content_type = content_type or self.content_type

        if content_type == 'json':
            headers['Accept'] = 'application/json'
            if body:
                headers['Content-Type'] = 'application/json'
                # NOTE(davechen):dump the body to bytes since WSGI requires
                # the body of the response to be `Bytestrings`.
                # see pep-3333:
                # https://www.python.org/dev/peps/pep-3333/#a-note-on-string-types
                return jsonutils.dump_as_bytes(body)

    def _from_content_type(self, response, content_type=None):
        """Attempt to decode JSON and XML automatically, if detected."""
        content_type = content_type or self.content_type

        if response.body is not None and response.body.strip():
            # if a body is provided, a Content-Type is also expected
            header = response.headers.get('Content-Type')
            self.assertIn(content_type, header)

            if content_type == 'json':
                response.result = jsonutils.loads(response.body)
            else:
                response.result = response.body

    def restful_request(self, method='GET', headers=None, body=None,
                        content_type=None, response_content_type=None,
                        **kwargs):
        """Serializes/deserializes json as request/response body.

        .. WARNING::

            * Existing Accept header will be overwritten.
            * Existing Content-Type header will be overwritten.

        """
        # Initialize headers dictionary
        headers = {} if not headers else headers

        body = self._to_content_type(body, headers, content_type)

        # Perform the HTTP request/response
        response = self.request(method=method, headers=headers, body=body,
                                **kwargs)

        response_content_type = response_content_type or content_type
        self._from_content_type(response, content_type=response_content_type)

        # we can save some code & improve coverage by always doing this
        if (method != 'HEAD' and
                response.status_code >= http_client.BAD_REQUEST):
            self.assertValidErrorResponse(response)

        # Contains the decoded response.body
        return response

    def _request(self, convert=True, **kwargs):
        if convert:
            response = self.restful_request(**kwargs)
        else:
            response = self.request(**kwargs)

        self.assertValidResponseHeaders(response)
        return response

    def public_request(self, **kwargs):
        return self._request(app=self.public_app, **kwargs)

    def admin_request(self, **kwargs):
        return self._request(app=self.admin_app, **kwargs)

    def _get_token(self, body):
        """Convenience method so that we can test authenticated requests."""
        r = self.public_request(method='POST', path='/v2.0/tokens', body=body)
        return self._get_token_id(r)

    def get_unscoped_token(self):
        """Convenience method so that we can test authenticated requests."""
        return self._get_token({
            'auth': {
                'passwordCredentials': {
                    'username': self.user_foo['name'],
                    'password': self.user_foo['password'],
                },
            },
        })

    def get_scoped_token(self, tenant_id=None):
        """Convenience method so that we can test authenticated requests."""
        if not tenant_id:
            tenant_id = self.tenant_bar['id']
        return self._get_token({
            'auth': {
                'passwordCredentials': {
                    'username': self.user_foo['name'],
                    'password': self.user_foo['password'],
                },
                'tenantId': tenant_id,
            },
        })

    def _get_token_id(self, r):
        """Helper method to return a token ID from a response.

        This needs to be overridden by child classes for on their content type.

        """
        raise NotImplementedError()
