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

from oslo_config import cfg

from keystone.common import dependency
from keystone.common import utils as ks_utils
from keystone import exception
from keystone.federation import constants as federation_constants
from keystone.i18n import _
from keystone.token import provider
from keystone.token.providers import common
from keystone.token.providers.fernet import token_formatters as tf


CONF = cfg.CONF


@dependency.requires('trust_api')
class Provider(common.BaseProvider):
    def __init__(self, *args, **kwargs):
        super(Provider, self).__init__(*args, **kwargs)

        self.token_formatter = tf.TokenFormatter()

    def needs_persistence(self):
        """Should the token be written to a backend."""
        return False

    def issue_v2_token(self, *args, **kwargs):
        token_id, token_data = super(Provider, self).issue_v2_token(
            *args, **kwargs)
        self._build_issued_at_info(token_id, token_data)
        return token_id, token_data

    def issue_v3_token(self, *args, **kwargs):
        token_id, token_data = super(Provider, self).issue_v3_token(
            *args, **kwargs)
        self._build_issued_at_info(token_id, token_data)
        return token_id, token_data

    def _build_issued_at_info(self, token_id, token_data):
        # NOTE(roxanaghe, lbragstad): We must use the creation time that
        # Fernet builds into it's token. The Fernet spec details that the
        # token creation time is built into the token, outside of the payload
        # provided by Keystone. This is the reason why we don't pass the
        # issued_at time in the payload. This also means that we shouldn't
        # return a token reference with a creation time that we created
        # when Fernet uses a different creation time. We should use the
        # creation time provided by Fernet because it's the creation time
        # that we have to rely on when we validate the token.
        fernet_creation_datetime_obj = self.token_formatter.creation_time(
            token_id)
        if token_data.get('access'):
            token_data['access']['token']['issued_at'] = ks_utils.isotime(
                at=fernet_creation_datetime_obj, subsecond=True)
        else:
            token_data['token']['issued_at'] = ks_utils.isotime(
                at=fernet_creation_datetime_obj, subsecond=True)

    def _build_federated_info(self, token_data):
        """Extract everything needed for federated tokens.

        This dictionary is passed to federated token formatters, which unpack
        the values and build federated Fernet tokens.

        """
        token_data = token_data['token']
        try:
            user = token_data['user']
            federation = user[federation_constants.FEDERATION]
            idp_id = federation['identity_provider']['id']
            protocol_id = federation['protocol']['id']
        except KeyError:
            # The token data doesn't have federated info, so we aren't dealing
            # with a federated token and no federated info to build.
            return

        group_ids = federation.get('groups')

        return {'group_ids': group_ids,
                'idp_id': idp_id,
                'protocol_id': protocol_id}

    def _rebuild_federated_info(self, federated_dict, user_id):
        """Format federated information into the token reference.

        The federated_dict is passed back from the federated token formatters.
        The responsibility of this method is to format the information passed
        back from the token formatter into the token reference before
        constructing the token data from the V3TokenDataHelper.

        """
        g_ids = federated_dict['group_ids']
        idp_id = federated_dict['idp_id']
        protocol_id = federated_dict['protocol_id']

        federated_info = {
            'groups': g_ids,
            'identity_provider': {'id': idp_id},
            'protocol': {'id': protocol_id}
        }

        token_dict = {
            'user': {
                federation_constants.FEDERATION: federated_info,
                'id': user_id,
                'name': user_id,
                'domain': {'id': CONF.federation.federated_domain_name,
                           'name': CONF.federation.federated_domain_name, },
            }
        }

        return token_dict

    def _rebuild_federated_token_roles(self, token_dict, federated_dict,
                                       user_id, project_id, domain_id):
        """Populate roles based on (groups, project/domain) pair.

        We must populate roles from (groups, project/domain) as ephemeral users
        don't exist in the backend. Upon success, a ``roles`` key will be added
        to ``token_dict``.

        :param token_dict: dictionary with data used for building token
        :param federated_dict: federated information such as identity provider
            protocol and set of group IDs
        :param user_id: user ID
        :param project_id: project ID the token is being scoped to
        :param domain_id: domain ID the token is being scoped to

        """
        group_ids = [x['id'] for x in federated_dict['group_ids']]
        self.v3_token_data_helper.populate_roles_for_groups(
            token_dict, group_ids, project_id, domain_id, user_id)

    # FIXME(lbragstad): Consolidate this into BaseProvider.validate_v2_token()
    def validate_v2_token(self, token_ref):
        """Validate a V2 formatted token.

        :param token_ref: reference describing the token to validate. Note that
                          token_ref is going to be a token ID.
        :returns: the token data
        :raises keystone.exception.TokenNotFound: if token format is invalid
        :raises keystone.exception.Unauthorized: if v3 token is used

        """
        try:
            (user_id, methods,
             audit_ids, domain_id,
             project_id, trust_id,
             federated_info, created_at,
             expires_at) = self.token_formatter.validate_token(token_ref)
        except exception.ValidationError:
            raise exception.TokenNotFound(token_id=token_ref)

        if trust_id or domain_id or federated_info:
            msg = _('This is not a v2.0 Fernet token. Use v3 for trust, '
                    'domain, or federated tokens.')
            raise exception.Unauthorized(msg)

        v3_token_data = self.v3_token_data_helper.get_token_data(
            user_id,
            methods,
            project_id=project_id,
            expires=expires_at,
            issued_at=created_at,
            token=token_ref,
            include_catalog=False,
            audit_info=audit_ids)
        token_data = self.v2_token_data_helper.v3_to_v2_token(v3_token_data)
        token_data['access']['token']['id'] = token_ref
        return token_data

    def _extract_v2_token_data(self, token_data):
        user_id = token_data['access']['user']['id']
        expires_at = token_data['access']['token']['expires']
        audit_ids = token_data['access']['token'].get('audit_ids')
        methods = ['password']
        if audit_ids:
            parent_audit_id = token_data['access']['token'].get(
                'parent_audit_id')
            audit_ids = provider.audit_info(parent_audit_id)
            if parent_audit_id:
                methods.append('token')
        project_id = token_data['access']['token'].get('tenant', {}).get('id')
        domain_id = None
        trust_id = None
        federated_info = None
        return (user_id, expires_at, audit_ids, methods, domain_id, project_id,
                trust_id, federated_info)

    def _extract_v3_token_data(self, token_data):
        """Extract information from a v3 token reference."""
        user_id = token_data['token']['user']['id']
        expires_at = token_data['token']['expires_at']
        audit_ids = token_data['token']['audit_ids']
        methods = token_data['token'].get('methods')
        domain_id = token_data['token'].get('domain', {}).get('id')
        project_id = token_data['token'].get('project', {}).get('id')
        trust_id = token_data['token'].get('OS-TRUST:trust', {}).get('id')
        federated_info = self._build_federated_info(token_data)

        return (user_id, expires_at, audit_ids, methods, domain_id, project_id,
                trust_id, federated_info)

    def _get_token_id(self, token_data):
        """Generate the token_id based upon the data in token_data.

        :param token_data: token information
        :type token_data: dict
        :rtype: six.text_type

        """
        # NOTE(lbragstad): Only v2.0 token responses include an 'access'
        # attribute.
        if token_data.get('access'):
            (user_id, expires_at, audit_ids, methods, domain_id, project_id,
                trust_id, federated_info) = self._extract_v2_token_data(
                    token_data)
        else:
            (user_id, expires_at, audit_ids, methods, domain_id, project_id,
                trust_id, federated_info) = self._extract_v3_token_data(
                    token_data)

        return self.token_formatter.create_token(user_id,
                                                 expires_at,
                                                 audit_ids,
                                                 methods=methods,
                                                 domain_id=domain_id,
                                                 project_id=project_id,
                                                 trust_id=trust_id,
                                                 federated_info=federated_info)

    @property
    def _supports_bind_authentication(self):
        """Return if the token provider supports bind authentication methods.

        :returns: False

        """
        return False
