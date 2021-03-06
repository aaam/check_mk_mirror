#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# ails.  You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

import config, defaults, hooks
from lib import *
import time, os, pprint, shutil, traceback
from valuespec import *

# Datastructures and functions needed before plugins can be loaded
loaded_with_language = False

# Custom user attributes
user_attributes  = {}
builtin_user_attribute_names = []
connection_dict = {}

# Load all userdb plugins
def load_plugins():
    global user_attributes
    global multisite_user_connectors
    global builtin_user_attribute_names

    # Do not cache the custom user attributes. They can be created by the user
    # during runtime, means they need to be loaded during each page request.
    # But delete the old definitions before to also apply removals of attributes
    if user_attributes:
        for attr_name in user_attributes.keys():
            if attr_name not in builtin_user_attribute_names:
                del user_attributes[attr_name]
        declare_custom_user_attrs()

    connection_dict.clear()
    for connection in config.user_connections:
        connection_dict[connection['id']] = connection

    global loaded_with_language
    if loaded_with_language == current_language:
        return

    # declare & initialize global vars
    user_attributes = {}
    multisite_user_connectors = {}

    load_web_plugins("userdb", globals())
    builtin_user_attribute_names = user_attributes.keys()
    declare_custom_user_attrs()

    # Connectors have the option to perform migration of configuration options
    # while the initial loading is performed
    for connector_class in multisite_user_connectors.values():
        connector_class.migrate_config()

    # This must be set after plugin loading to make broken plugins raise
    # exceptions all the time and not only the first time (when the plugins
    # are loaded).
    loaded_with_language = current_language


# Returns a list of two part tuples where the first element is the unique
# connection id and the second element the connector specification dict
def get_connections(only_enabled=False):
    connections = []
    for connector_id, connector_class in multisite_user_connectors.items():
        if connector_id == 'htpasswd':
            # htpasswd connector is enabled by default and always executed first
            connections.insert(0, ('htpasswd', connector_class({})))
        else:
            for connection_config in config.user_connections:
                if not only_enabled or not connection_config.get('disabled'):
                    connections.append((connection_config['id'], connector_class(connection_config)))
    return connections


def active_connections():
    return get_connections(only_enabled=True)


def cleanup_connection_id(connection_id):
    if connection_id is None:
        connection_id = 'htpasswd'

    # Old Check_MK used a static "ldap" connector id for all LDAP users.
    # Since Check_MK now supports multiple LDAP connections, the ID has
    # been changed to "default"
    if connection_id == 'ldap':
        connection_id = 'default'

    return connection_id


# Returns the connector dictionary of the given id
def get_connection(connection_id):
    return dict(get_connections()).get(connection_id)


# Returns a list of connection specific locked attributes
def locked_attributes(connection_id):
    return get_attributes(connection_id, "locked_attributes")


# Returns a list of connection specific multisite attributes
def multisite_attributes(connection_id):
    return get_attributes(connection_id, "multisite_attributes")


# Returns a list of connection specific non contact attributes
def non_contact_attributes(connection_id):
    return get_attributes(connection_id, "non_contact_attributes")


def get_attributes(connection_id, what):
    connection = get_connection(connection_id)
    if connection:
        return getattr(connection, what)()
    else:
        return []


def new_user_template(connection_id):
    new_user = {
        'serial':        0,
        'connector':     connection_id,
    }

    # Apply the default user profile
    new_user.update(config.default_user_profile)
    return new_user


def create_non_existing_user(connection_id, username):
    users = load_users(lock = True)
    if username in users:
        return # User exists. Nothing to do...

    users[username] = new_user_template(connection_id)
    save_users(users)

    # Call the sync function for this new user
    hook_sync(connection_id = connection_id, only_username = username)

# FIXME: Can we improve this easily? Would be nice not to have to call "load_users".
# Maybe a directory listing of profiles or a list of a small file would perform better
# than having to load the users, contacts etc. during each http request to multisite
def user_exists(username):
    return username in load_users().keys()

