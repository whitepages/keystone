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
import struct
import uuid

from cryptography import fernet
import msgpack
from oslo_config import cfg
from oslo_log import log
from oslo_utils import timeutils
from six.moves import map
from six.moves import urllib

from keystone.auth import plugins as auth_plugins
from keystone.common import utils as ks_utils
from keystone import exception
from keystone.i18n import _, _LI
from keystone.token import provider
from keystone.token.providers.fernet import utils


CONF = cfg.CONF
LOG = log.getLogger(__name__)

# Fernet byte indexes as as computed by pypi/keyless_fernet and defined in
# https://github.com/fernet/spec
TIMESTAMP_START = 1
TIMESTAMP_END = 9


class TokenFormatter(object):
    """Packs and unpacks payloads into tokens for transport."""

    @property
    def crypto(self):
        """Return a cryptography instance.

        You can extend this class with a custom crypto @property to provide
        your own token encoding / decoding. For example, using a different
        cryptography library (e.g. ``python-keyczar``) or to meet arbitrary
        security requirements.

        This @property just needs to return an object that implements
        ``encrypt(plaintext)`` and ``decrypt(ciphertext)``.

        """
        keys = utils.load_keys()

        if not keys:
            raise exception.KeysNotFound()

        fernet_instances = [fernet.Fernet(key) for key in keys]
        return fernet.MultiFernet(fernet_instances)

    def pack(self, payload):
        """Pack a payload for transport as a token.

        :type payload: six.binary_type
        :rtype: six.text_type

        """
        # base64 padding (if any) is not URL-safe
        return self.crypto.encrypt(payload).rstrip(b'=').decode('utf-8')

    def unpack(self, token):
        """Unpack a token, and validate the payload.

        :type token: six.text_type
        :rtype: six.binary_type

        """
        # TODO(lbragstad): Restore padding on token before decoding it.
        # Initially in Kilo, Fernet tokens were returned to the user with
        # padding appended to the token. Later in Liberty this padding was
        # removed and restored in the Fernet provider. The following if
        # statement ensures that we can validate tokens with and without token
        # padding, in the event of an upgrade and the tokens that are issued
        # throughout the upgrade. Remove this if statement when Mitaka opens
        # for development and exclusively use the restore_padding() class
        # method.
        if token.endswith('%3D'):
            token = urllib.parse.unquote(token)
        else:
            token = TokenFormatter.restore_padding(token)

        try:
            return self.crypto.decrypt(token.encode('utf-8'))
        except fernet.InvalidToken:
            raise exception.ValidationError(
                _('This is not a recognized Fernet token %s') % token)

    @classmethod
    def restore_padding(cls, token):
        """Restore padding based on token size.

        :param token: token to restore padding on
        :type token: six.text_type
        :returns: token with correct padding

        """
        # Re-inflate the padding
        mod_returned = len(token) % 4
        if mod_returned:
            missing_padding = 4 - mod_returned
            token += '=' * missing_padding
        return token

    @classmethod
    def creation_time(cls, fernet_token):
        """Returns the creation time of a valid Fernet token.

        :type fernet_token: six.text_type

        """
        fernet_token = TokenFormatter.restore_padding(fernet_token)
        # fernet_token is six.text_type

        # Fernet tokens are base64 encoded, so we need to unpack them first
        # urlsafe_b64decode() requires six.binary_type
        token_bytes = base64.urlsafe_b64decode(fernet_token.encode('utf-8'))

        # slice into the byte array to get just the timestamp
        timestamp_bytes = token_bytes[TIMESTAMP_START:TIMESTAMP_END]

        # convert those bytes to an integer
        # (it's a 64-bit "unsigned long long int" in C)
        timestamp_int = struct.unpack(">Q", timestamp_bytes)[0]

        # and with an integer, it's trivial to produce a datetime object
        created_at = datetime.datetime.utcfromtimestamp(timestamp_int)

        return created_at

    def create_token(self, user_id, expires_at, audit_ids, methods=None,
                     domain_id=None, project_id=None, trust_id=None,
                     federated_info=None):
        """Given a set of payload attributes, generate a Fernet token."""
        for payload_class in PAYLOAD_CLASSES:
            if payload_class.create_arguments_apply(
                    project_id=project_id, domain_id=domain_id,
                    trust_id=trust_id, federated_info=federated_info):
                break

        version = payload_class.version
        payload = payload_class.assemble(
            user_id, methods, project_id, domain_id, expires_at, audit_ids,
            trust_id, federated_info
        )

        versioned_payload = (version,) + payload
        serialized_payload = msgpack.packb(versioned_payload)
        token = self.pack(serialized_payload)

        # NOTE(lbragstad): We should warn against Fernet tokens that are over
        # 255 characters in length. This is mostly due to persisting the tokens
        # in a backend store of some kind that might have a limit of 255
        # characters. Even though Keystone isn't storing a Fernet token
        # anywhere, we can't say it isn't being stored somewhere else with
        # those kind of backend constraints.
        if len(token) > 255:
            LOG.info(_LI('Fernet token created with length of %d '
                         'characters, which exceeds 255 characters'),
                     len(token))

        return token

    def validate_token(self, token):
        """Validates a Fernet token and returns the payload attributes.

        :type token: six.text_type

        """
        serialized_payload = self.unpack(token)
        versioned_payload = msgpack.unpackb(serialized_payload)
        version, payload = versioned_payload[0], versioned_payload[1:]

        for payload_class in PAYLOAD_CLASSES:
            if version == payload_class.version:
                (user_id, methods, project_id, domain_id, expires_at,
                 audit_ids, trust_id, federated_info) = (
                    payload_class.disassemble(payload))
                break
        else:
            # If the token_format is not recognized, raise ValidationError.
            raise exception.ValidationError(_(
                'This is not a recognized Fernet payload version: %s') %
                version)

        # rather than appearing in the payload, the creation time is encoded
        # into the token format itself
        created_at = TokenFormatter.creation_time(token)
        created_at = ks_utils.isotime(at=created_at, subsecond=True)
        expires_at = timeutils.parse_isotime(expires_at)
        expires_at = ks_utils.isotime(at=expires_at, subsecond=True)

        return (user_id, methods, audit_ids, domain_id, project_id, trust_id,
                federated_info, created_at, expires_at)


