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

"""Main entry point into the Assignment service."""

import abc
import copy

from oslo_cache import core as oslo_cache
from oslo_config import cfg
from oslo_log import log
from oslo_log import versionutils
import six

from keystone.common import cache
from keystone.common import dependency
from keystone.common import driver_hints
from keystone.common import manager
from keystone import exception
from keystone.i18n import _
from keystone.i18n import _LI, _LE
from keystone import notifications


CONF = cfg.CONF
LOG = log.getLogger(__name__)

# This is a general cache region for assignment administration (CRUD
# operations).
MEMOIZE = cache.get_memoization_decorator(group='role')

# This builds a discrete cache region dedicated to role assignments computed
# for a given user + project/domain pair. Any write operation to add or remove
# any role assignment should invalidate this entire cache region.
COMPUTED_ASSIGNMENTS_REGION = oslo_cache.create_region()
MEMOIZE_COMPUTED_ASSIGNMENTS = cache.get_memoization_decorator(
    group='role',
    region=COMPUTED_ASSIGNMENTS_REGION)


@dependency.provider('assignment_api')
@dependency.requires('credential_api', 'identity_api', 'resource_api',
                     'revoke_api', 'role_api')
class Manager(manager.Manager):
    """Default pivot point for the Assignment backend.

    See :class:`keystone.common.manager.Manager` for more details on how this
    dynamically calls the backend.

    """

    driver_namespace = 'keystone.assignment'

    _PROJECT = 'project'
    _ROLE_REMOVED_FROM_USER = 'role_removed_from_user'
    _INVALIDATION_USER_PROJECT_TOKENS = 'invalidate_user_project_tokens'

    def __init__(self):
        assignment_driver = CONF.assignment.driver
        super(Manager, self).__init__(assignment_driver)

        # Make sure it is a driver version we support, and if it is a legacy
        # driver, then wrap it.
        if isinstance(self.driver, AssignmentDriverV8):
            self.driver = V9AssignmentWrapperForV8Driver(self.driver)
        elif not isinstance(self.driver, AssignmentDriverV9):
            raise exception.UnsupportedDriverVersion(driver=assignment_driver)

    def _get_group_ids_for_user_id(self, user_id):
        # TODO(morganfainberg): Implement a way to get only group_ids
        # instead of the more expensive to_dict() call for each record.
        return [x['id'] for
                x in self.identity_api.list_groups_for_user(user_id)]

    def list_user_ids_for_project(self, tenant_id):
        self.resource_api.get_project(tenant_id)
        assignment_list = self.list_role_assignments(
            project_id=tenant_id, effective=True)
        # Use set() to process the list to remove any duplicates
        return list(set([x['user_id'] for x in assignment_list]))

    def _list_parent_ids_of_project(self, project_id):
        if CONF.os_inherit.enabled:
            return [x['id'] for x in (
                self.resource_api.list_project_parents(project_id))]
        else:
            return []

    @MEMOIZE_COMPUTED_ASSIGNMENTS
    def get_roles_for_user_and_project(self, user_id, tenant_id):
        """Get the roles associated with a user within given project.

        This includes roles directly assigned to the user on the
        project, as well as those by virtue of group membership or
        inheritance.

        :returns: a list of role ids.
        :raises keystone.exception.ProjectNotFound: If the project doesn't
            exist.

        """
        self.resource_api.get_project(tenant_id)
        assignment_list = self.list_role_assignments(
            user_id=user_id, project_id=tenant_id, effective=True)
        # Use set() to process the list to remove any duplicates
        return list(set([x['role_id'] for x in assignment_list]))

    @MEMOIZE_COMPUTED_ASSIGNMENTS
    def get_roles_for_user_and_domain(self, user_id, domain_id):
        """Get the roles associated with a user within given domain.

        :returns: a list of role ids.
        :raises keystone.exception.DomainNotFound: If the domain doesn't exist.

        """
        self.resource_api.get_domain(domain_id)
        assignment_list = self.list_role_assignments(
            user_id=user_id, domain_id=domain_id, effective=True)
        # Use set() to process the list to remove any duplicates
        return list(set([x['role_id'] for x in assignment_list]))

    def get_roles_for_groups(self, group_ids, project_id=None, domain_id=None):
        """Get a list of roles for this group on domain and/or project."""
        if project_id is not None:
            self.resource_api.get_project(project_id)
            assignment_list = self.list_role_assignments(
                source_from_group_ids=group_ids, project_id=project_id,
                effective=True)
        elif domain_id is not None:
            assignment_list = self.list_role_assignments(
                source_from_group_ids=group_ids, domain_id=domain_id,
                effective=True)
        else:
            raise AttributeError(_("Must specify either domain or project"))

        role_ids = list(set([x['role_id'] for x in assignment_list]))
        return self.role_api.list_roles_from_ids(role_ids)

    def add_user_to_project(self, tenant_id, user_id):
        """Add user to a tenant by creating a default role relationship.

        :raises keystone.exception.ProjectNotFound: If the project doesn't
            exist.
        :raises keystone.exception.UserNotFound: If the user doesn't exist.

        """
        self.resource_api.get_project(tenant_id)
        try:
            self.role_api.get_role(CONF.member_role_id)
            self.driver.add_role_to_user_and_project(
                user_id,
                tenant_id,
                CONF.member_role_id)
        except exception.RoleNotFound:
            LOG.info(_LI("Creating the default role %s "
                         "because it does not exist."),
                     CONF.member_role_id)
            role = {'id': CONF.member_role_id,
                    'name': CONF.member_role_name}
            try:
                self.role_api.create_role(CONF.member_role_id, role)
            except exception.Conflict:
                LOG.info(_LI("Creating the default role %s failed because it "
                             "was already created"),
                         CONF.member_role_id)
            # now that default role exists, the add should succeed
            self.driver.add_role_to_user_and_project(
                user_id,
                tenant_id,
                CONF.member_role_id)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()

    @notifications.role_assignment('created')
    def _add_role_to_user_and_project_adapter(self, role_id, user_id=None,
                                              group_id=None, domain_id=None,
                                              project_id=None,
                                              inherited_to_projects=False,
                                              context=None):

        # The parameters for this method must match the parameters for
        # create_grant so that the notifications.role_assignment decorator
        # will work.

        self.resource_api.get_project(project_id)
        self.role_api.get_role(role_id)
        self.driver.add_role_to_user_and_project(user_id, project_id, role_id)

    def add_role_to_user_and_project(self, user_id, tenant_id, role_id):
        self._add_role_to_user_and_project_adapter(
            role_id, user_id=user_id, project_id=tenant_id)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()

    def remove_user_from_project(self, tenant_id, user_id):
        """Remove user from a tenant

        :raises keystone.exception.ProjectNotFound: If the project doesn't
            exist.
        :raises keystone.exception.UserNotFound: If the user doesn't exist.

        """
        roles = self.get_roles_for_user_and_project(user_id, tenant_id)
        if not roles:
            raise exception.NotFound(tenant_id)
        for role_id in roles:
            try:
                self.driver.remove_role_from_user_and_project(user_id,
                                                              tenant_id,
                                                              role_id)
                self.revoke_api.revoke_by_grant(role_id, user_id=user_id,
                                                project_id=tenant_id)

            except exception.RoleNotFound:
                LOG.debug("Removing role %s failed because it does not exist.",
                          role_id)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()

    # TODO(henry-nash): We might want to consider list limiting this at some
    # point in the future.
    def list_projects_for_user(self, user_id, hints=None):
        assignment_list = self.list_role_assignments(
            user_id=user_id, effective=True)
        # Use set() to process the list to remove any duplicates
        project_ids = list(set([x['project_id'] for x in assignment_list
                                if x.get('project_id')]))
        return self.resource_api.list_projects_from_ids(list(project_ids))

    # TODO(henry-nash): We might want to consider list limiting this at some
    # point in the future.
    def list_domains_for_user(self, user_id, hints=None):
        assignment_list = self.list_role_assignments(
            user_id=user_id, effective=True)
        # Use set() to process the list to remove any duplicates
        domain_ids = list(set([x['domain_id'] for x in assignment_list
                               if x.get('domain_id')]))
        return self.resource_api.list_domains_from_ids(domain_ids)

    def list_domains_for_groups(self, group_ids):
        assignment_list = self.list_role_assignments(
            source_from_group_ids=group_ids, effective=True)
        domain_ids = list(set([x['domain_id'] for x in assignment_list
                               if x.get('domain_id')]))
        return self.resource_api.list_domains_from_ids(domain_ids)

    def list_projects_for_groups(self, group_ids):
        assignment_list = self.list_role_assignments(
            source_from_group_ids=group_ids, effective=True)
        project_ids = list(set([x['project_id'] for x in assignment_list
                               if x.get('project_id')]))
        return self.resource_api.list_projects_from_ids(project_ids)

    @notifications.role_assignment('deleted')
    def _remove_role_from_user_and_project_adapter(self, role_id, user_id=None,
                                                   group_id=None,
                                                   domain_id=None,
                                                   project_id=None,
                                                   inherited_to_projects=False,
                                                   context=None):

        # The parameters for this method must match the parameters for
        # delete_grant so that the notifications.role_assignment decorator
        # will work.

        self.driver.remove_role_from_user_and_project(user_id, project_id,
                                                      role_id)
        if project_id:
            self._emit_invalidate_grant_token_persistence(user_id, project_id)
        else:
            self.identity_api.emit_invalidate_user_token_persistence(user_id)
        self.revoke_api.revoke_by_grant(role_id, user_id=user_id,
                                        project_id=project_id)

    def remove_role_from_user_and_project(self, user_id, tenant_id, role_id):
        self._remove_role_from_user_and_project_adapter(
            role_id, user_id=user_id, project_id=tenant_id)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()

    @notifications.internal(notifications.INVALIDATE_USER_TOKEN_PERSISTENCE)
    def _emit_invalidate_user_token_persistence(self, user_id):
        self.identity_api.emit_invalidate_user_token_persistence(user_id)

    def _emit_invalidate_grant_token_persistence(self, user_id, project_id):
        self.identity_api.emit_invalidate_grant_token_persistence(
            {'user_id': user_id, 'project_id': project_id}
        )

    @notifications.role_assignment('created')
    def create_grant(self, role_id, user_id=None, group_id=None,
                     domain_id=None, project_id=None,
                     inherited_to_projects=False, context=None):
        self.role_api.get_role(role_id)
        if domain_id:
            self.resource_api.get_domain(domain_id)
        if project_id:
            self.resource_api.get_project(project_id)
        self.driver.create_grant(role_id, user_id, group_id, domain_id,
                                 project_id, inherited_to_projects)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()

    def get_grant(self, role_id, user_id=None, group_id=None,
                  domain_id=None, project_id=None,
                  inherited_to_projects=False):
        role_ref = self.role_api.get_role(role_id)
        if domain_id:
            self.resource_api.get_domain(domain_id)
        if project_id:
            self.resource_api.get_project(project_id)
        self.check_grant_role_id(
            role_id, user_id, group_id, domain_id, project_id,
            inherited_to_projects)
        return role_ref

    def list_grants(self, user_id=None, group_id=None,
                    domain_id=None, project_id=None,
                    inherited_to_projects=False):
        if domain_id:
            self.resource_api.get_domain(domain_id)
        if project_id:
            self.resource_api.get_project(project_id)
        grant_ids = self.list_grant_role_ids(
            user_id, group_id, domain_id, project_id, inherited_to_projects)
        return self.role_api.list_roles_from_ids(grant_ids)

    @notifications.role_assignment('deleted')
    def _emit_revoke_user_grant(self, role_id, user_id, domain_id, project_id,
                                inherited_to_projects, context):
        self._emit_invalidate_grant_token_persistence(user_id, project_id)

    def delete_grant(self, role_id, user_id=None, group_id=None,
                     domain_id=None, project_id=None,
                     inherited_to_projects=False, context=None):
        if group_id is None:
            self.revoke_api.revoke_by_grant(user_id=user_id,
                                            role_id=role_id,
                                            domain_id=domain_id,
                                            project_id=project_id)
            self._emit_revoke_user_grant(
                role_id, user_id, domain_id, project_id,
                inherited_to_projects, context)
        else:
            try:
                # Group may contain a lot of users so revocation will be
                # by role & domain/project
                if domain_id is None:
                    self.revoke_api.revoke_by_project_role_assignment(
                        project_id, role_id
                    )
                else:
                    self.revoke_api.revoke_by_domain_role_assignment(
                        domain_id, role_id
                    )
                if CONF.token.revoke_by_id:
                    # NOTE(morganfainberg): The user ids are the important part
                    # for invalidating tokens below, so extract them here.
                    for user in self.identity_api.list_users_in_group(
                            group_id):
                        self._emit_revoke_user_grant(
                            role_id, user['id'], domain_id, project_id,
                            inherited_to_projects, context)
            except exception.GroupNotFound:
                LOG.debug('Group %s not found, no tokens to invalidate.',
                          group_id)

        # TODO(henry-nash): While having the call to get_role here mimics the
        # previous behavior (when it was buried inside the driver delete call),
        # this seems an odd place to have this check, given what we have
        # already done so far in this method. See Bug #1406776.
        self.role_api.get_role(role_id)

        if domain_id:
            self.resource_api.get_domain(domain_id)
        if project_id:
            self.resource_api.get_project(project_id)
        self.driver.delete_grant(role_id, user_id, group_id, domain_id,
                                 project_id, inherited_to_projects)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()

    # The methods _expand_indirect_assignment, _list_direct_role_assignments
    # and _list_effective_role_assignments below are only used on
    # list_role_assignments, but they are not in its scope as nested functions
    # since it would significantly increase McCabe complexity, that should be
    # kept as it is in order to detect unnecessarily complex code, which is not
    # this case.

    def _expand_indirect_assignment(self, ref, user_id=None, project_id=None,
                                    subtree_ids=None, expand_groups=True):
        """Returns a list of expanded role assignments.

        This methods is called for each discovered assignment that either needs
        a group assignment expanded into individual user assignments, or needs
        an inherited assignment to be applied to its children.

        In all cases, if either user_id and/or project_id is specified, then we
        filter the result on those values.

        If project_id is specified and subtree_ids is None, then this
        indicates that we are only interested in that one project. If
        subtree_ids is not None, then this is an indicator that any
        inherited assignments need to be expanded down the tree. The
        actual subtree_ids don't need to be used as a filter here, since we
        already ensured only those assignments that could affect them
        were passed to this method.

        If expand_groups is True then we expand groups out to a list of
        assignments, one for each member of that group.

        """
        def create_group_assignment(base_ref, user_id):
            """Creates a group assignment from the provided ref."""
            ref = copy.deepcopy(base_ref)

            ref['user_id'] = user_id

            indirect = ref.setdefault('indirect', {})
            indirect['group_id'] = ref.pop('group_id')

            return ref

        def expand_group_assignment(ref, user_id):
            """Expands group role assignment.

            For any group role assignment on a target, it is replaced by a list
            of role assignments containing one for each user of that group on
            that target.

            An example of accepted ref is::

            {
                'group_id': group_id,
                'project_id': project_id,
                'role_id': role_id
            }

            Once expanded, it should be returned as a list of entities like the
            one below, one for each each user_id in the provided group_id.

            ::

            {
                'user_id': user_id,
                'project_id': project_id,
                'role_id': role_id,
                'indirect' : {
                    'group_id': group_id
                }
            }

            Returned list will be formatted by the Controller, which will
            deduce a role assignment came from group membership if it has both
            'user_id' in the main body of the dict and 'group_id' in indirect
            subdict.

            """
            if user_id:
                return [create_group_assignment(ref, user_id=user_id)]

            return [create_group_assignment(ref, user_id=m['id'])
                    for m in self.identity_api.list_users_in_group(
                        ref['group_id'])]

        def expand_inherited_assignment(ref, user_id, project_id, subtree_ids,
                                        expand_groups):
            """Expands inherited role assignments.

            If expand_groups is True and this is a group role assignment on a
            target, replace it by a list of role assignments containing one for
            each user of that group, on every project under that target. If
            expand_groups is False, then return a group assignment on an
            inherited target.

            If this is a user role assignment on a specific target (i.e.
            project_id is specified, but subtree_ids is None) then simply
            format this as a single assignment (since we are effectively
            filtering on project_id). If however, project_id is None or
            subtree_ids is not None, then replace this one assignment with a
            list of role assignments for that user on every project under
            that target.

            An example of accepted ref is::

            {
                'group_id': group_id,
                'project_id': parent_id,
                'role_id': role_id,
                'inherited_to_projects': 'projects'
            }

            Once expanded, it should be returned as a list of entities like the
            one below, one for each each user_id in the provided group_id and
            for each subproject_id in the project_id subtree.

            ::

            {
                'user_id': user_id,
                'project_id': subproject_id,
                'role_id': role_id,
                'indirect' : {
                    'group_id': group_id,
                    'project_id': parent_id
                }
            }

            Returned list will be formatted by the Controller, which will
            deduce a role assignment came from group membership if it has both
            'user_id' in the main body of the dict and 'group_id' in the
            'indirect' subdict, as well as it is possible to deduce if it has
            come from inheritance if it contains both a 'project_id' in the
            main body of the dict and 'parent_id' in the 'indirect' subdict.

            """
            def create_inherited_assignment(base_ref, project_id):
                """Creates a project assignment from the provided ref.

                base_ref can either be a project or domain inherited
                assignment ref.

                """
                ref = copy.deepcopy(base_ref)

                indirect = ref.setdefault('indirect', {})
                if ref.get('project_id'):
                    indirect['project_id'] = ref.pop('project_id')
                else:
                    indirect['domain_id'] = ref.pop('domain_id')

                ref['project_id'] = project_id
                ref.pop('inherited_to_projects')

                return ref

            # Define expanded project list to which to apply this assignment
            if project_id:
                # Since ref is an inherited assignment and we are filtering by
                # project(s), we are only going to apply the assignment to the
                # relevant project(s)
                project_ids = [project_id]
                if subtree_ids:
                    project_ids += subtree_ids
                    # If this is a domain inherited assignment, then we know
                    # that all the project_ids will get this assignment. If
                    # it's a project inherited assignment, and the assignment
                    # point is an ancestor of project_id, then we know that
                    # again all the project_ids will get the assignment.  If,
                    # however, the assignment point is within the subtree,
                    # then only a partial tree will get the assignment.
                    if ref.get('project_id'):
                        if ref['project_id'] in project_ids:
                            project_ids = (
                                [x['id'] for x in
                                    self.resource_api.list_projects_in_subtree(
                                        ref['project_id'])])
            elif ref.get('domain_id'):
                # A domain inherited assignment, so apply it to all projects
                # in this domain
                project_ids = (
                    [x['id'] for x in
                        self.resource_api.list_projects_in_domain(
                            ref['domain_id'])])
            else:
                # It must be a project assignment, so apply it to its subtree
                project_ids = (
                    [x['id'] for x in
                        self.resource_api.list_projects_in_subtree(
                            ref['project_id'])])

            new_refs = []
            if 'group_id' in ref:
                if expand_groups:
                    # Expand role assignment to all group members on any
                    # inherited target of any of the projects
                    for ref in expand_group_assignment(ref, user_id):
                        new_refs += [create_inherited_assignment(ref, proj_id)
                                     for proj_id in project_ids]
                else:
                    # Just place the group assignment on any inherited target
                    # of any of the projects
                    new_refs += [create_inherited_assignment(ref, proj_id)
                                 for proj_id in project_ids]
            else:
                # Expand role assignment for all projects
                new_refs += [create_inherited_assignment(ref, proj_id)
                             for proj_id in project_ids]

            return new_refs

        if ref.get('inherited_to_projects') == 'projects':
            return expand_inherited_assignment(
                ref, user_id, project_id, subtree_ids, expand_groups)
        elif 'group_id' in ref and expand_groups:
            return expand_group_assignment(ref, user_id)
        return [ref]

    def _add_implied_roles(self, role_refs):
        """Expand out implied roles.

        The role_refs passed in have had all inheritance and group assignments
        expanded out. We now need to look at the role_id in each ref and see
        if it is a prior role for some implied roles. If it is, then we need to
        duplicate that ref, one for each implied role. We store the prior role
        in the indirect dict that is part of such a duplicated ref, so that a
        caller can determine where the assignment came from.

        """
        def _make_implied_ref_copy(prior_ref, implied_role_id):
            # Create a ref for an implied role from the ref of a prior role,
            # setting the new role_id to be the implied role and the indirect
            # role_id to be the prior role
            implied_ref = copy.deepcopy(prior_ref)
            implied_ref['role_id'] = implied_role_id
            indirect = implied_ref.setdefault('indirect', {})
            indirect['role_id'] = prior_ref['role_id']
            return implied_ref

        if not CONF.token.infer_roles:
            return role_refs
        try:
            implied_roles_cache = {}
            role_refs_to_check = list(role_refs)
            ref_results = list(role_refs)
            checked_role_refs = list()
            while(role_refs_to_check):
                next_ref = role_refs_to_check.pop()
                checked_role_refs.append(next_ref)
                next_role_id = next_ref['role_id']
                if next_role_id in implied_roles_cache:
                    implied_roles = implied_roles_cache[next_role_id]
                else:
                    implied_roles = (
                        self.role_api.list_implied_roles(next_role_id))
                    implied_roles_cache[next_role_id] = implied_roles
                for implied_role in implied_roles:
                    implied_ref = (
                        _make_implied_ref_copy(
                            next_ref, implied_role['implied_role_id']))
                    if implied_ref in checked_role_refs:
                        msg = _LE('Circular reference found '
                                  'role inference rules - %(prior_role_id)s.')
                        LOG.error(msg, {'prior_role_id': next_ref['role_id']})
                    else:
                        ref_results.append(implied_ref)
                        role_refs_to_check.append(implied_ref)
        except exception.NotImplemented:
            LOG.error('Role driver does not support implied roles.')

        return ref_results

    def _filter_by_role_id(self, role_id, ref_results):
        # if we arrive here, we need to filer by role_id.
        filter_results = []
        for ref in ref_results:
            if ref['role_id'] == role_id:
                filter_results.append(ref)
        return filter_results

    def _list_effective_role_assignments(self, role_id, user_id, group_id,
                                         domain_id, project_id, subtree_ids,
                                         inherited, source_from_group_ids):
        """List role assignments in effective mode.

        When using effective mode, besides the direct assignments, the indirect
        ones that come from grouping or inheritance are retrieved and will then
        be expanded.

        The resulting list of assignments will be filtered by the provided
        parameters. If subtree_ids is not None, then we also want to include
        all subtree_ids in the filter as well. Since we are in effective mode,
        group can never act as a filter (since group assignments are expanded
        into user roles) and domain can only be filter if we want non-inherited
        assignments, since domains can't inherit assignments.

        The goal of this method is to only ask the driver for those
        assignments as could effect the result based on the parameter filters
        specified, hence avoiding retrieving a huge list.

        """
        def list_role_assignments_for_actor(
                role_id, inherited, user_id=None, group_ids=None,
                project_id=None, subtree_ids=None, domain_id=None):
            """List role assignments for actor on target.

            List direct and indirect assignments for an actor, optionally
            for a given target (i.e. projects or domain).

            :param role_id: List for a specific role, can be None meaning all
                            roles
            :param inherited: Indicates whether inherited assignments or only
                              direct assignments are required.  If None, then
                              both are required.
            :param user_id: If not None, list only assignments that affect this
                            user.
            :param group_ids: A list of groups required. Only one of user_id
                              and group_ids can be specified
            :param project_id: If specified, only include those assignments
                               that affect at least this project, with
                               additionally any projects specified in
                               subtree_ids
            :param subtree_ids: The list of projects in the subtree. If
                                specified, also include those assignments that
                                affect these projects. These projects are
                                guaranteed to be in the same domain as the
                                project specified in project_id. subtree_ids
                                can only be specified if project_id has also
                                been specified.
            :param domain_id: If specified, only include those assignments
                              that affect this domain - by definition this will
                              not include any inherited assignments

            :returns: List of assignments matching the criteria. Any inherited
                      or group assignments that could affect the resulting
                      response are included.

            """
            project_ids_of_interest = None
            if project_id:
                if subtree_ids:
                    project_ids_of_interest = subtree_ids + [project_id]
                else:
                    project_ids_of_interest = [project_id]

            # List direct project role assignments
            non_inherited_refs = []
            if inherited is False or inherited is None:
                # Get non inherited assignments
                non_inherited_refs = self.driver.list_role_assignments(
                    role_id=role_id, domain_id=domain_id,
                    project_ids=project_ids_of_interest, user_id=user_id,
                    group_ids=group_ids, inherited_to_projects=False)

            inherited_refs = []
            if inherited is True or inherited is None:
                # Get inherited assignments
                if project_id:
                    # The project and any subtree are guaranteed to be owned by
                    # the same domain, so since we are filtering by these
                    # specific projects, then we can only get inherited
                    # assignments from their common domain or from any of
                    # their parents projects.

                    # List inherited assignments from the project's domain
                    proj_domain_id = self.resource_api.get_project(
                        project_id)['domain_id']
                    inherited_refs += self.driver.list_role_assignments(
                        role_id=role_id, domain_id=proj_domain_id,
                        user_id=user_id, group_ids=group_ids,
                        inherited_to_projects=True)

                    # For inherited assignments from projects, since we know
                    # they are from the same tree the only places these can
                    # come from are from parents of the main project or
                    # inherited assignments on the project or subtree itself.
                    source_ids = [project['id'] for project in
                                  self.resource_api.list_project_parents(
                                      project_id)]
                    if subtree_ids:
                        source_ids += project_ids_of_interest
                    if source_ids:
                        inherited_refs += self.driver.list_role_assignments(
                            role_id=role_id, project_ids=source_ids,
                            user_id=user_id, group_ids=group_ids,
                            inherited_to_projects=True)
                else:
                    # List inherited assignments without filtering by target
                    inherited_refs = self.driver.list_role_assignments(
                        role_id=role_id, user_id=user_id, group_ids=group_ids,
                        inherited_to_projects=True)

            return non_inherited_refs + inherited_refs

        # If filtering by group or inherited domain assignment the list is
        # guaranteed to be empty
        if group_id or (domain_id and inherited):
            return []

        if user_id and source_from_group_ids:
            # You can't do both - and since source_from_group_ids is only used
            # internally, this must be a coding error by the caller.
            msg = _('Cannot list assignments sourced from groups and filtered '
                    'by user ID.')
            raise exception.UnexpectedError(msg)

        # If filtering by domain, then only non-inherited assignments are
        # relevant, since domains don't inherit assignments
        inherited = False if domain_id else inherited

        # List user or explicit group assignments.
        # Due to the need to expand implied roles, this call will skip
        # filtering by role_id and instead return the whole set of roles.
        # Matching on the specified role is performed at the end.
        direct_refs = list_role_assignments_for_actor(
            role_id=None, user_id=user_id, group_ids=source_from_group_ids,
            project_id=project_id, subtree_ids=subtree_ids,
            domain_id=domain_id, inherited=inherited)

        # And those from the user's groups, so long as we are not restricting
        # to a set of source groups (in which case we already got those
        # assignments in the direct listing above).
        group_refs = []
        if not source_from_group_ids and user_id:
            group_ids = self._get_group_ids_for_user_id(user_id)
            if group_ids:
                group_refs = list_role_assignments_for_actor(
                    role_id=None, project_id=project_id,
                    subtree_ids=subtree_ids, group_ids=group_ids,
                    domain_id=domain_id, inherited=inherited)

        # Expand grouping and inheritance on retrieved role assignments
        refs = []
        expand_groups = (source_from_group_ids is None)
        for ref in (direct_refs + group_refs):
            refs += self._expand_indirect_assignment(
                ref, user_id, project_id, subtree_ids, expand_groups)

        refs = self._add_implied_roles(refs)
        if role_id:
            refs = self._filter_by_role_id(role_id, refs)

        return refs

    def _list_direct_role_assignments(self, role_id, user_id, group_id,
                                      domain_id, project_id, subtree_ids,
                                      inherited):
        """List role assignments without applying expansion.

        Returns a list of direct role assignments, where their attributes match
        the provided filters. If subtree_ids is not None, then we also want to
        include all subtree_ids in the filter as well.

        """
        group_ids = [group_id] if group_id else None
        project_ids_of_interest = None
        if project_id:
            if subtree_ids:
                project_ids_of_interest = subtree_ids + [project_id]
            else:
                project_ids_of_interest = [project_id]

        return self.driver.list_role_assignments(
            role_id=role_id, user_id=user_id, group_ids=group_ids,
            domain_id=domain_id, project_ids=project_ids_of_interest,
            inherited_to_projects=inherited)

    def list_role_assignments(self, role_id=None, user_id=None, group_id=None,
                              domain_id=None, project_id=None,
                              include_subtree=False, inherited=None,
                              effective=None, include_names=False,
                              source_from_group_ids=None):
        """List role assignments, honoring effective mode and provided filters.

        Returns a list of role assignments, where their attributes match the
        provided filters (role_id, user_id, group_id, domain_id, project_id and
        inherited). If include_subtree is True, then assignments on all
        descendants of the project specified by project_id are also included.
        The inherited filter defaults to None, meaning to get both
        non-inherited and inherited role assignments.

        If effective mode is specified, this means that rather than simply
        return the assignments that match the filters, any group or
        inheritance assignments will be expanded. Group assignments will
        become assignments for all the users in that group, and inherited
        assignments will be shown on the projects below the assignment point.
        Think of effective mode as being the list of assignments that actually
        affect a user, for example the roles that would be placed in a token.

        If include_names is set to true the entities' names are returned
        in addition to their id's.

        source_from_group_ids is a list of group IDs and, if specified, then
        only those assignments that are derived from membership of these groups
        are considered, and any such assignments will not be expanded into
        their user membership assignments. This is different to a group filter
        of the resulting list, instead being a restriction on which assignments
        should be considered before expansion of inheritance. This option is
        only used internally (i.e. it is not exposed at the API level) and is
        only supported in effective mode (since in regular mode there is no
        difference between this and a group filter, other than it is a list of
        groups).

        If OS-INHERIT extension is disabled or the used driver does not support
        inherited roles retrieval, inherited role assignments will be ignored.

        """
        if not CONF.os_inherit.enabled:
            if inherited:
                return []
            inherited = False

        subtree_ids = None
        if project_id and include_subtree:
            subtree_ids = (
                [x['id'] for x in
                    self.resource_api.list_projects_in_subtree(project_id)])

        if effective:
            role_assignments = self._list_effective_role_assignments(
                role_id, user_id, group_id, domain_id, project_id,
                subtree_ids, inherited, source_from_group_ids)
        else:
            role_assignments = self._list_direct_role_assignments(
                role_id, user_id, group_id, domain_id, project_id,
                subtree_ids, inherited)

        if include_names:
            return self._get_names_from_role_assignments(role_assignments)
        return role_assignments

    def _get_names_from_role_assignments(self, role_assignments):
        role_assign_list = []

        for role_asgmt in role_assignments:
            new_assign = {}
            for id_type, id_ in role_asgmt.items():
                if id_type == 'domain_id':
                    _domain = self.resource_api.get_domain(id_)
                    new_assign['domain_id'] = _domain['id']
                    new_assign['domain_name'] = _domain['name']
                elif id_type == 'user_id':
                    _user = self.identity_api.get_user(id_)
                    new_assign['user_id'] = _user['id']
                    new_assign['user_name'] = _user['name']
                    new_assign['user_domain_id'] = _user['domain_id']
                    new_assign['user_domain_name'] = (
                        self.resource_api.get_domain(_user['domain_id'])
                        ['name'])
                elif id_type == 'group_id':
                    _group = self.identity_api.get_group(id_)
                    new_assign['group_id'] = _group['id']
                    new_assign['group_name'] = _group['name']
                    new_assign['group_domain_id'] = _group['domain_id']
                    new_assign['group_domain_name'] = (
                        self.resource_api.get_domain(_group['domain_id'])
                        ['name'])
                elif id_type == 'project_id':
                    _project = self.resource_api.get_project(id_)
                    new_assign['project_id'] = _project['id']
                    new_assign['project_name'] = _project['name']
                    new_assign['project_domain_id'] = _project['domain_id']
                    new_assign['project_domain_name'] = (
                        self.resource_api.get_domain(_project['domain_id'])
                        ['name'])
                elif id_type == 'role_id':
                    _role = self.role_api.get_role(id_)
                    new_assign['role_id'] = _role['id']
                    new_assign['role_name'] = _role['name']
            role_assign_list.append(new_assign)
        return role_assign_list

    def delete_tokens_for_role_assignments(self, role_id):
        assignments = self.list_role_assignments(role_id=role_id)

        # Iterate over the assignments for this role and build the list of
        # user or user+project IDs for the tokens we need to delete
        user_ids = set()
        user_and_project_ids = list()
        for assignment in assignments:
            # If we have a project assignment, then record both the user and
            # project IDs so we can target the right token to delete. If it is
            # a domain assignment, we might as well kill all the tokens for
            # the user, since in the vast majority of cases all the tokens
            # for a user will be within one domain anyway, so not worth
            # trying to delete tokens for each project in the domain.
            if 'user_id' in assignment:
                if 'project_id' in assignment:
                    user_and_project_ids.append(
                        (assignment['user_id'], assignment['project_id']))
                elif 'domain_id' in assignment:
                    self._emit_invalidate_user_token_persistence(
                        assignment['user_id'])
            elif 'group_id' in assignment:
                # Add in any users for this group, being tolerant of any
                # cross-driver database integrity errors.
                try:
                    users = self.identity_api.list_users_in_group(
                        assignment['group_id'])
                except exception.GroupNotFound:
                    # Ignore it, but log a debug message
                    if 'project_id' in assignment:
                        target = _('Project (%s)') % assignment['project_id']
                    elif 'domain_id' in assignment:
                        target = _('Domain (%s)') % assignment['domain_id']
                    else:
                        target = _('Unknown Target')
                    msg = ('Group (%(group)s), referenced in assignment '
                           'for %(target)s, not found - ignoring.')
                    LOG.debug(msg, {'group': assignment['group_id'],
                                    'target': target})
                    continue

                if 'project_id' in assignment:
                    for user in users:
                        user_and_project_ids.append(
                            (user['id'], assignment['project_id']))
                elif 'domain_id' in assignment:
                    for user in users:
                        self._emit_invalidate_user_token_persistence(
                            user['id'])

        # Now process the built up lists.  Before issuing calls to delete any
        # tokens, let's try and minimize the number of calls by pruning out
        # any user+project deletions where a general token deletion for that
        # same user is also planned.
        user_and_project_ids_to_action = []
        for user_and_project_id in user_and_project_ids:
            if user_and_project_id[0] not in user_ids:
                user_and_project_ids_to_action.append(user_and_project_id)

        for user_id, project_id in user_and_project_ids_to_action:
            self._emit_invalidate_user_project_tokens_notification(
                {'user_id': user_id,
                 'project_id': project_id})

    @notifications.internal(
        notifications.INVALIDATE_USER_PROJECT_TOKEN_PERSISTENCE)
    def _emit_invalidate_user_project_tokens_notification(self, payload):
        # This notification's payload is a dict of user_id and
        # project_id so the token provider can invalidate the tokens
        # from persistence if persistence is enabled.
        pass