def user_locked(username):
    users = load_users()
    return users[username].get('locked', False)

def update_user_access_time(username):
    if not config.save_user_access_times:
        return
    save_custom_attr(username, 'last_seen', repr(time.time()))

def on_succeeded_login(username):
    num_failed = load_custom_attr(username, 'num_failed', saveint)
    if num_failed != None and num_failed != 0:
        save_custom_attr(username, 'num_failed', '0')

    update_user_access_time(username)

# userdb.need_to_change_pw returns either False or the reason description why the
# password needs to be changed
def need_to_change_pw(username):
    if load_custom_attr(username, 'enforce_pw_change', saveint) == 1:
        return 'enforced'

    last_pw_change = load_custom_attr(username, 'last_pw_change', saveint)
    max_pw_age = config.password_policy.get('max_age')
    if max_pw_age:
        if not last_pw_change:
            # The age of the password is unknown. Assume the user has just set
            # the password to have the first access after enabling password aging
            # as starting point for the password period. This bewares all users
            # from needing to set a new password after enabling aging.
            save_custom_attr(username, 'last_pw_change', str(int(time.time())))
            return False
        elif time.time() - last_pw_change > max_pw_age:
            return 'expired'
    return False

def on_failed_login(username):
    users = load_users(lock = True)
    if username in users:
        if "num_failed" in users[username]:
            users[username]["num_failed"] += 1
        else:
            users[username]["num_failed"] = 1

        if config.lock_on_logon_failures:
            if users[username]["num_failed"] >= config.lock_on_logon_failures:
                users[username]["locked"] = True

        save_users(users)

root_dir      = defaults.check_mk_configdir + "/wato/"
multisite_dir = defaults.default_config_dir + "/multisite.d/wato/"

#.
#   .-Users----------------------------------------------------------------.
#   |                       _   _                                          |
#   |                      | | | |___  ___ _ __ ___                        |
#   |                      | | | / __|/ _ \ '__/ __|                       |
#   |                      | |_| \__ \  __/ |  \__ \                       |
#   |                       \___/|___/\___|_|  |___/                       |
#   |                                                                      |
#   +----------------------------------------------------------------------+

def declare_user_attribute(name, vs, user_editable = True, permission = None,
                           show_in_table = False, topic = None, add_custom_macro = False,
                           domain = "multisite"):

    user_attributes[name] = {
        'valuespec'         : vs,
        'user_editable'     : user_editable,
        'show_in_table'     : show_in_table,
        'topic'             : topic and topic or 'personal',
        'add_custom_macro'  : add_custom_macro,
        'domain'            : domain,
    }

    # Permission needed for editing this attribute
    if permission:
        user_attributes[name]["permission"] = permission

def get_user_attributes():
    return user_attributes.items()

