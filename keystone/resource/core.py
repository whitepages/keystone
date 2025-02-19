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

"""Main entry point into the Resource service."""

import abc

from oslo_config import cfg
from oslo_log import log
from oslo_log import versionutils
import six

from keystone import assignment
from keystone.common import cache
from keystone.common import clean
from keystone.common import dependency
from keystone.common import driver_hints
from keystone.common import manager
from keystone.common import utils
from keystone import exception
from keystone.i18n import _, _LE, _LW
from keystone import notifications


CONF = cfg.CONF
LOG = log.getLogger(__name__)
MEMOIZE = cache.get_memoization_decorator(group='resource')


def calc_default_domain():
    return {'description':
            (u'Owns users and tenants (i.e. projects)'
                ' available on Identity API v2.'),
            'enabled': True,
            'id': CONF.identity.default_domain_id,
            'name': u'Default'}


@dependency.provider('resource_api')
@dependency.requires('assignment_api', 'credential_api', 'domain_config_api',
                     'identity_api', 'revoke_api')
class Manager(manager.Manager):
    """Default pivot point for the Resource backend.

    See :mod:`keystone.common.manager.Manager` for more details on how this
    dynamically calls the backend.

    """

    driver_namespace = 'keystone.resource'

    _DOMAIN = 'domain'
    _PROJECT = 'project'

    def __init__(self):
        # If there is a specific driver specified for resource, then use it.
        # Otherwise retrieve the driver type from the assignment driver.
        resource_driver = CONF.resource.driver

        if resource_driver is None:
            assignment_manager = dependency.get_provider('assignment_api')
            resource_driver = assignment_manager.default_resource_driver()

        super(Manager, self).__init__(resource_driver)

        # Make sure it is a driver version we support, and if it is a legacy
        # driver, then wrap it.
        if isinstance(self.driver, ResourceDriverV8):
            self.driver = V9ResourceWrapperForV8Driver(self.driver)
        elif not isinstance(self.driver, ResourceDriverV9):
            raise exception.UnsupportedDriverVersion(driver=resource_driver)

    def _get_hierarchy_depth(self, parents_list):
        return len(parents_list) + 1

    def _assert_max_hierarchy_depth(self, project_id, parents_list=None):
        if parents_list is None:
            parents_list = self.list_project_parents(project_id)
        max_depth = CONF.max_project_tree_depth
        if self._get_hierarchy_depth(parents_list) > max_depth:
            raise exception.ForbiddenAction(
                action=_('max hierarchy depth reached for '
                         '%s branch.') % project_id)

    def _assert_is_domain_project_constraints(self, project_ref, parent_ref):
        """Enforces specific constraints of projects that act as domains

        Called when is_domain is true, this method ensures that:

        * multiple domains are enabled
        * the project name is not the reserved name for a federated domain
        * the project is a root project

        :raises keystone.exception.ValidationError: If one of the constraints
            was not satisfied.
        """
        if (not self.identity_api.multiple_domains_supported and
                project_ref['id'] != CONF.identity.default_domain_id):
            raise exception.ValidationError(
                message=_('Multiple domains are not supported'))

        self.assert_domain_not_federated(project_ref['id'], project_ref)

        if parent_ref:
            raise exception.ValidationError(
                message=_('only root projects are allowed to act as '
                          'domains.'))

    def _assert_regular_project_constraints(self, project_ref, parent_ref):
        """Enforces regular project hierarchy constraints

        Called when is_domain is false, and the project must contain a valid
        parent_id or domain_id (this is checked in the controller). If the
        parent is a regular project, this project must be in the same domain
        of its parent.

        :raises keystone.exception.ValidationError: If one of the constraints
            was not satisfied.
        :raises keystone.exception.DomainNotFound: In case the domain is not
            found.
        """
        domain = self.get_domain(project_ref['domain_id'])

        if parent_ref:
            is_domain_parent = parent_ref.get('is_domain')
            parent_domain_id = parent_ref.get('domain_id')

            if not is_domain_parent and parent_domain_id != domain['id']:
                raise exception.ValidationError(
                    message=_('Cannot create project, since it specifies '
                              'its owner as domain %(domain_id)s, but '
                              'specifies a parent in a different domain '
                              '(%(parent_domain_id)s).')
                    % {'domain_id': domain['id'],
                       'parent_domain_id': parent_ref.get('domain_id')})

    def _find_domain_id(self, project_ref):
        parent_ref = self.get_project(project_ref['parent_id'])
        return parent_ref['domain_id']

    def _enforce_project_constraints(self, project_ref, parent_id):
        parent_ref = self.get_project(parent_id) if parent_id else None
        if project_ref.get('is_domain'):
            self._assert_is_domain_project_constraints(project_ref, parent_ref)
        else:
            self._assert_regular_project_constraints(project_ref, parent_ref)

        # The whole hierarchy must be enabled
        if parent_id:
            parents_list = self.list_project_parents(parent_id)
            parents_list.append(parent_ref)
            for ref in parents_list:
                if not ref.get('enabled', True):
                    raise exception.ValidationError(
                        message=_('cannot create a project in a '
                                  'branch containing a disabled '
                                  'project: %s') % ref['id'])

            self._assert_max_hierarchy_depth(project_ref.get('parent_id'),
                                             parents_list)

    def _raise_reserved_character_exception(self, entity_type, name):
        msg = _('%(entity)s name cannot contain the following reserved '
                'characters: %(chars)s')
        raise exception.ValidationError(
            message=msg % {
                'entity': entity_type,
                'chars': utils.list_url_unsafe_chars(name)
            })

    def create_project(self, project_id, project, initiator=None):
        project = project.copy()

        if (CONF.resource.project_name_url_safe != 'off' and
                utils.is_not_url_safe(project['name'])):
            self._raise_reserved_character_exception('Project',
                                                     project['name'])

        project.setdefault('enabled', True)
        project['enabled'] = clean.project_enabled(project['enabled'])
        project.setdefault('description', '')
        project.setdefault('domain_id', None)
        project.setdefault('parent_id', None)
        project.setdefault('is_domain', False)

        # If this is a top level project acting as a domain, the project_id
        # is, effectively, the domain_id - and for such projects we don't
        # bother to store a copy of it in the domain_id attribute.
        # If this is a non-domain top level project,then the domain_id must
        # have been specified by the caller (this is checked as part of the
        # project constraints)
        domain_id = project.get('domain_id')
        parent_id = project.get('parent_id')
        if not domain_id and parent_id:
            project['domain_id'] = self._find_domain_id(project)

        self._enforce_project_constraints(project, parent_id)

        ret = self.driver.create_project(project_id, project)
        notifications.Audit.created(self._PROJECT, project_id, initiator)
        if MEMOIZE.should_cache(ret):
            self.get_project.set(ret, self, project_id)
            self.get_project_by_name.set(ret, self, ret['name'],
                                         ret['domain_id'])
        return ret

    def assert_domain_enabled(self, domain_id, domain=None):
        """Assert the Domain is enabled.

        :raise AssertionError: if domain is disabled.
        """
        if domain is None:
            domain = self.get_domain(domain_id)
        if not domain.get('enabled', True):
            raise AssertionError(_('Domain is disabled: %s') % domain_id)

    def assert_domain_not_federated(self, domain_id, domain):
        """Assert the Domain's name and id do not match the reserved keyword.

        Note that the reserved keyword is defined in the configuration file,
        by default, it is 'Federated', it is also case insensitive.
        If config's option is empty the default hardcoded value 'Federated'
        will be used.

        :raise AssertionError: if domain named match the value in the config.

        """
        # NOTE(marek-denis): We cannot create this attribute in the __init__ as
        # config values are always initialized to default value.
        federated_domain = CONF.federation.federated_domain_name.lower()
        if (domain.get('name') and domain['name'].lower() == federated_domain):
            raise AssertionError(_('Domain cannot be named %s')
                                 % domain['name'])
        if (domain_id.lower() == federated_domain):
            raise AssertionError(_('Domain cannot have ID %s')
                                 % domain_id)

    def assert_project_enabled(self, project_id, project=None):
        """Assert the project is enabled and its associated domain is enabled.

        :raise AssertionError: if the project or domain is disabled.
        """
        if project is None:
            project = self.get_project(project_id)
        self.assert_domain_enabled(domain_id=project['domain_id'])
        if not project.get('enabled', True):
            raise AssertionError(_('Project is disabled: %s') % project_id)

    @notifications.disabled(_PROJECT, public=False)
    def _disable_project(self, project_id):
        """Emit a notification to the callback system project is been disabled.

        This method, and associated callback listeners, removes the need for
        making direct calls to other managers to take action (e.g. revoking
        project scoped tokens) when a project is disabled.

        :param project_id: project identifier
        :type project_id: string
        """
        pass

    def _assert_all_parents_are_enabled(self, project_id):
        parents_list = self.list_project_parents(project_id)
        for project in parents_list:
            if not project.get('enabled', True):
                raise exception.ForbiddenAction(
                    action=_('cannot enable project %s since it has '
                             'disabled parents') % project_id)

    def _assert_whole_subtree_is_disabled(self, project_id):
        subtree_list = self.list_projects_in_subtree(project_id)
        for ref in subtree_list:
            if ref.get('enabled', True):
                raise exception.ForbiddenAction(
                    action=_('cannot disable project %s since '
                             'its subtree contains enabled '
                             'projects') % project_id)

    def update_project(self, project_id, project, initiator=None):
        # Use the driver directly to prevent using old cached value.
        original_project = self.driver.get_project(project_id)
        project = project.copy()

        if (CONF.resource.project_name_url_safe != 'off' and
                'name' in project and
                project['name'] != original_project['name'] and
                utils.is_not_url_safe(project['name'])):
            self._raise_reserved_character_exception('Project',
                                                     project['name'])

        parent_id = original_project.get('parent_id')
        if 'parent_id' in project and project.get('parent_id') != parent_id:
            raise exception.ForbiddenAction(
                action=_('Update of `parent_id` is not allowed.'))

        if ('is_domain' in project and
                project['is_domain'] != original_project['is_domain']):
            raise exception.ValidationError(
                message=_('Update of `is_domain` is not allowed.'))

        if 'enabled' in project:
            project['enabled'] = clean.project_enabled(project['enabled'])

        # NOTE(rodrigods): for the current implementation we only allow to
        # disable a project if all projects below it in the hierarchy are
        # already disabled. This also means that we can not enable a
        # project that has disabled parents.
        original_project_enabled = original_project.get('enabled', True)
        project_enabled = project.get('enabled', True)
        if not original_project_enabled and project_enabled:
            self._assert_all_parents_are_enabled(project_id)
        if original_project_enabled and not project_enabled:
            # NOTE(htruta): In order to disable a regular project, all its
            # children must already be disabled. However, to keep
            # compatibility with the existing domain behaviour, we allow a
            # project acting as a domain to be disabled irrespective of the
            # state of its children. Disabling a project acting as domain
            # effectively disables its children.
            if not original_project.get('is_domain'):
                self._assert_whole_subtree_is_disabled(project_id)
            self._disable_project(project_id)

        ret = self.driver.update_project(project_id, project)
        notifications.Audit.updated(self._PROJECT, project_id, initiator)
        self.get_project.invalidate(self, project_id)
        self.get_project_by_name.invalidate(self, original_project['name'],
                                            original_project['domain_id'])

        if ('domain_id' in project and
           project['domain_id'] != original_project['domain_id']):
            # If the project's domain_id has been updated, invalidate user
            # role assignments cache region, as it may be caching inherited
            # assignments from the old domain to the specified project
            assignment.COMPUTED_ASSIGNMENTS_REGION.invalidate()

        return ret

    def delete_project(self, project_id, initiator=None):
        # Use the driver directly to prevent using old cached value.
        project = self.driver.get_project(project_id)
        if project['is_domain'] and project['enabled']:
            raise exception.ValidationError(
                message=_('cannot delete an enabled project acting as a '
                          'domain. Please disable the project %s first.')
                % project.get('id'))

        if not self.is_leaf_project(project_id):
            raise exception.ForbiddenAction(
                action=_('cannot delete the project %s since it is not '
                         'a leaf in the hierarchy.') % project_id)

        project_user_ids = (
            self.assignment_api.list_user_ids_for_project(project_id))
        for user_id in project_user_ids:
            payload = {'user_id': user_id, 'project_id': project_id}
            self._emit_invalidate_user_project_tokens_notification(payload)
        ret = self.driver.delete_project(project_id)
        self.assignment_api.delete_project_assignments(project_id)
        self.get_project.invalidate(self, project_id)
        self.get_project_by_name.invalidate(self, project['name'],
                                            project['domain_id'])
        self.credential_api.delete_credentials_for_project(project_id)
        notifications.Audit.deleted(self._PROJECT, project_id, initiator)

        # Invalidate user role assignments cache region, as it may be caching
        # role assignments where the target is the specified project
        assignment.COMPUTED_ASSIGNMENTS_REGION.invalidate()

        return ret

    def _filter_projects_list(self, projects_list, user_id):
        user_projects = self.assignment_api.list_projects_for_user(user_id)
        user_projects_ids = set([proj['id'] for proj in user_projects])
        # Keep only the projects present in user_projects
        return [proj for proj in projects_list
                if proj['id'] in user_projects_ids]

    def _assert_valid_project_id(self, project_id):
        if project_id is None:
            msg = _('Project field is required and cannot be empty.')
            raise exception.ValidationError(message=msg)
        # Check if project_id exists
        self.get_project(project_id)

    def list_project_parents(self, project_id, user_id=None):
        self._assert_valid_project_id(project_id)
        parents = self.driver.list_project_parents(project_id)
        # If a user_id was provided, the returned list should be filtered
        # against the projects this user has access to.
        if user_id:
            parents = self._filter_projects_list(parents, user_id)
        return parents

    def _build_parents_as_ids_dict(self, project, parents_by_id):
        # NOTE(rodrigods): we don't rely in the order of the projects returned
        # by the list_project_parents() method. Thus, we create a project cache
        # (parents_by_id) in order to access each parent in constant time and
        # traverse up the hierarchy.
        def traverse_parents_hierarchy(project):
            parent_id = project.get('parent_id')
            if not parent_id:
                return None

            parent = parents_by_id[parent_id]
            return {parent_id: traverse_parents_hierarchy(parent)}

        return traverse_parents_hierarchy(project)

    def get_project_parents_as_ids(self, project):
        """Gets the IDs from the parents from a given project.

        The project IDs are returned as a structured dictionary traversing up
        the hierarchy to the top level project. For example, considering the
        following project hierarchy::

                                    A
                                    |
                                  +-B-+
                                  |   |
                                  C   D

        If we query for project C parents, the expected return is the following
        dictionary::

            'parents': {
                B['id']: {
                    A['id']: None
                }
            }

        """
        parents_list = self.list_project_parents(project['id'])
        parents_as_ids = self._build_parents_as_ids_dict(
            project, {proj['id']: proj for proj in parents_list})
        return parents_as_ids

    def list_projects_in_subtree(self, project_id, user_id=None):
        self._assert_valid_project_id(project_id)
        subtree = self.driver.list_projects_in_subtree(project_id)
        # If a user_id was provided, the returned list should be filtered
        # against the projects this user has access to.
        if user_id:
            subtree = self._filter_projects_list(subtree, user_id)
        return subtree

    def _build_subtree_as_ids_dict(self, project_id, subtree_by_parent):
        # NOTE(rodrigods): we perform a depth first search to construct the
        # dictionaries representing each level of the subtree hierarchy. In
        # order to improve this traversal performance, we create a cache of
        # projects (subtree_py_parent) that accesses in constant time the
        # direct children of a given project.
        def traverse_subtree_hierarchy(project_id):
            children = subtree_by_parent.get(project_id)
            if not children:
                return None

            children_ids = {}
            for child in children:
                children_ids[child['id']] = traverse_subtree_hierarchy(
                    child['id'])
            return children_ids

        return traverse_subtree_hierarchy(project_id)

    def get_projects_in_subtree_as_ids(self, project_id):
        """Gets the IDs from the projects in the subtree from a given project.

        The project IDs are returned as a structured dictionary representing
        their hierarchy. For example, considering the following project
        hierarchy::

                                    A
                                    |
                                  +-B-+
                                  |   |
                                  C   D

        If we query for project A subtree, the expected return is the following
        dictionary::

            'subtree': {
                B['id']: {
                    C['id']: None,
                    D['id']: None
                }
            }

        """
        def _projects_indexed_by_parent(projects_list):
            projects_by_parent = {}
            for proj in projects_list:
                parent_id = proj.get('parent_id')
                if parent_id:
                    if parent_id in projects_by_parent:
                        projects_by_parent[parent_id].append(proj)
                    else:
                        projects_by_parent[parent_id] = [proj]
            return projects_by_parent

        subtree_list = self.list_projects_in_subtree(project_id)
        subtree_as_ids = self._build_subtree_as_ids_dict(
            project_id, _projects_indexed_by_parent(subtree_list))
        return subtree_as_ids

    @MEMOIZE
    def get_domain(self, domain_id):
        return self.driver.get_domain(domain_id)

    @MEMOIZE
    def get_domain_by_name(self, domain_name):
        return self.driver.get_domain_by_name(domain_name)

    def create_domain(self, domain_id, domain, initiator=None):
        if (not self.identity_api.multiple_domains_supported and
                domain_id != CONF.identity.default_domain_id):
            raise exception.Forbidden(_('Multiple domains are not supported'))
        if (CONF.resource.domain_name_url_safe != 'off' and
                utils.is_not_url_safe(domain['name'])):
            self._raise_reserved_character_exception('Domain', domain['name'])

        self.assert_domain_not_federated(domain_id, domain)
        domain.setdefault('enabled', True)
        domain['enabled'] = clean.domain_enabled(domain['enabled'])
        ret = self.driver.create_domain(domain_id, domain)

        notifications.Audit.created(self._DOMAIN, domain_id, initiator)

        if MEMOIZE.should_cache(ret):
            self.get_domain.set(ret, self, domain_id)
            self.get_domain_by_name.set(ret, self, ret['name'])
        return ret

    @manager.response_truncated
    def list_domains(self, hints=None):
        return self.driver.list_domains(hints or driver_hints.Hints())

    @notifications.disabled(_DOMAIN, public=False)
    def _disable_domain(self, domain_id):
        """Emit a notification to the callback system domain is been disabled.

        This method, and associated callback listeners, removes the need for
        making direct calls to other managers to take action (e.g. revoking
        domain scoped tokens) when a domain is disabled.

        :param domain_id: domain identifier
        :type domain_id: string
        """
        pass

    def update_domain(self, domain_id, domain, initiator=None):
        self.assert_domain_not_federated(domain_id, domain)
        # Use the driver directly to prevent using old cached value.
        original_domain = self.driver.get_domain(domain_id)
        if (CONF.resource.domain_name_url_safe != 'off' and
            'name' in domain and domain['name'] != original_domain['name'] and
                utils.is_not_url_safe(domain['name'])):
            self._raise_reserved_character_exception('Domain', domain['name'])
        if 'enabled' in domain:
            domain['enabled'] = clean.domain_enabled(domain['enabled'])
        ret = self.driver.update_domain(domain_id, domain)
        notifications.Audit.updated(self._DOMAIN, domain_id, initiator)
        # disable owned users & projects when the API user specifically set
        #     enabled=False
        if (original_domain.get('enabled', True) and
                not domain.get('enabled', True)):
            notifications.Audit.disabled(self._DOMAIN, domain_id, initiator,
                                         public=False)

        self.get_domain.invalidate(self, domain_id)
        self.get_domain_by_name.invalidate(self, original_domain['name'])
        return ret

    def delete_domain(self, domain_id, initiator=None):
        # Use the driver directly to prevent using old cached value.
        domain = self.driver.get_domain(domain_id)

        # To help avoid inadvertent deletes, we insist that the domain
        # has been previously disabled.  This also prevents a user deleting
        # their own domain since, once it is disabled, they won't be able
        # to get a valid token to issue this delete.
        if domain['enabled']:
            raise exception.ForbiddenAction(
                action=_('cannot delete a domain that is enabled, '
                         'please disable it first.'))

        self._delete_domain_contents(domain_id)
        # Delete any database stored domain config
        self.domain_config_api.delete_config_options(domain_id)
        self.domain_config_api.delete_config_options(domain_id, sensitive=True)
        self.domain_config_api.release_registration(domain_id)
        # TODO(henry-nash): Although the controller will ensure deletion of
        # all users & groups within the domain (which will cause all
        # assignments for those users/groups to also be deleted), there
        # could still be assignments on this domain for users/groups in
        # other domains - so we should delete these here by making a call
        # to the backend to delete all assignments for this domain.
        # (see Bug #1277847)
        self.driver.delete_domain(domain_id)
        notifications.Audit.deleted(self._DOMAIN, domain_id, initiator)
        self.get_domain.invalidate(self, domain_id)
        self.get_domain_by_name.invalidate(self, domain['name'])

        # Invalidate user role assignments cache region, as it may be caching
        # role assignments where the target is the specified domain
        assignment.COMPUTED_ASSIGNMENTS_REGION.invalidate()

    def _delete_domain_contents(self, domain_id):
        """Delete the contents of a domain.

        Before we delete a domain, we need to remove all the entities
        that are owned by it, i.e. Projects. To do this we
        call the delete function for these entities, which are
        themselves responsible for deleting any credentials and role grants
        associated with them as well as revoking any relevant tokens.

        """
        def _delete_projects(project, projects, examined):
            if project['id'] in examined:
                msg = _LE('Circular reference or a repeated entry found '
                          'projects hierarchy - %(project_id)s.')
                LOG.error(msg, {'project_id': project['id']})
                return

            examined.add(project['id'])
            children = [proj for proj in projects
                        if proj.get('parent_id') == project['id']]
            for proj in children:
                _delete_projects(proj, projects, examined)

            try:
                self.delete_project(project['id'])
            except exception.ProjectNotFound:
                LOG.debug(('Project %(projectid)s not found when '
                           'deleting domain contents for %(domainid)s, '
                           'continuing with cleanup.'),
                          {'projectid': project['id'],
                           'domainid': domain_id})

        proj_refs = self.list_projects_in_domain(domain_id)

        # Deleting projects recursively
        roots = [x for x in proj_refs if x.get('parent_id') is None]
        examined = set()
        for project in roots:
            _delete_projects(project, proj_refs, examined)

    @manager.response_truncated
    def list_projects(self, hints=None):
        return self.driver.list_projects(hints or driver_hints.Hints())

    # NOTE(henry-nash): list_projects_in_domain is actually an internal method
    # and not exposed via the API.  Therefore there is no need to support
    # driver hints for it.
    def list_projects_in_domain(self, domain_id):
        return self.driver.list_projects_in_domain(domain_id)

    @MEMOIZE
    def get_project(self, project_id):
        return self.driver.get_project(project_id)

    @MEMOIZE
    def get_project_by_name(self, project_name, domain_id):
        return self.driver.get_project_by_name(project_name, domain_id)

    @notifications.internal(
        notifications.INVALIDATE_USER_PROJECT_TOKEN_PERSISTENCE)
    def _emit_invalidate_user_project_tokens_notification(self, payload):
        # This notification's payload is a dict of user_id and
        # project_id so the token provider can invalidate the tokens
        # from persistence if persistence is enabled.
        pass


