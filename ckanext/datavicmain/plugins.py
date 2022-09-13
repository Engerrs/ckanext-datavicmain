# Plugins for ckanext-datavicmain
import time
import calendar
import logging
from six import text_type
import ckan.authz as authz

import ckan.model as model
import ckan.plugins as p
import ckan.plugins.toolkit as toolkit

from ckanext.syndicate.interfaces import ISyndicate, Profile

from ckanext.datavicmain import actions, helpers, validators, auth, auth_middleware, cli
from ckanext.datavicmain.syndication.odp import prepare_package_for_odp
from ckanext.datavicmain.syndication import listeners
import ckanext.datavicmain.utils as utils


config = toolkit.config
request = toolkit.request
get_action = toolkit.get_action
log = logging.getLogger(__name__)
workflow_enabled = False

# Conditionally import the the workflow extension helpers if workflow extension enabled in .ini
if "workflow" in config.get('ckan.plugins', False):
    from ckanext.workflow import helpers as workflow_helpers
    workflow_enabled = True


def parse_date(date_str):
    try:
        return calendar.timegm(time.strptime(date_str, "%Y-%m-%d"))
    except Exception as e:
        return None


def release_date(pkg_dict):
    """
    Copied from https://github.com/salsadigitalauorg/datavic_ckan_2.2/blob/develop/iar/src/ckanext-datavic/ckanext/datavic/plugin.py#L296
    :param pkg_dict:
    :return:
    """
    dates = []
    dates.append(pkg_dict['metadata_created'])
    for resource in pkg_dict['resources']:
        if 'release_date' in resource and resource['release_date'] != '' and resource['release_date'] != '1970-01-01':
            dates.append(resource['release_date'])
    dates.sort()
    return dates[0].split("T")[0]