def load_users(lock = False):
    filename = root_dir + "contacts.mk"

    if lock:
        # Make sure that the file exists without modifying it, *if* it exists
        # to be able to lock and realease the file properly.
        # Note: the lock will be released on next save_users() call or at
        #       end of page request automatically.
        file(filename, "a")
        aquire_lock(filename)

    if html.is_cached('users'):
        return html.get_cached('users')

    # First load monitoring contacts from Check_MK's world. If this is
    # the first time, then the file will be empty, which is no problem.
    # Execfile will the simply leave contacts = {} unchanged.
    try:
        vars = { "contacts" : {} }
        execfile(filename, vars, vars)
        contacts = vars["contacts"]
    except IOError:
        contacts = {} # a not existing file is ok, start with empty data
    except Exception, e:
        if config.debug:
            raise MKGeneralException(_("Cannot read configuration file %s: %s" %
                          (filename, e)))
        else:
            logger(LOG_ERR, 'load_users: Problem while loading contacts (%s - %s). '
                     'Initializing structure...' % (filename, e))
        contacts = {}

    # Now add information about users from the Web world
    filename = multisite_dir + "users.mk"
    try:
        vars = { "multisite_users" : {} }
        execfile(filename, vars, vars)
        users = vars["multisite_users"]
    except IOError:
        users = {} # not existing is ok -> empty structure
    except Exception, e:
        if config.debug:
            raise MKGeneralException(_("Cannot read configuration file %s: %s" %
                          (filename, e)))
        else:
            logger(LOG_ERR, 'load_users: Problem while loading users (%s - %s). '
                     'Initializing structure...' % (filename, e))
        users = {}

    # Merge them together. Monitoring users not known to Multisite
    # will be added later as normal users.
    result = {}
    for id, user in users.items():
        profile = contacts.get(id, {})
        profile.update(user)
        result[id] = profile

        # Convert non unicode mail addresses
        if type(profile.get("email")) == str:
            profile["email"] = profile["email"].decode("utf-8")

    # This loop is only neccessary if someone has edited
    # contacts.mk manually. But we want to support that as
    # far as possible.
    for id, contact in contacts.items():
        if id not in result:
            result[id] = contact
            result[id]["roles"] = [ "user" ]
            result[id]["locked"] = True
            result[id]["password"] = ""

    # Passwords are read directly from the apache htpasswd-file.
    # That way heroes of the command line will still be able to
    # change passwords with htpasswd. Users *only* appearing
    # in htpasswd will also be loaded and assigned to the role
    # they are getting according to the multisite old-style
    # configuration variables.

    def readlines(f):
        try:
            return file(f)
        except IOError:
            return []

    # FIXME TODO: Consolidate with htpasswd user connector
    filename = defaults.htpasswd_file
    for line in readlines(filename):
        line = line.strip()
        if ':' in line:
            id, password = line.strip().split(":")[:2]
            id = id.decode("utf-8")
            if password.startswith("!"):
                locked = True
                password = password[1:]
            else:
                locked = False
            if id in result:
                result[id]["password"] = password
                result[id]["locked"] = locked
            else:
                # Create entry if this is an admin user
                new_user = {
                    "roles"    : config.roles_of_user(id),
                    "password" : password,
                    "locked"   : False,
                }
                result[id] = new_user
            # Make sure that the user has an alias
            result[id].setdefault("alias", id)
        # Other unknown entries will silently be dropped. Sorry...

    # Now read the serials, only process for existing users
    serials_file = '%s/auth.serials' % os.path.dirname(defaults.htpasswd_file)
    for line in readlines(serials_file):
        line = line.strip()
        if ':' in line:
            user_id, serial = line.split(':')[:2]
            user_id = user_id.decode("utf-8")
            if user_id in result:
                result[user_id]['serial'] = saveint(serial)

    # Now read the user specific files
    dir = defaults.var_dir + "/web/"
    for d in os.listdir(dir):
        if d[0] != '.':
            id = d.decode("utf-8")

            # read special values from own files
            if id in result:
                for attr, conv_func in [
                        ('num_failed',        saveint),
                        ('last_pw_change',    saveint),
                        ('last_seen',         savefloat),
                        ('enforce_pw_change', lambda x: bool(saveint(x))),
                    ]:
                    val = load_custom_attr(id, attr, conv_func)
                    if val != None:
                        result[id][attr] = val

            # read automation secrets and add them to existing
            # users or create new users automatically
            try:
                secret = file(dir + d + "/automation.secret").read().strip()
            except IOError:
                secret = None
            if secret:
                if id in result:
                    result[id]["automation_secret"] = secret
                else:
                    result[id] = {
                        "roles" : ["guest"],
                        "automation_secret" : secret,
                    }

    # populate the users cache
    html.set_cache('users', result)

    return result

def load_custom_attr(userid, key, conv_func, default = None):
    basedir = defaults.var_dir + "/web/" + make_utf8(userid)
    try:
        return conv_func(file(basedir + '/' + key + '.mk').read().strip())
    except IOError:
        return default

