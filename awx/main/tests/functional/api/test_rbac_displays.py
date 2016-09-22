import pytest

from django.core.urlresolvers import reverse
from django.test.client import RequestFactory

from awx.main.models.jobs import JobTemplate
from awx.main.models import Role, Group
from awx.main.access import (
    access_registry,
    get_user_capabilities
)
from awx.main.utils import cache_list_capabilities
from awx.api.serializers import JobTemplateSerializer

# This file covers special-cases of displays of user_capabilities
# general functionality should be covered fully by unit tests, see:
#   awx/main/tests/unit/api/test_serializers.py :: 
#           TestJobTemplateSerializerGetSummaryFields.test_copy_edit_standard
#   awx/main/tests/unit/test_access.py ::
#           test_user_capabilities_method


@pytest.mark.django_db
class TestOptionsRBAC:
    """
    Several endpoints are relied-upon by the UI to list POST as an
    allowed action or not depending on whether the user has permission
    to create a resource.
    """

    def test_inventory_group_host_can_add(self, inventory, alice, options):
        inventory.admin_role.members.add(alice)

        response = options(reverse('api:inventory_hosts_list', args=[inventory.pk]), alice)
        assert 'POST' in response.data['actions']
        response = options(reverse('api:inventory_groups_list', args=[inventory.pk]), alice)
        assert 'POST' in response.data['actions']

    def test_inventory_group_host_can_not_add(self, inventory, bob, options):
        inventory.read_role.members.add(bob)

        response = options(reverse('api:inventory_hosts_list', args=[inventory.pk]), bob)
        assert 'POST' not in response.data['actions']
        response = options(reverse('api:inventory_groups_list', args=[inventory.pk]), bob)
        assert 'POST' not in response.data['actions']

    def test_user_list_can_add(self, org_member, org_admin, options):
        response = options(reverse('api:user_list'), org_admin)
        assert 'POST' in response.data['actions']

    def test_user_list_can_not_add(self, org_member, org_admin, options):
        response = options(reverse('api:user_list'), org_member)
        assert 'POST' not in response.data['actions']


@pytest.mark.django_db
class TestJobTemplateCopyEdit:
    """
    Tests contain scenarios that were raised as issues in the past,
    which resulted from failed copy/edit actions even though the buttons
    to do these actions were displayed.
    """

    @pytest.fixture
    def jt_copy_edit(self, job_template_factory, project):
        objects = job_template_factory(
            'copy-edit-job-template',
            project=project)
        return objects.job_template

    def fake_context(self, user):
        request = RequestFactory().get('/api/v1/resource/42/')
        request.user = user

        class FakeView(object):
            pass

        fake_view = FakeView()
        fake_view.request = request
        context = {}
        context['view'] = fake_view
        context['request'] = request
        return context

    def test_validation_bad_data_copy_edit(self, admin_user, project):
        """
        If a required resource (inventory here) was deleted, copying not allowed
        because doing so would caues a validation error
        """

        jt_res = JobTemplate.objects.create(
            job_type='run',
            project=project,
            inventory=None,  ask_inventory_on_launch=False, # not allowed
            credential=None, ask_credential_on_launch=True,
            name='deploy-job-template'
        )
        serializer = JobTemplateSerializer(jt_res)
        serializer.context = self.fake_context(admin_user)
        response = serializer.to_representation(jt_res)
        assert not response['summary_fields']['user_capabilities']['copy']
        assert response['summary_fields']['user_capabilities']['edit']

    def test_sys_admin_copy_edit(self, jt_copy_edit, admin_user):
        "Absent a validation error, system admins can do everything"
        serializer = JobTemplateSerializer(jt_copy_edit)
        serializer.context = self.fake_context(admin_user)
        response = serializer.to_representation(jt_copy_edit)
        assert response['summary_fields']['user_capabilities']['copy']
        assert response['summary_fields']['user_capabilities']['edit']

    def test_org_admin_copy_edit(self, jt_copy_edit, org_admin):
        "Organization admins SHOULD be able to copy a JT firmly in their org"
        serializer = JobTemplateSerializer(jt_copy_edit)
        serializer.context = self.fake_context(org_admin)
        response = serializer.to_representation(jt_copy_edit)
        assert response['summary_fields']['user_capabilities']['copy']
        assert response['summary_fields']['user_capabilities']['edit']

    def test_org_admin_foreign_cred_no_copy_edit(self, jt_copy_edit, org_admin, machine_credential):
        """
        Organization admins without access to the 3 related resources:
        SHOULD NOT be able to copy JT
        SHOULD be able to edit that job template, for nonsensitive changes
        """

        # Attach credential to JT that org admin can not use
        jt_copy_edit.credential = machine_credential
        jt_copy_edit.save()

        serializer = JobTemplateSerializer(jt_copy_edit)
        serializer.context = self.fake_context(org_admin)
        response = serializer.to_representation(jt_copy_edit)
        assert not response['summary_fields']['user_capabilities']['copy']
        assert response['summary_fields']['user_capabilities']['edit']

    def test_jt_admin_copy_edit(self, jt_copy_edit, rando):
        """
        JT admins wihout access to associated resources SHOULD NOT be able to copy
        SHOULD be able to make nonsensitive changes"""

        # random user given JT admin access only
        jt_copy_edit.admin_role.members.add(rando)
        jt_copy_edit.save()

        serializer = JobTemplateSerializer(jt_copy_edit)
        serializer.context = self.fake_context(rando)
        response = serializer.to_representation(jt_copy_edit)
        assert not response['summary_fields']['user_capabilities']['copy']
        assert response['summary_fields']['user_capabilities']['edit']

    def test_proj_jt_admin_copy_edit(self, jt_copy_edit, rando):
        "JT admins with access to associated resources SHOULD be able to copy"

        # random user given JT and project admin abilities
        jt_copy_edit.admin_role.members.add(rando)
        jt_copy_edit.save()
        jt_copy_edit.project.admin_role.members.add(rando)
        jt_copy_edit.project.save()

        serializer = JobTemplateSerializer(jt_copy_edit)
        serializer.context = self.fake_context(rando)
        response = serializer.to_representation(jt_copy_edit)
        assert response['summary_fields']['user_capabilities']['copy']
        assert response['summary_fields']['user_capabilities']['edit']