# The AssignmentDriverBase class is the set of driver methods from earlier
# drivers that we still support, that have not been removed or modified. This
# class is then used to created the augmented V8 and V9 version abstract driver
# classes, without having to duplicate a lot of abstract method signatures.
# If you remove a method from V9, then move the abstract methods from this Base
# class to the V8 class. Do not modify any of the method signatures in the Base
# class - changes should only be made in the V8 and subsequent classes.
@six.add_metaclass(abc.ABCMeta)
class AssignmentDriverBase(object):

    def _get_list_limit(self):
        return CONF.assignment.list_limit or CONF.list_limit

    @abc.abstractmethod
    def add_role_to_user_and_project(self, user_id, tenant_id, role_id):
        """Add a role to a user within given tenant.

        :raises keystone.exception.Conflict: If a duplicate role assignment
            exists.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def remove_role_from_user_and_project(self, user_id, tenant_id, role_id):
        """Remove a role from a user within given tenant.

        :raises keystone.exception.RoleNotFound: If the role doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    # assignment/grant crud

    @abc.abstractmethod
    def create_grant(self, role_id, user_id=None, group_id=None,
                     domain_id=None, project_id=None,
                     inherited_to_projects=False):
        """Creates a new assignment/grant.

        If the assignment is to a domain, then optionally it may be
        specified as inherited to owned projects (this requires
        the OS-INHERIT extension to be enabled).

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_grant_role_ids(self, user_id=None, group_id=None,
                            domain_id=None, project_id=None,
                            inherited_to_projects=False):
        """Lists role ids for assignments/grants."""
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def check_grant_role_id(self, role_id, user_id=None, group_id=None,
                            domain_id=None, project_id=None,
                            inherited_to_projects=False):
        """Checks an assignment/grant role id.

        :raises keystone.exception.RoleAssignmentNotFound: If the role
            assignment doesn't exist.
        :returns: None or raises an exception if grant not found

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_grant(self, role_id, user_id=None, group_id=None,
                     domain_id=None, project_id=None,
                     inherited_to_projects=False):
        """Deletes assignments/grants.

        :raises keystone.exception.RoleAssignmentNotFound: If the role
            assignment doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_role_assignments(self, role_id=None,
                              user_id=None, group_ids=None,
                              domain_id=None, project_ids=None,
                              inherited_to_projects=None):
        """Returns a list of role assignments for actors on targets.

        Available parameters represent values in which the returned role
        assignments attributes need to be filtered on.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_project_assignments(self, project_id):
        """Deletes all assignments for a project.

        :raises keystone.exception.ProjectNotFound: If the project doesn't
            exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_role_assignments(self, role_id):
        """Deletes all assignments for a role."""
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_user_assignments(self, user_id):
        """Deletes all assignments for a user.

        :raises keystone.exception.RoleNotFound: If the role doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_group_assignments(self, group_id):
        """Deletes all assignments for a group.

        :raises keystone.exception.RoleNotFound: If the role doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover


class AssignmentDriverV8(AssignmentDriverBase):
    """Removed or redefined methods from V8.

    Move the abstract methods of any methods removed or modified in later
    versions of the driver from AssignmentDriverBase to here. We maintain this
    so that legacy drivers, which will be a subclass of AssignmentDriverV8, can
    still reference them.

    """

    @abc.abstractmethod
    def list_user_ids_for_project(self, tenant_id):
        """Lists all user IDs with a role assignment in the specified project.

        :returns: a list of user_ids or an empty set.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_project_ids_for_user(self, user_id, group_ids, hints,
                                  inherited=False):
        """List all project ids associated with a given user.

        :param user_id: the user in question
        :param group_ids: the groups this user is a member of.  This list is
                          built in the Manager, so that the driver itself
                          does not have to call across to identity.
        :param hints: filter hints which the driver should
                      implement if at all possible.
        :param inherited: whether assignments marked as inherited should
                          be included.

        :returns: a list of project ids or an empty list.

        This method should not try and expand any inherited assignments,
        just report the projects that have the role for this user. The manager
        method is responsible for expanding out inherited assignments.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_domain_ids_for_user(self, user_id, group_ids, hints,
                                 inherited=False):
        """List all domain ids associated with a given user.

        :param user_id: the user in question
        :param group_ids: the groups this user is a member of.  This list is
                          built in the Manager, so that the driver itself
                          does not have to call across to identity.
        :param hints: filter hints which the driver should
                      implement if at all possible.
        :param inherited: whether to return domain_ids that have inherited
                          assignments or not.

        :returns: a list of domain ids or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_project_ids_for_groups(self, group_ids, hints,
                                    inherited=False):
        """List project ids accessible to specified groups.

        :param group_ids: List of group ids.
        :param hints: filter hints which the driver should
                      implement if at all possible.
        :param inherited: whether assignments marked as inherited should
                          be included.
        :returns: List of project ids accessible to specified groups.

        This method should not try and expand any inherited assignments,
        just report the projects that have the role for this group. The manager
        method is responsible for expanding out inherited assignments.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_domain_ids_for_groups(self, group_ids, inherited=False):
        """List domain ids accessible to specified groups.

        :param group_ids: List of group ids.
        :param inherited: whether to return domain_ids that have inherited
                          assignments or not.
        :returns: List of domain ids accessible to specified groups.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_role_ids_for_groups_on_project(
            self, group_ids, project_id, project_domain_id, project_parents):
        """List the group role ids for a specific project.

        Supports the ``OS-INHERIT`` role inheritance from the project's domain
        if supported by the assignment driver.

        :param group_ids: list of group ids
        :type group_ids: list
        :param project_id: project identifier
        :type project_id: str
        :param project_domain_id: project's domain identifier
        :type project_domain_id: str
        :param project_parents: list of parent ids of this project
        :type project_parents: list
        :returns: list of role ids for the project
        :rtype: list
        """
        raise exception.NotImplemented()

    @abc.abstractmethod
    def list_role_ids_for_groups_on_domain(self, group_ids, domain_id):
        """List the group role ids for a specific domain.

        :param group_ids: list of group ids
        :type group_ids: list
        :param domain_id: domain identifier
        :type domain_id: str
        :returns: list of role ids for the project
        :rtype: list
        """
        raise exception.NotImplemented()