class BasePayload(object):
    # each payload variant should have a unique version
    version = None

    @classmethod
    def create_arguments_apply(cls, **kwargs):
        """Check the arguments to see if they apply to this payload variant.

        :returns: True if the arguments indicate that this payload class is
                  needed for the token otherwise returns False.
        :rtype: bool

        """
        raise NotImplementedError()

    @classmethod
    def assemble(cls, user_id, methods, project_id, domain_id, expires_at,
                 audit_ids, trust_id, federated_info):
        """Assemble the payload of a token.

        :param user_id: identifier of the user in the token request
        :param methods: list of authentication methods used
        :param project_id: ID of the project to scope to
        :param domain_id: ID of the domain to scope to
        :param expires_at: datetime of the token's expiration
        :param audit_ids: list of the token's audit IDs
        :param trust_id: ID of the trust in effect
        :param federated_info: dictionary containing group IDs, the identity
                               provider ID, protocol ID, and federated domain
                               ID
        :returns: the payload of a token

        """
        raise NotImplementedError()

    @classmethod
    def disassemble(cls, payload):
        """Disassemble an unscoped payload into the component data.

        The tuple consists of::

            (user_id, methods, project_id, domain_id, expires_at_str,
             audit_ids, trust_id, federated_info)

        * ``methods`` are the auth methods.
        * federated_info is a dict contains the group IDs, the identity
          provider ID, the protocol ID, and the federated domain ID

        Fields will be set to None if they didn't apply to this payload type.

        :param payload: this variant of payload
        :returns: a tuple of the payloads component data

        """
        raise NotImplementedError()

    @classmethod
    def convert_uuid_hex_to_bytes(cls, uuid_string):
        """Compress UUID formatted strings to bytes.

        :param uuid_string: uuid string to compress to bytes
        :returns: a byte representation of the uuid

        """
        uuid_obj = uuid.UUID(uuid_string)
        return uuid_obj.bytes

    @classmethod
    def convert_uuid_bytes_to_hex(cls, uuid_byte_string):
        """Generate uuid.hex format based on byte string.

        :param uuid_byte_string: uuid string to generate from
        :returns: uuid hex formatted string

        """
        uuid_obj = uuid.UUID(bytes=uuid_byte_string)
        return uuid_obj.hex

    @classmethod
    def _convert_time_string_to_float(cls, time_string):
        """Convert a time formatted string to a float.

        :param time_string: time formatted string
        :returns: a timestamp as a float

        """
        time_object = timeutils.parse_isotime(time_string)
        return (timeutils.normalize_time(time_object) -
                datetime.datetime.utcfromtimestamp(0)).total_seconds()

    @classmethod
    def _convert_float_to_time_string(cls, time_float):
        """Convert a floating point timestamp to a string.

        :param time_float: integer representing timestamp
        :returns: a time formatted strings

        """
        time_object = datetime.datetime.utcfromtimestamp(time_float)
        return ks_utils.isotime(time_object, subsecond=True)

    @classmethod
    def attempt_convert_uuid_hex_to_bytes(cls, value):
        """Attempt to convert value to bytes or return value.

        :param value: value to attempt to convert to bytes
        :returns: tuple containing boolean indicating whether user_id was
                  stored as bytes and uuid value as bytes or the original value

        """
        try:
            return (True, cls.convert_uuid_hex_to_bytes(value))
        except ValueError:
            # this might not be a UUID, depending on the situation (i.e.
            # federation)
            return (False, value)


