# Copyright 2014 IBM Corp.
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

import os
import uuid

import fixtures
import mock
from oslo_config import cfg
from six.moves import range

from keystone.cmd import cli
from keystone.common import dependency
from keystone.common.sql import migration_helpers
from keystone.i18n import _
from keystone import resource
from keystone.tests import unit
from keystone.tests.unit.ksfixtures import database


CONF = cfg.CONF


class CliTestCase(unit.SQLDriverOverrides, unit.TestCase):
    def config_files(self):
        config_files = super(CliTestCase, self).config_files()
        config_files.append(unit.dirs.tests_conf('backend_sql.conf'))
        return config_files

    def test_token_flush(self):
        self.useFixture(database.Database())
        self.load_backends()
        cli.TokenFlush.main()


class CliBootStrapTestCase(unit.SQLDriverOverrides, unit.TestCase):

    def setUp(self):
        self.useFixture(database.Database())
        super(CliBootStrapTestCase, self).setUp()

    def config_files(self):
        self.config_fixture.register_cli_opt(cli.command_opt)
        config_files = super(CliBootStrapTestCase, self).config_files()
        config_files.append(unit.dirs.tests_conf('backend_sql.conf'))
        return config_files

    def config(self, config_files):
        CONF(args=['bootstrap', '--bootstrap-password', uuid.uuid4().hex],
             project='keystone',
             default_config_files=config_files)

    def test_bootstrap(self):
        bootstrap = cli.BootStrap()
        self._do_test_bootstrap(bootstrap)

    def _do_test_bootstrap(self, bootstrap):
        bootstrap.do_bootstrap()
        project = bootstrap.resource_manager.get_project_by_name(
            bootstrap.project_name,
            'default')
        user = bootstrap.identity_manager.get_user_by_name(
            bootstrap.username,
            'default')
        role = bootstrap.role_manager.get_role(bootstrap.role_id)
        role_list = (
            bootstrap.assignment_manager.get_roles_for_user_and_project(
                user['id'],
                project['id']))
        self.assertIs(len(role_list), 1)
        self.assertEqual(role_list[0], role['id'])
        # NOTE(morganfainberg): Pass an empty context, it isn't used by
        # `authenticate` method.
        bootstrap.identity_manager.authenticate(
            {},
            user['id'],
            bootstrap.password)

    def test_bootstrap_is_idempotent(self):
        # NOTE(morganfainberg): Ensure we can run bootstrap multiple times
        # without erroring.
        bootstrap = cli.BootStrap()
        self._do_test_bootstrap(bootstrap)
        self._do_test_bootstrap(bootstrap)


class CliBootStrapTestCaseWithEnvironment(CliBootStrapTestCase):

    def config(self, config_files):
        CONF(args=['bootstrap'], project='keystone',
             default_config_files=config_files)

    def setUp(self):
        super(CliBootStrapTestCaseWithEnvironment, self).setUp()
        self.password = uuid.uuid4().hex
        self.username = uuid.uuid4().hex
        self.project_name = uuid.uuid4().hex
        self.role_name = uuid.uuid4().hex
        self.default_domain = migration_helpers.get_default_domain()
        self.useFixture(
            fixtures.EnvironmentVariable('OS_BOOTSTRAP_PASSWORD',
                                         newvalue=self.password))
        self.useFixture(
            fixtures.EnvironmentVariable('OS_BOOTSTRAP_USERNAME',
                                         newvalue=self.username))
        self.useFixture(
            fixtures.EnvironmentVariable('OS_BOOTSTRAP_PROJECT_NAME',
                                         newvalue=self.project_name))
        self.useFixture(
            fixtures.EnvironmentVariable('OS_BOOTSTRAP_ROLE_NAME',
                                         newvalue=self.role_name))

    def test_assignment_created_with_user_exists(self):
        # test assignment can be created if user already exists.
        bootstrap = cli.BootStrap()
        bootstrap.resource_manager.create_domain(self.default_domain['id'],
                                                 self.default_domain)
        user_ref = unit.new_user_ref(self.default_domain['id'],
                                     name=self.username,
                                     password=self.password)
        bootstrap.identity_manager.create_user(user_ref)
        self._do_test_bootstrap(bootstrap)

    def test_assignment_created_with_project_exists(self):
        # test assignment can be created if project already exists.
        bootstrap = cli.BootStrap()
        bootstrap.resource_manager.create_domain(self.default_domain['id'],
                                                 self.default_domain)
        project_ref = unit.new_project_ref(self.default_domain['id'],
                                           name=self.project_name)
        bootstrap.resource_manager.create_project(project_ref['id'],
                                                  project_ref)
        self._do_test_bootstrap(bootstrap)

    def test_assignment_created_with_role_exists(self):
        # test assignment can be created if role already exists.
        bootstrap = cli.BootStrap()
        bootstrap.resource_manager.create_domain(self.default_domain['id'],
                                                 self.default_domain)
        role = unit.new_role_ref(name=self.role_name)
        bootstrap.role_manager.create_role(role['id'], role)
        self._do_test_bootstrap(bootstrap)