class AssignmentDriverV9(AssignmentDriverBase):
    """New or redefined methods from V8.

    Add any new V9 abstract methods (or those with modified signatures) to
    this class.

    """

    pass


class V9AssignmentWrapperForV8Driver(AssignmentDriverV9):
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
        what='keystone.assignment.AssignmentDriverV8',
        in_favor_of='keystone.assignment.AssignmentDriverV9',
        remove_in=+2)
    def __init__(self, wrapped_driver):
        self.driver = wrapped_driver

    def default_role_driver(self):
        return self.driver.default_role_driver()

    def default_resource_driver(self):
        return self.driver.default_resource_driver()

    def add_role_to_user_and_project(self, user_id, tenant_id, role_id):
        self.driver.add_role_to_user_and_project(user_id, tenant_id, role_id)

    def remove_role_from_user_and_project(self, user_id, tenant_id, role_id):
        self.driver.remove_role_from_user_and_project(
            user_id, tenant_id, role_id)

    def create_grant(self, role_id, user_id=None, group_id=None,
                     domain_id=None, project_id=None,
                     inherited_to_projects=False):
        self.driver.create_grant(
            role_id, user_id=user_id, group_id=group_id,
            domain_id=domain_id, project_id=project_id,
            inherited_to_projects=inherited_to_projects)

    def list_grant_role_ids(self, user_id=None, group_id=None,
                            domain_id=None, project_id=None,
                            inherited_to_projects=False):
        return self.driver.list_grant_role_ids(
            user_id=user_id, group_id=group_id,
            domain_id=domain_id, project_id=project_id,
            inherited_to_projects=inherited_to_projects)

    def check_grant_role_id(self, role_id, user_id=None, group_id=None,
                            domain_id=None, project_id=None,
                            inherited_to_projects=False):
        self.driver.check_grant_role_id(
            role_id, user_id=user_id, group_id=group_id,
            domain_id=domain_id, project_id=project_id,
            inherited_to_projects=inherited_to_projects)

    def delete_grant(self, role_id, user_id=None, group_id=None,
                     domain_id=None, project_id=None,
                     inherited_to_projects=False):
        self.driver.delete_grant(
            role_id, user_id=user_id, group_id=group_id,
            domain_id=domain_id, project_id=project_id,
            inherited_to_projects=inherited_to_projects)

    def list_role_assignments(self, role_id=None,
                              user_id=None, group_ids=None,
                              domain_id=None, project_ids=None,
                              inherited_to_projects=None):
        return self.driver.list_role_assignments(
            role_id=role_id,
            user_id=user_id, group_ids=group_ids,
            domain_id=domain_id, project_ids=project_ids,
            inherited_to_projects=inherited_to_projects)

    def delete_project_assignments(self, project_id):
        self.driver.delete_project_assignments(project_id)

    def delete_role_assignments(self, role_id):
        self.driver.delete_role_assignments(role_id)

    def delete_user_assignments(self, user_id):
        self.driver.delete_user_assignments(user_id)

    def delete_group_assignments(self, group_id):
        self.driver.delete_group_assignments(group_id)


