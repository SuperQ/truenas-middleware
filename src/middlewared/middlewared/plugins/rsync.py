# Copyright 2017 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import asyncio
import asyncssh
import contextlib
import enum
import glob
import os
import shlex
import tempfile

from middlewared.common.attachment import LockableFSAttachmentDelegate
from middlewared.schema import accepts, Bool, Cron, Dict, Str, Int, List, Patch, returns
from middlewared.validators import Range, Match
from middlewared.service import (
    CallError, SystemServiceService, ValidationErrors, job, item_method, private, SharingService, TaskPathService,
)
import middlewared.sqlalchemy as sa
from middlewared.utils.osc import run_command_with_user_context


RSYNC_PATH_LIMIT = 1023


class RsyncReturnCode(enum.Enum):
    # from rsync's "errcode.h"
    OK = 0
    SYNTAX = 1         # syntax or usage error
    PROTOCOL = 2       # protocol incompatibility
    FILESELECT = 3     # errors selecting input/output files, dirs
    UNSUPPORTED = 4    # requested action not supported
    STARTCLIENT = 5    # error starting client-server protocol
    SOCKETIO = 10      # error in socket IO
    FILEIO = 11        # error in file IO
    STREAMIO = 12      # error in rsync protocol data stream
    MESSAGEIO = 13     # errors with program diagnostics
    IPC = 14           # error in IPC code
    CRASHED = 15       # sibling crashed
    TERMINATED = 16    # sibling terminated abnormally
    SIGNAL1 = 19       # status returned when sent SIGUSR1
    SIGNAL = 20        # status returned when sent SIGINT, SIGTERM, SIGHUP
    WAITCHILD = 21     # some error returned by waitpid()
    MALLOC = 22        # error allocating core memory buffers
    PARTIAL = 23       # partial transfer
    VANISHED = 24      # file(s) vanished on sender side
    DEL_LIMIT = 25     # skipped some deletes due to --max-delete
    TIMEOUT = 30       # timeout in data send/receive
    CONTIMEOUT = 35    # timeout waiting for daemon connection

    @classmethod
    def nonfatals(cls):
        return tuple([rc.value for rc in [
            cls.OK,
            cls.VANISHED,
            cls.DEL_LIMIT
        ]])


class RsyncdModel(sa.Model):
    __tablename__ = 'services_rsyncd'

    id = sa.Column(sa.Integer(), primary_key=True)
    rsyncd_port = sa.Column(sa.Integer(), default=873)
    rsyncd_auxiliary = sa.Column(sa.Text())


class RsyncdService(SystemServiceService):

    class Config:
        service = "rsync"
        service_model = 'rsyncd'
        datastore_prefix = "rsyncd_"
        cli_namespace = 'service.rsync'

    ENTRY = Dict(
        'rsyncd_entry',
        Int('port', required=True, validators=[Range(min=1, max=65535)]),
        Int('id', required=True),
        Str('auxiliary', required=True, max_length=None),
    )

    async def do_update(self, data):
        """
        Update Rsyncd Service Configuration.

        `auxiliary` attribute can be used to pass on any additional parameters from rsyncd.conf(5).
        """
        old = await self.config()

        new = old.copy()
        new.update(data)

        await self._update_service(old, new)

        return new


class RsyncModModel(sa.Model):
    __tablename__ = 'services_rsyncmod'

    id = sa.Column(sa.Integer(), primary_key=True)
    rsyncmod_name = sa.Column(sa.String(120))
    rsyncmod_comment = sa.Column(sa.String(120))
    rsyncmod_path = sa.Column(sa.String(255))
    rsyncmod_mode = sa.Column(sa.String(120), default="rw")
    rsyncmod_maxconn = sa.Column(sa.Integer(), default=0)
    rsyncmod_user = sa.Column(sa.String(120), default="nobody")
    rsyncmod_group = sa.Column(sa.String(120), default="nobody")
    rsyncmod_hostsallow = sa.Column(sa.Text())
    rsyncmod_hostsdeny = sa.Column(sa.Text())
    rsyncmod_auxiliary = sa.Column(sa.Text())
    rsyncmod_enabled = sa.Column(sa.Boolean())