def save_custom_attr(userid, key, val):
    basedir = defaults.var_dir + "/web/" + make_utf8(userid)
    make_nagios_directory(basedir)
    create_user_file('%s/%s.mk' % (basedir, key), 'w').write('%s\n' % val)

def get_online_user_ids():
    online_threshold = time.time() - config.user_online_maxage
    users = []
    for user_id, user in load_users(lock = False).items():
        if user.get('last_seen', 0) >= online_threshold:
            users.append(user_id)
    return users

def split_dict(d, keylist, positive):
    return dict([(k,v) for (k,v) in d.items() if (k in keylist) == positive])

def save_users(profiles):

    # Add custom macros
    core_custom_macros =  [ k for k,o in user_attributes.items() if o.get('add_custom_macro') ]
    for user in profiles.keys():
        for macro in core_custom_macros:
            if profiles[user].get(macro):
                profiles[user]['_'+macro] = profiles[user][macro]

    multisite_custom_values = [ k for k,v in user_attributes.items() if v["domain"] == "multisite" ]

    # Keys not to put into contact definitions for Check_MK
    non_contact_keys = [
        "roles",
        "password",
        "locked",
        "automation_secret",
        "language",
        "serial",
        "connector",
        "num_failed",
        "enforce_pw_change",
        "last_pw_change",
        "last_seen",
    ] + multisite_custom_values

    # Keys to put into multisite configuration
    multisite_keys   = [
        "roles",
        "locked",
        "automation_secret",
        "alias",
        "language",
        "connector",
    ] + multisite_custom_values

    # Remove multisite keys in contacts.
    contacts = dict(
        e for e in
            [ (id, split_dict(user, non_contact_keys + non_contact_attributes(user.get('connector')), False))
               for (id, user)
               in profiles.items() ])

    # Only allow explicitely defined attributes to be written to multisite config
    users = {}
    for uid, profile in profiles.items():
        users[uid] = dict([ (p, val)
                            for p, val in profile.items()
                            if p in multisite_keys + multisite_attributes(profile.get('connector'))])


    # Check_MK's monitoring contacts
    filename = root_dir + "contacts.mk.new"
    out = create_user_file(filename, "w")
    out.write("# Written by Multisite UserDB\n# encoding: utf-8\n\n")
    out.write("contacts.update(\n%s\n)\n" % pprint.pformat(contacts))
    out.close()
    os.rename(filename, filename[:-4])

    # Users with passwords for Multisite
    filename = multisite_dir + "users.mk.new"
    make_nagios_directory(multisite_dir)
    out = create_user_file(filename, "w")
    out.write("# Written by Multisite UserDB\n# encoding: utf-8\n\n")
    out.write("multisite_users = \\\n%s\n" % pprint.pformat(users))
    out.close()
    os.rename(filename, filename[:-4])

    # Execute user connector save hooks
    hook_save(profiles)

    # Write out the users serials
    serials_file = '%s/auth.serials.new' % os.path.dirname(defaults.htpasswd_file)
    rename_file = True
    try:
        out = create_user_file(serials_file, "w")
    except:
        rename_file = False
        out = create_user_file(serials_file[:-4], "w")

    for user_id, user in profiles.items():
        out.write('%s:%d\n' % (make_utf8(user_id), user.get('serial', 0)))
    out.close()
    if rename_file:
        os.rename(serials_file, serials_file[:-4])

    # Write user specific files
    for user_id, user in profiles.items():
        user_dir = defaults.var_dir + "/web/" + user_id
        make_nagios_directory(user_dir)

        # authentication secret for local processes
        auth_file = user_dir + "/automation.secret"
        if "automation_secret" in user:
            create_user_file(auth_file, "w").write("%s\n" % user["automation_secret"])
        else:
            remove_user_file(auth_file)

        # Write out user attributes which are written to dedicated files in the user
        # profile directory. The primary reason to have separate files, is to reduce
        # the amount of data to be loaded during regular page processing
        save_custom_attr(user_id, 'serial', str(user.get('serial', 0)))
        save_custom_attr(user_id, 'num_failed', str(user.get('num_failed', 0)))
        save_custom_attr(user_id, 'enforce_pw_change', str(int(user.get('enforce_pw_change', False))))
        save_custom_attr(user_id, 'last_pw_change', str(user.get('last_pw_change', int(time.time()))))

        # Write out the last seent time
        if 'last_seen' in user:
            save_custom_attr(user_id, 'last_seen', repr(user['last_seen']))

    # Remove settings directories of non-existant users.
    # Beware: we removed this since it leads to violent destructions
    # if the user database is out of the scope of Check_MK. This is
    # e.g. the case, if mod_ldap is used for user authentication.
    # dir = defaults.var_dir + "/web"
    # for e in os.listdir(dir):
    #     if e not in ['.', '..'] and e not in profiles:
    #         entry = dir + "/" + e
    #         if os.path.isdir(entry):
    #             shutil.rmtree(entry)
    # But for the automation.secret this is ok, since automation users are not
    # created by other sources in common cases
    dir = defaults.var_dir + "/web"
    for user_dir in os.listdir(defaults.var_dir + "/web"):
        if user_dir not in ['.', '..'] and user_dir.decode("utf-8") not in profiles:
            entry = dir + "/" + user_dir
            if os.path.isdir(entry) and os.path.exists(entry + '/automation.secret'):
                os.unlink(entry + '/automation.secret')

    # Release the lock to make other threads access possible again asap
    # This lock is set by load_users() only in the case something is expected
    # to be written (like during user syncs, wato, ...)
    release_lock(root_dir + "contacts.mk")

    # populate the users cache
    html.set_cache('users', profiles)

    # Call the users_saved hook
    hooks.call("users-saved", profiles)

