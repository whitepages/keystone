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

import base64
import datetime
import hashlib
import os
import uuid

import msgpack
from oslo_utils import timeutils
from six.moves import urllib

from keystone.common import config
from keystone.common import utils
from keystone import exception
from keystone.federation import constants as federation_constants
from keystone.tests import unit
from keystone.tests.unit import ksfixtures
from keystone.tests.unit.ksfixtures import database
from keystone.token import provider
from keystone.token.providers import fernet
from keystone.token.providers.fernet import token_formatters
from keystone.token.providers.fernet import utils as fernet_utils


CONF = config.CONF


class TestFernetTokenProvider(unit.TestCase):
    def setUp(self):
        super(TestFernetTokenProvider, self).setUp()
        self.useFixture(ksfixtures.KeyRepository(self.config_fixture))
        self.provider = fernet.Provider()

    def test_supports_bind_authentication_returns_false(self):
        self.assertFalse(self.provider._supports_bind_authentication)

    def test_needs_persistence_returns_false(self):
        self.assertFalse(self.provider.needs_persistence())

    def test_invalid_v3_token_raises_token_not_found(self):
        # NOTE(lbragstad): Here we use the validate_non_persistent_token()
        # methods because the validate_v3_token() method is strictly for
        # validating UUID formatted tokens. It is written to assume cached
        # tokens from a backend, where validate_non_persistent_token() is not.
        token_id = uuid.uuid4().hex
        e = self.assertRaises(
            exception.TokenNotFound,
            self.provider.validate_non_persistent_token,
            token_id)
        self.assertIn(token_id, u'%s' % e)

    def test_invalid_v2_token_raises_token_not_found(self):
        token_id = uuid.uuid4().hex
        e = self.assertRaises(
            exception.TokenNotFound,
            self.provider.validate_v2_token,
            token_id)
        self.assertIn(token_id, u'%s' % e)


class TestValidate(unit.TestCase):
    def setUp(self):
        super(TestValidate, self).setUp()
        self.useFixture(ksfixtures.KeyRepository(self.config_fixture))
        self.useFixture(database.Database())
        self.load_backends()

    def config_overrides(self):
        super(TestValidate, self).config_overrides()
        self.config_fixture.config(group='token', provider='fernet')

    def test_validate_v3_token_simple(self):
        # Check the fields in the token result when use validate_v3_token
        # with a simple token.

        domain_ref = unit.new_domain_ref()
        domain_ref = self.resource_api.create_domain(domain_ref['id'],
                                                     domain_ref)

        user_ref = unit.new_user_ref(domain_ref['id'])
        user_ref = self.identity_api.create_user(user_ref)

        method_names = ['password']
        token_id, token_data_ = self.token_provider_api.issue_v3_token(
            user_ref['id'], method_names)

        token_data = self.token_provider_api.validate_v3_token(token_id)
        token = token_data['token']
        self.assertIsInstance(token['audit_ids'], list)
        self.assertIsInstance(token['expires_at'], str)
        self.assertIsInstance(token['issued_at'], str)
        self.assertEqual(method_names, token['methods'])
        exp_user_info = {
            'id': user_ref['id'],
            'name': user_ref['name'],
            'domain': {
                'id': domain_ref['id'],
                'name': domain_ref['name'],
            },
        }
        self.assertEqual(exp_user_info, token['user'])

    def test_validate_v3_token_federated_info(self):
        # Check the user fields in the token result when use validate_v3_token
        # when the token has federated info.

        domain_ref = unit.new_domain_ref()
        domain_ref = self.resource_api.create_domain(domain_ref['id'],
                                                     domain_ref)

        user_ref = unit.new_user_ref(domain_ref['id'])
        user_ref = self.identity_api.create_user(user_ref)

        method_names = ['mapped']

        group_ids = [uuid.uuid4().hex, ]
        identity_provider = uuid.uuid4().hex
        protocol = uuid.uuid4().hex
        auth_context = {
            'user_id': user_ref['id'],
            'group_ids': group_ids,
            federation_constants.IDENTITY_PROVIDER: identity_provider,
            federation_constants.PROTOCOL: protocol,
        }
        token_id, token_data_ = self.token_provider_api.issue_v3_token(
            user_ref['id'], method_names, auth_context=auth_context)

        token_data = self.token_provider_api.validate_v3_token(token_id)
        token = token_data['token']
        exp_user_info = {
            'id': user_ref['id'],
            'name': user_ref['id'],
            'domain': {'id': CONF.federation.federated_domain_name,
                       'name': CONF.federation.federated_domain_name, },
            federation_constants.FEDERATION: {
                'groups': [{'id': group_id} for group_id in group_ids],
                'identity_provider': {'id': identity_provider, },
                'protocol': {'id': protocol, },
            },
        }
        self.assertEqual(exp_user_info, token['user'])

    def test_validate_v3_token_trust(self):
        # Check the trust fields in the token result when use validate_v3_token
        # when the token has trust info.

        domain_ref = unit.new_domain_ref()
        domain_ref = self.resource_api.create_domain(domain_ref['id'],
                                                     domain_ref)

        user_ref = unit.new_user_ref(domain_ref['id'])
        user_ref = self.identity_api.create_user(user_ref)

        trustor_user_ref = unit.new_user_ref(domain_ref['id'])
        trustor_user_ref = self.identity_api.create_user(trustor_user_ref)

        project_ref = unit.new_project_ref(domain_id=domain_ref['id'])
        project_ref = self.resource_api.create_project(project_ref['id'],
                                                       project_ref)

        role_ref = unit.new_role_ref()
        role_ref = self.role_api.create_role(role_ref['id'], role_ref)

        self.assignment_api.create_grant(
            role_ref['id'], user_id=user_ref['id'],
            project_id=project_ref['id'])

        self.assignment_api.create_grant(
            role_ref['id'], user_id=trustor_user_ref['id'],
            project_id=project_ref['id'])

        trustor_user_id = trustor_user_ref['id']
        trustee_user_id = user_ref['id']
        trust_ref = unit.new_trust_ref(
            trustor_user_id, trustee_user_id, project_id=project_ref['id'],
            role_ids=[role_ref['id'], ])
        trust_ref = self.trust_api.create_trust(trust_ref['id'], trust_ref,
                                                trust_ref['roles'])

        method_names = ['password']

        token_id, token_data_ = self.token_provider_api.issue_v3_token(
            user_ref['id'], method_names, project_id=project_ref['id'],
            trust=trust_ref)

        token_data = self.token_provider_api.validate_v3_token(token_id)
        token = token_data['token']
        exp_trust_info = {
            'id': trust_ref['id'],
            'impersonation': False,
            'trustee_user': {'id': user_ref['id'], },
            'trustor_user': {'id': trustor_user_ref['id'], },
        }
        self.assertEqual(exp_trust_info, token['OS-TRUST:trust'])

    def test_validate_v3_token_validation_error_exc(self):
        # When the token format isn't recognized, TokenNotFound is raised.

        # A uuid string isn't a valid Fernet token.
        token_id = uuid.uuid4().hex
        self.assertRaises(exception.TokenNotFound,
                          self.token_provider_api.validate_v3_token, token_id)