# The ResourceDriverBase class is the set of driver methods from earlier
# drivers that we still support, that have not been removed or modified. This
# class is then used to created the augmented V8 and V9 version abstract driver
# classes, without having to duplicate a lot of abstract method signatures.
# If you remove a method from V9, then move the abstract methods from this Base
# class to the V8 class. Do not modify any of the method signatures in the Base
# class - changes should only be made in the V8 and subsequent classes.

@six.add_metaclass(abc.ABCMeta)
class ResourceDriverBase(object):

    def _get_list_limit(self):
        return CONF.resource.list_limit or CONF.list_limit

    # domain crud
    @abc.abstractmethod
    def create_domain(self, domain_id, domain):
        """Creates a new domain.

        :raises keystone.exception.Conflict: if the domain_id or domain name
                                             already exists

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_domains(self, hints):
        """List domains in the system.

        :param hints: filter hints which the driver should
                      implement if at all possible.

        :returns: a list of domain_refs or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_domains_from_ids(self, domain_ids):
        """List domains for the provided list of ids.

        :param domain_ids: list of ids

        :returns: a list of domain_refs.

        This method is used internally by the assignment manager to bulk read
        a set of domains given their ids.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_domain(self, domain_id):
        """Get a domain by ID.

        :returns: domain_ref
        :raises keystone.exception.DomainNotFound: if domain_id does not exist

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_domain_by_name(self, domain_name):
        """Get a domain by name.

        :returns: domain_ref
        :raises keystone.exception.DomainNotFound: if domain_name does not
                                                   exist

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def update_domain(self, domain_id, domain):
        """Updates an existing domain.

        :raises keystone.exception.DomainNotFound: if domain_id does not exist
        :raises keystone.exception.Conflict: if domain name already exists

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_domain(self, domain_id):
        """Deletes an existing domain.

        :raises keystone.exception.DomainNotFound: if domain_id does not exist

        """
        raise exception.NotImplemented()  # pragma: no cover

    # project crud
    @abc.abstractmethod
    def create_project(self, project_id, project):
        """Creates a new project.

        :raises keystone.exception.Conflict: if project_id or project name
                                             already exists

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_projects(self, hints):
        """List projects in the system.

        :param hints: filter hints which the driver should
                      implement if at all possible.

        :returns: a list of project_refs or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_projects_from_ids(self, project_ids):
        """List projects for the provided list of ids.

        :param project_ids: list of ids

        :returns: a list of project_refs.

        This method is used internally by the assignment manager to bulk read
        a set of projects given their ids.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_project_ids_from_domain_ids(self, domain_ids):
        """List project ids for the provided list of domain ids.

        :param domain_ids: list of domain ids

        :returns: a list of project ids owned by the specified domain ids.

        This method is used internally by the assignment manager to bulk read
        a set of project ids given a list of domain ids.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_projects_in_domain(self, domain_id):
        """List projects in the domain.

        :param domain_id: the driver MUST only return projects
                          within this domain.

        :returns: a list of project_refs or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_project(self, project_id):
        """Get a project by ID.

        :returns: project_ref
        :raises keystone.exception.ProjectNotFound: if project_id does not
                                                    exist

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def update_project(self, project_id, project):
        """Updates an existing project.

        :raises keystone.exception.ProjectNotFound: if project_id does not
                                                    exist
        :raises keystone.exception.Conflict: if project name already exists

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_project(self, project_id):
        """Deletes an existing project.

        :raises keystone.exception.ProjectNotFound: if project_id does not
                                                    exist

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_project_parents(self, project_id):
        """List all parents from a project by its ID.

        :param project_id: the driver will list the parents of this
                           project.

        :returns: a list of project_refs or an empty list.
        :raises keystone.exception.ProjectNotFound: if project_id does not
                                                    exist

        """
        raise exception.NotImplemented()

    @abc.abstractmethod
    def list_projects_in_subtree(self, project_id):
        """List all projects in the subtree below the hierarchy of the
        given project.

        :param project_id: the driver will get the subtree under
                           this project.

        :returns: a list of project_refs or an empty list
        :raises keystone.exception.ProjectNotFound: if project_id does not
                                                    exist

        """
        raise exception.NotImplemented()

    @abc.abstractmethod
    def is_leaf_project(self, project_id):
        """Checks if a project is a leaf in the hierarchy.

        :param project_id: the driver will check if this project
                           is a leaf in the hierarchy.

        :raises keystone.exception.ProjectNotFound: if project_id does not
                                                    exist

        """
        raise exception.NotImplemented()

    # Domain management functions for backends that only allow a single
    # domain.  Currently, this is only LDAP, but might be used by other
    # backends in the future.
    def _set_default_domain(self, ref):
        """If the domain ID has not been set, set it to the default."""
        if isinstance(ref, dict):
            if 'domain_id' not in ref:
                ref = ref.copy()
                ref['domain_id'] = CONF.identity.default_domain_id
            return ref
        elif isinstance(ref, list):
            return [self._set_default_domain(x) for x in ref]
        else:
            raise ValueError(_('Expected dict or list: %s') % type(ref))

    def _validate_default_domain(self, ref):
        """Validate that either the default domain or nothing is specified.

        Also removes the domain from the ref so that LDAP doesn't have to
        persist the attribute.

        """
        ref = ref.copy()
        domain_id = ref.pop('domain_id', CONF.identity.default_domain_id)
        self._validate_default_domain_id(domain_id)
        return ref

    def _validate_default_domain_id(self, domain_id):
        """Validate that the domain ID belongs to the default domain."""
        if domain_id != CONF.identity.default_domain_id:
            raise exception.DomainNotFound(domain_id=domain_id)