#.
#   .-Roles----------------------------------------------------------------.
#   |                       ____       _                                   |
#   |                      |  _ \ ___ | | ___  ___                         |
#   |                      | |_) / _ \| |/ _ \/ __|                        |
#   |                      |  _ < (_) | |  __/\__ \                        |
#   |                      |_| \_\___/|_|\___||___/                        |
#   |                                                                      |
#   +----------------------------------------------------------------------+

def load_roles():
    # Fake builtin roles into user roles.
    builtin_role_names = {  # Default names for builtin roles
        "admin" : _("Administrator"),
        "user"  : _("Normal monitoring user"),
        "guest" : _("Guest user"),
    }
    roles = dict([(id, {
         "alias" : builtin_role_names.get(id, id),
         "permissions" : {}, # use default everywhere
         "builtin": True})
                  for id in config.builtin_role_ids ])

    filename = multisite_dir + "roles.mk"
    try:
        vars = { "roles" : roles }
        execfile(filename, vars, vars)
        # Make sure that "general." is prefixed to the general permissions
        # (due to a code change that converted "use" into "general.use", etc.
        for role in roles.values():
            for pname, pvalue in role["permissions"].items():
                if "." not in pname:
                    del role["permissions"][pname]
                    role["permissions"]["general." + pname] = pvalue

        # Reflect the data in the roles dict kept in the config module needed
        # for instant changes in current page while saving modified roles.
        # Otherwise the hooks would work with old data when using helper
        # functions from the config module
        config.roles.update(vars['roles'])

        return vars["roles"]

    except IOError:
        return roles # Use empty structure, not existing file is ok!
    except Exception, e:
        if config.debug:
            raise MKGeneralException(_("Cannot read configuration file %s: %s" %
                          (filename, e)))
        else:
            logger(LOG_ERR, 'load_roles: Problem while loading roles (%s - %s). '
                     'Initializing structure...' % (filename, e))
        return roles

#.
#   .-Groups---------------------------------------------------------------.
#   |                    ____                                              |
#   |                   / ___|_ __ ___  _   _ _ __  ___                    |
#   |                  | |  _| '__/ _ \| | | | '_ \/ __|                   |
#   |                  | |_| | | | (_) | |_| | |_) \__ \                   |
#   |                   \____|_|  \___/ \__,_| .__/|___/                   |
#   |                                        |_|                           |
#   +----------------------------------------------------------------------+