class TestTokenFormatter(unit.TestCase):
    def setUp(self):
        super(TestTokenFormatter, self).setUp()
        self.useFixture(ksfixtures.KeyRepository(self.config_fixture))

    def test_restore_padding(self):
        # 'a' will result in '==' padding, 'aa' will result in '=' padding, and
        # 'aaa' will result in no padding.
        binary_to_test = [b'a', b'aa', b'aaa']

        for binary in binary_to_test:
            # base64.urlsafe_b64encode takes six.binary_type and returns
            # six.binary_type.
            encoded_string = base64.urlsafe_b64encode(binary)
            encoded_string = encoded_string.decode('utf-8')
            # encoded_string is now six.text_type.
            encoded_str_without_padding = encoded_string.rstrip('=')
            self.assertFalse(encoded_str_without_padding.endswith('='))
            encoded_str_with_padding_restored = (
                token_formatters.TokenFormatter.restore_padding(
                    encoded_str_without_padding)
            )
            self.assertEqual(encoded_string, encoded_str_with_padding_restored)

    def test_legacy_padding_validation(self):
        first_value = uuid.uuid4().hex
        second_value = uuid.uuid4().hex
        payload = (first_value, second_value)
        msgpack_payload = msgpack.packb(payload)
        # msgpack_payload is six.binary_type.

        tf = token_formatters.TokenFormatter()

        # NOTE(lbragstad): This method preserves the way that keystone used to
        # percent encode the tokens, prior to bug #1491926.
        def legacy_pack(payload):
            # payload is six.binary_type.
            encrypted_payload = tf.crypto.encrypt(payload)
            # encrypted_payload is six.binary_type.

            # the encrypted_payload is returned with padding appended
            self.assertTrue(encrypted_payload.endswith(b'='))

            # using urllib.parse.quote will percent encode the padding, like
            # keystone did in Kilo.
            percent_encoded_payload = urllib.parse.quote(encrypted_payload)
            # percent_encoded_payload is six.text_type.

            # ensure that the padding was actually percent encoded
            self.assertTrue(percent_encoded_payload.endswith('%3D'))
            return percent_encoded_payload

        token_with_legacy_padding = legacy_pack(msgpack_payload)
        # token_with_legacy_padding is six.text_type.

        # demonstrate the we can validate a payload that has been percent
        # encoded with the Fernet logic that existed in Kilo
        serialized_payload = tf.unpack(token_with_legacy_padding)
        # serialized_payload is six.binary_type.
        returned_payload = msgpack.unpackb(serialized_payload)
        # returned_payload contains six.binary_type.
        self.assertEqual(first_value, returned_payload[0].decode('utf-8'))
        self.assertEqual(second_value, returned_payload[1].decode('utf-8'))