class ResourceDriverV8(ResourceDriverBase):
    """Removed or redefined methods from V8.

    Move the abstract methods of any methods removed or modified in later
    versions of the driver from ResourceDriverBase to here. We maintain this
    so that legacy drivers, which will be a subclass of ResourceDriverV8, can
    still reference them.

    """

    @abc.abstractmethod
    def get_project_by_name(self, tenant_name, domain_id):
        """Get a tenant by name.

        :returns: tenant_ref
        :raises keystone.exception.ProjectNotFound: if a project with the
                             tenant_name does not exist within the domain

        """
        raise exception.NotImplemented()  # pragma: no cover


class ResourceDriverV9(ResourceDriverBase):
    """New or redefined methods from V8.

    Add any new V9 abstract methods (or those with modified signatures) to
    this class.

    """

    @abc.abstractmethod
    def get_project_by_name(self, project_name, domain_id):
        """Get a project by name.

        :returns: project_ref
        :raises keystone.exception.ProjectNotFound: if a project with the
                             project_name does not exist within the domain

        """
        raise exception.NotImplemented()  # pragma: no cover


class V9ResourceWrapperForV8Driver(ResourceDriverV9):
    """Wrapper class to supported a V8 legacy driver.

    In order to support legacy drivers without having to make the manager code
    driver-version aware, we wrap legacy drivers so that they look like the
    latest version. For the various changes made in a new driver, here are the
    actions needed in this wrapper:

    Method removed from new driver - remove the call-through method from this
                                     class, since the manager will no longer be
                                     calling it.
    Method signature (or meaning) changed - wrap the old method in a new
                                            signature here, and munge the input
                                            and output parameters accordingly.
    New method added to new driver - add a method to implement the new
                                     functionality here if possible. If that is
                                     not possible, then return NotImplemented,
                                     since we do not guarantee to support new
                                     functionality with legacy drivers.

    """

    @versionutils.deprecated(
        as_of=versionutils.deprecated.MITAKA,
        what='keystone.resource.ResourceDriverV8',
        in_favor_of='keystone.resource.ResourceDriverV9',
        remove_in=+2)
    def __init__(self, wrapped_driver):
        self.driver = wrapped_driver

    def get_project_by_name(self, project_name, domain_id):
        return self.driver.get_project_by_name(project_name, domain_id)

    def create_domain(self, domain_id, domain):
        return self.driver.create_domain(domain_id, domain)

    def list_domains(self, hints):
        return self.driver.list_domains(hints)

    def list_domains_from_ids(self, domain_ids):
        return self.driver.list_domains_from_ids(domain_ids)

    def get_domain(self, domain_id):
        return self.driver.get_domain(domain_id)

    def get_domain_by_name(self, domain_name):
        return self.driver.get_domain_by_name(domain_name)

    def update_domain(self, domain_id, domain):
        return self.driver.update_domain(domain_id, domain)

    def delete_domain(self, domain_id):
        self.driver.delete_domain(domain_id)

    def create_project(self, project_id, project):
        return self.driver.create_project(project_id, project)

    def list_projects(self, hints):
        return self.driver.list_projects(hints)

    def list_projects_from_ids(self, project_ids):
        return self.driver.list_projects_from_ids(project_ids)

    def list_project_ids_from_domain_ids(self, domain_ids):
        return self.driver.list_project_ids_from_domain_ids(domain_ids)

    def list_projects_in_domain(self, domain_id):
        return self.driver.list_projects_in_domain(domain_id)

    def get_project(self, project_id):
        return self.driver.get_project(project_id)

    def update_project(self, project_id, project):
        return self.driver.update_project(project_id, project)

    def delete_project(self, project_id):
        self.driver.delete_project(project_id)

    def list_project_parents(self, project_id):
        return self.driver.list_project_parents(project_id)

    def list_projects_in_subtree(self, project_id):
        return self.driver.list_projects_in_subtree(project_id)

    def is_leaf_project(self, project_id):
        return self.driver.is_leaf_project(project_id)