def load_group_information():
    try:
        # Load group information from Check_MK world
        vars = {}
        for what in ["host", "service", "contact" ]:
            vars["define_%sgroups" % what] = {}

        filename = root_dir + "groups.mk"
        try:
            execfile(filename, vars, vars)
        except IOError:
            return {} # skip on not existing file

        # Now load information from the Web world
        multisite_vars = {}
        for what in ["host", "service", "contact" ]:
            multisite_vars["multisite_%sgroups" % what] = {}

        filename = multisite_dir + "groups.mk"
        try:
            execfile(filename, multisite_vars, multisite_vars)
        except IOError:
            pass

        # Merge information from Check_MK and Multisite worlds together
        groups = {}
        for what in ["host", "service", "contact" ]:
            groups[what] = {}
            for id, alias in vars['define_%sgroups' % what].items():
                groups[what][id] = {
                    'alias': alias
                }
                if id in multisite_vars['multisite_%sgroups' % what]:
                    groups[what][id].update(multisite_vars['multisite_%sgroups' % what][id])

        return groups

    except Exception, e:
        if config.debug:
            raise MKGeneralException(_("Cannot read configuration file %s: %s" %
                          (filename, e)))
        else:
            logger(LOG_ERR, 'load_group_information: Problem while loading groups (%s - %s). '
                     'Initializing structure...' % (filename, e))
        return {}

#.
#   .-Custom-Attrs.--------------------------------------------------------.
#   |   ____          _                          _   _   _                 |
#   |  / ___|   _ ___| |_ ___  _ __ ___         / \ | |_| |_ _ __ ___      |
#   | | |  | | | / __| __/ _ \| '_ ` _ \ _____ / _ \| __| __| '__/ __|     |
#   | | |__| |_| \__ \ || (_) | | | | | |_____/ ___ \ |_| |_| |  \__ \_    |
#   |  \____\__,_|___/\__\___/|_| |_| |_|    /_/   \_\__|\__|_|  |___(_)   |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Mange custom attributes of users (in future hosts etc.)              |
#   '----------------------------------------------------------------------'

def load_custom_attrs():
    try:
        filename = multisite_dir + "custom_attrs.mk"
        if not os.path.exists(filename):
            return {}

        vars = {
            'wato_user_attrs': [],
        }
        execfile(filename, vars, vars)

        attrs = {}
        for what in [ "user" ]:
            attrs[what] = vars.get("wato_%s_attrs" % what, [])
        return attrs

    except Exception, e:
        if config.debug:
            raise MKGeneralException(_("Cannot read configuration file %s: %s" %
                          (filename, e)))
        else:
            logger(LOG_ERR, 'load_custom_attrs: Problem while loading custom attributes (%s - %s). '
                     'Initializing structure...' % (filename, e))
        return {}

def declare_custom_user_attrs():
    all_attrs = load_custom_attrs()
    attrs = all_attrs.setdefault('user', [])
    for attr in attrs:
        vs = globals()[attr['type']](title = attr['title'], help = attr['help'])
        declare_user_attribute(attr['name'], vs,
            user_editable = attr['user_editable'],
            show_in_table = attr.get('show_in_table', False),
            topic = attr.get('topic', 'personal'),
            add_custom_macro = attr.get('add_custom_macro', False )
        )

#.
#   .--ConnectorCfg--------------------------------------------------------.
#   |    ____                            _              ____  __           |
#   |   / ___|___  _ __  _ __   ___  ___| |_ ___  _ __ / ___|/ _| __ _     |
#   |  | |   / _ \| '_ \| '_ \ / _ \/ __| __/ _ \| '__| |   | |_ / _` |    |
#   |  | |__| (_) | | | | | | |  __/ (__| || (_) | |  | |___|  _| (_| |    |
#   |   \____\___/|_| |_|_| |_|\___|\___|\__\___/|_|   \____|_|  \__, |    |
#   |                                                            |___/     |
#   +----------------------------------------------------------------------+
#   | The user can enable and configure a list of user connectors which    |
#   | are then used by the userdb to fetch user / group information from   |
#   | external sources like LDAP servers.                                  |
#   '----------------------------------------------------------------------'