class RsyncModService(SharingService):

    share_task_type = 'Rsync Module'

    class Config:
        datastore = 'services.rsyncmod'
        datastore_prefix = 'rsyncmod_'
        datastore_extend = 'rsyncmod.rsync_mod_extend'
        cli_namespace = 'service.rsync_mod'

    ENTRY = Patch(
        'rsyncmod_create', 'rsyncmod_entry',
        ('add', Bool('locked')),
        ('add', Int('id')),
    )

    @private
    async def rsync_mod_extend(self, data):
        data['hostsallow'] = data['hostsallow'].split()
        data['hostsdeny'] = data['hostsdeny'].split()
        data['mode'] = data['mode'].upper()
        return data

    @private
    async def common_validation(self, data, schema_name):
        verrors = ValidationErrors()

        await self.validate_path_field(data, schema_name, verrors)

        for entity in ('user', 'group'):
            value = data.get(entity)
            try:
                await self.middleware.call(f'{entity}.get_{entity}_obj', {f'{entity}name': value})
            except Exception:
                verrors.add(
                    f'{schema_name}.{entity}',
                    f'Please specify a valid {entity}'
                )

        verrors.check()

        data['hostsallow'] = ' '.join(data['hostsallow'])
        data['hostsdeny'] = ' '.join(data['hostsdeny'])
        data['mode'] = data['mode'].lower()

        return data

    @accepts(Dict(
        'rsyncmod_create',
        Bool('enabled', default=True),
        Str('name', validators=[Match(r'[^/\]]')]),
        Str('comment'),
        Str('path', required=True, max_length=RSYNC_PATH_LIMIT),
        Str('mode', enum=['RO', 'RW', 'WO'], required=True),
        Int('maxconn'),
        Str('user', default='nobody'),
        Str('group', default='nobody'),
        List('hostsallow', items=[Str('hostsallow')]),
        List('hostsdeny', items=[Str('hostdeny')]),
        Str('auxiliary', max_length=None),
        register=True,
    ))
    async def do_create(self, data):
        """
        Create a Rsyncmod module.

        `path` represents the path to a dataset. Path length is limited to 1023 characters maximum as per the limit
        enforced by FreeBSD. It is possible that we reach this max length recursively while transferring data. In that
        case, the user must ensure the maximum path will not be too long or modify the recursed path to shorter
        than the limit.

        `maxconn` is an integer value representing the maximum number of simultaneous connections. Zero represents
        unlimited.

        `hostsallow` is a list of patterns to match hostname/ip address of a connecting client. If list is empty,
        all hosts are allowed.

        `hostsdeny` is a list of patterns to match hostname/ip address of a connecting client. If the pattern is
        matched, access is denied to the client. If no client should be denied, this should be left empty.

        `auxiliary` attribute can be used to pass on any additional parameters from rsyncd.conf(5).
        """

        data = await self.common_validation(data, 'rsyncmod_create')

        data['id'] = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('rsync', 'reload')

        return await self.get_instance(data['id'])

    @accepts(Int('id'), Patch('rsyncmod_create', 'rsyncmod_update', ('attr', {'update': True})))
    async def do_update(self, id, data):
        """
        Update Rsyncmod module of `id`.
        """
        module = await self.get_instance(id)
        module.update(data)

        module = await self.common_validation(module, 'rsyncmod_update')
        module.pop(self.locked_field)

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            id,
            module,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('rsync', 'reload')

        return await self.get_instance(id)

    async def do_delete(self, id):
        """
        Delete Rsyncmod module of `id`.
        """
        return await self.middleware.call('datastore.delete', self._config.datastore, id)