Driver = manager.create_legacy_driver(AssignmentDriverV8)


@dependency.provider('role_api')
@dependency.requires('assignment_api')
class RoleManager(manager.Manager):
    """Default pivot point for the Role backend."""

    driver_namespace = 'keystone.role'

    _ROLE = 'role'

    def __init__(self):
        # If there is a specific driver specified for role, then use it.
        # Otherwise retrieve the driver type from the assignment driver.
        role_driver = CONF.role.driver

        if role_driver is None:
            assignment_manager = dependency.get_provider('assignment_api')
            role_driver = assignment_manager.default_role_driver()

        super(RoleManager, self).__init__(role_driver)

        # Make sure it is a driver version we support, and if it is a legacy
        # driver, then wrap it.
        if isinstance(self.driver, RoleDriverV8):
            self.driver = V9RoleWrapperForV8Driver(self.driver)
        elif not isinstance(self.driver, RoleDriverV9):
            raise exception.UnsupportedDriverVersion(driver=role_driver)

    @MEMOIZE
    def get_role(self, role_id):
        return self.driver.get_role(role_id)

    def create_role(self, role_id, role, initiator=None):
        ret = self.driver.create_role(role_id, role)
        notifications.Audit.created(self._ROLE, role_id, initiator)
        if MEMOIZE.should_cache(ret):
            self.get_role.set(ret, self, role_id)
        return ret

    @manager.response_truncated
    def list_roles(self, hints=None):
        return self.driver.list_roles(hints or driver_hints.Hints())

    def update_role(self, role_id, role, initiator=None):
        ret = self.driver.update_role(role_id, role)
        notifications.Audit.updated(self._ROLE, role_id, initiator)
        self.get_role.invalidate(self, role_id)
        return ret

    def delete_role(self, role_id, initiator=None):
        self.assignment_api.delete_tokens_for_role_assignments(role_id)
        self.assignment_api.delete_role_assignments(role_id)
        self.driver.delete_role(role_id)
        notifications.Audit.deleted(self._ROLE, role_id, initiator)
        self.get_role.invalidate(self, role_id)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()

    # TODO(ayoung): Add notification
    def create_implied_role(self, prior_role_id, implied_role_id):
        implied_role = self.driver.get_role(implied_role_id)
        self.driver.get_role(prior_role_id)
        if implied_role['name'] == CONF.assignment.root_role:
            raise exception.InvalidImpliedRole(role_id=implied_role_id)
        response = self.driver.create_implied_role(
            prior_role_id, implied_role_id)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()
        return response

    def delete_implied_role(self, prior_role_id, implied_role_id):
        self.driver.delete_implied_role(prior_role_id, implied_role_id)
        COMPUTED_ASSIGNMENTS_REGION.invalidate()