def load_connection_config():
    filename = multisite_dir + "user_connections.mk"
    if not os.path.exists(filename):
        return []
    try:
        vars = {
            "user_connections" : [],
        }
        execfile(filename, vars, vars)
        return vars["user_connections"]

    except Exception, e:
        if config.debug:
            raise MKGeneralException(_("Cannot read configuration file %s: %s" %
                          (filename, e)))
        return vars["user_connections"]


def save_connection_config(connections):
    make_nagios_directory(multisite_dir)
    out = create_user_file(multisite_dir + "user_connections.mk", "w")
    out.write("# Written by Multisite UserDB\n# encoding: utf-8\n\n")
    out.write("user_connections = \\\n%s\n\n" % pprint.pformat(connections))

#.
#   .--ConnectorAPI--------------------------------------------------------.
#   |     ____                            _              _    ____ ___     |
#   |    / ___|___  _ __  _ __   ___  ___| |_ ___  _ __ / \  |  _ \_ _|    |
#   |   | |   / _ \| '_ \| '_ \ / _ \/ __| __/ _ \| '__/ _ \ | |_) | |     |
#   |   | |__| (_) | | | | | | |  __/ (__| || (_) | | / ___ \|  __/| |     |
#   |    \____\___/|_| |_|_| |_|\___|\___|\__\___/|_|/_/   \_\_|  |___|    |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Implements the base class for User Connector classes. It implements  |
#   | basic mechanisms and default methods which might/should be           |
#   | overridden by the specific connector classes.                        |
#   '----------------------------------------------------------------------'

# FIXME: How to declare methods/attributes forced to be overridden?
class UserConnector(object):
    def __init__(self, config):
        super(UserConnector, self).__init__()
        self._config = config

    @classmethod
    def type(self):
        return None

    # The string representing this connector to humans
    @classmethod
    def title(self):
        return None


    @classmethod
    def short_title(self):
        return _('htpasswd')

    #
    # USERDB API METHODS
    #

    @classmethod
    def migrate_config(self):
        pass

    # Optional: Hook function can be registered here to be executed
    # to validate a login issued by a user.
    # Gets parameters: username, password
    # Has to return either:
    #     '<user_id>' -> Login succeeded
    #     False       -> Login failed
    #     None        -> Unknown user
    def check_credentials(self, user_id, password):
        return None

    # Optional: Hook function can be registered here to be executed
    # to synchronize all users.
    def do_sync(self, add_to_changelog, only_username):
        pass

    # Optional: Tells whether or not the given user is currently
    # locked which would mean that he is not allowed to login.
    def is_locked(self, user_id):
        return False

    # Optional: Hook function can be registered here to be xecuted
    # on each call to a multisite page, even on ajax requests etc.
    def on_page_load(self):
        pass

    # Optional: Hook function can be registered here to be xecuted
    # to save all users.
    def save_users(self, users):
        pass

    # List of user attributes locked for all users attached to this
    # connection. Those locked attributes are read-only in WATO.
    def locked_attributes(self):
        return []

    def multisite_attributes(self):
        return []

    def non_contact_attributes(self):
        return []


#.
#   .-Hooks----------------------------------------------------------------.
#   |                     _   _             _                              |
#   |                    | | | | ___   ___ | | _____                       |
#   |                    | |_| |/ _ \ / _ \| |/ / __|                      |
#   |                    |  _  | (_) | (_) |   <\__ \                      |
#   |                    |_| |_|\___/ \___/|_|\_\___/                      |
#   |                                                                      |
#   +----------------------------------------------------------------------+


