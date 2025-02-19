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
"""
To run these tests against a live database:

1. Modify the file ``keystone/tests/unit/config_files/backend_sql.conf`` to use
   the connection for your live database.
2. Set up a blank, live database
3. Run the tests using::

    tox -e py27 -- keystone.tests.unit.test_sql_upgrade

WARNING::

    Your database will be wiped.

    Do not do this against a database with valuable data as
    all data will be lost.
"""

import copy
import json
import uuid

from migrate.versioning import api as versioning_api
import mock
from oslo_config import cfg
from oslo_db import exception as db_exception
from oslo_db.sqlalchemy import migration
from oslo_db.sqlalchemy import session as db_session
from sqlalchemy.engine import reflection
import sqlalchemy.exc
from sqlalchemy import schema

from keystone.common import sql
from keystone.common.sql import migrate_repo
from keystone.common.sql import migration_helpers
from keystone import exception
from keystone.tests import unit
from keystone.tests.unit import default_fixtures
from keystone.tests.unit.ksfixtures import database


CONF = cfg.CONF
DEFAULT_DOMAIN_ID = CONF.identity.default_domain_id

# NOTE(morganfainberg): This should be updated when each DB migration collapse
# is done to mirror the expected structure of the DB in the format of
# { <DB_TABLE_NAME>: [<COLUMN>, <COLUMN>, ...], ... }
INITIAL_TABLE_STRUCTURE = {
    'credential': [
        'id', 'user_id', 'project_id', 'blob', 'type', 'extra',
    ],
    'domain': [
        'id', 'name', 'enabled', 'extra',
    ],
    'endpoint': [
        'id', 'legacy_endpoint_id', 'interface', 'region_id', 'service_id',
        'url', 'enabled', 'extra',
    ],
    'group': [
        'id', 'domain_id', 'name', 'description', 'extra',
    ],
    'policy': [
        'id', 'type', 'blob', 'extra',
    ],
    'project': [
        'id', 'name', 'extra', 'description', 'enabled', 'domain_id',
        'parent_id',
    ],
    'role': [
        'id', 'name', 'extra',
    ],
    'service': [
        'id', 'type', 'extra', 'enabled',
    ],
    'token': [
        'id', 'expires', 'extra', 'valid', 'trust_id', 'user_id',
    ],
    'trust': [
        'id', 'trustor_user_id', 'trustee_user_id', 'project_id',
        'impersonation', 'deleted_at', 'expires_at', 'remaining_uses', 'extra',
    ],
    'trust_role': [
        'trust_id', 'role_id',
    ],
    'user': [
        'id', 'name', 'extra', 'password', 'enabled', 'domain_id',
        'default_project_id',
    ],
    'user_group_membership': [
        'user_id', 'group_id',
    ],
    'region': [
        'id', 'description', 'parent_region_id', 'extra',
    ],
    'assignment': [
        'type', 'actor_id', 'target_id', 'role_id', 'inherited',
    ],
    'id_mapping': [
        'public_id', 'domain_id', 'local_id', 'entity_type',
    ],
    'whitelisted_config': [
        'domain_id', 'group', 'option', 'value',
    ],
    'sensitive_config': [
        'domain_id', 'group', 'option', 'value',
    ],
}