# The RoleDriverBase class is the set of driver methods from earlier
# drivers that we still support, that have not been removed or modified. This
# class is then used to created the augmented V8 and V9 version abstract driver
# classes, without having to duplicate a lot of abstract method signatures.
# If you remove a method from V9, then move the abstract methods from this Base
# class to the V8 class. Do not modify any of the method signatures in the Base
# class - changes should only be made in the V8 and subsequent classes.
@six.add_metaclass(abc.ABCMeta)
class RoleDriverBase(object):

    def _get_list_limit(self):
        return CONF.role.list_limit or CONF.list_limit

    @abc.abstractmethod
    def create_role(self, role_id, role):
        """Creates a new role.

        :raises keystone.exception.Conflict: If a duplicate role exists.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_roles(self, hints):
        """List roles in the system.

        :param hints: filter hints which the driver should
                      implement if at all possible.

        :returns: a list of role_refs or an empty list.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_roles_from_ids(self, role_ids):
        """List roles for the provided list of ids.

        :param role_ids: list of ids

        :returns: a list of role_refs.

        This method is used internally by the assignment manager to bulk read
        a set of roles given their ids.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def get_role(self, role_id):
        """Get a role by ID.

        :returns: role_ref
        :raises keystone.exception.RoleNotFound: If the role doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def update_role(self, role_id, role):
        """Updates an existing role.

        :raises keystone.exception.RoleNotFound: If the role doesn't exist.
        :raises keystone.exception.Conflict: If a duplicate role exists.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_role(self, role_id):
        """Deletes an existing role.

        :raises keystone.exception.RoleNotFound: If the role doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover


