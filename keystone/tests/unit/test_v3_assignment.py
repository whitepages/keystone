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

import random
import uuid

from oslo_config import cfg
from six.moves import http_client
from six.moves import range
from testtools import matchers

from keystone.tests import unit
from keystone.tests.unit import test_v3


CONF = cfg.CONF


class AssignmentTestCase(test_v3.RestfulTestCase,
                         test_v3.AssignmentTestMixin):
    """Test roles and role assignments."""

    def setUp(self):
        super(AssignmentTestCase, self).setUp()

        self.group = unit.new_group_ref(domain_id=self.domain_id)
        self.group = self.identity_api.create_group(self.group)
        self.group_id = self.group['id']

    # Role CRUD tests

    def test_create_role(self):
        """Call ``POST /roles``."""
        ref = unit.new_role_ref()
        r = self.post(
            '/roles',
            body={'role': ref})
        return self.assertValidRoleResponse(r, ref)

    def test_create_role_bad_request(self):
        """Call ``POST /roles``."""
        self.post('/roles', body={'role': {}},
                  expected_status=http_client.BAD_REQUEST)

    def test_list_roles(self):
        """Call ``GET /roles``."""
        resource_url = '/roles'
        r = self.get(resource_url)
        self.assertValidRoleListResponse(r, ref=self.role,
                                         resource_url=resource_url)

    def test_get_role(self):
        """Call ``GET /roles/{role_id}``."""
        r = self.get('/roles/%(role_id)s' % {
            'role_id': self.role_id})
        self.assertValidRoleResponse(r, self.role)

    def test_update_role(self):
        """Call ``PATCH /roles/{role_id}``."""
        ref = unit.new_role_ref()
        del ref['id']
        r = self.patch('/roles/%(role_id)s' % {
            'role_id': self.role_id},
            body={'role': ref})
        self.assertValidRoleResponse(r, ref)

    def test_delete_role(self):
        """Call ``DELETE /roles/{role_id}``."""
        self.delete('/roles/%(role_id)s' % {
            'role_id': self.role_id})

    def test_create_member_role(self):
        """Call ``POST /roles``."""
        # specify only the name on creation
        ref = unit.new_role_ref(name=CONF.member_role_name)
        r = self.post(
            '/roles',
            body={'role': ref})
        self.assertValidRoleResponse(r, ref)

        # but the ID should be set as defined in CONF
        self.assertEqual(CONF.member_role_id, r.json['role']['id'])

    # Role Grants tests

    def test_crud_user_project_role_grants(self):
        role = unit.new_role_ref()
        self.role_api.create_role(role['id'], role)

        collection_url = (
            '/projects/%(project_id)s/users/%(user_id)s/roles' % {
                'project_id': self.project['id'],
                'user_id': self.user['id']})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': role['id']}

        # There is a role assignment for self.user on self.project
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=self.role,
                                         expected_length=1)

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=role,
                                         resource_url=collection_url,
                                         expected_length=2)

        self.delete(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=self.role, expected_length=1)
        self.assertIn(collection_url, r.result['links']['self'])

    def test_crud_user_project_role_grants_no_user(self):
        """Grant role on a project to a user that doesn't exist.

        When grant a role on a project to a user that doesn't exist, the server
        returns Not Found for the user.

        """
        user_id = uuid.uuid4().hex

        collection_url = (
            '/projects/%(project_id)s/users/%(user_id)s/roles' % {
                'project_id': self.project['id'], 'user_id': user_id})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        self.put(member_url, expected_status=http_client.NOT_FOUND)

    def test_crud_user_domain_role_grants(self):
        collection_url = (
            '/domains/%(domain_id)s/users/%(user_id)s/roles' % {
                'domain_id': self.domain_id,
                'user_id': self.user['id']})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=self.role,
                                         resource_url=collection_url)

        self.delete(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, expected_length=0,
                                         resource_url=collection_url)

    def test_crud_user_domain_role_grants_no_user(self):
        """Grant role on a domain to a user that doesn't exist.

        When grant a role on a domain to a user that doesn't exist, the server
        returns 404 Not Found for the user.

        """
        user_id = uuid.uuid4().hex

        collection_url = (
            '/domains/%(domain_id)s/users/%(user_id)s/roles' % {
                'domain_id': self.domain_id, 'user_id': user_id})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        self.put(member_url, expected_status=http_client.NOT_FOUND)

    def test_crud_group_project_role_grants(self):
        collection_url = (
            '/projects/%(project_id)s/groups/%(group_id)s/roles' % {
                'project_id': self.project_id,
                'group_id': self.group_id})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=self.role,
                                         resource_url=collection_url)

        self.delete(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, expected_length=0,
                                         resource_url=collection_url)

    def test_crud_group_project_role_grants_no_group(self):
        """Grant role on a project to a group that doesn't exist.

        When grant a role on a project to a group that doesn't exist, the
        server returns 404 Not Found for the group.

        """
        group_id = uuid.uuid4().hex

        collection_url = (
            '/projects/%(project_id)s/groups/%(group_id)s/roles' % {
                'project_id': self.project_id,
                'group_id': group_id})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        self.put(member_url, expected_status=http_client.NOT_FOUND)

    def test_crud_group_domain_role_grants(self):
        collection_url = (
            '/domains/%(domain_id)s/groups/%(group_id)s/roles' % {
                'domain_id': self.domain_id,
                'group_id': self.group_id})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=self.role,
                                         resource_url=collection_url)

        self.delete(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, expected_length=0,
                                         resource_url=collection_url)

    def test_crud_group_domain_role_grants_no_group(self):
        """Grant role on a domain to a group that doesn't exist.

        When grant a role on a domain to a group that doesn't exist, the server
        returns 404 Not Found for the group.

        """
        group_id = uuid.uuid4().hex

        collection_url = (
            '/domains/%(domain_id)s/groups/%(group_id)s/roles' % {
                'domain_id': self.domain_id,
                'group_id': group_id})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        self.put(member_url, expected_status=http_client.NOT_FOUND)

    def _create_new_user_and_assign_role_on_project(self):
        """Create a new user and assign user a role on a project."""
        # Create a new user
        new_user = unit.new_user_ref(domain_id=self.domain_id)
        user_ref = self.identity_api.create_user(new_user)
        # Assign the user a role on the project
        collection_url = (
            '/projects/%(project_id)s/users/%(user_id)s/roles' % {
                'project_id': self.project_id,
                'user_id': user_ref['id']})
        member_url = ('%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id})
        self.put(member_url)
        # Check the user has the role assigned
        self.head(member_url)
        return member_url, user_ref

    def test_delete_user_before_removing_role_assignment_succeeds(self):
        """Call ``DELETE`` on the user before the role assignment."""
        member_url, user = self._create_new_user_and_assign_role_on_project()
        # Delete the user from identity backend
        self.identity_api.driver.delete_user(user['id'])
        # Clean up the role assignment
        self.delete(member_url)
        # Make sure the role is gone
        self.head(member_url, expected_status=http_client.NOT_FOUND)

    def test_delete_user_and_check_role_assignment_fails(self):
        """Call ``DELETE`` on the user and check the role assignment."""
        member_url, user = self._create_new_user_and_assign_role_on_project()
        # Delete the user from identity backend
        self.identity_api.delete_user(user['id'])
        # We should get a 404 Not Found when looking for the user in the
        # identity backend because we're not performing a delete operation on
        # the role.
        self.head(member_url, expected_status=http_client.NOT_FOUND)

    def test_token_revoked_once_group_role_grant_revoked(self):
        """Test token is revoked when group role grant is revoked

        When a role granted to a group is revoked for a given scope,
        all tokens related to this scope and belonging to one of the members
        of this group should be revoked.

        The revocation should be independently to the presence
        of the revoke API.
        """
        # creates grant from group on project.
        self.assignment_api.create_grant(role_id=self.role['id'],
                                         project_id=self.project['id'],
                                         group_id=self.group['id'])

        # adds user to the group.
        self.identity_api.add_user_to_group(user_id=self.user['id'],
                                            group_id=self.group['id'])

        # creates a token for the user
        auth_body = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=self.project['id'])
        token_resp = self.post('/auth/tokens', body=auth_body)
        token = token_resp.headers.get('x-subject-token')

        # validates the returned token; it should be valid.
        self.head('/auth/tokens',
                  headers={'x-subject-token': token},
                  expected_status=http_client.OK)

        # revokes the grant from group on project.
        self.assignment_api.delete_grant(role_id=self.role['id'],
                                         project_id=self.project['id'],
                                         group_id=self.group['id'])

        # validates the same token again; it should not longer be valid.
        self.head('/auth/tokens',
                  headers={'x-subject-token': token},
                  expected_status=http_client.NOT_FOUND)

    @unit.skip_if_cache_disabled('assignment')
    def test_delete_grant_from_user_and_project_invalidate_cache(self):
        # create a new project
        new_project = unit.new_project_ref(domain_id=self.domain_id)
        self.resource_api.create_project(new_project['id'], new_project)

        collection_url = (
            '/projects/%(project_id)s/users/%(user_id)s/roles' % {
                'project_id': new_project['id'],
                'user_id': self.user['id']})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        # create the user a grant on the new project
        self.put(member_url)

        # check the grant that was just created
        self.head(member_url)
        resp = self.get(collection_url)
        self.assertValidRoleListResponse(resp, ref=self.role,
                                         resource_url=collection_url)

        # delete the grant
        self.delete(member_url)

        # get the collection and ensure there are no roles on the project
        resp = self.get(collection_url)
        self.assertListEqual(resp.json_body['roles'], [])

    @unit.skip_if_cache_disabled('assignment')
    def test_delete_grant_from_user_and_domain_invalidates_cache(self):
        # create a new domain
        new_domain = unit.new_domain_ref()
        self.resource_api.create_domain(new_domain['id'], new_domain)

        collection_url = (
            '/domains/%(domain_id)s/users/%(user_id)s/roles' % {
                'domain_id': new_domain['id'],
                'user_id': self.user['id']})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        # create the user a grant on the new domain
        self.put(member_url)

        # check the grant that was just created
        self.head(member_url)
        resp = self.get(collection_url)
        self.assertValidRoleListResponse(resp, ref=self.role,
                                         resource_url=collection_url)

        # delete the grant
        self.delete(member_url)

        # get the collection and ensure there are no roles on the domain
        resp = self.get(collection_url)
        self.assertListEqual(resp.json_body['roles'], [])

    @unit.skip_if_cache_disabled('assignment')
    def test_delete_grant_from_group_and_project_invalidates_cache(self):
        # create a new project
        new_project = unit.new_project_ref(domain_id=self.domain_id)
        self.resource_api.create_project(new_project['id'], new_project)

        collection_url = (
            '/projects/%(project_id)s/groups/%(group_id)s/roles' % {
                'project_id': new_project['id'],
                'group_id': self.group['id']})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        # create the group a grant on the new project
        self.put(member_url)

        # check the grant that was just created
        self.head(member_url)
        resp = self.get(collection_url)
        self.assertValidRoleListResponse(resp, ref=self.role,
                                         resource_url=collection_url)

        # delete the grant
        self.delete(member_url)

        # get the collection and ensure there are no roles on the project
        resp = self.get(collection_url)
        self.assertListEqual(resp.json_body['roles'], [])

    @unit.skip_if_cache_disabled('assignment')
    def test_delete_grant_from_group_and_domain_invalidates_cache(self):
        # create a new domain
        new_domain = unit.new_domain_ref()
        self.resource_api.create_domain(new_domain['id'], new_domain)

        collection_url = (
            '/domains/%(domain_id)s/groups/%(group_id)s/roles' % {
                'domain_id': new_domain['id'],
                'group_id': self.group['id']})
        member_url = '%(collection_url)s/%(role_id)s' % {
            'collection_url': collection_url,
            'role_id': self.role_id}

        # create the group a grant on the new domain
        self.put(member_url)

        # check the grant that was just created
        self.head(member_url)
        resp = self.get(collection_url)
        self.assertValidRoleListResponse(resp, ref=self.role,
                                         resource_url=collection_url)

        # delete the grant
        self.delete(member_url)

        # get the collection and ensure there are no roles on the domain
        resp = self.get(collection_url)
        self.assertListEqual(resp.json_body['roles'], [])

    # Role Assignments tests

    def test_get_role_assignments(self):
        """Call ``GET /role_assignments``.

        The sample data set up already has a user, group and project
        that is part of self.domain. We use these plus a new user
        we create as our data set, making sure we ignore any
        role assignments that are already in existence.

        Since we don't yet support a first class entity for role
        assignments, we are only testing the LIST API.  To create
        and delete the role assignments we use the old grant APIs.

        Test Plan:

        - Create extra user for tests
        - Get a list of all existing role assignments
        - Add a new assignment for each of the four combinations, i.e.
          group+domain, user+domain, group+project, user+project, using
          the same role each time
        - Get a new list of all role assignments, checking these four new
          ones have been added
        - Then delete the four we added
        - Get a new list of all role assignments, checking the four have
          been removed

        """
        # Since the default fixtures already assign some roles to the
        # user it creates, we also need a new user that will not have any
        # existing assignments
        user1 = unit.new_user_ref(domain_id=self.domain['id'])
        user1 = self.identity_api.create_user(user1)

        collection_url = '/role_assignments'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   resource_url=collection_url)
        existing_assignments = len(r.result.get('role_assignments'))

        # Now add one of each of the four types of assignment, making sure
        # that we get them all back.
        gd_entity = self.build_role_assignment_entity(domain_id=self.domain_id,
                                                      group_id=self.group_id,
                                                      role_id=self.role_id)
        self.put(gd_entity['links']['assignment'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 1,
            resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, gd_entity)

        ud_entity = self.build_role_assignment_entity(domain_id=self.domain_id,
                                                      user_id=user1['id'],
                                                      role_id=self.role_id)
        self.put(ud_entity['links']['assignment'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 2,
            resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, ud_entity)

        gp_entity = self.build_role_assignment_entity(
            project_id=self.project_id, group_id=self.group_id,
            role_id=self.role_id)
        self.put(gp_entity['links']['assignment'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 3,
            resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, gp_entity)

        up_entity = self.build_role_assignment_entity(
            project_id=self.project_id, user_id=user1['id'],
            role_id=self.role_id)
        self.put(up_entity['links']['assignment'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 4,
            resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, up_entity)

        # Now delete the four we added and make sure they are removed
        # from the collection.

        self.delete(gd_entity['links']['assignment'])
        self.delete(ud_entity['links']['assignment'])
        self.delete(gp_entity['links']['assignment'])
        self.delete(up_entity['links']['assignment'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments,
            resource_url=collection_url)
        self.assertRoleAssignmentNotInListResponse(r, gd_entity)
        self.assertRoleAssignmentNotInListResponse(r, ud_entity)
        self.assertRoleAssignmentNotInListResponse(r, gp_entity)
        self.assertRoleAssignmentNotInListResponse(r, up_entity)

    def test_get_effective_role_assignments(self):
        """Call ``GET /role_assignments?effective``.

        Test Plan:

        - Create two extra user for tests
        - Add these users to a group
        - Add a role assignment for the group on a domain
        - Get a list of all role assignments, checking one has been added
        - Then get a list of all effective role assignments - the group
          assignment should have turned into assignments on the domain
          for each of the group members.

        """
        user1 = unit.create_user(self.identity_api,
                                 domain_id=self.domain['id'])
        user2 = unit.create_user(self.identity_api,
                                 domain_id=self.domain['id'])

        self.identity_api.add_user_to_group(user1['id'], self.group['id'])
        self.identity_api.add_user_to_group(user2['id'], self.group['id'])

        collection_url = '/role_assignments'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   resource_url=collection_url)
        existing_assignments = len(r.result.get('role_assignments'))

        gd_entity = self.build_role_assignment_entity(domain_id=self.domain_id,
                                                      group_id=self.group_id,
                                                      role_id=self.role_id)
        self.put(gd_entity['links']['assignment'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 1,
            resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, gd_entity)

        # Now re-read the collection asking for effective roles - this
        # should mean the group assignment is translated into the two
        # member user assignments
        collection_url = '/role_assignments?effective'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 2,
            resource_url=collection_url)
        ud_entity = self.build_role_assignment_entity(
            link=gd_entity['links']['assignment'], domain_id=self.domain_id,
            user_id=user1['id'], role_id=self.role_id)
        self.assertRoleAssignmentInListResponse(r, ud_entity)
        ud_entity = self.build_role_assignment_entity(
            link=gd_entity['links']['assignment'], domain_id=self.domain_id,
            user_id=user2['id'], role_id=self.role_id)
        self.assertRoleAssignmentInListResponse(r, ud_entity)

    def test_check_effective_values_for_role_assignments(self):
        """Call ``GET /role_assignments?effective=value``.

        Check the various ways of specifying the 'effective'
        query parameter.  If the 'effective' query parameter
        is included then this should always be treated as meaning 'True'
        unless it is specified as:

        {url}?effective=0

        This is by design to match the agreed way of handling
        policy checking on query/filter parameters.

        Test Plan:

        - Create two extra user for tests
        - Add these users to a group
        - Add a role assignment for the group on a domain
        - Get a list of all role assignments, checking one has been added
        - Then issue various request with different ways of defining
          the 'effective' query parameter. As we have tested the
          correctness of the data coming back when we get effective roles
          in other tests, here we just use the count of entities to
          know if we are getting effective roles or not

        """
        user1 = unit.create_user(self.identity_api,
                                 domain_id=self.domain['id'])
        user2 = unit.create_user(self.identity_api,
                                 domain_id=self.domain['id'])

        self.identity_api.add_user_to_group(user1['id'], self.group['id'])
        self.identity_api.add_user_to_group(user2['id'], self.group['id'])

        collection_url = '/role_assignments'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   resource_url=collection_url)
        existing_assignments = len(r.result.get('role_assignments'))

        gd_entity = self.build_role_assignment_entity(domain_id=self.domain_id,
                                                      group_id=self.group_id,
                                                      role_id=self.role_id)
        self.put(gd_entity['links']['assignment'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 1,
            resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, gd_entity)

        # Now re-read the collection asking for effective roles,
        # using the most common way of defining "effective'. This
        # should mean the group assignment is translated into the two
        # member user assignments
        collection_url = '/role_assignments?effective'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 2,
            resource_url=collection_url)
        # Now set 'effective' to false explicitly - should get
        # back the regular roles
        collection_url = '/role_assignments?effective=0'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 1,
            resource_url=collection_url)
        # Now try setting  'effective' to 'False' explicitly- this is
        # NOT supported as a way of setting a query or filter
        # parameter to false by design. Hence we should get back
        # effective roles.
        collection_url = '/role_assignments?effective=False'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 2,
            resource_url=collection_url)
        # Now set 'effective' to True explicitly
        collection_url = '/role_assignments?effective=True'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r,
            expected_length=existing_assignments + 2,
            resource_url=collection_url)

    def test_filtered_role_assignments(self):
        """Call ``GET /role_assignments?filters``.

        Test Plan:

        - Create extra users, group, role and project for tests
        - Make the following assignments:
          Give group1, role1 on project1 and domain
          Give user1, role2 on project1 and domain
          Make User1 a member of Group1
        - Test a series of single filter list calls, checking that
          the correct results are obtained
        - Test a multi-filtered list call
        - Test listing all effective roles for a given user
        - Test the equivalent of the list of roles in a project scoped
          token (all effective roles for a user on a project)

        """
        # Since the default fixtures already assign some roles to the
        # user it creates, we also need a new user that will not have any
        # existing assignments
        user1 = unit.create_user(self.identity_api,
                                 domain_id=self.domain['id'])
        user2 = unit.create_user(self.identity_api,
                                 domain_id=self.domain['id'])

        group1 = unit.new_group_ref(domain_id=self.domain['id'])
        group1 = self.identity_api.create_group(group1)
        self.identity_api.add_user_to_group(user1['id'], group1['id'])
        self.identity_api.add_user_to_group(user2['id'], group1['id'])
        project1 = unit.new_project_ref(domain_id=self.domain['id'])
        self.resource_api.create_project(project1['id'], project1)
        self.role1 = unit.new_role_ref()
        self.role_api.create_role(self.role1['id'], self.role1)
        self.role2 = unit.new_role_ref()
        self.role_api.create_role(self.role2['id'], self.role2)

        # Now add one of each of the four types of assignment

        gd_entity = self.build_role_assignment_entity(
            domain_id=self.domain_id, group_id=group1['id'],
            role_id=self.role1['id'])
        self.put(gd_entity['links']['assignment'])

        ud_entity = self.build_role_assignment_entity(domain_id=self.domain_id,
                                                      user_id=user1['id'],
                                                      role_id=self.role2['id'])
        self.put(ud_entity['links']['assignment'])

        gp_entity = self.build_role_assignment_entity(
            project_id=project1['id'],
            group_id=group1['id'],
            role_id=self.role1['id'])
        self.put(gp_entity['links']['assignment'])

        up_entity = self.build_role_assignment_entity(
            project_id=project1['id'],
            user_id=user1['id'],
            role_id=self.role2['id'])
        self.put(up_entity['links']['assignment'])

        # Now list by various filters to make sure we get back the right ones

        collection_url = ('/role_assignments?scope.project.id=%s' %
                          project1['id'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=2,
                                                   resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, up_entity)
        self.assertRoleAssignmentInListResponse(r, gp_entity)

        collection_url = ('/role_assignments?scope.domain.id=%s' %
                          self.domain['id'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=2,
                                                   resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, ud_entity)
        self.assertRoleAssignmentInListResponse(r, gd_entity)

        collection_url = '/role_assignments?user.id=%s' % user1['id']
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=2,
                                                   resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, up_entity)
        self.assertRoleAssignmentInListResponse(r, ud_entity)

        collection_url = '/role_assignments?group.id=%s' % group1['id']
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=2,
                                                   resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, gd_entity)
        self.assertRoleAssignmentInListResponse(r, gp_entity)

        collection_url = '/role_assignments?role.id=%s' % self.role1['id']
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=2,
                                                   resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, gd_entity)
        self.assertRoleAssignmentInListResponse(r, gp_entity)

        # Let's try combining two filers together....

        collection_url = (
            '/role_assignments?user.id=%(user_id)s'
            '&scope.project.id=%(project_id)s' % {
                'user_id': user1['id'],
                'project_id': project1['id']})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=1,
                                                   resource_url=collection_url)
        self.assertRoleAssignmentInListResponse(r, up_entity)

        # Now for a harder one - filter for user with effective
        # roles - this should return role assignment that were directly
        # assigned as well as by virtue of group membership

        collection_url = ('/role_assignments?effective&user.id=%s' %
                          user1['id'])
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=4,
                                                   resource_url=collection_url)
        # Should have the two direct roles...
        self.assertRoleAssignmentInListResponse(r, up_entity)
        self.assertRoleAssignmentInListResponse(r, ud_entity)
        # ...and the two via group membership...
        gp1_link = self.build_role_assignment_link(
            project_id=project1['id'],
            group_id=group1['id'],
            role_id=self.role1['id'])
        gd1_link = self.build_role_assignment_link(domain_id=self.domain_id,
                                                   group_id=group1['id'],
                                                   role_id=self.role1['id'])

        up1_entity = self.build_role_assignment_entity(
            link=gp1_link, project_id=project1['id'],
            user_id=user1['id'], role_id=self.role1['id'])
        ud1_entity = self.build_role_assignment_entity(
            link=gd1_link, domain_id=self.domain_id, user_id=user1['id'],
            role_id=self.role1['id'])
        self.assertRoleAssignmentInListResponse(r, up1_entity)
        self.assertRoleAssignmentInListResponse(r, ud1_entity)

        # ...and for the grand-daddy of them all, simulate the request
        # that would generate the list of effective roles in a project
        # scoped token.

        collection_url = (
            '/role_assignments?effective&user.id=%(user_id)s'
            '&scope.project.id=%(project_id)s' % {
                'user_id': user1['id'],
                'project_id': project1['id']})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=2,
                                                   resource_url=collection_url)
        # Should have one direct role and one from group membership...
        self.assertRoleAssignmentInListResponse(r, up_entity)
        self.assertRoleAssignmentInListResponse(r, up1_entity)