Driver = manager.create_legacy_driver(ResourceDriverV8)


MEMOIZE_CONFIG = cache.get_memoization_decorator(group='domain_config')


@dependency.provider('domain_config_api')
class DomainConfigManager(manager.Manager):
    """Default pivot point for the Domain Config backend."""

    # NOTE(henry-nash): In order for a config option to be stored in the
    # standard table, it must be explicitly whitelisted. Options marked as
    # sensitive are stored in a separate table. Attempting to store options
    # that are not listed as either whitelisted or sensitive will raise an
    # exception.
    #
    # Only those options that affect the domain-specific driver support in
    # the identity manager are supported.

    driver_namespace = 'keystone.resource.domain_config'

    whitelisted_options = {
        'identity': ['driver'],
        'ldap': [
            'url', 'user', 'suffix', 'use_dumb_member', 'dumb_member',
            'allow_subtree_delete', 'query_scope', 'page_size',
            'alias_dereferencing', 'debug_level', 'chase_referrals',
            'user_tree_dn', 'user_filter', 'user_objectclass',
            'user_id_attribute', 'user_name_attribute', 'user_mail_attribute',
            'user_pass_attribute', 'user_enabled_attribute',
            'user_enabled_invert', 'user_enabled_mask', 'user_enabled_default',
            'user_attribute_ignore', 'user_default_project_id_attribute',
            'user_allow_create', 'user_allow_update', 'user_allow_delete',
            'user_enabled_emulation', 'user_enabled_emulation_dn',
            'user_enabled_emulation_use_group_config',
            'user_additional_attribute_mapping', 'group_tree_dn',
            'group_filter', 'group_objectclass', 'group_id_attribute',
            'group_name_attribute', 'group_member_attribute',
            'group_desc_attribute', 'group_attribute_ignore',
            'group_allow_create', 'group_allow_update', 'group_allow_delete',
            'group_additional_attribute_mapping', 'tls_cacertfile',
            'tls_cacertdir', 'use_tls', 'tls_req_cert', 'use_pool',
            'pool_size', 'pool_retry_max', 'pool_retry_delay',
            'pool_connection_timeout', 'pool_connection_lifetime',
            'use_auth_pool', 'auth_pool_size', 'auth_pool_connection_lifetime'
        ]
    }
    sensitive_options = {
        'identity': [],
        'ldap': ['password']
    }

    def __init__(self):
        super(DomainConfigManager, self).__init__(CONF.domain_config.driver)

    def _assert_valid_config(self, config):
        """Ensure the options in the config are valid.

        This method is called to validate the request config in create and
        update manager calls.

        :param config: config structure being created or updated

        """
        # Something must be defined in the request
        if not config:
            raise exception.InvalidDomainConfig(
                reason=_('No options specified'))

        # Make sure the groups/options defined in config itself are valid
        for group in config:
            if (not config[group] or not
                    isinstance(config[group], dict)):
                msg = _('The value of group %(group)s specified in the '
                        'config should be a dictionary of options') % {
                            'group': group}
                raise exception.InvalidDomainConfig(reason=msg)
            for option in config[group]:
                self._assert_valid_group_and_option(group, option)

    def _assert_valid_group_and_option(self, group, option):
        """Ensure the combination of group and option is valid.

        :param group: optional group name, if specified it must be one
                      we support
        :param option: optional option name, if specified it must be one
                       we support and a group must also be specified

        """
        if not group and not option:
            # For all calls, it's OK for neither to be defined, it means you
            # are operating on all config options for that domain.
            return

        if not group and option:
            # Our API structure should prevent this from ever happening, so if
            # it does, then this is coding error.
            msg = _('Option %(option)s found with no group specified while '
                    'checking domain configuration request') % {
                        'option': option}
            raise exception.UnexpectedError(exception=msg)

        if (group and group not in self.whitelisted_options and
                group not in self.sensitive_options):
            msg = _('Group %(group)s is not supported '
                    'for domain specific configurations') % {'group': group}
            raise exception.InvalidDomainConfig(reason=msg)

        if option:
            if (option not in self.whitelisted_options[group] and option not in
                    self.sensitive_options[group]):
                msg = _('Option %(option)s in group %(group)s is not '
                        'supported for domain specific configurations') % {
                            'group': group, 'option': option}
                raise exception.InvalidDomainConfig(reason=msg)

    def _is_sensitive(self, group, option):
        return option in self.sensitive_options[group]

    def _config_to_list(self, config):
        """Build whitelisted and sensitive lists for use by backend drivers."""
        whitelisted = []
        sensitive = []
        for group in config:
            for option in config[group]:
                the_list = (sensitive if self._is_sensitive(group, option)
                            else whitelisted)
                the_list.append({
                    'group': group, 'option': option,
                    'value': config[group][option]})

        return whitelisted, sensitive

    def _list_to_config(self, whitelisted, sensitive=None, req_option=None):
        """Build config dict from a list of option dicts.

        :param whitelisted: list of dicts containing options and their groups,
                            this has already been filtered to only contain
                            those options to include in the output.
        :param sensitive: list of dicts containing sensitive options and their
                          groups, this has already been filtered to only
                          contain those options to include in the output.
        :param req_option: the individual option requested

        :returns: a config dict, including sensitive if specified

        """
        the_list = whitelisted + (sensitive or [])
        if not the_list:
            return {}

        if req_option:
            # The request was specific to an individual option, so
            # no need to include the group in the output. We first check that
            # there is only one option in the answer (and that it's the right
            # one) - if not, something has gone wrong and we raise an error
            if len(the_list) > 1 or the_list[0]['option'] != req_option:
                LOG.error(_LE('Unexpected results in response for domain '
                              'config - %(count)s responses, first option is '
                              '%(option)s, expected option %(expected)s'),
                          {'count': len(the_list), 'option': list[0]['option'],
                           'expected': req_option})
                raise exception.UnexpectedError(
                    _('An unexpected error occurred when retrieving domain '
                      'configs'))
            return {the_list[0]['option']: the_list[0]['value']}

        config = {}
        for option in the_list:
            config.setdefault(option['group'], {})
            config[option['group']][option['option']] = option['value']

        return config

    def create_config(self, domain_id, config):
        """Create config for a domain

        :param domain_id: the domain in question
        :param config: the dict of config groups/options to assign to the
                       domain

        Creates a new config, overwriting any previous config (no Conflict
        error will be generated).

        :returns: a dict of group dicts containing the options, with any that
                  are sensitive removed
        :raises keystone.exception.InvalidDomainConfig: when the config
                contains options we do not support

        """
        self._assert_valid_config(config)
        whitelisted, sensitive = self._config_to_list(config)
        # Delete any existing config
        self.delete_config_options(domain_id)
        self.delete_config_options(domain_id, sensitive=True)
        # ...and create the new one
        for option in whitelisted:
            self.create_config_option(
                domain_id, option['group'], option['option'], option['value'])
        for option in sensitive:
            self.create_config_option(
                domain_id, option['group'], option['option'], option['value'],
                sensitive=True)
        # Since we are caching on the full substituted config, we just
        # invalidate here, rather than try and create the right result to
        # cache.
        self.get_config_with_sensitive_info.invalidate(self, domain_id)
        return self._list_to_config(whitelisted)

    def get_config(self, domain_id, group=None, option=None):
        """Get config, or partial config, for a domain

        :param domain_id: the domain in question
        :param group: an optional specific group of options
        :param option: an optional specific option within the group

        :returns: a dict of group dicts containing the whitelisted options,
                  filtered by group and option specified
        :raises keystone.exception.DomainConfigNotFound: when no config found
                that matches domain_id, group and option specified
        :raises keystone.exception.InvalidDomainConfig: when the config
                and group/option parameters specify an option we do not
                support

        An example response::

            {
                'ldap': {
                    'url': 'myurl'
                    'user_tree_dn': 'OU=myou'},
                'identity': {
                    'driver': 'ldap'}

            }

        """
        self._assert_valid_group_and_option(group, option)
        whitelisted = self.list_config_options(domain_id, group, option)
        if whitelisted:
            return self._list_to_config(whitelisted, req_option=option)

        if option:
            msg = _('option %(option)s in group %(group)s') % {
                'group': group, 'option': option}
        elif group:
            msg = _('group %(group)s') % {'group': group}
        else:
            msg = _('any options')
        raise exception.DomainConfigNotFound(
            domain_id=domain_id, group_or_option=msg)

    def update_config(self, domain_id, config, group=None, option=None):
        """Update config, or partial config, for a domain

        :param domain_id: the domain in question
        :param config: the config dict containing and groups/options being
                       updated
        :param group: an optional specific group of options, which if specified
                      must appear in config, with no other groups
        :param option: an optional specific option within the group, which if
                       specified must appear in config, with no other options

        The contents of the supplied config will be merged with the existing
        config for this domain, updating or creating new options if these did
        not previously exist. If group or option is specified, then the update
        will be limited to those specified items and the inclusion of other
        options in the supplied config will raise an exception, as will the
        situation when those options do not already exist in the current
        config.

        :returns: a dict of groups containing all whitelisted options
        :raises keystone.exception.InvalidDomainConfig: when the config
                and group/option parameters specify an option we do not
                support or one that does not exist in the original config

        """
        def _assert_valid_update(domain_id, config, group=None, option=None):
            """Ensure the combination of config, group and option is valid."""
            self._assert_valid_config(config)
            self._assert_valid_group_and_option(group, option)

            # If a group has been specified, then the request is to
            # explicitly only update the options in that group - so the config
            # must not contain anything else. Further, that group must exist in
            # the original config. Likewise, if an option has been specified,
            # then the group in the config must only contain that option and it
            # also must exist in the original config.
            if group:
                if len(config) != 1 or (option and len(config[group]) != 1):
                    if option:
                        msg = _('Trying to update option %(option)s in group '
                                '%(group)s, so that, and only that, option '
                                'must be specified  in the config') % {
                                    'group': group, 'option': option}
                    else:
                        msg = _('Trying to update group %(group)s, so that, '
                                'and only that, group must be specified in '
                                'the config') % {'group': group}
                    raise exception.InvalidDomainConfig(reason=msg)

                # So we now know we have the right number of entries in the
                # config that align with a group/option being specified, but we
                # must also make sure they match.
                if group not in config:
                    msg = _('request to update group %(group)s, but config '
                            'provided contains group %(group_other)s '
                            'instead') % {
                                'group': group,
                                'group_other': list(config.keys())[0]}
                    raise exception.InvalidDomainConfig(reason=msg)
                if option and option not in config[group]:
                    msg = _('Trying to update option %(option)s in group '
                            '%(group)s, but config provided contains option '
                            '%(option_other)s instead') % {
                                'group': group, 'option': option,
                                'option_other': list(config[group].keys())[0]}
                    raise exception.InvalidDomainConfig(reason=msg)

                # Finally, we need to check if the group/option specified
                # already exists in the original config - since if not, to keep
                # with the semantics of an update, we need to fail with
                # a DomainConfigNotFound
                if not self._get_config_with_sensitive_info(domain_id,
                                                            group, option):
                    if option:
                        msg = _('option %(option)s in group %(group)s') % {
                            'group': group, 'option': option}
                        raise exception.DomainConfigNotFound(
                            domain_id=domain_id, group_or_option=msg)
                    else:
                        msg = _('group %(group)s') % {'group': group}
                        raise exception.DomainConfigNotFound(
                            domain_id=domain_id, group_or_option=msg)

        def _update_or_create(domain_id, option, sensitive):
            """Update the option, if it doesn't exist then create it."""
            try:
                self.create_config_option(
                    domain_id, option['group'], option['option'],
                    option['value'], sensitive=sensitive)
            except exception.Conflict:
                self.update_config_option(
                    domain_id, option['group'], option['option'],
                    option['value'], sensitive=sensitive)

        update_config = config
        if group and option:
            # The config will just be a dict containing the option and
            # its value, so make it look like a single option under the
            # group in question
            update_config = {group: config}

        _assert_valid_update(domain_id, update_config, group, option)

        whitelisted, sensitive = self._config_to_list(update_config)

        for new_option in whitelisted:
            _update_or_create(domain_id, new_option, sensitive=False)
        for new_option in sensitive:
            _update_or_create(domain_id, new_option, sensitive=True)

        self.get_config_with_sensitive_info.invalidate(self, domain_id)
        return self.get_config(domain_id)

    def delete_config(self, domain_id, group=None, option=None):
        """Delete config, or partial config, for the domain.

        :param domain_id: the domain in question
        :param group: an optional specific group of options
        :param option: an optional specific option within the group

        If group and option are None, then the entire config for the domain
        is deleted. If group is not None, then just that group of options will
        be deleted. If group and option are both specified, then just that
        option is deleted.

        :raises keystone.exception.InvalidDomainConfig: when group/option
                parameters specify an option we do not support or one that
                does not exist in the original config.

        """
        self._assert_valid_group_and_option(group, option)
        if group:
            # As this is a partial delete, then make sure the items requested
            # are valid and exist in the current config
            current_config = self._get_config_with_sensitive_info(domain_id)
            # Raise an exception if the group/options specified don't exist in
            # the current config so that the delete method provides the
            # correct error semantics.
            current_group = current_config.get(group)
            if not current_group:
                msg = _('group %(group)s') % {'group': group}
                raise exception.DomainConfigNotFound(
                    domain_id=domain_id, group_or_option=msg)
            if option and not current_group.get(option):
                msg = _('option %(option)s in group %(group)s') % {
                    'group': group, 'option': option}
                raise exception.DomainConfigNotFound(
                    domain_id=domain_id, group_or_option=msg)

        self.delete_config_options(domain_id, group, option)
        self.delete_config_options(domain_id, group, option, sensitive=True)
        self.get_config_with_sensitive_info.invalidate(self, domain_id)

    def _get_config_with_sensitive_info(self, domain_id, group=None,
                                        option=None):
        """Get config for a domain/group/option with sensitive info included.

        This is only used by the methods within this class, which may need to
        check individual groups or options.

        """
        whitelisted = self.list_config_options(domain_id, group, option)
        sensitive = self.list_config_options(domain_id, group, option,
                                             sensitive=True)

        # Check if there are any sensitive substitutions needed. We first try
        # and simply ensure any sensitive options that have valid substitution
        # references in the whitelisted options are substituted. We then check
        # the resulting whitelisted option and raise a warning if there
        # appears to be an unmatched or incorrectly constructed substitution
        # reference. To avoid the risk of logging any sensitive options that
        # have already been substituted, we first take a copy of the
        # whitelisted option.

        # Build a dict of the sensitive options ready to try substitution
        sensitive_dict = {s['option']: s['value'] for s in sensitive}

        for each_whitelisted in whitelisted:
            if not isinstance(each_whitelisted['value'], six.string_types):
                # We only support substitutions into string types, if its an
                # integer, list etc. then just continue onto the next one
                continue

            # Store away the original value in case we need to raise a warning
            # after substitution.
            original_value = each_whitelisted['value']
            warning_msg = ''
            try:
                each_whitelisted['value'] = (
                    each_whitelisted['value'] % sensitive_dict)
            except KeyError:
                warning_msg = _LW(
                    'Found what looks like an unmatched config option '
                    'substitution reference - domain: %(domain)s, group: '
                    '%(group)s, option: %(option)s, value: %(value)s. Perhaps '
                    'the config option to which it refers has yet to be '
                    'added?')
            except (ValueError, TypeError):
                warning_msg = _LW(
                    'Found what looks like an incorrectly constructed '
                    'config option substitution reference - domain: '
                    '%(domain)s, group: %(group)s, option: %(option)s, '
                    'value: %(value)s.')

            if warning_msg:
                LOG.warning(warning_msg % {
                    'domain': domain_id,
                    'group': each_whitelisted['group'],
                    'option': each_whitelisted['option'],
                    'value': original_value})

        return self._list_to_config(whitelisted, sensitive)

    @MEMOIZE_CONFIG
    def get_config_with_sensitive_info(self, domain_id):
        """Get config for a domain with sensitive info included.

        This method is not exposed via the public API, but is used by the
        identity manager to initialize a domain with the fully formed config
        options.

        """
        return self._get_config_with_sensitive_info(domain_id)

    def get_config_default(self, group=None, option=None):
        """Get default config, or partial default config

        :param group: an optional specific group of options
        :param option: an optional specific option within the group

        :returns: a dict of group dicts containing the default options,
                  filtered by group and option if specified
        :raises keystone.exception.InvalidDomainConfig: when the config
                and group/option parameters specify an option we do not
                support (or one that is not whitelisted).

        An example response::

            {
                'ldap': {
                    'url': 'myurl',
                    'user_tree_dn': 'OU=myou',
                    ....},
                'identity': {
                    'driver': 'ldap'}

            }

        """
        def _option_dict(group, option):
            group_attr = getattr(CONF, group)
            if group_attr is None:
                msg = _('Group  %s not found in config') % group
                raise exception.UnexpectedError(msg)
            return {'group': group, 'option': option,
                    'value': getattr(group_attr, option)}

        self._assert_valid_group_and_option(group, option)
        config_list = []
        if group:
            if option:
                if option not in self.whitelisted_options[group]:
                    msg = _('Reading the default for option %(option)s in '
                            'group %(group)s is not supported') % {
                                'option': option, 'group': group}
                    raise exception.InvalidDomainConfig(reason=msg)
                config_list.append(_option_dict(group, option))
            else:
                for each_option in self.whitelisted_options[group]:
                    config_list.append(_option_dict(group, each_option))
        else:
            for each_group in self.whitelisted_options:
                for each_option in self.whitelisted_options[each_group]:
                    config_list.append(_option_dict(each_group, each_option))

        return self._list_to_config(config_list, req_option=option)