class UnscopedPayload(BasePayload):
    version = 0

    @classmethod
    def create_arguments_apply(cls, **kwargs):
        return True

    @classmethod
    def assemble(cls, user_id, methods, project_id, domain_id, expires_at,
                 audit_ids, trust_id, federated_info):
        b_user_id = cls.attempt_convert_uuid_hex_to_bytes(user_id)
        methods = auth_plugins.convert_method_list_to_integer(methods)
        expires_at_int = cls._convert_time_string_to_float(expires_at)
        b_audit_ids = list(map(provider.random_urlsafe_str_to_bytes,
                           audit_ids))
        return (b_user_id, methods, expires_at_int, b_audit_ids)

    @classmethod
    def disassemble(cls, payload):
        (is_stored_as_bytes, user_id) = payload[0]
        if is_stored_as_bytes:
            user_id = cls.convert_uuid_bytes_to_hex(user_id)
        methods = auth_plugins.convert_integer_to_method_list(payload[1])
        expires_at_str = cls._convert_float_to_time_string(payload[2])
        audit_ids = list(map(provider.base64_encode, payload[3]))
        project_id = None
        domain_id = None
        trust_id = None
        federated_info = None
        return (user_id, methods, project_id, domain_id, expires_at_str,
                audit_ids, trust_id, federated_info)


class DomainScopedPayload(BasePayload):
    version = 1

    @classmethod
    def create_arguments_apply(cls, **kwargs):
        return kwargs['domain_id']

    @classmethod
    def assemble(cls, user_id, methods, project_id, domain_id, expires_at,
                 audit_ids, trust_id, federated_info):
        b_user_id = cls.attempt_convert_uuid_hex_to_bytes(user_id)
        methods = auth_plugins.convert_method_list_to_integer(methods)
        try:
            b_domain_id = cls.convert_uuid_hex_to_bytes(domain_id)
        except ValueError:
            # the default domain ID is configurable, and probably isn't a UUID
            if domain_id == CONF.identity.default_domain_id:
                b_domain_id = domain_id
            else:
                raise
        expires_at_int = cls._convert_time_string_to_float(expires_at)
        b_audit_ids = list(map(provider.random_urlsafe_str_to_bytes,
                           audit_ids))
        return (b_user_id, methods, b_domain_id, expires_at_int, b_audit_ids)

    @classmethod
    def disassemble(cls, payload):
        (is_stored_as_bytes, user_id) = payload[0]
        if is_stored_as_bytes:
            user_id = cls.convert_uuid_bytes_to_hex(user_id)
        methods = auth_plugins.convert_integer_to_method_list(payload[1])
        try:
            domain_id = cls.convert_uuid_bytes_to_hex(payload[2])
        except ValueError:
            # the default domain ID is configurable, and probably isn't a UUID
            if payload[2] == CONF.identity.default_domain_id:
                domain_id = payload[2]
            else:
                raise
        expires_at_str = cls._convert_float_to_time_string(payload[3])
        audit_ids = list(map(provider.base64_encode, payload[4]))
        project_id = None
        trust_id = None
        federated_info = None

        return (user_id, methods, project_id, domain_id, expires_at_str,
                audit_ids, trust_id, federated_info)