class RoleDriverV8(RoleDriverBase):
    """Removed or redefined methods from V8.

    Move the abstract methods of any methods removed or modified in later
    versions of the driver from RoleDriverBase to here. We maintain this
    so that legacy drivers, which will be a subclass of RoleDriverV8, can
    still reference them.

    """

    pass


class RoleDriverV9(RoleDriverBase):
    """New or redefined methods from V8.

    Add any new V9 abstract methods (or those with modified signatures) to
    this class.

    """

    @abc.abstractmethod
    def get_implied_role(self, prior_role_id, implied_role_id):
        """Fetches a role inference rule

        :raises keystone.exception.ImpliedRoleNotFound: If the implied role
            doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def create_implied_role(self, prior_role_id, implied_role_id):
        """Creates a role inference rule

        :raises: keystone.exception.RoleNotFound: If the role doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def delete_implied_role(self, prior_role_id, implied_role_id):
        """Deletes a role inference rule

        :raises keystone.exception.ImpliedRoleNotFound: If the implied role
            doesn't exist.

        """
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_role_inference_rules(self):
        """Lists all the rules used to imply one role from another"""
        raise exception.NotImplemented()  # pragma: no cover

    @abc.abstractmethod
    def list_implied_roles(self, prior_role_id):
        """Lists roles implied from the prior role ID"""
        raise exception.NotImplemented()  # pragma: no cover