class SqlMigrateBase(unit.SQLDriverOverrides, unit.TestCase):
    def initialize_sql(self):
        self.metadata = sqlalchemy.MetaData()
        self.metadata.bind = self.engine

    def config_files(self):
        config_files = super(SqlMigrateBase, self).config_files()
        config_files.append(unit.dirs.tests_conf('backend_sql.conf'))
        return config_files

    def repo_package(self):
        return sql

    def setUp(self):
        super(SqlMigrateBase, self).setUp()
        database.initialize_sql_session()
        conn_str = CONF.database.connection
        if (conn_str != unit.IN_MEM_DB_CONN_STRING and
                conn_str.startswith('sqlite') and
                conn_str[10:] == unit.DEFAULT_TEST_DB_FILE):
            # Override the default with a DB that is specific to the migration
            # tests only if the DB Connection string is the same as the global
            # default. This is required so that no conflicts occur due to the
            # global default DB already being under migrate control. This is
            # only needed if the DB is not-in-memory
            db_file = unit.dirs.tmp('keystone_migrate_test.db')
            self.config_fixture.config(
                group='database',
                connection='sqlite:///%s' % db_file)

        # create and share a single sqlalchemy engine for testing
        self.engine = sql.get_engine()
        self.Session = db_session.get_maker(self.engine, autocommit=False)
        self.addCleanup(sqlalchemy.orm.session.Session.close_all)

        self.initialize_sql()
        self.repo_path = migration_helpers.find_migrate_repo(
            self.repo_package())
        self.schema = versioning_api.ControlledSchema.create(
            self.engine,
            self.repo_path, self.initial_db_version)

        # auto-detect the highest available schema version in the migrate_repo
        self.max_version = self.schema.repository.version().version

        self.addCleanup(sql.cleanup)

        # drop tables and FKs.
        self.addCleanup(self._cleanupDB)

    def _cleanupDB(self):
        meta = sqlalchemy.MetaData()
        meta.bind = self.engine
        meta.reflect(self.engine)

        with self.engine.begin() as conn:
            inspector = reflection.Inspector.from_engine(self.engine)
            metadata = schema.MetaData()
            tbs = []
            all_fks = []

            for table_name in inspector.get_table_names():
                fks = []
                for fk in inspector.get_foreign_keys(table_name):
                    if not fk['name']:
                        continue
                    fks.append(
                        schema.ForeignKeyConstraint((), (), name=fk['name']))
                table = schema.Table(table_name, metadata, *fks)
                tbs.append(table)
                all_fks.extend(fks)

            for fkc in all_fks:
                if self.engine.name != 'sqlite':
                    conn.execute(schema.DropConstraint(fkc))

            for table in tbs:
                conn.execute(schema.DropTable(table))

    def select_table(self, name):
        table = sqlalchemy.Table(name,
                                 self.metadata,
                                 autoload=True)
        s = sqlalchemy.select([table])
        return s

    def assertTableExists(self, table_name):
        try:
            self.select_table(table_name)
        except sqlalchemy.exc.NoSuchTableError:
            raise AssertionError('Table "%s" does not exist' % table_name)

    def assertTableDoesNotExist(self, table_name):
        """Asserts that a given table exists cannot be selected by name."""
        # Switch to a different metadata otherwise you might still
        # detect renamed or dropped tables
        try:
            temp_metadata = sqlalchemy.MetaData()
            temp_metadata.bind = self.engine
            sqlalchemy.Table(table_name, temp_metadata, autoload=True)
        except sqlalchemy.exc.NoSuchTableError:
            pass
        else:
            raise AssertionError('Table "%s" already exists' % table_name)

    def upgrade(self, *args, **kwargs):
        self._migrate(*args, **kwargs)

    def _migrate(self, version, repository=None, downgrade=False,
                 current_schema=None):
        repository = repository or self.repo_path
        err = ''
        version = versioning_api._migrate_version(self.schema,
                                                  version,
                                                  not downgrade,
                                                  err)
        if not current_schema:
            current_schema = self.schema
        changeset = current_schema.changeset(version)
        for ver, change in changeset:
            self.schema.runchange(ver, change, changeset.step)
        self.assertEqual(self.schema.version, version)

    def assertTableColumns(self, table_name, expected_cols):
        """Asserts that the table contains the expected set of columns."""
        self.initialize_sql()
        table = self.select_table(table_name)
        actual_cols = [col.name for col in table.columns]
        # Check if the columns are equal, but allow for a different order,
        # which might occur after an upgrade followed by a downgrade
        self.assertItemsEqual(expected_cols, actual_cols,
                              '%s table' % table_name)

    @property
    def initial_db_version(self):
        return getattr(self, '_initial_db_version', 0)