class RoleAssignmentBaseTestCase(test_v3.RestfulTestCase,
                                 test_v3.AssignmentTestMixin):
    """Base class for testing /v3/role_assignments API behavior."""

    MAX_HIERARCHY_BREADTH = 3
    MAX_HIERARCHY_DEPTH = CONF.max_project_tree_depth - 1

    def load_sample_data(self):
        """Creates sample data to be used on tests.

        Created data are i) a role and ii) a domain containing: a project
        hierarchy and 3 users within 3 groups.

        """
        def create_project_hierarchy(parent_id, depth):
            """Creates a random project hierarchy."""
            if depth == 0:
                return

            breadth = random.randint(1, self.MAX_HIERARCHY_BREADTH)

            subprojects = []
            for i in range(breadth):
                subprojects.append(unit.new_project_ref(
                    domain_id=self.domain_id, parent_id=parent_id))
                self.resource_api.create_project(subprojects[-1]['id'],
                                                 subprojects[-1])

            new_parent = subprojects[random.randint(0, breadth - 1)]
            create_project_hierarchy(new_parent['id'], depth - 1)

        super(RoleAssignmentBaseTestCase, self).load_sample_data()

        # Create a domain
        self.domain = unit.new_domain_ref()
        self.domain_id = self.domain['id']
        self.resource_api.create_domain(self.domain_id, self.domain)

        # Create a project hierarchy
        self.project = unit.new_project_ref(domain_id=self.domain_id)
        self.project_id = self.project['id']
        self.resource_api.create_project(self.project_id, self.project)

        # Create a random project hierarchy
        create_project_hierarchy(self.project_id,
                                 random.randint(1, self.MAX_HIERARCHY_DEPTH))

        # Create 3 users
        self.user_ids = []
        for i in range(3):
            user = unit.new_user_ref(domain_id=self.domain_id)
            user = self.identity_api.create_user(user)
            self.user_ids.append(user['id'])

        # Create 3 groups
        self.group_ids = []
        for i in range(3):
            group = unit.new_group_ref(domain_id=self.domain_id)
            group = self.identity_api.create_group(group)
            self.group_ids.append(group['id'])

            # Put 2 members on each group
            self.identity_api.add_user_to_group(user_id=self.user_ids[i],
                                                group_id=group['id'])
            self.identity_api.add_user_to_group(user_id=self.user_ids[i % 2],
                                                group_id=group['id'])

        self.assignment_api.create_grant(user_id=self.user_id,
                                         project_id=self.project_id,
                                         role_id=self.role_id)

        # Create a role
        self.role = unit.new_role_ref()
        self.role_id = self.role['id']
        self.role_api.create_role(self.role_id, self.role)

        # Set default user and group to be used on tests
        self.default_user_id = self.user_ids[0]
        self.default_group_id = self.group_ids[0]

    def get_role_assignments(self, expected_status=http_client.OK, **filters):
        """Returns the result from querying role assignment API + queried URL.

        Calls GET /v3/role_assignments?<params> and returns its result, where
        <params> is the HTTP query parameters form of effective option plus
        filters, if provided. Queried URL is returned as well.

        :returns: a tuple containing the list role assignments API response and
                  queried URL.

        """
        query_url = self._get_role_assignments_query_url(**filters)
        response = self.get(query_url, expected_status=expected_status)

        return (response, query_url)

    def _get_role_assignments_query_url(self, **filters):
        """Returns non-effective role assignments query URL from given filters.

        :param filters: query parameters are created with the provided filters
                        on role assignments attributes. Valid filters are:
                        role_id, domain_id, project_id, group_id, user_id and
                        inherited_to_projects.

        :returns: role assignments query URL.

        """
        return self.build_role_assignment_query_url(**filters)