class RsyncTaskModel(sa.Model):
    __tablename__ = 'tasks_rsync'

    id = sa.Column(sa.Integer(), primary_key=True)
    rsync_path = sa.Column(sa.String(255))
    rsync_remotehost = sa.Column(sa.String(120), nullable=True)
    rsync_remoteport = sa.Column(sa.SmallInteger(), nullable=True)
    rsync_remotemodule = sa.Column(sa.String(120), nullable=True)
    rsync_ssh_credentials_id = sa.Column(sa.ForeignKey('system_keychaincredential.id'), index=True, nullable=True)
    rsync_desc = sa.Column(sa.String(120))
    rsync_minute = sa.Column(sa.String(100), default="00")
    rsync_hour = sa.Column(sa.String(100), default="*")
    rsync_daymonth = sa.Column(sa.String(100), default="*")
    rsync_month = sa.Column(sa.String(100), default='*')
    rsync_dayweek = sa.Column(sa.String(100), default="*")
    rsync_user = sa.Column(sa.String(60))
    rsync_recursive = sa.Column(sa.Boolean(), default=True)
    rsync_times = sa.Column(sa.Boolean(), default=True)
    rsync_compress = sa.Column(sa.Boolean(), default=True)
    rsync_archive = sa.Column(sa.Boolean(), default=False)
    rsync_delete = sa.Column(sa.Boolean(), default=False)
    rsync_quiet = sa.Column(sa.Boolean(), default=False)
    rsync_preserveperm = sa.Column(sa.Boolean(), default=False)
    rsync_preserveattr = sa.Column(sa.Boolean(), default=False)
    rsync_extra = sa.Column(sa.Text())
    rsync_enabled = sa.Column(sa.Boolean(), default=True)
    rsync_mode = sa.Column(sa.String(20), default='module')
    rsync_remotepath = sa.Column(sa.String(255))
    rsync_direction = sa.Column(sa.String(10), default='PUSH')
    rsync_delayupdates = sa.Column(sa.Boolean(), default=True)