class ProjectScopedPayload(BasePayload):
    version = 2

    @classmethod
    def create_arguments_apply(cls, **kwargs):
        return kwargs['project_id']

    @classmethod
    def assemble(cls, user_id, methods, project_id, domain_id, expires_at,
                 audit_ids, trust_id, federated_info):
        b_user_id = cls.attempt_convert_uuid_hex_to_bytes(user_id)
        methods = auth_plugins.convert_method_list_to_integer(methods)
        b_project_id = cls.attempt_convert_uuid_hex_to_bytes(project_id)
        expires_at_int = cls._convert_time_string_to_float(expires_at)
        b_audit_ids = list(map(provider.random_urlsafe_str_to_bytes,
                           audit_ids))
        return (b_user_id, methods, b_project_id, expires_at_int, b_audit_ids)

    @classmethod
    def disassemble(cls, payload):
        (is_stored_as_bytes, user_id) = payload[0]
        if is_stored_as_bytes:
            user_id = cls.convert_uuid_bytes_to_hex(user_id)
        methods = auth_plugins.convert_integer_to_method_list(payload[1])
        (is_stored_as_bytes, project_id) = payload[2]
        if is_stored_as_bytes:
            project_id = cls.convert_uuid_bytes_to_hex(project_id)
        expires_at_str = cls._convert_float_to_time_string(payload[3])
        audit_ids = list(map(provider.base64_encode, payload[4]))
        domain_id = None
        trust_id = None
        federated_info = None

        return (user_id, methods, project_id, domain_id, expires_at_str,
                audit_ids, trust_id, federated_info)


class TrustScopedPayload(BasePayload):
    version = 3

    @classmethod
    def create_arguments_apply(cls, **kwargs):
        return kwargs['trust_id']

    @classmethod
    def assemble(cls, user_id, methods, project_id, domain_id, expires_at,
                 audit_ids, trust_id, federated_info):
        b_user_id = cls.attempt_convert_uuid_hex_to_bytes(user_id)
        methods = auth_plugins.convert_method_list_to_integer(methods)
        b_project_id = cls.attempt_convert_uuid_hex_to_bytes(project_id)
        b_trust_id = cls.convert_uuid_hex_to_bytes(trust_id)
        expires_at_int = cls._convert_time_string_to_float(expires_at)
        b_audit_ids = list(map(provider.random_urlsafe_str_to_bytes,
                           audit_ids))

        return (b_user_id, methods, b_project_id, expires_at_int, b_audit_ids,
                b_trust_id)

    @classmethod
    def disassemble(cls, payload):
        (is_stored_as_bytes, user_id) = payload[0]
        if is_stored_as_bytes:
            user_id = cls.convert_uuid_bytes_to_hex(user_id)
        methods = auth_plugins.convert_integer_to_method_list(payload[1])
        (is_stored_as_bytes, project_id) = payload[2]
        if is_stored_as_bytes:
            project_id = cls.convert_uuid_bytes_to_hex(project_id)
        expires_at_str = cls._convert_float_to_time_string(payload[3])
        audit_ids = list(map(provider.base64_encode, payload[4]))
        trust_id = cls.convert_uuid_bytes_to_hex(payload[5])
        domain_id = None
        federated_info = None

        return (user_id, methods, project_id, domain_id, expires_at_str,
                audit_ids, trust_id, federated_info)


class FederatedUnscopedPayload(BasePayload):
    version = 4

    @classmethod
    def create_arguments_apply(cls, **kwargs):
        return kwargs['federated_info']

    @classmethod
    def pack_group_id(cls, group_dict):
        return cls.attempt_convert_uuid_hex_to_bytes(group_dict['id'])

    @classmethod
    def unpack_group_id(cls, group_id_in_bytes):
        (is_stored_as_bytes, group_id) = group_id_in_bytes
        if is_stored_as_bytes:
            group_id = cls.convert_uuid_bytes_to_hex(group_id)
        return {'id': group_id}

    @classmethod
    def assemble(cls, user_id, methods, project_id, domain_id, expires_at,
                 audit_ids, trust_id, federated_info):
        b_user_id = cls.attempt_convert_uuid_hex_to_bytes(user_id)
        methods = auth_plugins.convert_method_list_to_integer(methods)
        b_group_ids = list(map(cls.pack_group_id,
                               federated_info['group_ids']))
        b_idp_id = cls.attempt_convert_uuid_hex_to_bytes(
            federated_info['idp_id'])
        protocol_id = federated_info['protocol_id']
        expires_at_int = cls._convert_time_string_to_float(expires_at)
        b_audit_ids = list(map(provider.random_urlsafe_str_to_bytes,
                               audit_ids))

        return (b_user_id, methods, b_group_ids, b_idp_id, protocol_id,
                expires_at_int, b_audit_ids)

    @classmethod
    def disassemble(cls, payload):
        (is_stored_as_bytes, user_id) = payload[0]
        if is_stored_as_bytes:
            user_id = cls.convert_uuid_bytes_to_hex(user_id)
        methods = auth_plugins.convert_integer_to_method_list(payload[1])
        group_ids = list(map(cls.unpack_group_id, payload[2]))
        (is_stored_as_bytes, idp_id) = payload[3]
        if is_stored_as_bytes:
            idp_id = cls.convert_uuid_bytes_to_hex(idp_id)
        protocol_id = payload[4]
        expires_at_str = cls._convert_float_to_time_string(payload[5])
        audit_ids = list(map(provider.base64_encode, payload[6]))
        federated_info = dict(group_ids=group_ids, idp_id=idp_id,
                              protocol_id=protocol_id)
        project_id = None
        domain_id = None
        trust_id = None
        return (user_id, methods, project_id, domain_id, expires_at_str,
                audit_ids, trust_id, federated_info)