class SqlUpgradeTests(SqlMigrateBase):

    _initial_db_version = migrate_repo.DB_INIT_VERSION

    def test_blank_db_to_start(self):
        self.assertTableDoesNotExist('user')

    def test_start_version_db_init_version(self):
        version = migration.db_version(sql.get_engine(), self.repo_path,
                                       migrate_repo.DB_INIT_VERSION)
        self.assertEqual(
            migrate_repo.DB_INIT_VERSION,
            version,
            'DB is not at version %s' % migrate_repo.DB_INIT_VERSION)

    def test_upgrade_add_initial_tables(self):
        self.upgrade(migrate_repo.DB_INIT_VERSION + 1)
        self.check_initial_table_structure()

    def check_initial_table_structure(self):
        for table in INITIAL_TABLE_STRUCTURE:
            self.assertTableColumns(table, INITIAL_TABLE_STRUCTURE[table])

        # Ensure the default domain was properly created.
        default_domain = migration_helpers.get_default_domain()

        meta = sqlalchemy.MetaData()
        meta.bind = self.engine

        domain_table = sqlalchemy.Table('domain', meta, autoload=True)

        session = self.Session()
        q = session.query(domain_table)
        refs = q.all()

        self.assertEqual(1, len(refs))
        for k in default_domain.keys():
            self.assertEqual(default_domain[k], getattr(refs[0], k))

    def insert_dict(self, session, table_name, d, table=None):
        """Naively inserts key-value pairs into a table, given a dictionary."""
        if table is None:
            this_table = sqlalchemy.Table(table_name, self.metadata,
                                          autoload=True)
        else:
            this_table = table
        insert = this_table.insert().values(**d)
        session.execute(insert)
        session.commit()

    def test_kilo_squash(self):
        self.upgrade(67)

        # In 053 the size of ID and parent region ID columns were changed
        table = sqlalchemy.Table('region', self.metadata, autoload=True)
        self.assertEqual(255, table.c.id.type.length)
        self.assertEqual(255, table.c.parent_region_id.type.length)
        table = sqlalchemy.Table('endpoint', self.metadata, autoload=True)
        self.assertEqual(255, table.c.region_id.type.length)

        # In 054 an index was created for the actor_id of the assignment table
        table = sqlalchemy.Table('assignment', self.metadata, autoload=True)
        index_data = [(idx.name, list(idx.columns.keys()))
                      for idx in table.indexes]
        self.assertIn(('ix_actor_id', ['actor_id']), index_data)

        # In 055 indexes were created for user and trust IDs in the token table
        table = sqlalchemy.Table('token', self.metadata, autoload=True)
        index_data = [(idx.name, list(idx.columns.keys()))
                      for idx in table.indexes]
        self.assertIn(('ix_token_user_id', ['user_id']), index_data)
        self.assertIn(('ix_token_trust_id', ['trust_id']), index_data)

        # In 062 the role ID foreign key was removed from the assignment table
        if self.engine.name == "mysql":
            self.assertFalse(self.does_fk_exist('assignment', 'role_id'))

        # In 064 the domain ID FK was removed from the group and user tables
        if self.engine.name != 'sqlite':
            # sqlite does not support FK deletions (or enforcement)
            self.assertFalse(self.does_fk_exist('group', 'domain_id'))
            self.assertFalse(self.does_fk_exist('user', 'domain_id'))

        # In 067 the role ID index was removed from the assignment table
        if self.engine.name == "mysql":
            self.assertFalse(self._does_index_exist('assignment',
                                                    'assignment_role_id_fkey'))

    def test_insert_assignment_inherited_pk(self):
        ASSIGNMENT_TABLE_NAME = 'assignment'
        INHERITED_COLUMN_NAME = 'inherited'
        ROLE_TABLE_NAME = 'role'

        self.upgrade(72)

        # Check that the 'inherited' column is not part of the PK
        self.assertFalse(self.does_pk_exist(ASSIGNMENT_TABLE_NAME,
                                            INHERITED_COLUMN_NAME))

        session = self.Session()

        role = {'id': uuid.uuid4().hex,
                'name': uuid.uuid4().hex}
        self.insert_dict(session, ROLE_TABLE_NAME, role)

        # Create both inherited and noninherited role assignments
        inherited = {'type': 'UserProject',
                     'actor_id': uuid.uuid4().hex,
                     'target_id': uuid.uuid4().hex,
                     'role_id': role['id'],
                     'inherited': True}

        noninherited = inherited.copy()
        noninherited['inherited'] = False

        # Create another inherited role assignment as a spoiler
        spoiler = inherited.copy()
        spoiler['actor_id'] = uuid.uuid4().hex

        self.insert_dict(session, ASSIGNMENT_TABLE_NAME, inherited)
        self.insert_dict(session, ASSIGNMENT_TABLE_NAME, spoiler)

        # Since 'inherited' is not part of the PK, we can't insert noninherited
        self.assertRaises(db_exception.DBDuplicateEntry,
                          self.insert_dict,
                          session,
                          ASSIGNMENT_TABLE_NAME,
                          noninherited)

        session.close()

        self.upgrade(73)

        session = self.Session()
        self.metadata.clear()

        # Check that the 'inherited' column is now part of the PK
        self.assertTrue(self.does_pk_exist(ASSIGNMENT_TABLE_NAME,
                                           INHERITED_COLUMN_NAME))

        # The noninherited role assignment can now be inserted
        self.insert_dict(session, ASSIGNMENT_TABLE_NAME, noninherited)

        assignment_table = sqlalchemy.Table(ASSIGNMENT_TABLE_NAME,
                                            self.metadata,
                                            autoload=True)

        assignments = session.query(assignment_table).all()
        for assignment in (inherited, spoiler, noninherited):
            self.assertIn((assignment['type'], assignment['actor_id'],
                           assignment['target_id'], assignment['role_id'],
                           assignment['inherited']),
                          assignments)

    def does_pk_exist(self, table, pk_column):
        """Checks whether a column is primary key on a table."""
        inspector = reflection.Inspector.from_engine(self.engine)
        pk_columns = inspector.get_pk_constraint(table)['constrained_columns']

        return pk_column in pk_columns

    def does_fk_exist(self, table, fk_column):
        inspector = reflection.Inspector.from_engine(self.engine)
        for fk in inspector.get_foreign_keys(table):
            if fk_column in fk['constrained_columns']:
                return True
        return False

    def does_index_exist(self, table_name, index_name):
        meta = sqlalchemy.MetaData(bind=self.engine)
        table = sqlalchemy.Table(table_name, meta, autoload=True)
        return index_name in [idx.name for idx in table.indexes]

    def test_endpoint_policy_upgrade(self):
        self.assertTableDoesNotExist('policy_association')
        self.upgrade(81)
        self.assertTableColumns('policy_association',
                                ['id', 'policy_id', 'endpoint_id',
                                 'service_id', 'region_id'])

    @mock.patch.object(migration_helpers, 'get_db_version', return_value=1)
    def test_endpoint_policy_already_migrated(self, mock_ep):

        # By setting the return value to 1, the migration has already been
        # run, and there's no need to create the table again

        self.upgrade(81)

        mock_ep.assert_called_once_with(extension='endpoint_policy',
                                        engine=mock.ANY)

        # It won't exist because we are mocking it, but we can verify
        # that 081 did not create the table
        self.assertTableDoesNotExist('policy_association')

    def test_create_federation_tables(self):
        self.identity_provider = 'identity_provider'
        self.federation_protocol = 'federation_protocol'
        self.service_provider = 'service_provider'
        self.mapping = 'mapping'
        self.remote_ids = 'idp_remote_ids'

        self.assertTableDoesNotExist(self.identity_provider)
        self.assertTableDoesNotExist(self.federation_protocol)
        self.assertTableDoesNotExist(self.service_provider)
        self.assertTableDoesNotExist(self.mapping)
        self.assertTableDoesNotExist(self.remote_ids)

        self.upgrade(82)
        self.assertTableColumns(self.identity_provider,
                                ['id', 'description', 'enabled'])

        self.assertTableColumns(self.federation_protocol,
                                ['id', 'idp_id', 'mapping_id'])

        self.assertTableColumns(self.mapping,
                                ['id', 'rules'])

        self.assertTableColumns(self.service_provider,
                                ['id', 'description', 'enabled', 'auth_url',
                                 'relay_state_prefix', 'sp_url'])

        self.assertTableColumns(self.remote_ids, ['idp_id', 'remote_id'])

        federation_protocol = sqlalchemy.Table(self.federation_protocol,
                                               self.metadata,
                                               autoload=True)
        self.assertFalse(federation_protocol.c.mapping_id.nullable)

        sp_table = sqlalchemy.Table(self.service_provider,
                                    self.metadata,
                                    autoload=True)
        self.assertFalse(sp_table.c.auth_url.nullable)
        self.assertFalse(sp_table.c.sp_url.nullable)

    @mock.patch.object(migration_helpers, 'get_db_version', return_value=8)
    def test_federation_already_migrated(self, mock_federation):

        # By setting the return value to 8, the migration has already been
        # run, and there's no need to create the table again.
        self.upgrade(82)

        mock_federation.assert_any_call(extension='federation',
                                        engine=mock.ANY)

        # It won't exist because we are mocking it, but we can verify
        # that 082 did not create the table.
        self.assertTableDoesNotExist('identity_provider')
        self.assertTableDoesNotExist('federation_protocol')
        self.assertTableDoesNotExist('mapping')
        self.assertTableDoesNotExist('service_provider')
        self.assertTableDoesNotExist('idp_remote_ids')

    def test_create_oauth_tables(self):
        consumer = 'consumer'
        request_token = 'request_token'
        access_token = 'access_token'
        self.assertTableDoesNotExist(consumer)
        self.assertTableDoesNotExist(request_token)
        self.assertTableDoesNotExist(access_token)
        self.upgrade(83)
        self.assertTableColumns(consumer,
                                ['id',
                                 'description',
                                 'secret',
                                 'extra'])
        self.assertTableColumns(request_token,
                                ['id',
                                 'request_secret',
                                 'verifier',
                                 'authorizing_user_id',
                                 'requested_project_id',
                                 'role_ids',
                                 'consumer_id',
                                 'expires_at'])
        self.assertTableColumns(access_token,
                                ['id',
                                 'access_secret',
                                 'authorizing_user_id',
                                 'project_id',
                                 'role_ids',
                                 'consumer_id',
                                 'expires_at'])

    @mock.patch.object(migration_helpers, 'get_db_version', return_value=5)
    def test_oauth1_already_migrated(self, mock_oauth1):

        # By setting the return value to 5, the migration has already been
        # run, and there's no need to create the table again.
        self.upgrade(83)

        mock_oauth1.assert_any_call(extension='oauth1', engine=mock.ANY)

        # It won't exist because we are mocking it, but we can verify
        # that 083 did not create the table.
        self.assertTableDoesNotExist('consumer')
        self.assertTableDoesNotExist('request_token')
        self.assertTableDoesNotExist('access_token')

    def test_create_revoke_table(self):
        self.assertTableDoesNotExist('revocation_event')
        self.upgrade(84)
        self.assertTableColumns('revocation_event',
                                ['id', 'domain_id', 'project_id', 'user_id',
                                 'role_id', 'trust_id', 'consumer_id',
                                 'access_token_id', 'issued_before',
                                 'expires_at', 'revoked_at',
                                 'audit_chain_id', 'audit_id'])

    @mock.patch.object(migration_helpers, 'get_db_version', return_value=2)
    def test_revoke_already_migrated(self, mock_revoke):

        # By setting the return value to 2, the migration has already been
        # run, and there's no need to create the table again.
        self.upgrade(84)

        mock_revoke.assert_any_call(extension='revoke', engine=mock.ANY)

        # It won't exist because we are mocking it, but we can verify
        # that 084 did not create the table.
        self.assertTableDoesNotExist('revocation_event')

    def test_project_is_domain_upgrade(self):
        self.upgrade(74)
        self.assertTableColumns('project',
                                ['id', 'name', 'extra', 'description',
                                 'enabled', 'domain_id', 'parent_id',
                                 'is_domain'])

    def test_implied_roles_upgrade(self):
        self.upgrade(87)
        self.assertTableColumns('implied_role',
                                ['prior_role_id', 'implied_role_id'])
        self.assertTrue(self.does_fk_exist('implied_role', 'prior_role_id'))
        self.assertTrue(self.does_fk_exist('implied_role', 'implied_role_id'))

    def test_add_config_registration(self):
        config_registration = 'config_register'
        self.upgrade(74)
        self.assertTableDoesNotExist(config_registration)
        self.upgrade(75)
        self.assertTableColumns(config_registration, ['type', 'domain_id'])

    def test_endpoint_filter_upgrade(self):
        def assert_tables_columns_exist():
            self.assertTableColumns('project_endpoint',
                                    ['endpoint_id', 'project_id'])
            self.assertTableColumns('endpoint_group',
                                    ['id', 'name', 'description', 'filters'])
            self.assertTableColumns('project_endpoint_group',
                                    ['endpoint_group_id', 'project_id'])

        self.assertTableDoesNotExist('project_endpoint')
        self.upgrade(85)
        assert_tables_columns_exist()

    @mock.patch.object(migration_helpers, 'get_db_version', return_value=2)
    def test_endpoint_filter_already_migrated(self, mock_endpoint_filter):

        # By setting the return value to 2, the migration has already been
        # run, and there's no need to create the table again.
        self.upgrade(85)

        mock_endpoint_filter.assert_any_call(extension='endpoint_filter',
                                             engine=mock.ANY)

        # It won't exist because we are mocking it, but we can verify
        # that 085 did not create the table.
        self.assertTableDoesNotExist('project_endpoint')
        self.assertTableDoesNotExist('endpoint_group')
        self.assertTableDoesNotExist('project_endpoint_group')

    def test_add_trust_unique_constraint_upgrade(self):
        self.upgrade(86)
        inspector = reflection.Inspector.from_engine(self.engine)
        constraints = inspector.get_unique_constraints('trust')
        constraint_names = [constraint['name'] for constraint in constraints]
        self.assertIn('duplicate_trust_constraint', constraint_names)

    def populate_user_table(self, with_pass_enab=False,
                            with_pass_enab_domain=False):
        # Populate the appropriate fields in the user
        # table, depending on the parameters:
        #
        # Default: id, name, extra
        # pass_enab: Add password, enabled as well
        # pass_enab_domain: Add password, enabled and domain as well
        #
        this_table = sqlalchemy.Table("user",
                                      self.metadata,
                                      autoload=True)
        for user in default_fixtures.USERS:
            extra = copy.deepcopy(user)
            extra.pop('id')
            extra.pop('name')

            if with_pass_enab:
                password = extra.pop('password', None)
                enabled = extra.pop('enabled', True)
                ins = this_table.insert().values(
                    {'id': user['id'],
                     'name': user['name'],
                     'password': password,
                     'enabled': bool(enabled),
                     'extra': json.dumps(extra)})
            else:
                if with_pass_enab_domain:
                    password = extra.pop('password', None)
                    enabled = extra.pop('enabled', True)
                    extra.pop('domain_id')
                    ins = this_table.insert().values(
                        {'id': user['id'],
                         'name': user['name'],
                         'domain_id': user['domain_id'],
                         'password': password,
                         'enabled': bool(enabled),
                         'extra': json.dumps(extra)})
                else:
                    ins = this_table.insert().values(
                        {'id': user['id'],
                         'name': user['name'],
                         'extra': json.dumps(extra)})
            self.engine.execute(ins)

    def populate_tenant_table(self, with_desc_enab=False,
                              with_desc_enab_domain=False):
        # Populate the appropriate fields in the tenant or
        # project table, depending on the parameters
        #
        # Default: id, name, extra
        # desc_enab: Add description, enabled as well
        # desc_enab_domain: Add description, enabled and domain as well,
        #                   plus use project instead of tenant
        #
        if with_desc_enab_domain:
            # By this time tenants are now projects
            this_table = sqlalchemy.Table("project",
                                          self.metadata,
                                          autoload=True)
        else:
            this_table = sqlalchemy.Table("tenant",
                                          self.metadata,
                                          autoload=True)

        for tenant in default_fixtures.TENANTS:
            extra = copy.deepcopy(tenant)
            extra.pop('id')
            extra.pop('name')

            if with_desc_enab:
                desc = extra.pop('description', None)
                enabled = extra.pop('enabled', True)
                ins = this_table.insert().values(
                    {'id': tenant['id'],
                     'name': tenant['name'],
                     'description': desc,
                     'enabled': bool(enabled),
                     'extra': json.dumps(extra)})
            else:
                if with_desc_enab_domain:
                    desc = extra.pop('description', None)
                    enabled = extra.pop('enabled', True)
                    extra.pop('domain_id')
                    ins = this_table.insert().values(
                        {'id': tenant['id'],
                         'name': tenant['name'],
                         'domain_id': tenant['domain_id'],
                         'description': desc,
                         'enabled': bool(enabled),
                         'extra': json.dumps(extra)})
                else:
                    ins = this_table.insert().values(
                        {'id': tenant['id'],
                         'name': tenant['name'],
                         'extra': json.dumps(extra)})
            self.engine.execute(ins)

    def _mysql_check_all_tables_innodb(self):
        database = self.engine.url.database

        connection = self.engine.connect()
        # sanity check
        total = connection.execute("SELECT count(*) "
                                   "from information_schema.TABLES "
                                   "where TABLE_SCHEMA='%(database)s'" %
                                   dict(database=database))
        self.assertTrue(total.scalar() > 0, "No tables found. Wrong schema?")

        noninnodb = connection.execute("SELECT table_name "
                                       "from information_schema.TABLES "
                                       "where TABLE_SCHEMA='%(database)s' "
                                       "and ENGINE!='InnoDB' "
                                       "and TABLE_NAME!='migrate_version'" %
                                       dict(database=database))
        names = [x[0] for x in noninnodb]
        self.assertEqual([], names,
                         "Non-InnoDB tables exist")

        connection.close()