class RoleAssignmentFailureTestCase(RoleAssignmentBaseTestCase):
    """Class for testing invalid query params on /v3/role_assignments API.

    Querying domain and project, or user and group results in a HTTP 400 Bad
    Request, since a role assignment must contain only a single pair of (actor,
    target). In addition, since filtering on role assignments applies only to
    the final result, effective mode cannot be combined with i) group or ii)
    domain and inherited, because it would always result in an empty list.

    """

    def test_get_role_assignments_by_domain_and_project(self):
        self.get_role_assignments(domain_id=self.domain_id,
                                  project_id=self.project_id,
                                  expected_status=http_client.BAD_REQUEST)

    def test_get_role_assignments_by_user_and_group(self):
        self.get_role_assignments(user_id=self.default_user_id,
                                  group_id=self.default_group_id,
                                  expected_status=http_client.BAD_REQUEST)

    def test_get_role_assignments_by_effective_and_inherited(self):
        self.config_fixture.config(group='os_inherit', enabled=True)

        self.get_role_assignments(domain_id=self.domain_id, effective=True,
                                  inherited_to_projects=True,
                                  expected_status=http_client.BAD_REQUEST)

    def test_get_role_assignments_by_effective_and_group(self):
        self.get_role_assignments(effective=True,
                                  group_id=self.default_group_id,
                                  expected_status=http_client.BAD_REQUEST)