@pytest.fixture
def mock_access_method(mocker):
    mock_method = mocker.MagicMock()
    mock_method.return_value = 'foobar'
    mock_method.__name__ = 'bars' # Required for a logging statement
    return mock_method

@pytest.mark.django_db
class TestAccessListCapabilities:
    """
    Test that the access_list serializer shows the exact output of the RoleAccess.can_attach
     - looks at /api/v1/inventories/N/access_list/
     - test for types: direct, indirect, and team access
    """

    extra_kwargs = dict(skip_sub_obj_read_check=False, data={})

    def _assert_one_in_list(self, data, sublist='direct_access'):
        "Establish that exactly 1 type of access exists so we know the entry is the right one"
        assert len(data['results']) == 1
        assert len(data['results'][0]['summary_fields'][sublist]) == 1
    
    def test_access_list_direct_access_capability(
            self, inventory, rando, get, mocker, mock_access_method):
        inventory.admin_role.members.add(rando)

        with mocker.patch.object(access_registry[Role][0], 'can_unattach', mock_access_method):
            response = get(reverse('api:inventory_access_list', args=(inventory.id,)), rando)

        mock_access_method.assert_called_once_with(inventory.admin_role, rando, 'members', **self.extra_kwargs)
        self._assert_one_in_list(response.data)
        direct_access_list = response.data['results'][0]['summary_fields']['direct_access']
        assert direct_access_list[0]['role']['user_capabilities']['unattach'] == 'foobar'

    def test_access_list_indirect_access_capability(
            self, inventory, organization, org_admin, get, mocker, mock_access_method):
        with mocker.patch.object(access_registry[Role][0], 'can_unattach', mock_access_method):
            response = get(reverse('api:inventory_access_list', args=(inventory.id,)), org_admin)

        mock_access_method.assert_called_once_with(organization.admin_role, org_admin, 'members', **self.extra_kwargs)
        self._assert_one_in_list(response.data, sublist='indirect_access')
        indirect_access_list = response.data['results'][0]['summary_fields']['indirect_access']
        assert indirect_access_list[0]['role']['user_capabilities']['unattach'] == 'foobar'

    def test_access_list_team_direct_access_capability(
            self, inventory, team, team_member, get, mocker, mock_access_method):
        team.member_role.children.add(inventory.admin_role)

        with mocker.patch.object(access_registry[Role][0], 'can_unattach', mock_access_method):
            response = get(reverse('api:inventory_access_list', args=(inventory.id,)), team_member)

        mock_access_method.assert_called_once_with(inventory.admin_role, team.member_role, 'parents', **self.extra_kwargs)
        self._assert_one_in_list(response.data)
        direct_access_list = response.data['results'][0]['summary_fields']['direct_access']
        assert direct_access_list[0]['role']['user_capabilities']['unattach'] == 'foobar'