@six.add_metaclass(abc.ABCMeta)
class DomainConfigDriverV8(object):
    """Interface description for a Domain Config driver."""

    @abc.abstractmethod
    def create_config_option(self, domain_id, group, option, value,
                             sensitive=False):
        """Creates a config option for a domain.

        :param domain_id: the domain for this option
        :param group: the group name
        :param option: the option name
        :param value: the value to assign to this option
        :param sensitive: whether the option is sensitive

        :returns: dict containing group, option and value
        :raises keystone.exception.Conflict: when the option already exists

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_config_option(self, domain_id, group, option, sensitive=False):
        """Gets the config option for a domain.

        :param domain_id: the domain for this option
        :param group: the group name
        :param option: the option name
        :param sensitive: whether the option is sensitive

        :returns: dict containing group, option and value
        :raises keystone.exception.DomainConfigNotFound: the option doesn't
                                                         exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_config_options(self, domain_id, group=None, option=False,
                            sensitive=False):
        """Gets a config options for a domain.

        :param domain_id: the domain for this option
        :param group: optional group option name
        :param option: optional option name. If group is None, then this
                       parameter is ignored
        :param sensitive: whether the option is sensitive

        :returns: list of dicts containing group, option and value

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def update_config_option(self, domain_id, group, option, value,
                             sensitive=False):
        """Updates a config option for a domain.

        :param domain_id: the domain for this option
        :param group: the group option name
        :param option: the option name
        :param value: the value to assign to this option
        :param sensitive: whether the option is sensitive

        :returns: dict containing updated group, option and value
        :raises keystone.exception.DomainConfigNotFound: the option doesn't
                                                         exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_config_options(self, domain_id, group=None, option=None,
                              sensitive=False):
        """Deletes config options for a domain.

        Allows deletion of all options for a domain, all options in a group
        or a specific option. The driver is silent if there are no options
        to delete.

        :param domain_id: the domain for this option
        :param group: optional group option name
        :param option: optional option name. If group is None, then this
                       parameter is ignored
        :param sensitive: whether the option is sensitive

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def obtain_registration(self, domain_id, type):
        """Try and register this domain to use the type specified.

        :param domain_id: the domain required
        :param type: type of registration
        :returns: True if the domain was registered, False otherwise. Failing
                  to register means that someone already has it (which could
                  even be the domain being requested).

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def read_registration(self, type):
        """Get the domain ID of who is registered to use this type.

        :param type: type of registration
        :returns: domain_id of who is registered.
        :raises keystone.exception.ConfigRegistrationNotFound: If nobody is
            registered.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def release_registration(self, domain_id, type=None):
        """Release registration if it is held by the domain specified.

        If the specified domain is registered for this domain then free it,
        if it is not then do nothing - no exception is raised.

        :param domain_id: the domain in question
        :param type: type of registration, if None then all registrations
                     for this domain will be freed

        """
        raise exception.NotImplemented()  # pragma: no cover


DomainConfigDriver = manager.create_legacy_driver(DomainConfigDriverV8)
