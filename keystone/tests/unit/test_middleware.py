# Copyright 2012 OpenStack Foundation
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

import copy
import hashlib
import uuid

from oslo_config import cfg
from six.moves import http_client
import webtest

from keystone.common import authorization
from keystone.common import tokenless_auth
from keystone import exception
from keystone.federation import constants as federation_constants
from keystone import middleware
from keystone.tests import unit
from keystone.tests.unit import mapping_fixtures
from keystone.tests.unit import test_backend_sql


CONF = cfg.CONF


class MiddlewareRequestTestBase(unit.TestCase):

    MIDDLEWARE_CLASS = None  # override this in subclasses

    def _application(self):
        """A base wsgi application that returns a simple response."""
        def app(environ, start_response):
            # WSGI requires the body of the response to be six.binary_type
            body = uuid.uuid4().hex.encode('utf-8')
            resp_headers = [('Content-Type', 'text/html; charset=utf8'),
                            ('Content-Length', str(len(body)))]
            start_response('200 OK', resp_headers)
            return [body]

        return app

    def _generate_app_response(self, app, headers=None, method='get',
                               path='/', **kwargs):
        """Given a wsgi application wrap it in webtest and call it."""
        return getattr(webtest.TestApp(app), method)(path,
                                                     headers=headers or {},
                                                     **kwargs)

    def _middleware_failure(self, exc, *args, **kwargs):
        """Assert that an exception is being thrown from process_request."""
        # NOTE(jamielennox): This is a little ugly. We need to call the webtest
        # framework so that the correct RequestClass object is created for when
        # we call process_request. However because we go via webtest we only
        # see the response object and not the actual exception that is thrown
        # by process_request. To get around this we subclass process_request
        # with something that checks for the right type of exception being
        # thrown so we can test the middle of the request process.
        # TODO(jamielennox): Change these tests to test the value of the
        # response rather than the error that is raised.

        class _Failing(self.MIDDLEWARE_CLASS):

            _called = False

            def process_request(i_self, *i_args, **i_kwargs):
                # i_ to distinguish it from and not clobber the outer vars
                e = self.assertRaises(exc,
                                      super(_Failing, i_self).process_request,
                                      *i_args, **i_kwargs)
                i_self._called = True
                raise e

        # by default the returned status when an uncaught exception is raised
        # for validation or caught errors this will likely be 400
        kwargs.setdefault('status', http_client.INTERNAL_SERVER_ERROR)  # 500

        app = _Failing(self._application())
        resp = self._generate_app_response(app, *args, **kwargs)
        self.assertTrue(app._called)
        return resp

    def _do_middleware_response(self, *args, **kwargs):
        """Wrap a middleware around a sample application and call it."""
        app = self.MIDDLEWARE_CLASS(self._application())
        return self._generate_app_response(app, *args, **kwargs)

    def _do_middleware_request(self, *args, **kwargs):
        """The request object from a successful middleware call."""
        return self._do_middleware_response(*args, **kwargs).request


class TokenAuthMiddlewareTest(MiddlewareRequestTestBase):

    MIDDLEWARE_CLASS = middleware.TokenAuthMiddleware

    def test_request(self):
        headers = {middleware.AUTH_TOKEN_HEADER: 'MAGIC'}
        req = self._do_middleware_request(headers=headers)
        context = req.environ[middleware.CONTEXT_ENV]
        self.assertEqual('MAGIC', context['token_id'])


class AdminTokenAuthMiddlewareTest(MiddlewareRequestTestBase):

    MIDDLEWARE_CLASS = middleware.AdminTokenAuthMiddleware

    def test_request_admin(self):
        headers = {middleware.AUTH_TOKEN_HEADER: CONF.admin_token}
        req = self._do_middleware_request(headers=headers)
        self.assertTrue(req.environ[middleware.CONTEXT_ENV]['is_admin'])

    def test_request_non_admin(self):
        headers = {middleware.AUTH_TOKEN_HEADER: 'NOT-ADMIN'}
        req = self._do_middleware_request(headers=headers)
        self.assertFalse(req.environ[middleware.CONTEXT_ENV]['is_admin'])


class PostParamsMiddlewareTest(MiddlewareRequestTestBase):

    MIDDLEWARE_CLASS = middleware.PostParamsMiddleware

    def test_request_with_params(self):
        req = self._do_middleware_request(params='arg1=one', method='post')
        self.assertEqual({"arg1": "one"}, req.environ[middleware.PARAMS_ENV])