class RoleAssignmentDirectTestCase(RoleAssignmentBaseTestCase):
    """Class for testing direct assignments on /v3/role_assignments API.

    Direct assignments on a domain or project have effect on them directly,
    instead of on their project hierarchy, i.e they are non-inherited. In
    addition, group direct assignments are not expanded to group's users.

    Tests on this class make assertions on the representation and API filtering
    of direct assignments.

    """

    def _test_get_role_assignments(self, **filters):
        """Generic filtering test method.

        According to the provided filters, this method:
        - creates a new role assignment;
        - asserts that list role assignments API reponds correctly;
        - deletes the created role assignment.

        :param filters: filters to be considered when listing role assignments.
                        Valid filters are: role_id, domain_id, project_id,
                        group_id, user_id and inherited_to_projects.

        """
        # Fills default assignment with provided filters
        test_assignment = self._set_default_assignment_attributes(**filters)

        # Create new role assignment for this test
        self.assignment_api.create_grant(**test_assignment)

        # Get expected role assignments
        expected_assignments = self._list_expected_role_assignments(
            **test_assignment)

        # Get role assignments from API
        response, query_url = self.get_role_assignments(**test_assignment)
        self.assertValidRoleAssignmentListResponse(response,
                                                   resource_url=query_url)
        self.assertEqual(len(expected_assignments),
                         len(response.result.get('role_assignments')))

        # Assert that expected role assignments were returned by the API call
        for assignment in expected_assignments:
            self.assertRoleAssignmentInListResponse(response, assignment)

        # Delete created role assignment
        self.assignment_api.delete_grant(**test_assignment)

    def _set_default_assignment_attributes(self, **attribs):
        """Inserts default values for missing attributes of role assignment.

        If no actor, target or role are provided, they will default to values
        from sample data.

        :param attribs: info from a role assignment entity. Valid attributes
                        are: role_id, domain_id, project_id, group_id, user_id
                        and inherited_to_projects.

        """
        if not any(target in attribs
                   for target in ('domain_id', 'projects_id')):
            attribs['project_id'] = self.project_id

        if not any(actor in attribs for actor in ('user_id', 'group_id')):
            attribs['user_id'] = self.default_user_id

        if 'role_id' not in attribs:
            attribs['role_id'] = self.role_id

        return attribs

    def _list_expected_role_assignments(self, **filters):
        """Given the filters, it returns expected direct role assignments.

        :param filters: filters that will be considered when listing role
                        assignments. Valid filters are: role_id, domain_id,
                        project_id, group_id, user_id and
                        inherited_to_projects.

        :returns: the list of the expected role assignments.

        """
        return [self.build_role_assignment_entity(**filters)]

    # Test cases below call the generic test method, providing different filter
    # combinations. Filters are provided as specified in the method name, after
    # 'by'. For example, test_get_role_assignments_by_project_user_and_role
    # calls the generic test method with project_id, user_id and role_id.

    def test_get_role_assignments_by_domain(self, **filters):
        self._test_get_role_assignments(domain_id=self.domain_id, **filters)

    def test_get_role_assignments_by_project(self, **filters):
        self._test_get_role_assignments(project_id=self.project_id, **filters)

    def test_get_role_assignments_by_user(self, **filters):
        self._test_get_role_assignments(user_id=self.default_user_id,
                                        **filters)

    def test_get_role_assignments_by_group(self, **filters):
        self._test_get_role_assignments(group_id=self.default_group_id,
                                        **filters)

    def test_get_role_assignments_by_role(self, **filters):
        self._test_get_role_assignments(role_id=self.role_id, **filters)

    def test_get_role_assignments_by_domain_and_user(self, **filters):
        self.test_get_role_assignments_by_domain(user_id=self.default_user_id,
                                                 **filters)

    def test_get_role_assignments_by_domain_and_group(self, **filters):
        self.test_get_role_assignments_by_domain(
            group_id=self.default_group_id, **filters)

    def test_get_role_assignments_by_project_and_user(self, **filters):
        self.test_get_role_assignments_by_project(user_id=self.default_user_id,
                                                  **filters)

    def test_get_role_assignments_by_project_and_group(self, **filters):
        self.test_get_role_assignments_by_project(
            group_id=self.default_group_id, **filters)

    def test_get_role_assignments_by_domain_user_and_role(self, **filters):
        self.test_get_role_assignments_by_domain_and_user(role_id=self.role_id,
                                                          **filters)

    def test_get_role_assignments_by_domain_group_and_role(self, **filters):
        self.test_get_role_assignments_by_domain_and_group(
            role_id=self.role_id, **filters)

    def test_get_role_assignments_by_project_user_and_role(self, **filters):
        self.test_get_role_assignments_by_project_and_user(
            role_id=self.role_id, **filters)

    def test_get_role_assignments_by_project_group_and_role(self, **filters):
        self.test_get_role_assignments_by_project_and_group(
            role_id=self.role_id, **filters)


class RoleAssignmentInheritedTestCase(RoleAssignmentDirectTestCase):
    """Class for testing inherited assignments on /v3/role_assignments API.

    Inherited assignments on a domain or project have no effect on them
    directly, but on the projects under them instead.

    Tests on this class do not make assertions on the effect of inherited
    assignments, but in their representation and API filtering.

    """

    def config_overrides(self):
        super(RoleAssignmentBaseTestCase, self).config_overrides()
        self.config_fixture.config(group='os_inherit', enabled=True)

    def _test_get_role_assignments(self, **filters):
        """Adds inherited_to_project filter to expected entity in tests."""
        super(RoleAssignmentInheritedTestCase,
              self)._test_get_role_assignments(inherited_to_projects=True,
                                               **filters)


class RoleAssignmentEffectiveTestCase(RoleAssignmentInheritedTestCase):
    """Class for testing inheritance effects on /v3/role_assignments API.

    Inherited assignments on a domain or project have no effect on them
    directly, but on the projects under them instead.

    Tests on this class make assertions on the effect of inherited assignments
    and API filtering.

    """

    def _get_role_assignments_query_url(self, **filters):
        """Returns effective role assignments query URL from given filters.

        For test methods in this class, effetive will always be true. As in
        effective mode, inherited_to_projects, group_id, domain_id and
        project_id will always be desconsidered from provided filters.

        :param filters: query parameters are created with the provided filters.
                        Valid filters are: role_id, domain_id, project_id,
                        group_id, user_id and inherited_to_projects.

        :returns: role assignments query URL.

        """
        query_filters = filters.copy()
        query_filters.pop('inherited_to_projects')

        query_filters.pop('group_id', None)
        query_filters.pop('domain_id', None)
        query_filters.pop('project_id', None)

        return self.build_role_assignment_query_url(effective=True,
                                                    **query_filters)

    def _list_expected_role_assignments(self, **filters):
        """Given the filters, it returns expected direct role assignments.

        :param filters: filters that will be considered when listing role
                        assignments. Valid filters are: role_id, domain_id,
                        project_id, group_id, user_id and
                        inherited_to_projects.

        :returns: the list of the expected role assignments.

        """
        # Get assignment link, to be put on 'links': {'assignment': link}
        assignment_link = self.build_role_assignment_link(**filters)

        # Expand group membership
        user_ids = [None]
        if filters.get('group_id'):
            user_ids = [user['id'] for user in
                        self.identity_api.list_users_in_group(
                            filters['group_id'])]
        else:
            user_ids = [self.default_user_id]

        # Expand role inheritance
        project_ids = [None]
        if filters.get('domain_id'):
            project_ids = [project['id'] for project in
                           self.resource_api.list_projects_in_domain(
                               filters.pop('domain_id'))]
        else:
            project_ids = [project['id'] for project in
                           self.resource_api.list_projects_in_subtree(
                               self.project_id)]

        # Compute expected role assignments
        assignments = []
        for project_id in project_ids:
            filters['project_id'] = project_id
            for user_id in user_ids:
                filters['user_id'] = user_id
                assignments.append(self.build_role_assignment_entity(
                    link=assignment_link, **filters))

        return assignments