class CliDomainConfigAllTestCase(unit.SQLDriverOverrides, unit.TestCase):

    def setUp(self):
        self.useFixture(database.Database())
        super(CliDomainConfigAllTestCase, self).setUp()
        self.load_backends()
        self.config_fixture.config(
            group='identity',
            domain_config_dir=unit.TESTCONF + '/domain_configs_multi_ldap')
        self.domain_count = 3
        self.setup_initial_domains()

    def config_files(self):
        self.config_fixture.register_cli_opt(cli.command_opt)
        self.addCleanup(self.cleanup)
        config_files = super(CliDomainConfigAllTestCase, self).config_files()
        config_files.append(unit.dirs.tests_conf('backend_sql.conf'))
        return config_files

    def cleanup(self):
        CONF.reset()
        CONF.unregister_opt(cli.command_opt)

    def cleanup_domains(self):
        for domain in self.domains:
            if domain == 'domain_default':
                # Not allowed to delete the default domain, but should at least
                # delete any domain-specific config for it.
                self.domain_config_api.delete_config(
                    CONF.identity.default_domain_id)
                continue
            this_domain = self.domains[domain]
            this_domain['enabled'] = False
            self.resource_api.update_domain(this_domain['id'], this_domain)
            self.resource_api.delete_domain(this_domain['id'])
        self.domains = {}

    def config(self, config_files):
        CONF(args=['domain_config_upload', '--all'], project='keystone',
             default_config_files=config_files)

    def setup_initial_domains(self):

        def create_domain(domain):
            return self.resource_api.create_domain(domain['id'], domain)

        self.domains = {}
        self.addCleanup(self.cleanup_domains)
        for x in range(1, self.domain_count):
            domain = 'domain%s' % x
            self.domains[domain] = create_domain(
                {'id': uuid.uuid4().hex, 'name': domain})
        self.domains['domain_default'] = create_domain(
            resource.calc_default_domain())

    def test_config_upload(self):
        # The values below are the same as in the domain_configs_multi_ldap
        # directory of test config_files.
        default_config = {
            'ldap': {'url': 'fake://memory',
                     'user': 'cn=Admin',
                     'password': 'password',
                     'suffix': 'cn=example,cn=com'},
            'identity': {'driver': 'ldap'}
        }
        domain1_config = {
            'ldap': {'url': 'fake://memory1',
                     'user': 'cn=Admin',
                     'password': 'password',
                     'suffix': 'cn=example,cn=com'},
            'identity': {'driver': 'ldap'}
        }
        domain2_config = {
            'ldap': {'url': 'fake://memory',
                     'user': 'cn=Admin',
                     'password': 'password',
                     'suffix': 'cn=myroot,cn=com',
                     'group_tree_dn': 'ou=UserGroups,dc=myroot,dc=org',
                     'user_tree_dn': 'ou=Users,dc=myroot,dc=org'},
            'identity': {'driver': 'ldap'}
        }

        # Clear backend dependencies, since cli loads these manually
        dependency.reset()
        cli.DomainConfigUpload.main()

        res = self.domain_config_api.get_config_with_sensitive_info(
            CONF.identity.default_domain_id)
        self.assertEqual(default_config, res)
        res = self.domain_config_api.get_config_with_sensitive_info(
            self.domains['domain1']['id'])
        self.assertEqual(domain1_config, res)
        res = self.domain_config_api.get_config_with_sensitive_info(
            self.domains['domain2']['id'])
        self.assertEqual(domain2_config, res)