class JsonBodyMiddlewareTest(MiddlewareRequestTestBase):

    MIDDLEWARE_CLASS = middleware.JsonBodyMiddleware

    def test_request_with_params(self):
        headers = {'Content-Type': 'application/json'}
        params = '{"arg1": "one", "arg2": ["a"]}'
        req = self._do_middleware_request(params=params,
                                          headers=headers,
                                          method='post')
        self.assertEqual({"arg1": "one", "arg2": ["a"]},
                         req.environ[middleware.PARAMS_ENV])

    def test_malformed_json(self):
        headers = {'Content-Type': 'application/json'}
        self._do_middleware_response(params='{"arg1": "on',
                                     headers=headers,
                                     method='post',
                                     status=http_client.BAD_REQUEST)

    def test_not_dict_body(self):
        headers = {'Content-Type': 'application/json'}
        resp = self._do_middleware_response(params='42',
                                            headers=headers,
                                            method='post',
                                            status=http_client.BAD_REQUEST)

        self.assertIn('valid JSON object', resp.json['error']['message'])

    def test_no_content_type(self):
        headers = {'Content-Type': ''}
        params = '{"arg1": "one", "arg2": ["a"]}'
        req = self._do_middleware_request(params=params,
                                          headers=headers,
                                          method='post')
        self.assertEqual({"arg1": "one", "arg2": ["a"]},
                         req.environ[middleware.PARAMS_ENV])

    def test_unrecognized_content_type(self):
        headers = {'Content-Type': 'text/plain'}
        self._do_middleware_response(params='{"arg1": "one", "arg2": ["a"]}',
                                     headers=headers,
                                     method='post',
                                     status=http_client.BAD_REQUEST)

    def test_unrecognized_content_type_without_body(self):
        headers = {'Content-Type': 'text/plain'}
        req = self._do_middleware_request(headers=headers)
        self.assertEqual({}, req.environ.get(middleware.PARAMS_ENV, {}))