class VersionTests(SqlMigrateBase):

    _initial_db_version = migrate_repo.DB_INIT_VERSION

    def test_core_initial(self):
        """Get the version before migrated, it's the initial DB version."""
        version = migration_helpers.get_db_version()
        self.assertEqual(migrate_repo.DB_INIT_VERSION, version)

    def test_core_max(self):
        """When get the version after upgrading, it's the new version."""
        self.upgrade(self.max_version)
        version = migration_helpers.get_db_version()
        self.assertEqual(self.max_version, version)

    def test_assert_not_schema_downgrade(self):
        self.upgrade(self.max_version)
        self.assertRaises(
            db_exception.DbMigrationError,
            migration_helpers._sync_common_repo,
            self.max_version - 1)

    def test_extension_not_controlled(self):
        """When get the version before controlling, raises DbMigrationError."""
        self.assertRaises(db_exception.DbMigrationError,
                          migration_helpers.get_db_version,
                          extension='federation')

    def test_unexpected_extension(self):
        """The version for a non-existent extension raises ImportError."""
        extension_name = uuid.uuid4().hex
        self.assertRaises(ImportError,
                          migration_helpers.get_db_version,
                          extension=extension_name)

    def test_unversioned_extension(self):
        """The version for extensions without migrations raise an exception."""
        self.assertRaises(exception.MigrationNotProvided,
                          migration_helpers.get_db_version,
                          extension='admin_crud')