# This hook is called to validate the login credentials provided by a user
def hook_login(username, password):
    for connection_id, connection in active_connections():
        result = connection.check_credentials(username, password)
        # None        -> User unknown, means continue with other connectors
        # '<user_id>' -> success
        # False       -> failed
        if result not in [ False, None ]:
            username = result
            if type(username) not in [ str, unicode ]:
                raise MKInternalError(_("The username returned by the %s "
                    "connector is not of type string (%r).") % (connection_id, username))
            # Check wether or not the user exists (and maybe create it)
            create_non_existing_user(connection_id, username)

            # Now, after successfull login (and optional user account
            # creation), check wether or not the user is locked.
            # In e.g. htpasswd connector this is checked by validating the
            # password against the hash in the htpasswd file prefixed with
            # a "!". But when using other conectors it might be neccessary
            # to validate the user "locked" attribute.
            if connection.is_locked(username):
                return False # The account is locked

            return result

        elif result == False:
            return result


def show_exception(connection_id, title, e, debug=True):
    html.show_error(
        "<b>" + connection_id + ' - ' + title + "</b>"
        "<pre>%s</pre>" % (debug and traceback.format_exc() or e)
    )


# Hook function can be registered here to be executed to synchronize all users.
# Is called on:
#   a) before rendering the user management page in WATO
#   b) a user is created during login (only for this user)
#   c) Before activating the changes in WATO
def hook_sync(connection_id = None, add_to_changelog = False, only_username = None, raise_exc = False):
    if connection_id:
        connections = [ (connection_id, get_connection(connection_id)) ]
    else:
        connections = active_connections()

    no_errors = True
    for connection_id, connection in connections:
        try:
            connection.do_sync(add_to_changelog, only_username)
        except MKLDAPException, e:
            if raise_exc:
                raise
            show_exception(connection_id, _("Error during sync"), e, debug=config.debug)
            no_errors = False
        except Exception, e:
            if raise_exc:
                raise
            show_exception(connection_id, _("Error during sync"), e)
            no_errors = False
    return no_errors

# Hook function can be registered here to be executed during saving of the
# new user construct
def hook_save(users):
    for connection_id, connection in active_connections():
        try:
            connection.save_users(users)
        except Exception, e:
            if config.debug:
                raise
            else:
                show_exception(connection_id, _("Error during saving"), e)

# This function registers general stuff, which is independet of the single
# connectors to each page load. It is exectued AFTER all other page hooks.
def general_page_hook():
    # Working around the problem that the auth.php file needed for multisite based
    # authorization of external addons might not exist when setting up a new installation
    # We assume: Each user must visit this login page before using the multisite based
    #            authorization. So we can easily create the file here if it is missing.
    # This is a good place to replace old api based files in the future.
    auth_php = defaults.var_dir + '/wato/auth/auth.php'
    if not os.path.exists(auth_php) or os.path.getsize(auth_php) == 0:
        create_auth_file("page_hook", load_users())

    # Create initial auth.serials file, same issue as auth.php above
    serials_file = '%s/auth.serials' % os.path.dirname(defaults.htpasswd_file)
    if not os.path.exists(serials_file) or os.path.getsize(serials_file) == 0:
        save_users(load_users(lock = True))

# Hook function can be registered here to execute actions on a "regular" base without
# user triggered action. This hook is called on each page load.
# Catch all exceptions and log them to apache error log. Let exceptions raise trough
# when debug mode is enabled.
def hook_page():
    if 'page' not in config.userdb_automatic_sync:
        return

    for connection_id, connection in active_connections():
        try:
            connection.on_page_load()
        except:
            if config.debug:
                raise
            else:
                import traceback
                logger(LOG_ERR, 'Exception (%s, page handler): %s' %
                            (connection_id, traceback.format_exc()))

    general_page_hook()

def ajax_sync():
    try:
        hook_sync(add_to_changelog = False, raise_exc = True)
        html.write('OK\n')
    except Exception, e:
        if config.debug:
            raise
        else:
            html.write('ERROR %s\n' % e)