class DatasetForm(p.SingletonPlugin, toolkit.DefaultDatasetForm):
    ''' A plugin that provides some metadata fields and
    overrides the default dataset form
    '''
    p.implements(p.ITemplateHelpers)
    p.implements(p.IConfigurer, inherit=True)
    p.implements(p.IPackageController, inherit=True)
    p.implements(p.IRoutes, inherit=True)
    p.implements(p.IActions)
    p.implements(p.IAuthFunctions)
    p.implements(p.IMiddleware, inherit=True)
    p.implements(p.IBlueprint)
    p.implements(p.IValidators)
    p.implements(p.IClick)
    p.implements(ISyndicate, inherit=True)

    def make_middleware(self, app, config):
        return auth_middleware.AuthMiddleware(app, config)

    # IBlueprint
    def get_blueprint(self):
        return helpers._register_blueprints()

    # IValidators
    def get_validators(self):
        return {
            'datavic_tag_string': validators.datavic_tag_string
        }

    # IAuthFunctions
    def get_auth_functions(self):
        return {
            'user_update': auth.datavic_user_update,
            'package_update': auth.datavic_package_update,
            'user_reset': auth.datavic_user_reset,
        }

    # IActions
    def get_actions(self):
        return {
            # DATAVICIAR-42: Override CKAN's core `user_create` method
            'user_create': actions.datavic_user_create
        }

    ## helper methods ##

    @classmethod
    def organization_list_objects(cls, org_names=[]):
        ''' Make a action-api call to fetch the a list of full dict objects (for each organization) '''
        context = {
            'model': model,
            'session': model.Session,
            'user': toolkit.g.user,
        }

        options = {'all_fields': True}
        if org_names and len(org_names):
            t = type(org_names[0])
            if t is str:
                options['organizations'] = org_names
            elif t is dict:
                options['organizations'] = map(lambda org: org.get('name'), org_names)

        return get_action('organization_list')(context, options)

    @classmethod
    def organization_dict_objects(cls, org_names=[]):
        ''' Similar to organization_list_objects but returns a dict keyed to the organization name. '''
        results = {}
        for org in cls.organization_list_objects(org_names):
            results[org['name']] = org
        return results

    @classmethod
    def is_admin(cls, owner_org):
        if workflow_enabled:
            user = toolkit.g.userobj
            if authz.is_sysadmin(user.name):
                return True
            else:
                role = workflow_helpers.role_in_org(owner_org, user.name)
                if role == 'admin':
                    return True

    def is_sysadmin(self):
        user = toolkit.g.user
        if authz.is_sysadmin(user):
            return True

    def historical_resources_list(self, resource_list):
        sorted_resource_list = {}
        i = 0
        for resource in resource_list:
            i += 1
            if resource.get('period_start') is not None and resource.get('period_start') != 'None' and resource.get(
                    'period_start') != '':
                key = parse_date(resource.get('period_start')[:10]) or '9999999999' + str(i)
            else:
                key = '9999999999' + str(i)
            resource['key'] = key
            # print parser.parse(resource.get('period_start')).strftime("%Y-%M-%d") + " " + resource.get('period_start')
            sorted_resource_list[key] = resource

        list = sorted(sorted_resource_list.values(), key=lambda item: int(item.get('key')), reverse=True)
        # for item in list:
        #    print item.get('period_start') + " " + str(item.get('key'))
        return list

    def historical_resources_range(resource_list):
        range_from = ""
        from_ts = None
        range_to = ""
        to_ts = None
        for resource in resource_list:

            if resource.get('period_start') is not None and resource.get('period_start') != 'None' and resource.get(
                    'period_start') != '':
                ts = parse_date(resource.get('period_start')[:10])
                if ts and (from_ts is None or ts < from_ts):
                    from_ts = ts
                    range_from = resource.get('period_start')[:10]
            if resource.get('period_end') is not None and resource.get('period_end') != 'None' and resource.get(
                    'period_end') != '':
                ts = parse_date(resource.get('period_end')[:10])
                if ts and (to_ts is None or ts > to_ts):
                    to_ts = ts
                    range_to = resource.get('period_end')[:10]

        if range_from != "" and range_to != "":
            return range_from + " to " + range_to
        elif range_from != "" or range_to != "":
            return range_from + range_to
        else:
            return None

    def is_historical(self):
        if toolkit.g.action == 'historical':
            return True

    def get_formats(self, limit=100):
        try:
            # Get any additional formats added in the admin settings
            additional_formats = [x.strip() for x in config.get('ckan.datavic.authorised_resource_formats', []).split(',')]
            q = request.GET.get('q', '')
            list_of_formats = [x.encode('utf-8') for x in
                               get_action('format_autocomplete')({}, {'q': q, 'limit': limit}) if x] + additional_formats
            list_of_formats = sorted(list(set(list_of_formats)))
            dict_of_formats = []
            for item in list_of_formats:
                if item == ' ' or item == '':
                    continue
                else:
                    dict_of_formats.append({'value': item.lower(), 'text': item.upper()})
            dict_of_formats.insert(0, {'value': '', 'text': 'Please select'})

        except Exception as e:
            return []
        else:
            return dict_of_formats

    def repopulate_user_role(self):
        if 'submit' in request.params:
            return request.params['role']
        else:
            return 'member'

    ## ITemplateHelpers interface ##

    def get_helpers(self):
        ''' Return a dict of named helper functions (as defined in the ITemplateHelpers interface).
        These helpers will be available under the 'h' thread-local global object.
        '''
        return {
            'organization_list_objects': self.organization_list_objects,
            'organization_dict_objects': self.organization_dict_objects,
            'dataset_extra_fields': helpers.dataset_fields,
            'resource_extra_fields': helpers.resource_fields,
            'workflow_status_options': helpers.workflow_status_options,
            'is_admin': self.is_admin,
            'workflow_status_pretty': helpers.workflow_status_pretty,
            'historical_resources_list': self.historical_resources_list,
            'historical_resources_range': self.historical_resources_range,
            'is_historical': self.is_historical,
            'get_formats': self.get_formats,
            'is_sysadmin': self.is_sysadmin,
            'repopulate_user_role': self.repopulate_user_role,
            'group_list': helpers.group_list,
            'autoselect_workflow_status_option': helpers.autoselect_workflow_status_option,
            'release_date': release_date,
            'is_dataset_harvested': helpers.is_dataset_harvested,
            'is_user_account_pending_review': helpers.is_user_account_pending_review,
            'option_value_to_label': helpers.option_value_to_label,
            'field_choices': helpers.field_choices,
            'user_org_can_upload': helpers.user_org_can_upload,
            'is_ready_for_publish': helpers.is_ready_for_publish,
        }

    ## IConfigurer interface ##
    def update_config_schema(self, schema):
        schema.update({
            'ckan.datavic.authorised_resource_formats': [
                toolkit.get_validator('ignore_missing'),
                text_type
            ],
            'ckan.datavic.request_access_review_emails': [
                toolkit.get_validator('ignore_missing'),
                text_type
            ]
        })

        return schema

    def update_config(self, config):
        ''' Setup the (fanstatic) resource library, public and template directory '''
        p.toolkit.add_public_directory(config, 'public')
        p.toolkit.add_template_directory(config, 'templates')
        p.toolkit.add_resource('public', 'ckanext-datavicmain')
        p.toolkit.add_resource('webassets', 'ckanext-datavicmain')
        p.toolkit.add_ckan_admin_tab(
            p.toolkit.config,
            'datavicmain.admin_report',
            'Admin Report',
            icon='user-o'
        )

    # IPackageController

    def after_create(self, context, pkg_dict):
        # Only add packages to groups when being created via the CKAN UI (i.e. not during harvesting)
        if repr(toolkit.request) != '<LocalProxy unbound>' and toolkit.get_endpoint()[0] in ['dataset', 'package']:
            # Add the package to the group ("category")
            pkg_group = pkg_dict.get('category', None)
            pkg_name = pkg_dict.get('name', None)
            pkg_type = pkg_dict.get('type', None)
            if pkg_group and pkg_type in ['dataset', 'package']:
                group = model.Group.get(pkg_group)
                group.add_package_by_name(pkg_name)
                # DATAVIC-251 - Create activity for private datasets
                helpers.set_private_activity(pkg_dict, context, str('new'))
        pass

    def after_update(self, context, pkg_dict):
        # Only add packages to groups when being updated via the CKAN UI (i.e. not during harvesting)
        if repr(toolkit.request) != '<LocalProxy unbound>' and toolkit.get_endpoint()[0] in ['dataset', 'package']:
            if 'type' in pkg_dict and pkg_dict['type'] in ['dataset', 'package']:
                helpers.add_package_to_group(pkg_dict, context)
                # DATAVIC-251 - Create activity for private datasets
                helpers.set_private_activity(pkg_dict, context, str('changed'))

    # IClick
    def get_commands(self):
        return cli.get_commands()

    # ISyndicate
    def _requires_public_removal(self, pkg: model.Package, profile: Profile) -> bool:
        """Decide, whether the package must be deleted from Discover.
        """
        is_syndicated = bool(pkg.extras.get(profile.field_id))
        is_deleted = pkg.state == "deleted"
        is_archived = pkg.extras.get("workflow_status") == "archived"
        return is_syndicated and (is_deleted or is_archived)


    def prepare_package_for_syndication(self, package_id, data_dict, profile):
        if profile.id == "odp":
            data_dict = prepare_package_for_odp(package_id, data_dict)

        pkg = model.Package.get(package_id)
        assert pkg, f"Cannot syndicate non-existing package {package_id}"

        if self._requires_public_removal(pkg, profile):
            data_dict["state"] = "deleted"

        return data_dict

    def skip_syndication(
        self, package: model.Package, profile: Profile
    ) -> bool:
        if self._requires_public_removal(package, profile):
            log.debug("Syndicate %s because it requires removal", package.id)
            return False

        if package.private:
            log.debug("Do not syndicate %s because it is private", package.id)
            return True

        if  'published' != package.extras.get("workflow_status"):
            log.debug("Do not syndicate %s because it is not published", package.id)
            return True

        return False