class FederatedScopedPayload(FederatedUnscopedPayload):
    version = None

    @classmethod
    def assemble(cls, user_id, methods, project_id, domain_id, expires_at,
                 audit_ids, trust_id, federated_info):
        b_user_id = cls.attempt_convert_uuid_hex_to_bytes(user_id)
        methods = auth_plugins.convert_method_list_to_integer(methods)
        b_scope_id = cls.attempt_convert_uuid_hex_to_bytes(
            project_id or domain_id)
        b_group_ids = list(map(cls.pack_group_id,
                               federated_info['group_ids']))
        b_idp_id = cls.attempt_convert_uuid_hex_to_bytes(
            federated_info['idp_id'])
        protocol_id = federated_info['protocol_id']
        expires_at_int = cls._convert_time_string_to_float(expires_at)
        b_audit_ids = list(map(provider.random_urlsafe_str_to_bytes,
                               audit_ids))

        return (b_user_id, methods, b_scope_id, b_group_ids, b_idp_id,
                protocol_id, expires_at_int, b_audit_ids)

    @classmethod
    def disassemble(cls, payload):
        (is_stored_as_bytes, user_id) = payload[0]
        if is_stored_as_bytes:
            user_id = cls.convert_uuid_bytes_to_hex(user_id)
        methods = auth_plugins.convert_integer_to_method_list(payload[1])
        (is_stored_as_bytes, scope_id) = payload[2]
        if is_stored_as_bytes:
            scope_id = cls.convert_uuid_bytes_to_hex(scope_id)
        project_id = (
            scope_id
            if cls.version == FederatedProjectScopedPayload.version else None)
        domain_id = (
            scope_id
            if cls.version == FederatedDomainScopedPayload.version else None)
        group_ids = list(map(cls.unpack_group_id, payload[3]))
        (is_stored_as_bytes, idp_id) = payload[4]
        if is_stored_as_bytes:
            idp_id = cls.convert_uuid_bytes_to_hex(idp_id)
        protocol_id = payload[5]
        expires_at_str = cls._convert_float_to_time_string(payload[6])
        audit_ids = list(map(provider.base64_encode, payload[7]))
        federated_info = dict(idp_id=idp_id, protocol_id=protocol_id,
                              group_ids=group_ids)
        trust_id = None
        return (user_id, methods, project_id, domain_id, expires_at_str,
                audit_ids, trust_id, federated_info)


class FederatedProjectScopedPayload(FederatedScopedPayload):
    version = 5

    @classmethod
    def create_arguments_apply(cls, **kwargs):
        return kwargs['project_id'] and kwargs['federated_info']


class FederatedDomainScopedPayload(FederatedScopedPayload):
    version = 6

    @classmethod
    def create_arguments_apply(cls, **kwargs):
        return kwargs['domain_id'] and kwargs['federated_info']


# For now, the order of the classes in the following list is important. This
# is because the way they test that the payload applies to them in
# the create_arguments_apply method requires that the previous ones rejected
# the payload arguments. For example, UnscopedPayload must be last since it's
# the catch-all after all the other payloads have been checked.
# TODO(blk-u): Clean up the create_arguments_apply methods so that they don't
# depend on the previous classes then these can be in any order.
PAYLOAD_CLASSES = [
    TrustScopedPayload,
    FederatedProjectScopedPayload,
    FederatedDomainScopedPayload,
    FederatedUnscopedPayload,
    ProjectScopedPayload,
    DomainScopedPayload,
    UnscopedPayload,
]