class AuthContextMiddlewareTest(test_backend_sql.SqlTests,
                                MiddlewareRequestTestBase):

    MIDDLEWARE_CLASS = middleware.AuthContextMiddleware

    def setUp(self):
        super(AuthContextMiddlewareTest, self).setUp()
        self.client_issuer = uuid.uuid4().hex
        self.untrusted_client_issuer = uuid.uuid4().hex
        self.trusted_issuer = self.client_issuer
        self.config_fixture.config(group='tokenless_auth',
                                   trusted_issuer=[self.trusted_issuer])

        # client_issuer is encoded because you can't hash
        # unicode objects with hashlib.
        # This idp_id is calculated based on sha256(self.client_issuer)
        hashed_idp = hashlib.sha256(self.client_issuer.encode('utf-8'))
        self.idp_id = hashed_idp.hexdigest()
        self._load_sample_data()

    def _load_sample_data(self):
        self.protocol_id = 'x509'

        # 1) Create a domain for the user.
        self.domain = unit.new_domain_ref()
        self.domain_id = self.domain['id']
        self.domain_name = self.domain['name']
        self.resource_api.create_domain(self.domain_id, self.domain)

        # 2) Create a project for the user.
        self.project = unit.new_project_ref(domain_id=self.domain_id)
        self.project_id = self.project['id']
        self.project_name = self.project['name']

        self.resource_api.create_project(self.project_id, self.project)

        # 3) Create a user in new domain.
        self.user = unit.new_user_ref(domain_id=self.domain_id,
                                      project_id=self.project_id)

        self.user = self.identity_api.create_user(self.user)

        # Add IDP
        self.idp = self._idp_ref(id=self.idp_id)
        self.federation_api.create_idp(self.idp['id'],
                                       self.idp)

        # Add a role
        self.role = unit.new_role_ref()
        self.role_id = self.role['id']
        self.role_name = self.role['name']
        self.role_api.create_role(self.role_id, self.role)

        # Add a group
        self.group = unit.new_group_ref(domain_id=self.domain_id)
        self.group = self.identity_api.create_group(self.group)

        # Assign a role to the user on a project
        self.assignment_api.add_role_to_user_and_project(
            user_id=self.user['id'],
            tenant_id=self.project_id,
            role_id=self.role_id)

        # Assign a role to the group on a project
        self.assignment_api.create_grant(
            role_id=self.role_id,
            group_id=self.group['id'],
            project_id=self.project_id)

    def _load_mapping_rules(self, rules):
        # Add a mapping
        self.mapping = self._mapping_ref(rules=rules)
        self.federation_api.create_mapping(self.mapping['id'],
                                           self.mapping)
        # Add protocols
        self.proto_x509 = self._proto_ref(mapping_id=self.mapping['id'])
        self.proto_x509['id'] = self.protocol_id
        self.federation_api.create_protocol(self.idp['id'],
                                            self.proto_x509['id'],
                                            self.proto_x509)

    def _idp_ref(self, id=None):
        idp = {
            'id': id or uuid.uuid4().hex,
            'enabled': True,
            'description': uuid.uuid4().hex
        }
        return idp

    def _proto_ref(self, mapping_id=None):
        proto = {
            'id': uuid.uuid4().hex,
            'mapping_id': mapping_id or uuid.uuid4().hex
        }
        return proto

    def _mapping_ref(self, rules=None):
        if rules is None:
            mapped_rules = {}
        else:
            mapped_rules = rules.get('rules', {})
        return {
            'id': uuid.uuid4().hex,
            'rules': mapped_rules
        }

    def _assert_tokenless_auth_context(self, context, ephemeral_user=False):
        self.assertIsNotNone(context)
        self.assertEqual(self.project_id, context['project_id'])
        self.assertIn(self.role_name, context['roles'])
        if ephemeral_user:
            self.assertEqual(self.group['id'], context['group_ids'][0])
            self.assertEqual('ephemeral',
                             context[federation_constants.PROTOCOL])
            self.assertEqual(self.idp_id,
                             context[federation_constants.IDENTITY_PROVIDER])
        else:
            self.assertEqual(self.user['id'], context['user_id'])

    def _create_context(self, request, mapping_ref=None,
                        exception_expected=False):
        """Builds the auth context from the given arguments.

        auth context will be returned from the AuthContextMiddleware based on
        what is being passed in the given request and what mapping is being
        setup in the backend DB.

        :param request: HTTP request
        :param mapping_ref: A mapping in JSON structure will be setup in the
            backend DB for mapping a user or a group.
        :param exception_expected: Sets to True when an exception is expected
            to raised based on the given arguments.
        :returns: context an auth context contains user and role information
        :rtype: dict
        """
        if mapping_ref:
            self._load_mapping_rules(mapping_ref)

        if not exception_expected:
            (middleware.AuthContextMiddleware('Tokenless_auth_test').
                process_request(request))
            context = request.environ.get(authorization.AUTH_CONTEXT_ENV)
        else:
            context = middleware.AuthContextMiddleware('Tokenless_auth_test')
        return context

    def test_context_already_exists(self):
        stub_value = uuid.uuid4().hex
        env = {authorization.AUTH_CONTEXT_ENV: stub_value}
        req = self._do_middleware_request(extra_environ=env)
        self.assertEqual(stub_value,
                         req.environ.get(authorization.AUTH_CONTEXT_ENV))

    def test_not_applicable_to_token_request(self):
        req = self._do_middleware_request(path='/auth/tokens', method='post')
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self.assertIsNone(context)

    def test_no_tokenless_attributes_request(self):
        req = self._do_middleware_request()
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self.assertIsNone(context)

    def test_no_issuer_attribute_request(self):
        env = {}
        env['HTTP_X_PROJECT_ID'] = uuid.uuid4().hex
        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self.assertIsNone(context)

    def test_has_only_issuer_and_project_name_request(self):
        env = {}
        # SSL_CLIENT_I_DN is the attribute name that wsgi env
        # references to issuer of the client certificate.
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = uuid.uuid4().hex
        self._middleware_failure(exception.ValidationError,
                                 extra_environ=env,
                                 status=400)

    def test_has_only_issuer_and_project_domain_name_request(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = uuid.uuid4().hex
        self._middleware_failure(exception.ValidationError,
                                 extra_environ=env,
                                 status=400)

    def test_has_only_issuer_and_project_domain_id_request(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_DOMAIN_ID'] = uuid.uuid4().hex
        self._middleware_failure(exception.ValidationError,
                                 extra_environ=env,
                                 status=400)

    def test_missing_both_domain_and_project_request(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        self._middleware_failure(exception.ValidationError,
                                 extra_environ=env,
                                 status=400)

    def test_empty_trusted_issuer_list(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_ID'] = uuid.uuid4().hex

        self.config_fixture.config(group='tokenless_auth',
                                   trusted_issuer=[])

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self.assertIsNone(context)

    def test_client_issuer_not_trusted(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.untrusted_client_issuer
        env['HTTP_X_PROJECT_ID'] = uuid.uuid4().hex
        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self.assertIsNone(context)

    def test_proj_scope_with_proj_id_and_proj_dom_id_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_ID'] = self.project_id
        env['HTTP_X_PROJECT_DOMAIN_ID'] = self.domain_id
        # SSL_CLIENT_USER_NAME and SSL_CLIENT_DOMAIN_NAME are the types
        # defined in the mapping that will map to the user name and
        # domain name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        env['SSL_CLIENT_DOMAIN_NAME'] = self.domain_name

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINNAME)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context)

    def test_proj_scope_with_proj_id_only_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_ID'] = self.project_id
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        env['SSL_CLIENT_DOMAIN_NAME'] = self.domain_name

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINNAME)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context)

    def test_proj_scope_with_proj_name_and_proj_dom_id_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_ID'] = self.domain_id
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        env['SSL_CLIENT_DOMAIN_NAME'] = self.domain_name

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINNAME)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context)

    def test_proj_scope_with_proj_name_and_proj_dom_name_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        env['SSL_CLIENT_DOMAIN_NAME'] = self.domain_name

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINNAME)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context)

    def test_proj_scope_with_proj_name_only_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_id
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        env['SSL_CLIENT_DOMAIN_NAME'] = self.domain_name

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINNAME)

        self._middleware_failure(exception.ValidationError,
                                 extra_environ=env,
                                 status=400)

    def test_mapping_with_userid_and_domainid_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_ID'] = self.user['id']
        env['SSL_CLIENT_DOMAIN_ID'] = self.domain_id

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERID_AND_DOMAINID)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context)

    def test_mapping_with_userid_and_domainname_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_ID'] = self.user['id']
        env['SSL_CLIENT_DOMAIN_NAME'] = self.domain_name

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERID_AND_DOMAINNAME)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context)

    def test_mapping_with_username_and_domainid_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        env['SSL_CLIENT_DOMAIN_ID'] = self.domain_id

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINID)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context)

    def test_only_domain_name_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_ID'] = self.project_id
        env['HTTP_X_PROJECT_DOMAIN_ID'] = self.domain_id
        env['SSL_CLIENT_DOMAIN_NAME'] = self.domain_name

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_DOMAINNAME_ONLY)

        self._middleware_failure(exception.ValidationError,
                                 extra_environ=env,
                                 status=400)

    def test_only_domain_id_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_ID'] = self.project_id
        env['HTTP_X_PROJECT_DOMAIN_ID'] = self.domain_id
        env['SSL_CLIENT_DOMAIN_ID'] = self.domain_id

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_DOMAINID_ONLY)

        self._middleware_failure(exception.ValidationError,
                                 extra_environ=env,
                                 status=400)

    def test_missing_domain_data_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_ID'] = self.project_id
        env['HTTP_X_PROJECT_DOMAIN_ID'] = self.domain_id
        env['SSL_CLIENT_USER_NAME'] = self.user['name']

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_ONLY)

        self._middleware_failure(exception.ValidationError,
                                 extra_environ=env,
                                 status=400)

    def test_userid_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_ID'] = self.project_id
        env['HTTP_X_PROJECT_DOMAIN_ID'] = self.domain_id
        env['SSL_CLIENT_USER_ID'] = self.user['id']

        self._load_mapping_rules(mapping_fixtures.MAPPING_WITH_USERID_ONLY)
        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context)

    def test_domain_disable_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        env['SSL_CLIENT_DOMAIN_ID'] = self.domain_id

        self.domain['enabled'] = False
        self.domain = self.resource_api.update_domain(
            self.domain['id'], self.domain)

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINID)
        self._middleware_failure(exception.Unauthorized,
                                 extra_environ=env,
                                 status=401)

    def test_user_disable_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        env['SSL_CLIENT_DOMAIN_ID'] = self.domain_id

        self.user['enabled'] = False
        self.user = self.identity_api.update_user(self.user['id'], self.user)

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINID)

        self._middleware_failure(AssertionError,
                                 extra_environ=env)

    def test_invalid_user_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_ID'] = self.project_id
        env['HTTP_X_PROJECT_DOMAIN_ID'] = self.domain_id
        env['SSL_CLIENT_USER_NAME'] = uuid.uuid4().hex
        env['SSL_CLIENT_DOMAIN_NAME'] = self.domain_name

        self._load_mapping_rules(
            mapping_fixtures.MAPPING_WITH_USERNAME_AND_DOMAINNAME)

        self._middleware_failure(exception.UserNotFound,
                                 extra_environ=env,
                                 status=404)

    def test_ephemeral_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        self.config_fixture.config(group='tokenless_auth',
                                   protocol='ephemeral')
        self.protocol_id = 'ephemeral'
        mapping = copy.deepcopy(mapping_fixtures.MAPPING_FOR_EPHEMERAL_USER)
        mapping['rules'][0]['local'][0]['group']['id'] = self.group['id']
        self._load_mapping_rules(mapping)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context, ephemeral_user=True)

    def test_ephemeral_with_default_user_type_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        self.config_fixture.config(group='tokenless_auth',
                                   protocol='ephemeral')
        self.protocol_id = 'ephemeral'
        # this mapping does not have the user type defined
        # and it should defaults to 'ephemeral' which is
        # the expected type for the test case.
        mapping = copy.deepcopy(
            mapping_fixtures.MAPPING_FOR_DEFAULT_EPHEMERAL_USER)
        mapping['rules'][0]['local'][0]['group']['id'] = self.group['id']
        self._load_mapping_rules(mapping)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context, ephemeral_user=True)

    def test_ephemeral_any_user_success(self):
        """Ephemeral user does not need a specified user
        Keystone is not looking to match the user, but a corresponding group.
        """
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = uuid.uuid4().hex
        self.config_fixture.config(group='tokenless_auth',
                                   protocol='ephemeral')
        self.protocol_id = 'ephemeral'
        mapping = copy.deepcopy(mapping_fixtures.MAPPING_FOR_EPHEMERAL_USER)
        mapping['rules'][0]['local'][0]['group']['id'] = self.group['id']
        self._load_mapping_rules(mapping)

        req = self._do_middleware_request(extra_environ=env)
        context = req.environ.get(authorization.AUTH_CONTEXT_ENV)
        self._assert_tokenless_auth_context(context, ephemeral_user=True)

    def test_ephemeral_invalid_scope_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = uuid.uuid4().hex
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = uuid.uuid4().hex
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        self.config_fixture.config(group='tokenless_auth',
                                   protocol='ephemeral')
        self.protocol_id = 'ephemeral'
        mapping = copy.deepcopy(mapping_fixtures.MAPPING_FOR_EPHEMERAL_USER)
        mapping['rules'][0]['local'][0]['group']['id'] = self.group['id']
        self._load_mapping_rules(mapping)

        self._middleware_failure(exception.Unauthorized,
                                 extra_environ=env,
                                 status=401)

    def test_ephemeral_no_group_found_fail(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        self.config_fixture.config(group='tokenless_auth',
                                   protocol='ephemeral')
        self.protocol_id = 'ephemeral'
        mapping = copy.deepcopy(mapping_fixtures.MAPPING_FOR_EPHEMERAL_USER)
        mapping['rules'][0]['local'][0]['group']['id'] = uuid.uuid4().hex
        self._load_mapping_rules(mapping)

        self._middleware_failure(exception.MappedGroupNotFound,
                                 extra_environ=env)

    def test_ephemeral_incorrect_mapping_fail(self):
        """Ephemeral user picks up the non-ephemeral user mapping.
        Looking up the mapping with protocol Id 'x509' will load up
        the non-ephemeral user mapping, results unauthenticated.
        """
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        env['HTTP_X_PROJECT_NAME'] = self.project_name
        env['HTTP_X_PROJECT_DOMAIN_NAME'] = self.domain_name
        env['SSL_CLIENT_USER_NAME'] = self.user['name']
        # This will pick up the incorrect mapping
        self.config_fixture.config(group='tokenless_auth',
                                   protocol='x509')
        self.protocol_id = 'x509'
        mapping = copy.deepcopy(mapping_fixtures.MAPPING_FOR_EPHEMERAL_USER)
        mapping['rules'][0]['local'][0]['group']['id'] = uuid.uuid4().hex
        self._load_mapping_rules(mapping)

        self._middleware_failure(exception.MappedGroupNotFound,
                                 extra_environ=env)

    def test_create_idp_id_success(self):
        env = {}
        env['SSL_CLIENT_I_DN'] = self.client_issuer
        auth = tokenless_auth.TokenlessAuthHelper(env)
        idp_id = auth._build_idp_id()
        self.assertEqual(self.idp_id, idp_id)

    def test_create_idp_id_attri_not_found_fail(self):
        env = {}
        env[uuid.uuid4().hex] = self.client_issuer
        auth = tokenless_auth.TokenlessAuthHelper(env)
        expected_msg = ('Could not determine Identity Provider ID. The '
                        'configuration option %s was not found in the '
                        'request environment.' %
                        CONF.tokenless_auth.issuer_attribute)
        # Check the content of the exception message as well
        self.assertRaisesRegexp(exception.TokenlessAuthConfigError,
                                expected_msg,
                                auth._build_idp_id)