class RsyncTaskService(TaskPathService):

    share_task_type = 'Rsync'

    class Config:
        datastore = 'tasks.rsync'
        datastore_prefix = 'rsync_'
        datastore_extend = 'rsynctask.rsync_task_extend'
        datastore_extend_context = 'rsynctask.rsync_task_extend_context'
        cli_namespace = 'task.rsync'

    ENTRY = Patch(
        'rsync_task_create', 'rsync_task_entry',
        ('rm', {'name': 'ssh_credentials'}),
        ('rm', {'name': 'validate_rpath'}),
        ('add', Int('id')),
        ('add', Dict('ssh_credentials', null=True, additional_attrs=True)),
        ('add', Bool('locked')),
        ('add', Dict('job', null=True, additional_attrs=True)),
    )

    @private
    async def rsync_task_extend(self, data, context):
        try:
            data['extra'] = shlex.split(data['extra'].replace('"', r'"\"').replace("'", r'"\"'))
        except ValueError:
            # This is to handle the case where the extra value is misconfigured for old cases
            # Moving on, we are going to verify that it can be split successfully using shlex
            data['extra'] = data['extra'].split()

        for field in ('mode', 'direction'):
            data[field] = data[field].upper()
        Cron.convert_db_format_to_schedule(data)
        data['job'] = context['jobs'].get(data['id'])
        return data

    @private
    async def rsync_task_extend_context(self, rows, extra):
        jobs = {}
        for j in await self.middleware.call("core.get_jobs", [("method", "=", "rsynctask.run")],
                                            {"order_by": ["id"]}):
            try:
                task_id = int(j["arguments"][0])
            except (IndexError, TypeError, ValueError):
                continue

            if task_id in jobs and jobs[task_id]["state"] == "RUNNING":
                continue

            jobs[task_id] = j

        return {
            "jobs": jobs,
        }

    @private
    async def validate_rsync_task(self, data, schema):
        verrors = ValidationErrors()

        # Windows users can have spaces in their usernames
        # http://www.freebsd.org/cgi/query-pr.cgi?pr=164808

        username = data.get('user')
        if ' ' in username:
            verrors.add(f'{schema}.user', 'User names cannot have spaces')
            raise verrors

        user = None
        with contextlib.suppress(KeyError):
            user = await self.middleware.call('dscache.get_uncached_user', username)

        if not user:
            verrors.add(f'{schema}.user', f'Provided user "{username}" does not exist')
            raise verrors

        await self.validate_path_field(data, schema, verrors)

        data['extra'] = ' '.join(data['extra'])
        try:
            shlex.split(data['extra'].replace('"', r'"\"').replace("'", r'"\"'))
        except ValueError as e:
            verrors.add(f'{schema}.extra', f'Please specify valid value: {e}')

        if data['mode'] == 'MODULE':
            if not data['remotehost']:
                verrors.add(f'{schema}.remotehost', 'This field is required')

            if not data['remotemodule']:
                verrors.add(f'{schema}.remotemodule', 'This field is required')

        if data['mode'] == 'SSH':
            connect_kwargs = None
            if data['ssh_credentials']:
                try:
                    ssh_credentials = await self.middleware.call(
                        'keychaincredential.get_of_type',
                        data['ssh_credentials'],
                        'SSH_CREDENTIALS',
                    )
                except CallError as e:
                    verrors.add(f'{schema}.ssh_credentials', e.errmsg)
                else:
                    ssh_keypair = await self.middleware.call(
                        'keychaincredential.get_of_type',
                        ssh_credentials['attributes']['private_key'],
                        'SSH_KEY_PAIR',
                    )
                    connect_kwargs = {
                        "host": ssh_credentials['attributes']['host'],
                        "port": ssh_credentials['attributes']['port'],
                        'username': ssh_credentials['attributes']['username'],
                        'client_keys': [asyncssh.import_private_key(ssh_keypair['attributes']['private_key'])],
                        'known_hosts': None,
                    }
            else:
                if not data['remotehost']:
                    verrors.add(f'{schema}.remotehost', 'This field is required')

                if not data['remoteport']:
                    verrors.add(f'{schema}.remoteport', 'This field is required')

                search = os.path.join(user['pw_dir'], '.ssh', 'id_[edr]*')
                exclude_from_search = os.path.join(user['pw_dir'], '.ssh', 'id_[edr]*pub')
                key_files = set(glob.glob(search)) - set(glob.glob(exclude_from_search))
                if not key_files:
                    verrors.add(
                        f'{schema}.user',
                        'In order to use rsync over SSH you need a user'
                        ' with a private key (DSA/ECDSA/RSA) set up in home dir.'
                    )
                else:
                    for file in set(key_files):
                        # file holds a private key and it's permissions should be 600
                        if os.stat(file).st_mode & 0o077 != 0:
                            verrors.add(
                                f'{schema}.user',
                                f'Permissions {str(oct(os.stat(file).st_mode & 0o777))[2:]} for {file} are too open. Please '
                                f'correct them by running chmod 600 {file}'
                            )
                            key_files.discard(file)

                    if key_files:
                        if '@' in data['remotehost']:
                            remote_username, remote_host = data['remotehost'].rsplit('@', 1)
                        else:
                            remote_username = username
                            remote_host = data['remotehost']

                        connect_kwargs = {
                            'host': remote_host,
                            'port': data['remoteport'],
                            'username': remote_username,
                            'client_keys': key_files,
                            'known_hosts': None,
                        }

            remote_path = data.get('remotepath')
            if not remote_path:
                verrors.add(f'{schema}.remotepath', 'This field is required')

            if data['enabled'] and data['validate_rpath'] and connect_kwargs:
                try:
                    async with await asyncio.wait_for(
                        asyncssh.connect(**connect_kwargs), timeout=5,
                    ) as conn:
                        await conn.run(f'test -d {shlex.quote(remote_path)}', check=True)
                except asyncio.TimeoutError:
                    verrors.add(
                        f'{schema}.remotehost',
                        'SSH timeout occurred. Remote path cannot be validated.'
                    )
                except OSError as e:
                    if e.errno == 113:
                        verrors.add(
                            f'{schema}.remotehost',
                            f'Connection to the remote host {connect_kwargs["host"]} on port {connect_kwargs["port"]} '
                            'failed.'
                        )
                    else:
                        verrors.add(
                            f'{schema}.remotehost',
                            e.__str__()
                        )
                except asyncssh.DisconnectError as e:
                    verrors.add(
                        f'{schema}.remotehost',
                        f'Disconnect Error[ error code {e.code} ] was generated when trying to communicate with remote '
                        f'host {connect_kwargs["host"]} and remote user {connect_kwargs["username"]}.'
                    )
                except asyncssh.ProcessError as e:
                    if e.code == '1':
                        verrors.add(
                            f'{schema}.remotepath',
                            'The Remote Path you specified does not exist or is not a directory.'
                            'Either create one yourself on the remote machine or uncheck the '
                            'validate_rpath field'
                        )
                    else:
                        verrors.add(
                            f'{schema}.remotepath',
                            f'Connection to Remote Host was successful but failed to verify '
                            f'Remote Path. {e.__str__()}'
                        )
                except asyncssh.Error as e:
                    if e.__class__.__name__ in e.__str__():
                        exception_reason = e.__str__()
                    else:
                        exception_reason = e.__class__.__name__ + ' ' + e.__str__()
                    verrors.add(
                        f'{schema}.remotepath',
                        f'Remote Path could not be validated. An exception was raised. {exception_reason}'
                    )

        data.pop('validate_rpath', None)

        # Keeping compatibility with legacy UI
        for field in ('mode', 'direction'):
            data[field] = data[field].lower()

        return verrors, data

    @accepts(Dict(
        'rsync_task_create',
        Str('path', required=True, max_length=RSYNC_PATH_LIMIT),
        Str('user', required=True),
        Str('mode', enum=['MODULE', 'SSH'], default='MODULE'),
        Str('remotehost', null=True, default=None),
        Int('remoteport', null=True, default=None),
        Str('remotemodule', null=True, default=None),
        Int('ssh_credentials', null=True, default=None),
        Str('remotepath'),
        Bool('validate_rpath', default=True),
        Str('direction', enum=['PULL', 'PUSH'], default='PUSH'),
        Str('desc'),
        Cron(
            'schedule',
            defaults={'minute': '00'},
        ),
        Bool('recursive'),
        Bool('times'),
        Bool('compress'),
        Bool('archive'),
        Bool('delete'),
        Bool('quiet'),
        Bool('preserveperm'),
        Bool('preserveattr'),
        Bool('delayupdates'),
        List('extra', items=[Str('extra')]),
        Bool('enabled', default=True),
        register=True,
    ))
    async def do_create(self, data):
        """
        Create a Rsync Task.

        See the comment in Rsyncmod about `path` length limits.

        `remotehost` is ip address or hostname of the remote system. If username differs on the remote host,
        "username@remote_host" format should be used.

        `mode` represents different operating mechanisms for Rsync i.e Rsync Module mode / Rsync SSH mode.

        In SSH mode, if `ssh_credentials` (a keychain credential of `SSH_CREDENTIALS` type) is specified then it is used
        to connect to the remote host. If it is not specified, then keys in `user`'s .ssh directory are used.
        `remotehost` and `remoteport` are not used in this case.

        `remotemodule` is the name of remote module, this attribute should be specified when `mode` is set to MODULE.

        `remotepath` specifies the path on the remote system.

        `validate_rpath` is a boolean which when sets validates the existence of the remote path.

        `direction` specifies if data should be PULLED or PUSHED from the remote system.

        `compress` when set reduces the size of the data which is to be transmitted.

        `archive` when set makes rsync run recursively, preserving symlinks, permissions, modification times, group,
        and special files.

        `delete` when set deletes files in the destination directory which do not exist in the source directory.

        `preserveperm` when set preserves original file permissions.

        .. examples(websocket)::

          Create a Rsync Task which pulls data from a remote system every 5 minutes.

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "rsynctask.create",
                "params": [{
                    "enabled": true,
                    "schedule": {
                        "minute": "5",
                        "hour": "*",
                        "dom": "*",
                        "month": "*",
                        "dow": "*"
                    },
                    "desc": "Test rsync task",
                    "user": "root",
                    "mode": "MODULE",
                    "remotehost": "root@192.168.0.10",
                    "compress": true,
                    "archive": true,
                    "direction": "PULL",
                    "path": "/mnt/vol1/rsync_dataset",
                    "remotemodule": "remote_module1"
                }]
            }
        """
        verrors, data = await self.validate_rsync_task(data, 'rsync_task_create')
        if verrors:
            raise verrors

        Cron.convert_schedule_to_db_format(data)

        data['id'] = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )
        await self.middleware.call('service.restart', 'cron')

        return await self.get_instance(data['id'])

    @accepts(
        Int('id', validators=[Range(min=1)]),
        Patch('rsync_task_create', 'rsync_task_update', ('attr', {'update': True}))
    )
    async def do_update(self, id, data):
        """
        Update Rsync Task of `id`.
        """
        data.setdefault('validate_rpath', True)

        old = await self.query(filters=[('id', '=', id)], options={'get': True})
        old.pop(self.locked_field)
        old.pop('job')

        new = old.copy()
        if new['ssh_credentials']:
            new['ssh_credentials'] = new['ssh_credentials']['id']
        new.update(data)

        verrors, new = await self.validate_rsync_task(new, 'rsync_task_update')
        if verrors:
            raise verrors

        Cron.convert_schedule_to_db_format(new)

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            id,
            new,
            {'prefix': self._config.datastore_prefix}
        )
        await self.middleware.call('service.restart', 'cron')

        return await self.get_instance(id)

    async def do_delete(self, id):
        """
        Delete Rsync Task of `id`.
        """
        res = await self.middleware.call('datastore.delete', self._config.datastore, id)
        await self.middleware.call('service.restart', 'cron')
        return res

    @private
    @contextlib.contextmanager
    def commandline(self, id):
        """
        Helper method to generate the rsync command avoiding code duplication.
        """
        rsync = self.middleware.call_sync('rsynctask.get_instance', id)
        path = shlex.quote(rsync['path'])

        with contextlib.ExitStack() as exit_stack:
            line = ['rsync']
            for name, flag in (
                ('archive', '-a'),
                ('compress', '-zz'),
                ('delayupdates', '--delay-updates'),
                ('delete', '--delete-delay'),
                ('preserveattr', '-X'),
                ('preserveperm', '-p'),
                ('recursive', '-r'),
                ('times', '-t'),
            ):
                if rsync[name]:
                    line.append(flag)
            if rsync['extra']:
                line.append(' '.join(rsync['extra']))

            if not rsync['ssh_credentials']:
                # Do not use username if one is specified in host field
                # See #5096 for more details
                if '@' in rsync['remotehost']:
                    remote = rsync['remotehost']
                else:
                    remote = f'"{rsync["user"]}"@{rsync["remotehost"]}'

            if rsync['mode'] == 'MODULE':
                module_args = [path, f'{remote}::"{rsync["remotemodule"]}"']
                if rsync['direction'] != 'PUSH':
                    module_args.reverse()
                line += module_args
            else:
                if rsync['ssh_credentials']:
                    credentials = rsync['ssh_credentials']['attributes']
                    key_pair = self.middleware.call_sync(
                        'keychaincredential.get_of_type',
                        credentials['private_key'],
                        'SSH_KEY_PAIR',
                    )

                    remote = f'"{credentials["username"]}"@{credentials["host"]}'
                    port = credentials['port']

                    user = self.middleware.call_sync('dscache.get_uncached_user', rsync['user'])

                    private_key_file = exit_stack.enter_context(tempfile.NamedTemporaryFile('w'))
                    os.fchmod(private_key_file.fileno(), 0o600)
                    os.fchown(private_key_file.fileno(), user['pw_uid'], user['pw_gid'])
                    private_key_file.write(key_pair['attributes']['private_key'])
                    private_key_file.flush()

                    host_key_file = exit_stack.enter_context(tempfile.NamedTemporaryFile('w'))
                    os.fchmod(host_key_file.fileno(), 0o600)
                    os.fchown(host_key_file.fileno(), user['pw_uid'], user['pw_gid'])
                    host_key_file.write('\n'.join([
                        (
                            f'{credentials["host"]} {host_key}' if credentials['port'] == 22
                            else f'[{credentials["host"]}]:{credentials["port"]} {host_key}'
                        )
                        for host_key in credentials['remote_host_key'].split("\n")
                        if host_key.strip() and not host_key.strip().startswith("#")
                    ]))
                    host_key_file.flush()

                    extra_args = f'-i {private_key_file.name} -o UserKnownHostsFile={host_key_file.name}'
                else:
                    port = rsync['remoteport']
                    extra_args = ''

                line += [
                    '-e',
                    f'"ssh -p {port} -o BatchMode=yes -o StrictHostKeyChecking=yes {extra_args}"'
                ]
                path_args = [path, f'{remote}:"{shlex.quote(rsync["remotepath"])}"']
                if rsync['direction'] != 'PUSH':
                    path_args.reverse()
                line += path_args

            if rsync['quiet']:
                line += ['>', '/dev/null', '2>&1']

            yield ' '.join(line)

    @item_method
    @accepts(Int('id'))
    @returns()
    @job(lock=lambda args: args[-1], lock_queue_size=1, logs=True)
    def run(self, job, id):
        """
        Job to run rsync task of `id`.

        Output is saved to job log excerpt (not syslog).
        """
        self.middleware.call_sync('network.general.will_perform_activity', 'rsync')

        rsync = self.middleware.call_sync('rsynctask.get_instance', id)
        if rsync['locked']:
            self.middleware.call_sync('rsynctask.generate_locked_alert', id)
            return

        with self.commandline(id) as commandline:
            cp = run_command_with_user_context(
                commandline, rsync['user'], output=False, callback=lambda v: job.logs_fd.write(v),
            )

        for klass in ('RsyncSuccess', 'RsyncFailed') if not rsync['quiet'] else ():
            self.middleware.call_sync('alert.oneshot_delete', klass, rsync['id'])

        if cp.returncode not in RsyncReturnCode.nonfatals():
            err = None
            if cp.returncode == RsyncReturnCode.STREAMIO and rsync['compress']:
                err = (
                    "rsync command with compression enabled failed with STREAMIO error. "
                    "This may indicate that remote server lacks support for the new-style "
                    "compression used by TrueNAS."
                )

            if not rsync['quiet']:
                self.middleware.call_sync('alert.oneshot_create', 'RsyncFailed', {
                    'id': rsync['id'],
                    'direction': rsync['direction'],
                    'path': rsync['path'],
                })

            if err:
                msg = f'{err} Check logs for further information'
            else:
                try:
                    rc_name = RsyncReturnCode(cp.returncode).name
                except ValueError:
                    rc_name = 'UNKNOWN'

                msg = (
                    f'rsync command returned {cp.returncode} - {rc_name}. '
                    'Check logs for further information.'
                )
            raise CallError(msg)

        elif not rsync['quiet']:
            self.middleware.call_sync('alert.oneshot_create', 'RsyncSuccess', {
                'id': rsync['id'],
                'direction': rsync['direction'],
                'path': rsync['path'],
            })


class RsyncModuleFSAttachmentDelegate(LockableFSAttachmentDelegate):
    name = 'rsync_module'
    title = 'Rsync Module'
    service = 'rsync'
    service_class = RsyncModService

    async def restart_reload_services(self, attachments):
        await self._service_change('rsync', 'reload')


class RsyncFSAttachmentDelegate(LockableFSAttachmentDelegate):
    name = 'rsync'
    title = 'Rsync Task'
    service_class = RsyncTaskService
    resource_name = 'path'

    async def restart_reload_services(self, attachments):
        await self.middleware.call('service.restart', 'cron')


async def setup(middleware):
    await middleware.call('pool.dataset.register_attachment_delegate', RsyncModuleFSAttachmentDelegate(middleware))
    await middleware.call('pool.dataset.register_attachment_delegate', RsyncFSAttachmentDelegate(middleware))
    await middleware.call('network.general.register_activity', 'rsync', 'Rsync')