class CliDomainConfigSingleDomainTestCase(CliDomainConfigAllTestCase):

    def config(self, config_files):
        CONF(args=['domain_config_upload', '--domain-name', 'Default'],
             project='keystone', default_config_files=config_files)

    def test_config_upload(self):
        # The values below are the same as in the domain_configs_multi_ldap
        # directory of test config_files.
        default_config = {
            'ldap': {'url': 'fake://memory',
                     'user': 'cn=Admin',
                     'password': 'password',
                     'suffix': 'cn=example,cn=com'},
            'identity': {'driver': 'ldap'}
        }

        # Clear backend dependencies, since cli loads these manually
        dependency.reset()
        cli.DomainConfigUpload.main()

        res = self.domain_config_api.get_config_with_sensitive_info(
            CONF.identity.default_domain_id)
        self.assertEqual(default_config, res)
        res = self.domain_config_api.get_config_with_sensitive_info(
            self.domains['domain1']['id'])
        self.assertEqual({}, res)
        res = self.domain_config_api.get_config_with_sensitive_info(
            self.domains['domain2']['id'])
        self.assertEqual({}, res)

    def test_no_overwrite_config(self):
        # Create a config for the default domain
        default_config = {
            'ldap': {'url': uuid.uuid4().hex},
            'identity': {'driver': 'ldap'}
        }
        self.domain_config_api.create_config(
            CONF.identity.default_domain_id, default_config)

        # Now try and upload the settings in the configuration file for the
        # default domain
        dependency.reset()
        with mock.patch('six.moves.builtins.print') as mock_print:
            self.assertRaises(unit.UnexpectedExit, cli.DomainConfigUpload.main)
            file_name = ('keystone.%s.conf' %
                         resource.calc_default_domain()['name'])
            error_msg = _(
                'Domain: %(domain)s already has a configuration defined - '
                'ignoring file: %(file)s.') % {
                    'domain': resource.calc_default_domain()['name'],
                    'file': os.path.join(CONF.identity.domain_config_dir,
                                         file_name)}
            mock_print.assert_has_calls([mock.call(error_msg)])

        res = self.domain_config_api.get_config(
            CONF.identity.default_domain_id)
        # The initial config should not have been overwritten
        self.assertEqual(default_config, res)


class CliDomainConfigNoOptionsTestCase(CliDomainConfigAllTestCase):

    def config(self, config_files):
        CONF(args=['domain_config_upload'],
             project='keystone', default_config_files=config_files)

    def test_config_upload(self):
        dependency.reset()
        with mock.patch('six.moves.builtins.print') as mock_print:
            self.assertRaises(unit.UnexpectedExit, cli.DomainConfigUpload.main)
            mock_print.assert_has_calls(
                [mock.call(
                    _('At least one option must be provided, use either '
                      '--all or --domain-name'))])


class CliDomainConfigTooManyOptionsTestCase(CliDomainConfigAllTestCase):

    def config(self, config_files):
        CONF(args=['domain_config_upload', '--all', '--domain-name',
                   'Default'],
             project='keystone', default_config_files=config_files)

    def test_config_upload(self):
        dependency.reset()
        with mock.patch('six.moves.builtins.print') as mock_print:
            self.assertRaises(unit.UnexpectedExit, cli.DomainConfigUpload.main)
            mock_print.assert_has_calls(
                [mock.call(_('The --all option cannot be used with '
                             'the --domain-name option'))])


class CliDomainConfigInvalidDomainTestCase(CliDomainConfigAllTestCase):

    def config(self, config_files):
        self.invalid_domain_name = uuid.uuid4().hex
        CONF(args=['domain_config_upload', '--domain-name',
                   self.invalid_domain_name],
             project='keystone', default_config_files=config_files)

    def test_config_upload(self):
        dependency.reset()
        with mock.patch('six.moves.builtins.print') as mock_print:
            self.assertRaises(unit.UnexpectedExit, cli.DomainConfigUpload.main)
            file_name = 'keystone.%s.conf' % self.invalid_domain_name
            error_msg = (_(
                'Invalid domain name: %(domain)s found in config file name: '
                '%(file)s - ignoring this file.') % {
                    'domain': self.invalid_domain_name,
                    'file': os.path.join(CONF.identity.domain_config_dir,
                                         file_name)})
            mock_print.assert_has_calls([mock.call(error_msg)])