class V9RoleWrapperForV8Driver(RoleDriverV9):
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
        what='keystone.assignment.RoleDriverV8',
        in_favor_of='keystone.assignment.RoleDriverV9',
        remove_in=+2)
    def __init__(self, wrapped_driver):
        self.driver = wrapped_driver

    def create_role(self, role_id, role):
        return self.driver.create_role(role_id, role)

    def list_roles(self, hints):
        return self.driver.list_roles(hints)

    def list_roles_from_ids(self, role_ids):
        return self.driver.list_roles_from_ids(role_ids)

    def get_role(self, role_id):
        return self.driver.get_role(role_id)

    def update_role(self, role_id, role):
        return self.driver.update_role(role_id, role)

    def delete_role(self, role_id):
        self.driver.delete_role(role_id)

    def get_implied_role(self, prior_role_id, implied_role_id):
        raise exception.NotImplemented()  # pragma: no cover

    def create_implied_role(self, prior_role_id, implied_role_id):
        raise exception.NotImplemented()  # pragma: no cover

    def delete_implied_role(self, prior_role_id, implied_role_id):
        raise exception.NotImplemented()  # pragma: no cover

    def list_implied_roles(self, prior_role_id):
        raise exception.NotImplemented()  # pragma: no cover

    def list_role_inference_rules(self):
        raise exception.NotImplemented()  # pragma: no cover

RoleDriver = manager.create_legacy_driver(RoleDriverV8)