class AssignmentInheritanceTestCase(test_v3.RestfulTestCase,
                                    test_v3.AssignmentTestMixin):
    """Test inheritance crud and its effects."""

    def config_overrides(self):
        super(AssignmentInheritanceTestCase, self).config_overrides()
        self.config_fixture.config(group='os_inherit', enabled=True)

    def test_get_token_from_inherited_user_domain_role_grants(self):
        # Create a new user to ensure that no grant is loaded from sample data
        user = unit.create_user(self.identity_api, domain_id=self.domain_id)

        # Define domain and project authentication data
        domain_auth_data = self.build_authentication_request(
            user_id=user['id'],
            password=user['password'],
            domain_id=self.domain_id)
        project_auth_data = self.build_authentication_request(
            user_id=user['id'],
            password=user['password'],
            project_id=self.project_id)

        # Check the user cannot get a domain nor a project token
        self.v3_create_token(domain_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # Grant non-inherited role for user on domain
        non_inher_ud_link = self.build_role_assignment_link(
            domain_id=self.domain_id, user_id=user['id'], role_id=self.role_id)
        self.put(non_inher_ud_link)

        # Check the user can get only a domain token
        self.v3_create_token(domain_auth_data)
        self.v3_create_token(project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # Create inherited role
        inherited_role = unit.new_role_ref(name='inherited')
        self.role_api.create_role(inherited_role['id'], inherited_role)

        # Grant inherited role for user on domain
        inher_ud_link = self.build_role_assignment_link(
            domain_id=self.domain_id, user_id=user['id'],
            role_id=inherited_role['id'], inherited_to_projects=True)
        self.put(inher_ud_link)

        # Check the user can get both a domain and a project token
        self.v3_create_token(domain_auth_data)
        self.v3_create_token(project_auth_data)

        # Delete inherited grant
        self.delete(inher_ud_link)

        # Check the user can only get a domain token
        self.v3_create_token(domain_auth_data)
        self.v3_create_token(project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # Delete non-inherited grant
        self.delete(non_inher_ud_link)

        # Check the user cannot get a domain token anymore
        self.v3_create_token(domain_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_get_token_from_inherited_group_domain_role_grants(self):
        # Create a new group and put a new user in it to
        # ensure that no grant is loaded from sample data
        user = unit.create_user(self.identity_api, domain_id=self.domain_id)

        group = unit.new_group_ref(domain_id=self.domain['id'])
        group = self.identity_api.create_group(group)
        self.identity_api.add_user_to_group(user['id'], group['id'])

        # Define domain and project authentication data
        domain_auth_data = self.build_authentication_request(
            user_id=user['id'],
            password=user['password'],
            domain_id=self.domain_id)
        project_auth_data = self.build_authentication_request(
            user_id=user['id'],
            password=user['password'],
            project_id=self.project_id)

        # Check the user cannot get a domain nor a project token
        self.v3_create_token(domain_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # Grant non-inherited role for user on domain
        non_inher_gd_link = self.build_role_assignment_link(
            domain_id=self.domain_id, user_id=user['id'], role_id=self.role_id)
        self.put(non_inher_gd_link)

        # Check the user can get only a domain token
        self.v3_create_token(domain_auth_data)
        self.v3_create_token(project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # Create inherited role
        inherited_role = unit.new_role_ref(name='inherited')
        self.role_api.create_role(inherited_role['id'], inherited_role)

        # Grant inherited role for user on domain
        inher_gd_link = self.build_role_assignment_link(
            domain_id=self.domain_id, user_id=user['id'],
            role_id=inherited_role['id'], inherited_to_projects=True)
        self.put(inher_gd_link)

        # Check the user can get both a domain and a project token
        self.v3_create_token(domain_auth_data)
        self.v3_create_token(project_auth_data)

        # Delete inherited grant
        self.delete(inher_gd_link)

        # Check the user can only get a domain token
        self.v3_create_token(domain_auth_data)
        self.v3_create_token(project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # Delete non-inherited grant
        self.delete(non_inher_gd_link)

        # Check the user cannot get a domain token anymore
        self.v3_create_token(domain_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def _test_crud_inherited_and_direct_assignment_on_target(self, target_url):
        # Create a new role to avoid assignments loaded from sample data
        role = unit.new_role_ref()
        self.role_api.create_role(role['id'], role)

        # Define URLs
        direct_url = '%s/users/%s/roles/%s' % (
            target_url, self.user_id, role['id'])
        inherited_url = '/OS-INHERIT/%s/inherited_to_projects' % direct_url

        # Create the direct assignment
        self.put(direct_url)
        # Check the direct assignment exists, but the inherited one does not
        self.head(direct_url)
        self.head(inherited_url, expected_status=http_client.NOT_FOUND)

        # Now add the inherited assignment
        self.put(inherited_url)
        # Check both the direct and inherited assignment exist
        self.head(direct_url)
        self.head(inherited_url)

        # Delete indirect assignment
        self.delete(inherited_url)
        # Check the direct assignment exists, but the inherited one does not
        self.head(direct_url)
        self.head(inherited_url, expected_status=http_client.NOT_FOUND)

        # Now delete the inherited assignment
        self.delete(direct_url)
        # Check that none of them exist
        self.head(direct_url, expected_status=http_client.NOT_FOUND)
        self.head(inherited_url, expected_status=http_client.NOT_FOUND)

    def test_crud_inherited_and_direct_assignment_on_domains(self):
        self._test_crud_inherited_and_direct_assignment_on_target(
            '/domains/%s' % self.domain_id)

    def test_crud_inherited_and_direct_assignment_on_projects(self):
        self._test_crud_inherited_and_direct_assignment_on_target(
            '/projects/%s' % self.project_id)

    def test_crud_user_inherited_domain_role_grants(self):
        role_list = []
        for _ in range(2):
            role = unit.new_role_ref()
            self.role_api.create_role(role['id'], role)
            role_list.append(role)

        # Create a non-inherited role as a spoiler
        self.assignment_api.create_grant(
            role_list[1]['id'], user_id=self.user['id'],
            domain_id=self.domain_id)

        base_collection_url = (
            '/OS-INHERIT/domains/%(domain_id)s/users/%(user_id)s/roles' % {
                'domain_id': self.domain_id,
                'user_id': self.user['id']})
        member_url = '%(collection_url)s/%(role_id)s/inherited_to_projects' % {
            'collection_url': base_collection_url,
            'role_id': role_list[0]['id']}
        collection_url = base_collection_url + '/inherited_to_projects'

        self.put(member_url)

        # Check we can read it back
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=role_list[0],
                                         resource_url=collection_url)

        # Now delete and check its gone
        self.delete(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, expected_length=0,
                                         resource_url=collection_url)

    def test_list_role_assignments_for_inherited_domain_grants(self):
        """Call ``GET /role_assignments with inherited domain grants``.

        Test Plan:

        - Create 4 roles
        - Create a domain with a user and two projects
        - Assign two direct roles to project1
        - Assign a spoiler role to project2
        - Issue the URL to add inherited role to the domain
        - Issue the URL to check it is indeed on the domain
        - Issue the URL to check effective roles on project1 - this
          should return 3 roles.

        """
        role_list = []
        for _ in range(4):
            role = unit.new_role_ref()
            self.role_api.create_role(role['id'], role)
            role_list.append(role)

        domain = unit.new_domain_ref()
        self.resource_api.create_domain(domain['id'], domain)
        user1 = unit.create_user(self.identity_api, domain_id=domain['id'])
        project1 = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project1['id'], project1)
        project2 = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project2['id'], project2)
        # Add some roles to the project
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project1['id'], role_list[0]['id'])
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project1['id'], role_list[1]['id'])
        # ..and one on a different project as a spoiler
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project2['id'], role_list[2]['id'])

        # Now create our inherited role on the domain
        base_collection_url = (
            '/OS-INHERIT/domains/%(domain_id)s/users/%(user_id)s/roles' % {
                'domain_id': domain['id'],
                'user_id': user1['id']})
        member_url = '%(collection_url)s/%(role_id)s/inherited_to_projects' % {
            'collection_url': base_collection_url,
            'role_id': role_list[3]['id']}
        collection_url = base_collection_url + '/inherited_to_projects'

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=role_list[3],
                                         resource_url=collection_url)

        # Now use the list domain role assignments api to check if this
        # is included
        collection_url = (
            '/role_assignments?user.id=%(user_id)s'
            '&scope.domain.id=%(domain_id)s' % {
                'user_id': user1['id'],
                'domain_id': domain['id']})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=1,
                                                   resource_url=collection_url)
        ud_entity = self.build_role_assignment_entity(
            domain_id=domain['id'], user_id=user1['id'],
            role_id=role_list[3]['id'], inherited_to_projects=True)
        self.assertRoleAssignmentInListResponse(r, ud_entity)

        # Now ask for effective list role assignments - the role should
        # turn into a project role, along with the two direct roles that are
        # on the project
        collection_url = (
            '/role_assignments?effective&user.id=%(user_id)s'
            '&scope.project.id=%(project_id)s' % {
                'user_id': user1['id'],
                'project_id': project1['id']})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=3,
                                                   resource_url=collection_url)
        # An effective role for an inherited role will be a project
        # entity, with a domain link to the inherited assignment
        ud_url = self.build_role_assignment_link(
            domain_id=domain['id'], user_id=user1['id'],
            role_id=role_list[3]['id'], inherited_to_projects=True)
        up_entity = self.build_role_assignment_entity(
            link=ud_url, project_id=project1['id'],
            user_id=user1['id'], role_id=role_list[3]['id'],
            inherited_to_projects=True)
        self.assertRoleAssignmentInListResponse(r, up_entity)

    def test_list_role_assignments_include_names(self):
        """Call ``GET /role_assignments with include names``.

        Test Plan:

        - Create a domain with a group and a user
        - Create a project with a group and a user

        """
        role1 = unit.new_role_ref()
        self.role_api.create_role(role1['id'], role1)
        user1 = unit.create_user(self.identity_api, domain_id=self.domain_id)
        group = unit.new_group_ref(domain_id=self.domain_id)
        group = self.identity_api.create_group(group)
        project1 = unit.new_project_ref(domain_id=self.domain_id)
        self.resource_api.create_project(project1['id'], project1)

        expected_entity1 = self.build_role_assignment_entity_include_names(
            role_ref=role1,
            project_ref=project1,
            user_ref=user1)
        self.put(expected_entity1['links']['assignment'])
        expected_entity2 = self.build_role_assignment_entity_include_names(
            role_ref=role1,
            domain_ref=self.domain,
            group_ref=group)
        self.put(expected_entity2['links']['assignment'])
        expected_entity3 = self.build_role_assignment_entity_include_names(
            role_ref=role1,
            domain_ref=self.domain,
            user_ref=user1)
        self.put(expected_entity3['links']['assignment'])
        expected_entity4 = self.build_role_assignment_entity_include_names(
            role_ref=role1,
            project_ref=project1,
            group_ref=group)
        self.put(expected_entity4['links']['assignment'])

        collection_url_domain = (
            '/role_assignments?include_names&scope.domain.id=%(domain_id)s' % {
                'domain_id': self.domain_id})
        rs_domain = self.get(collection_url_domain)
        collection_url_project = (
            '/role_assignments?include_names&'
            'scope.project.id=%(project_id)s' % {
                'project_id': project1['id']})
        rs_project = self.get(collection_url_project)
        collection_url_group = (
            '/role_assignments?include_names&group.id=%(group_id)s' % {
                'group_id': group['id']})
        rs_group = self.get(collection_url_group)
        collection_url_user = (
            '/role_assignments?include_names&user.id=%(user_id)s' % {
                'user_id': user1['id']})
        rs_user = self.get(collection_url_user)
        collection_url_role = (
            '/role_assignments?include_names&role.id=%(role_id)s' % {
                'role_id': role1['id']})
        rs_role = self.get(collection_url_role)
        # Make sure all entities were created successfully
        self.assertEqual(rs_domain.status_int, http_client.OK)
        self.assertEqual(rs_project.status_int, http_client.OK)
        self.assertEqual(rs_group.status_int, http_client.OK)
        self.assertEqual(rs_user.status_int, http_client.OK)
        # Make sure we can get back the correct number of entities
        self.assertValidRoleAssignmentListResponse(
            rs_domain,
            expected_length=2,
            resource_url=collection_url_domain)
        self.assertValidRoleAssignmentListResponse(
            rs_project,
            expected_length=2,
            resource_url=collection_url_project)
        self.assertValidRoleAssignmentListResponse(
            rs_group,
            expected_length=2,
            resource_url=collection_url_group)
        self.assertValidRoleAssignmentListResponse(
            rs_user,
            expected_length=2,
            resource_url=collection_url_user)
        self.assertValidRoleAssignmentListResponse(
            rs_role,
            expected_length=4,
            resource_url=collection_url_role)
        # Verify all types of entities have the correct format
        self.assertRoleAssignmentInListResponse(rs_domain, expected_entity2)
        self.assertRoleAssignmentInListResponse(rs_project, expected_entity1)
        self.assertRoleAssignmentInListResponse(rs_group, expected_entity4)
        self.assertRoleAssignmentInListResponse(rs_user, expected_entity3)
        self.assertRoleAssignmentInListResponse(rs_role, expected_entity1)

    def test_list_role_assignments_for_disabled_inheritance_extension(self):
        """Call ``GET /role_assignments with inherited domain grants``.

        Test Plan:

        - Issue the URL to add inherited role to the domain
        - Issue the URL to check effective roles on project include the
          inherited role
        - Disable the extension
        - Re-check the effective roles, proving the inherited role no longer
          shows up.

        """
        role_list = []
        for _ in range(4):
            role = unit.new_role_ref()
            self.role_api.create_role(role['id'], role)
            role_list.append(role)

        domain = unit.new_domain_ref()
        self.resource_api.create_domain(domain['id'], domain)
        user1 = unit.create_user(self.identity_api, domain_id=domain['id'])
        project1 = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project1['id'], project1)
        project2 = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project2['id'], project2)
        # Add some roles to the project
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project1['id'], role_list[0]['id'])
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project1['id'], role_list[1]['id'])
        # ..and one on a different project as a spoiler
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project2['id'], role_list[2]['id'])

        # Now create our inherited role on the domain
        base_collection_url = (
            '/OS-INHERIT/domains/%(domain_id)s/users/%(user_id)s/roles' % {
                'domain_id': domain['id'],
                'user_id': user1['id']})
        member_url = '%(collection_url)s/%(role_id)s/inherited_to_projects' % {
            'collection_url': base_collection_url,
            'role_id': role_list[3]['id']}
        collection_url = base_collection_url + '/inherited_to_projects'

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=role_list[3],
                                         resource_url=collection_url)

        # Get effective list role assignments - the role should
        # turn into a project role, along with the two direct roles that are
        # on the project
        collection_url = (
            '/role_assignments?effective&user.id=%(user_id)s'
            '&scope.project.id=%(project_id)s' % {
                'user_id': user1['id'],
                'project_id': project1['id']})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=3,
                                                   resource_url=collection_url)

        ud_url = self.build_role_assignment_link(
            domain_id=domain['id'], user_id=user1['id'],
            role_id=role_list[3]['id'], inherited_to_projects=True)
        up_entity = self.build_role_assignment_entity(
            link=ud_url, project_id=project1['id'],
            user_id=user1['id'], role_id=role_list[3]['id'],
            inherited_to_projects=True)

        self.assertRoleAssignmentInListResponse(r, up_entity)

        # Disable the extension and re-check the list, the role inherited
        # from the project should no longer show up
        self.config_fixture.config(group='os_inherit', enabled=False)
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=2,
                                                   resource_url=collection_url)

        self.assertRoleAssignmentNotInListResponse(r, up_entity)

    def test_list_role_assignments_for_inherited_group_domain_grants(self):
        """Call ``GET /role_assignments with inherited group domain grants``.

        Test Plan:

        - Create 4 roles
        - Create a domain with a user and two projects
        - Assign two direct roles to project1
        - Assign a spoiler role to project2
        - Issue the URL to add inherited role to the domain
        - Issue the URL to check it is indeed on the domain
        - Issue the URL to check effective roles on project1 - this
          should return 3 roles.

        """
        role_list = []
        for _ in range(4):
            role = unit.new_role_ref()
            self.role_api.create_role(role['id'], role)
            role_list.append(role)

        domain = unit.new_domain_ref()
        self.resource_api.create_domain(domain['id'], domain)
        user1 = unit.create_user(self.identity_api, domain_id=domain['id'])
        user2 = unit.create_user(self.identity_api, domain_id=domain['id'])
        group1 = unit.new_group_ref(domain_id=domain['id'])
        group1 = self.identity_api.create_group(group1)
        self.identity_api.add_user_to_group(user1['id'],
                                            group1['id'])
        self.identity_api.add_user_to_group(user2['id'],
                                            group1['id'])
        project1 = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project1['id'], project1)
        project2 = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project2['id'], project2)
        # Add some roles to the project
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project1['id'], role_list[0]['id'])
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project1['id'], role_list[1]['id'])
        # ..and one on a different project as a spoiler
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project2['id'], role_list[2]['id'])

        # Now create our inherited role on the domain
        base_collection_url = (
            '/OS-INHERIT/domains/%(domain_id)s/groups/%(group_id)s/roles' % {
                'domain_id': domain['id'],
                'group_id': group1['id']})
        member_url = '%(collection_url)s/%(role_id)s/inherited_to_projects' % {
            'collection_url': base_collection_url,
            'role_id': role_list[3]['id']}
        collection_url = base_collection_url + '/inherited_to_projects'

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=role_list[3],
                                         resource_url=collection_url)

        # Now use the list domain role assignments api to check if this
        # is included
        collection_url = (
            '/role_assignments?group.id=%(group_id)s'
            '&scope.domain.id=%(domain_id)s' % {
                'group_id': group1['id'],
                'domain_id': domain['id']})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=1,
                                                   resource_url=collection_url)
        gd_entity = self.build_role_assignment_entity(
            domain_id=domain['id'], group_id=group1['id'],
            role_id=role_list[3]['id'], inherited_to_projects=True)
        self.assertRoleAssignmentInListResponse(r, gd_entity)

        # Now ask for effective list role assignments - the role should
        # turn into a user project role, along with the two direct roles
        # that are on the project
        collection_url = (
            '/role_assignments?effective&user.id=%(user_id)s'
            '&scope.project.id=%(project_id)s' % {
                'user_id': user1['id'],
                'project_id': project1['id']})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=3,
                                                   resource_url=collection_url)
        # An effective role for an inherited role will be a project
        # entity, with a domain link to the inherited assignment
        up_entity = self.build_role_assignment_entity(
            link=gd_entity['links']['assignment'], project_id=project1['id'],
            user_id=user1['id'], role_id=role_list[3]['id'],
            inherited_to_projects=True)
        self.assertRoleAssignmentInListResponse(r, up_entity)

    def test_filtered_role_assignments_for_inherited_grants(self):
        """Call ``GET /role_assignments?scope.OS-INHERIT:inherited_to``.

        Test Plan:

        - Create 5 roles
        - Create a domain with a user, group and two projects
        - Assign three direct spoiler roles to projects
        - Issue the URL to add an inherited user role to the domain
        - Issue the URL to add an inherited group role to the domain
        - Issue the URL to filter by inherited roles - this should
          return just the 2 inherited roles.

        """
        role_list = []
        for _ in range(5):
            role = unit.new_role_ref()
            self.role_api.create_role(role['id'], role)
            role_list.append(role)

        domain = unit.new_domain_ref()
        self.resource_api.create_domain(domain['id'], domain)
        user1 = unit.create_user(self.identity_api, domain_id=domain['id'])
        group1 = unit.new_group_ref(domain_id=domain['id'])
        group1 = self.identity_api.create_group(group1)
        project1 = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project1['id'], project1)
        project2 = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project2['id'], project2)
        # Add some spoiler roles to the projects
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project1['id'], role_list[0]['id'])
        self.assignment_api.add_role_to_user_and_project(
            user1['id'], project2['id'], role_list[1]['id'])
        # Create a non-inherited role as a spoiler
        self.assignment_api.create_grant(
            role_list[2]['id'], user_id=user1['id'], domain_id=domain['id'])

        # Now create two inherited roles on the domain, one for a user
        # and one for a domain
        base_collection_url = (
            '/OS-INHERIT/domains/%(domain_id)s/users/%(user_id)s/roles' % {
                'domain_id': domain['id'],
                'user_id': user1['id']})
        member_url = '%(collection_url)s/%(role_id)s/inherited_to_projects' % {
            'collection_url': base_collection_url,
            'role_id': role_list[3]['id']}
        collection_url = base_collection_url + '/inherited_to_projects'

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=role_list[3],
                                         resource_url=collection_url)

        base_collection_url = (
            '/OS-INHERIT/domains/%(domain_id)s/groups/%(group_id)s/roles' % {
                'domain_id': domain['id'],
                'group_id': group1['id']})
        member_url = '%(collection_url)s/%(role_id)s/inherited_to_projects' % {
            'collection_url': base_collection_url,
            'role_id': role_list[4]['id']}
        collection_url = base_collection_url + '/inherited_to_projects'

        self.put(member_url)
        self.head(member_url)
        r = self.get(collection_url)
        self.assertValidRoleListResponse(r, ref=role_list[4],
                                         resource_url=collection_url)

        # Now use the list role assignments api to get a list of inherited
        # roles on the domain - should get back the two roles
        collection_url = (
            '/role_assignments?scope.OS-INHERIT:inherited_to=projects')
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   expected_length=2,
                                                   resource_url=collection_url)
        ud_entity = self.build_role_assignment_entity(
            domain_id=domain['id'], user_id=user1['id'],
            role_id=role_list[3]['id'], inherited_to_projects=True)
        gd_entity = self.build_role_assignment_entity(
            domain_id=domain['id'], group_id=group1['id'],
            role_id=role_list[4]['id'], inherited_to_projects=True)
        self.assertRoleAssignmentInListResponse(r, ud_entity)
        self.assertRoleAssignmentInListResponse(r, gd_entity)

    def _setup_hierarchical_projects_scenario(self):
        """Creates basic hierarchical projects scenario.

        This basic scenario contains a root with one leaf project and
        two roles with the following names: non-inherited and inherited.

        """
        # Create project hierarchy
        root = unit.new_project_ref(domain_id=self.domain['id'])
        leaf = unit.new_project_ref(domain_id=self.domain['id'],
                                    parent_id=root['id'])

        self.resource_api.create_project(root['id'], root)
        self.resource_api.create_project(leaf['id'], leaf)

        # Create 'non-inherited' and 'inherited' roles
        non_inherited_role = unit.new_role_ref(name='non-inherited')
        self.role_api.create_role(non_inherited_role['id'], non_inherited_role)
        inherited_role = unit.new_role_ref(name='inherited')
        self.role_api.create_role(inherited_role['id'], inherited_role)

        return (root['id'], leaf['id'],
                non_inherited_role['id'], inherited_role['id'])

    def test_get_token_from_inherited_user_project_role_grants(self):
        # Create default scenario
        root_id, leaf_id, non_inherited_role_id, inherited_role_id = (
            self._setup_hierarchical_projects_scenario())

        # Define root and leaf projects authentication data
        root_project_auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=root_id)
        leaf_project_auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=leaf_id)

        # Check the user cannot get a token on root nor leaf project
        self.v3_create_token(root_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(leaf_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # Grant non-inherited role for user on leaf project
        non_inher_up_link = self.build_role_assignment_link(
            project_id=leaf_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_up_link)

        # Check the user can only get a token on leaf project
        self.v3_create_token(root_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(leaf_project_auth_data)

        # Grant inherited role for user on root project
        inher_up_link = self.build_role_assignment_link(
            project_id=root_id, user_id=self.user['id'],
            role_id=inherited_role_id, inherited_to_projects=True)
        self.put(inher_up_link)

        # Check the user still can get a token only on leaf project
        self.v3_create_token(root_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(leaf_project_auth_data)

        # Delete non-inherited grant
        self.delete(non_inher_up_link)

        # Check the inherited role still applies for leaf project
        self.v3_create_token(root_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(leaf_project_auth_data)

        # Delete inherited grant
        self.delete(inher_up_link)

        # Check the user cannot get a token on leaf project anymore
        self.v3_create_token(leaf_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_get_token_from_inherited_group_project_role_grants(self):
        # Create default scenario
        root_id, leaf_id, non_inherited_role_id, inherited_role_id = (
            self._setup_hierarchical_projects_scenario())

        # Create group and add user to it
        group = unit.new_group_ref(domain_id=self.domain['id'])
        group = self.identity_api.create_group(group)
        self.identity_api.add_user_to_group(self.user['id'], group['id'])

        # Define root and leaf projects authentication data
        root_project_auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=root_id)
        leaf_project_auth_data = self.build_authentication_request(
            user_id=self.user['id'],
            password=self.user['password'],
            project_id=leaf_id)

        # Check the user cannot get a token on root nor leaf project
        self.v3_create_token(root_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(leaf_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

        # Grant non-inherited role for group on leaf project
        non_inher_gp_link = self.build_role_assignment_link(
            project_id=leaf_id, group_id=group['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_gp_link)

        # Check the user can only get a token on leaf project
        self.v3_create_token(root_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(leaf_project_auth_data)

        # Grant inherited role for group on root project
        inher_gp_link = self.build_role_assignment_link(
            project_id=root_id, group_id=group['id'],
            role_id=inherited_role_id, inherited_to_projects=True)
        self.put(inher_gp_link)

        # Check the user still can get a token only on leaf project
        self.v3_create_token(root_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)
        self.v3_create_token(leaf_project_auth_data)

        # Delete no-inherited grant
        self.delete(non_inher_gp_link)

        # Check the inherited role still applies for leaf project
        self.v3_create_token(leaf_project_auth_data)

        # Delete inherited grant
        self.delete(inher_gp_link)

        # Check the user cannot get a token on leaf project anymore
        self.v3_create_token(leaf_project_auth_data,
                             expected_status=http_client.UNAUTHORIZED)

    def test_get_role_assignments_for_project_hierarchy(self):
        """Call ``GET /role_assignments``.

        Test Plan:

        - Create 2 roles
        - Create a hierarchy of projects with one root and one leaf project
        - Issue the URL to add a non-inherited user role to the root project
        - Issue the URL to add an inherited user role to the root project
        - Issue the URL to get all role assignments - this should return just
          2 roles (non-inherited and inherited) in the root project.

        """
        # Create default scenario
        root_id, leaf_id, non_inherited_role_id, inherited_role_id = (
            self._setup_hierarchical_projects_scenario())

        # Grant non-inherited role
        non_inher_up_entity = self.build_role_assignment_entity(
            project_id=root_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_up_entity['links']['assignment'])

        # Grant inherited role
        inher_up_entity = self.build_role_assignment_entity(
            project_id=root_id, user_id=self.user['id'],
            role_id=inherited_role_id, inherited_to_projects=True)
        self.put(inher_up_entity['links']['assignment'])

        # Get role assignments
        collection_url = '/role_assignments'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   resource_url=collection_url)

        # Assert that the user has non-inherited role on root project
        self.assertRoleAssignmentInListResponse(r, non_inher_up_entity)

        # Assert that the user has inherited role on root project
        self.assertRoleAssignmentInListResponse(r, inher_up_entity)

        # Assert that the user does not have non-inherited role on leaf project
        non_inher_up_entity = self.build_role_assignment_entity(
            project_id=leaf_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.assertRoleAssignmentNotInListResponse(r, non_inher_up_entity)

        # Assert that the user does not have inherited role on leaf project
        inher_up_entity['scope']['project']['id'] = leaf_id
        self.assertRoleAssignmentNotInListResponse(r, inher_up_entity)

    def test_get_effective_role_assignments_for_project_hierarchy(self):
        """Call ``GET /role_assignments?effective``.

        Test Plan:

        - Create 2 roles
        - Create a hierarchy of projects with one root and one leaf project
        - Issue the URL to add a non-inherited user role to the root project
        - Issue the URL to add an inherited user role to the root project
        - Issue the URL to get effective role assignments - this should return
          1 role (non-inherited) on the root project and 1 role (inherited) on
          the leaf project.

        """
        # Create default scenario
        root_id, leaf_id, non_inherited_role_id, inherited_role_id = (
            self._setup_hierarchical_projects_scenario())

        # Grant non-inherited role
        non_inher_up_entity = self.build_role_assignment_entity(
            project_id=root_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_up_entity['links']['assignment'])

        # Grant inherited role
        inher_up_entity = self.build_role_assignment_entity(
            project_id=root_id, user_id=self.user['id'],
            role_id=inherited_role_id, inherited_to_projects=True)
        self.put(inher_up_entity['links']['assignment'])

        # Get effective role assignments
        collection_url = '/role_assignments?effective'
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   resource_url=collection_url)

        # Assert that the user has non-inherited role on root project
        self.assertRoleAssignmentInListResponse(r, non_inher_up_entity)

        # Assert that the user does not have inherited role on root project
        self.assertRoleAssignmentNotInListResponse(r, inher_up_entity)

        # Assert that the user does not have non-inherited role on leaf project
        non_inher_up_entity = self.build_role_assignment_entity(
            project_id=leaf_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.assertRoleAssignmentNotInListResponse(r, non_inher_up_entity)

        # Assert that the user has inherited role on leaf project
        inher_up_entity['scope']['project']['id'] = leaf_id
        self.assertRoleAssignmentInListResponse(r, inher_up_entity)

    def test_project_id_specified_if_include_subtree_specified(self):
        """When using include_subtree, you must specify a project ID."""
        self.get('/role_assignments?include_subtree=True',
                 expected_status=http_client.BAD_REQUEST)
        self.get('/role_assignments?scope.project.id&'
                 'include_subtree=True',
                 expected_status=http_client.BAD_REQUEST)

    def test_get_role_assignments_for_project_tree(self):
        """Get role_assignment?scope.project.id=X?include_subtree``.

        Test Plan:

        - Create 2 roles and a hierarchy of projects with one root and one leaf
        - Issue the URL to add a non-inherited user role to the root project
          and the leaf project
        - Issue the URL to get role assignments for the root project but
          not the subtree - this should return just the root assignment
        - Issue the URL to get role assignments for the root project and
          it's subtree - this should return both assignments
        - Check that explicitly setting include_subtree to False is the
          equivalent to not including it at all in the query.

        """
        # Create default scenario
        root_id, leaf_id, non_inherited_role_id, unused_role_id = (
            self._setup_hierarchical_projects_scenario())

        # Grant non-inherited role to root and leaf projects
        non_inher_entity_root = self.build_role_assignment_entity(
            project_id=root_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_entity_root['links']['assignment'])
        non_inher_entity_leaf = self.build_role_assignment_entity(
            project_id=leaf_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_entity_leaf['links']['assignment'])

        # Without the subtree, we should get the one assignment on the
        # root project
        collection_url = (
            '/role_assignments?scope.project.id=%(project)s' % {
                'project': root_id})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r, resource_url=collection_url)

        self.assertThat(r.result['role_assignments'], matchers.HasLength(1))
        self.assertRoleAssignmentInListResponse(r, non_inher_entity_root)

        # With the subtree, we should get both assignments
        collection_url = (
            '/role_assignments?scope.project.id=%(project)s'
            '&include_subtree=True' % {
                'project': root_id})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r, resource_url=collection_url)

        self.assertThat(r.result['role_assignments'], matchers.HasLength(2))
        self.assertRoleAssignmentInListResponse(r, non_inher_entity_root)
        self.assertRoleAssignmentInListResponse(r, non_inher_entity_leaf)

        # With subtree=0, we should also only get the one assignment on the
        # root project
        collection_url = (
            '/role_assignments?scope.project.id=%(project)s'
            '&include_subtree=0' % {
                'project': root_id})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r, resource_url=collection_url)

        self.assertThat(r.result['role_assignments'], matchers.HasLength(1))
        self.assertRoleAssignmentInListResponse(r, non_inher_entity_root)

    def test_get_effective_role_assignments_for_project_tree(self):
        """Get role_assignment ?project_id=X?include_subtree=True?effective``.

        Test Plan:

        - Create 2 roles and a hierarchy of projects with one root and 4 levels
          of child project
        - Issue the URL to add a non-inherited user role to the root project
          and a level 1 project
        - Issue the URL to add an inherited user role on the level 2 project
        - Issue the URL to get effective role assignments for the level 1
          project and it's subtree - this should return a role (non-inherited)
          on the level 1 project and roles (inherited) on each of the level
          2, 3 and 4 projects

        """
        # Create default scenario
        root_id, leaf_id, non_inherited_role_id, inherited_role_id = (
            self._setup_hierarchical_projects_scenario())

        # Add some extra projects to the project hierarchy
        level2 = unit.new_project_ref(domain_id=self.domain['id'],
                                      parent_id=leaf_id)
        level3 = unit.new_project_ref(domain_id=self.domain['id'],
                                      parent_id=level2['id'])
        level4 = unit.new_project_ref(domain_id=self.domain['id'],
                                      parent_id=level3['id'])
        self.resource_api.create_project(level2['id'], level2)
        self.resource_api.create_project(level3['id'], level3)
        self.resource_api.create_project(level4['id'], level4)

        # Grant non-inherited role to root (as a spoiler) and to
        # the level 1 (leaf) project
        non_inher_entity_root = self.build_role_assignment_entity(
            project_id=root_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_entity_root['links']['assignment'])
        non_inher_entity_leaf = self.build_role_assignment_entity(
            project_id=leaf_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_entity_leaf['links']['assignment'])

        # Grant inherited role to level 2
        inher_entity = self.build_role_assignment_entity(
            project_id=level2['id'], user_id=self.user['id'],
            role_id=inherited_role_id, inherited_to_projects=True)
        self.put(inher_entity['links']['assignment'])

        # Get effective role assignments
        collection_url = (
            '/role_assignments?scope.project.id=%(project)s'
            '&include_subtree=True&effective' % {
                'project': leaf_id})
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(
            r, resource_url=collection_url)

        # There should be three assignments returned in total
        self.assertThat(r.result['role_assignments'], matchers.HasLength(3))

        # Assert that the user does not non-inherited role on root project
        self.assertRoleAssignmentNotInListResponse(r, non_inher_entity_root)

        # Assert that the user does have non-inherited role on leaf project
        self.assertRoleAssignmentInListResponse(r, non_inher_entity_leaf)

        # Assert that the user has inherited role on levels 3 and 4
        inher_entity['scope']['project']['id'] = level3['id']
        self.assertRoleAssignmentInListResponse(r, inher_entity)
        inher_entity['scope']['project']['id'] = level4['id']
        self.assertRoleAssignmentInListResponse(r, inher_entity)

    def test_get_inherited_role_assignments_for_project_hierarchy(self):
        """Call ``GET /role_assignments?scope.OS-INHERIT:inherited_to``.

        Test Plan:

        - Create 2 roles
        - Create a hierarchy of projects with one root and one leaf project
        - Issue the URL to add a non-inherited user role to the root project
        - Issue the URL to add an inherited user role to the root project
        - Issue the URL to filter inherited to projects role assignments - this
          should return 1 role (inherited) on the root project.

        """
        # Create default scenario
        root_id, leaf_id, non_inherited_role_id, inherited_role_id = (
            self._setup_hierarchical_projects_scenario())

        # Grant non-inherited role
        non_inher_up_entity = self.build_role_assignment_entity(
            project_id=root_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.put(non_inher_up_entity['links']['assignment'])

        # Grant inherited role
        inher_up_entity = self.build_role_assignment_entity(
            project_id=root_id, user_id=self.user['id'],
            role_id=inherited_role_id, inherited_to_projects=True)
        self.put(inher_up_entity['links']['assignment'])

        # Get inherited role assignments
        collection_url = ('/role_assignments'
                          '?scope.OS-INHERIT:inherited_to=projects')
        r = self.get(collection_url)
        self.assertValidRoleAssignmentListResponse(r,
                                                   resource_url=collection_url)

        # Assert that the user does not have non-inherited role on root project
        self.assertRoleAssignmentNotInListResponse(r, non_inher_up_entity)

        # Assert that the user has inherited role on root project
        self.assertRoleAssignmentInListResponse(r, inher_up_entity)

        # Assert that the user does not have non-inherited role on leaf project
        non_inher_up_entity = self.build_role_assignment_entity(
            project_id=leaf_id, user_id=self.user['id'],
            role_id=non_inherited_role_id)
        self.assertRoleAssignmentNotInListResponse(r, non_inher_up_entity)

        # Assert that the user does not have inherited role on leaf project
        inher_up_entity['scope']['project']['id'] = leaf_id
        self.assertRoleAssignmentNotInListResponse(r, inher_up_entity)


class AssignmentInheritanceDisabledTestCase(test_v3.RestfulTestCase):
    """Test inheritance crud and its effects."""

    def config_overrides(self):
        super(AssignmentInheritanceDisabledTestCase, self).config_overrides()
        self.config_fixture.config(group='os_inherit', enabled=False)

    def test_crud_inherited_role_grants_failed_if_disabled(self):
        role = unit.new_role_ref()
        self.role_api.create_role(role['id'], role)

        base_collection_url = (
            '/OS-INHERIT/domains/%(domain_id)s/users/%(user_id)s/roles' % {
                'domain_id': self.domain_id,
                'user_id': self.user['id']})
        member_url = '%(collection_url)s/%(role_id)s/inherited_to_projects' % {
            'collection_url': base_collection_url,
            'role_id': role['id']}
        collection_url = base_collection_url + '/inherited_to_projects'

        self.put(member_url, expected_status=http_client.NOT_FOUND)
        self.head(member_url, expected_status=http_client.NOT_FOUND)
        self.get(collection_url, expected_status=http_client.NOT_FOUND)
        self.delete(member_url, expected_status=http_client.NOT_FOUND)


class ImpliedRolesTests(test_v3.RestfulTestCase, test_v3.AssignmentTestMixin,
                        unit.TestCase):
    def _create_role(self):
        """Call ``POST /roles``."""
        ref = unit.new_role_ref()
        r = self.post('/roles', body={'role': ref})
        return self.assertValidRoleResponse(r, ref)

    def test_list_implied_roles_none(self):
        self.prior = self._create_role()
        url = '/roles/%s/implies' % (self.prior['id'])
        response = self.get(url).json["role_inference"]
        self.assertEqual(self.prior['id'], response['prior_role']['id'])
        self.assertEqual(0, len(response['implies']))

    def _create_implied_role(self, prior, implied):
        self.put('/roles/%s/implies/%s' % (prior['id'], implied['id']),
                 expected_status=http_client.CREATED)

    def _delete_implied_role(self, prior, implied):
        self.delete('/roles/%s/implies/%s' % (prior['id'], implied['id']))

    def _setup_prior_two_implied(self):
        self.prior = self._create_role()
        self.implied1 = self._create_role()
        self._create_implied_role(self.prior, self.implied1)
        self.implied2 = self._create_role()
        self._create_implied_role(self.prior, self.implied2)

    def _assert_expected_implied_role_response(
            self, expected_prior_id, expected_implied_ids):
        r = self.get('/roles/%s/implies' % expected_prior_id)
        response = r.json["role_inference"]
        self.assertEqual(expected_prior_id, response['prior_role']['id'])

        actual_implied_ids = [implied['id'] for implied in response['implies']]

        for expected_id in expected_implied_ids:
            self.assertIn(expected_id, actual_implied_ids)
        self.assertEqual(len(expected_implied_ids), len(response['implies']))

        self.assertIsNotNone(response['prior_role']['links']['self'])
        for implied in response['implies']:
            self.assertIsNotNone(implied['links']['self'])

    def _assert_two_roles_implied(self):
        self._assert_expected_implied_role_response(
            self.prior['id'], [self.implied1['id'], self.implied2['id']])

    def _assert_one_role_implied(self):
        self._assert_expected_implied_role_response(
            self.prior['id'], [self.implied1['id']])

        self.get('/roles/%s/implies/%s' %
                 (self.prior['id'], self.implied2['id']),
                 expected_status=http_client.NOT_FOUND)

    def _assert_two_rules_defined(self):
        r = self.get('/role_inferences/')

        rules = r.result['role_inferences']

        self.assertEqual(self.prior['id'], rules[0]['prior_role']['id'])
        self.assertEqual(2, len(rules[0]['implies']))
        implied_ids = [implied['id'] for implied in rules[0]['implies']]
        implied_names = [implied['name'] for implied in rules[0]['implies']]

        self.assertIn(self.implied1['id'], implied_ids)
        self.assertIn(self.implied2['id'], implied_ids)
        self.assertIn(self.implied1['name'], implied_names)
        self.assertIn(self.implied2['name'], implied_names)

    def _assert_one_rule_defined(self):
        r = self.get('/role_inferences/')
        rules = r.result['role_inferences']
        self.assertEqual(self.prior['id'], rules[0]['prior_role']['id'])
        self.assertEqual(self.implied1['id'], rules[0]['implies'][0]['id'])
        self.assertEqual(self.implied1['name'], rules[0]['implies'][0]['name'])
        self.assertEqual(1, len(rules[0]['implies']))

    def test_list_all_rules(self):
        self._setup_prior_two_implied()
        self._assert_two_rules_defined()

        self._delete_implied_role(self.prior, self.implied2)
        self._assert_one_rule_defined()

    def test_CRD_implied_roles(self):

        self._setup_prior_two_implied()
        self._assert_two_roles_implied()

        self._delete_implied_role(self.prior, self.implied2)
        self._assert_one_role_implied()

    def _create_three_roles(self):
        self.role_list = []
        for _ in range(3):
            role = unit.new_role_ref()
            self.role_api.create_role(role['id'], role)
            self.role_list.append(role)

    def _create_test_domain_user_project(self):
        domain = unit.new_domain_ref()
        self.resource_api.create_domain(domain['id'], domain)
        user = unit.create_user(self.identity_api, domain_id=domain['id'])
        project = unit.new_project_ref(domain_id=domain['id'])
        self.resource_api.create_project(project['id'], project)
        return domain, user, project

    def _assign_top_role_to_user_on_project(self, user, project):
        self.assignment_api.add_role_to_user_and_project(
            user['id'], project['id'], self.role_list[0]['id'])

    def _build_effective_role_assignments_url(self, user):
        return '/role_assignments?effective&user.id=%(user_id)s' % {
            'user_id': user['id']}

    def _assert_all_roles_in_assignment(self, response, user):
        # Now use the list role assignments api to check that all three roles
        # appear in the collection
        self.assertValidRoleAssignmentListResponse(
            response,
            expected_length=len(self.role_list),
            resource_url=self._build_effective_role_assignments_url(user))

    def _assert_initial_assignment_in_effective(self, response, user, project):
        # The initial assignment should be there (the link url will be
        # generated and checked automatically since it matches the assignment)
        entity = self.build_role_assignment_entity(
            project_id=project['id'],
            user_id=user['id'], role_id=self.role_list[0]['id'])
        self.assertRoleAssignmentInListResponse(response, entity)

    def _assert_effective_role_for_implied_has_prior_in_links(
            self, response, user, project, prior_index, implied_index):
        # An effective role for an implied role will have the prior role
        # assignment in the links
        prior_link = '/prior_roles/%(prior)s/implies/%(implied)s' % {
            'prior': self.role_list[prior_index]['id'],
            'implied': self.role_list[implied_index]['id']}
        link = self.build_role_assignment_link(
            project_id=project['id'], user_id=user['id'],
            role_id=self.role_list[prior_index]['id'])
        entity = self.build_role_assignment_entity(
            link=link, project_id=project['id'],
            user_id=user['id'], role_id=self.role_list[implied_index]['id'],
            prior_link=prior_link)
        self.assertRoleAssignmentInListResponse(response, entity)

    def test_list_role_assignments_with_implied_roles(self):
        """Call ``GET /role_assignments`` with implied role grant.

        Test Plan:
        - Create a domain with a user and a project
        - Create 3 roles
        - Role 0 implies role 1 and role 1 implies role 2
        - Assign the top role to the project
        - Issue the URL to check effective roles on project - this
          should return all 3 roles.
        - Check the links of the 3 roles indicate the prior role where
          appropriate

        """
        (domain, user, project) = self._create_test_domain_user_project()
        self._create_three_roles()
        self._create_implied_role(self.role_list[0], self.role_list[1])
        self._create_implied_role(self.role_list[1], self.role_list[2])
        self._assign_top_role_to_user_on_project(user, project)

        response = self.get(self._build_effective_role_assignments_url(user))
        r = response

        self._assert_all_roles_in_assignment(r, user)
        self._assert_initial_assignment_in_effective(response, user, project)
        self._assert_effective_role_for_implied_has_prior_in_links(
            response, user, project, 0, 1)
        self._assert_effective_role_for_implied_has_prior_in_links(
            response, user, project, 1, 2)

    def test_root_role_as_implied_role_forbidden(self):
        self.config_fixture.config(group='assignment', root_role='root')

        root_role = unit.new_role_ref()
        root_role['name'] = 'root'
        self.role_api.create_role(root_role['id'], root_role)
        prior = self._create_role()
        url = '/roles/%s/implies/%s' % (prior['id'], root_role['id'])
        self.put(url, expected_status=http_client.FORBIDDEN)