class TestPayloads(unit.TestCase):
    def assertTimestampsEqual(self, expected, actual):
        # The timestamp that we get back when parsing the payload may not
        # exactly match the timestamp that was put in the payload due to
        # conversion to and from a float.

        exp_time = timeutils.parse_isotime(expected)
        actual_time = timeutils.parse_isotime(actual)

        # the granularity of timestamp string is microseconds and it's only the
        # last digit in the representation that's different, so use a delta
        # just above nanoseconds.
        return self.assertCloseEnoughForGovernmentWork(exp_time, actual_time,
                                                       delta=1e-05)

    def test_uuid_hex_to_byte_conversions(self):
        payload_cls = token_formatters.BasePayload

        expected_hex_uuid = uuid.uuid4().hex
        uuid_obj = uuid.UUID(expected_hex_uuid)
        expected_uuid_in_bytes = uuid_obj.bytes
        actual_uuid_in_bytes = payload_cls.convert_uuid_hex_to_bytes(
            expected_hex_uuid)
        self.assertEqual(expected_uuid_in_bytes, actual_uuid_in_bytes)
        actual_hex_uuid = payload_cls.convert_uuid_bytes_to_hex(
            expected_uuid_in_bytes)
        self.assertEqual(expected_hex_uuid, actual_hex_uuid)

    def test_time_string_to_float_conversions(self):
        payload_cls = token_formatters.BasePayload

        original_time_str = utils.isotime(subsecond=True)
        time_obj = timeutils.parse_isotime(original_time_str)
        expected_time_float = (
            (timeutils.normalize_time(time_obj) -
             datetime.datetime.utcfromtimestamp(0)).total_seconds())

        # NOTE(lbragstad): The token expiration time for Fernet tokens is
        # passed in the payload of the token. This is different from the token
        # creation time, which is handled by Fernet and doesn't support
        # subsecond precision because it is a timestamp integer.
        self.assertIsInstance(expected_time_float, float)

        actual_time_float = payload_cls._convert_time_string_to_float(
            original_time_str)
        self.assertIsInstance(actual_time_float, float)
        self.assertEqual(expected_time_float, actual_time_float)

        # Generate expected_time_str using the same time float. Using
        # original_time_str from utils.isotime will occasionally fail due to
        # floating point rounding differences.
        time_object = datetime.datetime.utcfromtimestamp(actual_time_float)
        expected_time_str = utils.isotime(time_object, subsecond=True)

        actual_time_str = payload_cls._convert_float_to_time_string(
            actual_time_float)
        self.assertEqual(expected_time_str, actual_time_str)

    def _test_payload(self, payload_class, exp_user_id=None, exp_methods=None,
                      exp_project_id=None, exp_domain_id=None,
                      exp_trust_id=None, exp_federated_info=None):
        exp_user_id = exp_user_id or uuid.uuid4().hex
        exp_methods = exp_methods or ['password']
        exp_expires_at = utils.isotime(timeutils.utcnow(), subsecond=True)
        exp_audit_ids = [provider.random_urlsafe_str()]

        payload = payload_class.assemble(
            exp_user_id, exp_methods, exp_project_id, exp_domain_id,
            exp_expires_at, exp_audit_ids, exp_trust_id, exp_federated_info)

        (user_id, methods, project_id, domain_id, expires_at, audit_ids,
         trust_id, federated_info) = payload_class.disassemble(payload)

        self.assertEqual(exp_user_id, user_id)
        self.assertEqual(exp_methods, methods)
        self.assertTimestampsEqual(exp_expires_at, expires_at)
        self.assertEqual(exp_audit_ids, audit_ids)
        self.assertEqual(exp_project_id, project_id)
        self.assertEqual(exp_domain_id, domain_id)
        self.assertEqual(exp_trust_id, trust_id)

        if exp_federated_info:
            self.assertDictEqual(exp_federated_info, federated_info)
        else:
            self.assertIsNone(federated_info)

    def test_unscoped_payload(self):
        self._test_payload(token_formatters.UnscopedPayload)

    def test_project_scoped_payload(self):
        self._test_payload(token_formatters.ProjectScopedPayload,
                           exp_project_id=uuid.uuid4().hex)

    def test_domain_scoped_payload(self):
        self._test_payload(token_formatters.DomainScopedPayload,
                           exp_domain_id=uuid.uuid4().hex)

    def test_domain_scoped_payload_with_default_domain(self):
        self._test_payload(token_formatters.DomainScopedPayload,
                           exp_domain_id=CONF.identity.default_domain_id)

    def test_trust_scoped_payload(self):
        self._test_payload(token_formatters.TrustScopedPayload,
                           exp_project_id=uuid.uuid4().hex,
                           exp_trust_id=uuid.uuid4().hex)

    def test_unscoped_payload_with_non_uuid_user_id(self):
        self._test_payload(token_formatters.UnscopedPayload,
                           exp_user_id='someNonUuidUserId')

    def test_unscoped_payload_with_16_char_non_uuid_user_id(self):
        self._test_payload(token_formatters.UnscopedPayload,
                           exp_user_id='0123456789abcdef')

    def test_project_scoped_payload_with_non_uuid_ids(self):
        self._test_payload(token_formatters.ProjectScopedPayload,
                           exp_user_id='someNonUuidUserId',
                           exp_project_id='someNonUuidProjectId')

    def test_project_scoped_payload_with_16_char_non_uuid_ids(self):
        self._test_payload(token_formatters.ProjectScopedPayload,
                           exp_user_id='0123456789abcdef',
                           exp_project_id='0123456789abcdef')

    def test_domain_scoped_payload_with_non_uuid_user_id(self):
        self._test_payload(token_formatters.DomainScopedPayload,
                           exp_user_id='nonUuidUserId',
                           exp_domain_id=uuid.uuid4().hex)

    def test_domain_scoped_payload_with_16_char_non_uuid_user_id(self):
        self._test_payload(token_formatters.DomainScopedPayload,
                           exp_user_id='0123456789abcdef',
                           exp_domain_id=uuid.uuid4().hex)

    def test_trust_scoped_payload_with_non_uuid_ids(self):
        self._test_payload(token_formatters.TrustScopedPayload,
                           exp_user_id='someNonUuidUserId',
                           exp_project_id='someNonUuidProjectId',
                           exp_trust_id=uuid.uuid4().hex)

    def test_trust_scoped_payload_with_16_char_non_uuid_ids(self):
        self._test_payload(token_formatters.TrustScopedPayload,
                           exp_user_id='0123456789abcdef',
                           exp_project_id='0123456789abcdef',
                           exp_trust_id=uuid.uuid4().hex)

    def _test_federated_payload_with_ids(self, exp_user_id, exp_group_id):
        exp_federated_info = {'group_ids': [{'id': exp_group_id}],
                              'idp_id': uuid.uuid4().hex,
                              'protocol_id': uuid.uuid4().hex}

        self._test_payload(token_formatters.FederatedUnscopedPayload,
                           exp_user_id=exp_user_id,
                           exp_federated_info=exp_federated_info)

    def test_federated_payload_with_non_uuid_ids(self):
        self._test_federated_payload_with_ids('someNonUuidUserId',
                                              'someNonUuidGroupId')

    def test_federated_payload_with_16_char_non_uuid_ids(self):
        self._test_federated_payload_with_ids('0123456789abcdef',
                                              '0123456789abcdef')

    def test_federated_project_scoped_payload(self):
        exp_federated_info = {'group_ids': [{'id': 'someNonUuidGroupId'}],
                              'idp_id': uuid.uuid4().hex,
                              'protocol_id': uuid.uuid4().hex}

        self._test_payload(token_formatters.FederatedProjectScopedPayload,
                           exp_user_id='someNonUuidUserId',
                           exp_methods=['token'],
                           exp_project_id=uuid.uuid4().hex,
                           exp_federated_info=exp_federated_info)

    def test_federated_domain_scoped_payload(self):
        exp_federated_info = {'group_ids': [{'id': 'someNonUuidGroupId'}],
                              'idp_id': uuid.uuid4().hex,
                              'protocol_id': uuid.uuid4().hex}

        self._test_payload(token_formatters.FederatedDomainScopedPayload,
                           exp_user_id='someNonUuidUserId',
                           exp_methods=['token'],
                           exp_domain_id=uuid.uuid4().hex,
                           exp_federated_info=exp_federated_info)