@pytest.mark.django_db
def test_team_roles_unattach(mocker, team, team_member, inventory, mock_access_method, get):
    team.member_role.children.add(inventory.admin_role)

    with mocker.patch.object(access_registry[Role][0], 'can_unattach', mock_access_method):
        response = get(reverse('api:team_roles_list', args=(team.id,)), team_member)

    # Did we assess whether team_member can remove team's permission to the inventory?
    mock_access_method.assert_called_once_with(
        inventory.admin_role, team.member_role, 'parents', skip_sub_obj_read_check=True, data={})
    assert response.data['results'][0]['summary_fields']['user_capabilities']['unattach'] == 'foobar'

@pytest.mark.django_db
def test_user_roles_unattach(mocker, organization, alice, bob, mock_access_method, get):
    # Add to same organization so that alice and bob can see each other
    organization.member_role.members.add(alice)
    organization.member_role.members.add(bob)

    with mocker.patch.object(access_registry[Role][0], 'can_unattach', mock_access_method):
        response = get(reverse('api:user_roles_list', args=(alice.id,)), bob)

    # Did we assess whether bob can remove alice's permission to the inventory?
    mock_access_method.assert_called_once_with(
        organization.member_role, alice, 'members', skip_sub_obj_read_check=True, data={})
    assert response.data['results'][0]['summary_fields']['user_capabilities']['unattach'] == 'foobar'

@pytest.mark.django_db
def test_team_roles_unattach_functional(team, team_member, inventory, get):
    team.member_role.children.add(inventory.admin_role)
    response = get(reverse('api:team_roles_list', args=(team.id,)), team_member)
    # Team member should be able to remove access to inventory, becauase
    # the inventory admin_role grants that ability
    assert response.data['results'][0]['summary_fields']['user_capabilities']['unattach']

@pytest.mark.django_db
def test_user_roles_unattach_functional(organization, alice, bob, get):
    organization.member_role.members.add(alice)
    organization.member_role.members.add(bob)
    response = get(reverse('api:user_roles_list', args=(alice.id,)), bob)
    # Org members can not revoke the membership of other members
    assert not response.data['results'][0]['summary_fields']['user_capabilities']['unattach']


@pytest.mark.django_db
def test_prefetch_jt_capabilities(job_template, rando):
    job_template.execute_role.members.add(rando)
    qs = JobTemplate.objects.all()
    cache_list_capabilities(qs, ['admin', 'execute'], JobTemplate, rando)
    assert qs[0].capabilities_cache == {'edit': False, 'start': True}

@pytest.mark.django_db
def test_prefetch_group_capabilities(group, rando):
    group.inventory.adhoc_role.members.add(rando)
    qs = Group.objects.all()
    cache_list_capabilities(qs, ['inventory.admin', 'inventory.adhoc'], Group, rando)
    assert qs[0].capabilities_cache == {'edit': False, 'adhoc': True}

@pytest.mark.django_db
def test_prefetch_jt_copy_capability(job_template, project, inventory, machine_credential, rando):
    job_template.project = project
    job_template.inventory = inventory
    job_template.credential = machine_credential
    job_template.save()

    qs = JobTemplate.objects.all()
    cache_list_capabilities(qs, [{'copy': [
        'project.use', 'inventory.use', 'credential.use',
        'cloud_credential.use', 'network_credential.use'
    ]}], JobTemplate, rando)
    assert qs[0].capabilities_cache == {'copy': False}

    project.use_role.members.add(rando)
    inventory.use_role.members.add(rando)
    machine_credential.use_role.members.add(rando)

    cache_list_capabilities(qs, [{'copy': [
        'project.use', 'inventory.use', 'credential.use',
        'cloud_credential.use', 'network_credential.use'
    ]}], JobTemplate, rando)
    assert qs[0].capabilities_cache == {'copy': True}

@pytest.mark.django_db
def test_group_update_capabilities_possible(group, inventory_source, admin_user):
    group.inventory_source = inventory_source
    group.save()

    capabilities = get_user_capabilities(admin_user, group, method_list=['start'])
    assert capabilities['start']

@pytest.mark.django_db
def test_group_update_capabilities_impossible(group, inventory_source, admin_user):
    inventory_source.source = ""
    inventory_source.save()
    group.inventory_source = inventory_source
    group.save()

    capabilities = get_user_capabilities(admin_user, group, method_list=['start'])
    assert not capabilities['start']