class TestFernetKeyRotation(unit.TestCase):
    def setUp(self):
        super(TestFernetKeyRotation, self).setUp()

        # A collection of all previously-seen signatures of the key
        # repository's contents.
        self.key_repo_signatures = set()

    @property
    def keys(self):
        """Key files converted to numbers."""
        return sorted(
            int(x) for x in os.listdir(CONF.fernet_tokens.key_repository))

    @property
    def key_repository_size(self):
        """The number of keys in the key repository."""
        return len(self.keys)

    @property
    def key_repository_signature(self):
        """Create a "thumbprint" of the current key repository.

        Because key files are renamed, this produces a hash of the contents of
        the key files, ignoring their filenames.

        The resulting signature can be used, for example, to ensure that you
        have a unique set of keys after you perform a key rotation (taking a
        static set of keys, and simply shuffling them, would fail such a test).

        """
        # Load the keys into a list, keys is list of six.text_type.
        keys = fernet_utils.load_keys()

        # Sort the list of keys by the keys themselves (they were previously
        # sorted by filename).
        keys.sort()

        # Create the thumbprint using all keys in the repository.
        signature = hashlib.sha1()
        for key in keys:
            # Need to convert key to six.binary_type for update.
            signature.update(key.encode('utf-8'))
        return signature.hexdigest()

    def assertRepositoryState(self, expected_size):
        """Validate the state of the key repository."""
        self.assertEqual(expected_size, self.key_repository_size)
        self.assertUniqueRepositoryState()

    def assertUniqueRepositoryState(self):
        """Ensures that the current key repo state has not been seen before."""
        # This is assigned to a variable because it takes some work to
        # calculate.
        signature = self.key_repository_signature

        # Ensure the signature is not in the set of previously seen signatures.
        self.assertNotIn(signature, self.key_repo_signatures)

        # Add the signature to the set of repository signatures to validate
        # that we don't see it again later.
        self.key_repo_signatures.add(signature)

    def test_rotation(self):
        # Initializing a key repository results in this many keys. We don't
        # support max_active_keys being set any lower.
        min_active_keys = 2

        # Simulate every rotation strategy up to "rotating once a week while
        # maintaining a year's worth of keys."
        for max_active_keys in range(min_active_keys, 52 + 1):
            self.config_fixture.config(group='fernet_tokens',
                                       max_active_keys=max_active_keys)

            # Ensure that resetting the key repository always results in 2
            # active keys.
            self.useFixture(ksfixtures.KeyRepository(self.config_fixture))

            # Validate the initial repository state.
            self.assertRepositoryState(expected_size=min_active_keys)

            # The repository should be initialized with a staged key (0) and a
            # primary key (1). The next key is just auto-incremented.
            exp_keys = [0, 1]
            next_key_number = exp_keys[-1] + 1  # keep track of next key
            self.assertEqual(exp_keys, self.keys)

            # Rotate the keys just enough times to fully populate the key
            # repository.
            for rotation in range(max_active_keys - min_active_keys):
                fernet_utils.rotate_keys()
                self.assertRepositoryState(expected_size=rotation + 3)

                exp_keys.append(next_key_number)
                next_key_number += 1
                self.assertEqual(exp_keys, self.keys)

            # We should have a fully populated key repository now.
            self.assertEqual(max_active_keys, self.key_repository_size)

            # Rotate an additional number of times to ensure that we maintain
            # the desired number of active keys.
            for rotation in range(10):
                fernet_utils.rotate_keys()
                self.assertRepositoryState(expected_size=max_active_keys)

                exp_keys.pop(1)
                exp_keys.append(next_key_number)
                next_key_number += 1
                self.assertEqual(exp_keys, self.keys)

    def test_non_numeric_files(self):
        self.useFixture(ksfixtures.KeyRepository(self.config_fixture))
        evil_file = os.path.join(CONF.fernet_tokens.key_repository, '99.bak')
        with open(evil_file, 'w'):
            pass
        fernet_utils.rotate_keys()
        self.assertTrue(os.path.isfile(evil_file))
        keys = 0
        for x in os.listdir(CONF.fernet_tokens.key_repository):
            if x == '99.bak':
                continue
            keys += 1
        self.assertEqual(3, keys)


class TestLoadKeys(unit.TestCase):
    def test_non_numeric_files(self):
        self.useFixture(ksfixtures.KeyRepository(self.config_fixture))
        evil_file = os.path.join(CONF.fernet_tokens.key_repository, '~1')
        with open(evil_file, 'w'):
            pass
        keys = fernet_utils.load_keys()
        self.assertEqual(2, len(keys))
        self.assertTrue(len(keys[0]))
