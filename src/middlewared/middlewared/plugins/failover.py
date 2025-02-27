import asyncio
import base64
import errno
import logging
import os
import re
import shutil
import socket
import textwrap
import time
from functools import partial

from middlewared.plugins.failover_.journal import SQL_QUEUE
from middlewared.plugins.failover_.utils import throttle_condition
from middlewared.schema import accepts, Bool, Dict, Int, List, NOT_PROVIDED, Str, returns, Patch
from middlewared.service import (
    job, no_auth_required, pass_app, private, throttle, CallError, ConfigService, ValidationErrors,
)
import middlewared.sqlalchemy as sa
from middlewared.plugins.auth import AuthService, SessionManagerCredentials
from middlewared.plugins.config import FREENAS_DATABASE
from middlewared.plugins.datastore.connection import DatastoreService
from middlewared.utils.contextlib import asyncnullcontext
from middlewared.plugins.failover_.zpool_cachefile import ZPOOL_CACHE_FILE, ZPOOL_CACHE_FILE_OVERWRITE

ENCRYPTION_CACHE_LOCK = asyncio.Lock()

logger = logging.getLogger('failover')


class TruenasNodeSessionManagerCredentials(SessionManagerCredentials):
    pass


class FailoverModel(sa.Model):
    __tablename__ = 'system_failover'

    id = sa.Column(sa.Integer(), primary_key=True)
    disabled = sa.Column(sa.Boolean(), default=False)
    master_node = sa.Column(sa.String(1))
    timeout = sa.Column(sa.Integer(), default=0)


class FailoverService(ConfigService):

    HA_MODE = None
    HA_LICENSED = None
    LAST_STATUS = None
    LAST_DISABLEDREASONS = None

    class Config:
        datastore = 'system.failover'
        datastore_extend = 'failover.failover_extend'
        cli_namespace = 'system.failover'

    ENTRY = Dict(
        'failover_entry',
        Int('id', required=True),
        Bool('disabled', required=True),
        Int('timeout', required=True),
        Bool('master', required=True),
    )

    @private
    async def failover_extend(self, data):
        data['master'] = await self.middleware.call('failover.node') == data.pop('master_node')
        return data

    @accepts(Patch(
        'failover_entry', 'failover_update',
        ('edit', {'name': 'master', 'method': lambda x: setattr(x, 'null', True)}),
        ('rm', {'name': 'id'}),
        ('attr', {'update': True}),
    ))
    async def do_update(self, data):
        """
        Update failover state.

        `disabled` When true indicates that HA will be disabled.
        `master`  Marks the particular node in the chassis as the master node.
                    The standby node will have the opposite value.

        `timeout` is the time to WAIT until a failover occurs when a network
            event occurs on an interface that is marked critical for failover AND
            HA is enabled and working appropriately.

            The default time to wait is 2 seconds.
            **NOTE**
                This setting does NOT effect the `disabled` or `master` parameters.
        """
        master = data.pop('master', NOT_PROVIDED)

        old = await self.middleware.call('datastore.config', 'system.failover')

        new = old.copy()
        new.update(data)

        if master is not NOT_PROVIDED:
            # The node making the call is the one we want to make MASTER by default
            new['master_node'] = await self.middleware.call('failover.node')
        else:
            new['master_node'] = await self._master_node(master)

        verrors = ValidationErrors()
        if new['disabled'] is False:
            if not await self.middleware.call('interface.query', [('failover_critical', '=', True)]):
                verrors.add(
                    'failover_update.disabled',
                    'You need at least one critical interface to enable failover.',
                )
        verrors.check()

        await self.middleware.call('datastore.update', 'system.failover', new['id'], new)

        if new['disabled']:
            if new['master_node'] == await self.middleware.call('failover.node'):
                await self.middleware.call('failover.force_master')
            else:
                await self.middleware.call('failover.call_remote', 'failover.force_master')

        return await self.config()

    async def _master_node(self, master):
        node = await self.middleware.call('failover.node')
        if node == 'A':
            if master:
                return 'A'
            else:
                return 'B'
        elif node == 'B':
            if master:
                return 'B'
            else:
                return 'A'
        else:
            raise CallError('Unable to change node state in MANUAL mode')

    @accepts()
    @returns(Bool())
    def licensed(self):
        """
        Checks whether this instance is licensed as a HA unit.
        """
        # update the class attribute so that all instances
        # of this class see the correct value
        if FailoverService.HA_LICENSED is None:
            info = self.middleware.call_sync('system.license')
            if info is not None and info['system_serial_ha']:
                FailoverService.HA_LICENSED = True
            else:
                FailoverService.HA_LICENSED = False

        return FailoverService.HA_LICENSED

    @private
    async def ha_mode(self):
        # update the class attribute so that all instances
        # of this class see the correct value
        if FailoverService.HA_MODE is None:
            FailoverService.HA_MODE = await self.middleware.call(
                'failover.enclosure.detect'
            )

        return FailoverService.HA_MODE

    @accepts()
    @returns(Str())
    async def hardware(self):
        """
        Returns the hardware type for an HA system.
          ECHOSTREAM
          ECHOWARP
          PUMA
          BHYVE
          MANUAL
        """
        return (await self.middleware.call('failover.ha_mode'))[0]

    @accepts()
    @returns(Str())
    async def node(self):
        """
        Returns the slot position in the chassis that
        the controller is located.
          A - First node
          B - Seconde Node
          MANUAL - slot position in chassis could not be determined
        """
        return (await self.middleware.call('failover.ha_mode'))[1]

    @private
    @accepts()
    @returns(List(Str('interface')))
    async def internal_interfaces(self):
        """
        This is a p2p ethernet connection on HA systems.
        """
        return await self.middleware.call('failover.internal_interface.detect')

    @no_auth_required
    @throttle(seconds=2, condition=throttle_condition)
    @accepts()
    @returns(Str())
    @pass_app(rest=True)
    async def status(self, app):
        """
        Get the current HA status.

        Returns:
            MASTER
            BACKUP
            ELECTING
            IMPORTING
            ERROR
            SINGLE
        """
        status = await self._status(app)
        if status != self.LAST_STATUS:
            self.LAST_STATUS = status
            self.middleware.send_event('failover.status', 'CHANGED', fields={'status': status})

        return status

    async def _status(self, app):
        try:
            status = await self.middleware.call('cache.get', 'failover_status')
        except KeyError:
            status = await self.middleware.call('failover.status.get_local', app)
            if status:
                await self.middleware.call('cache.put', 'failover_status', status, 300)

        if status:
            return status

        try:
            # timeout of 5 seconds is necessary here since this could be called
            # when the other node has been forcefully rebooted so the websocket
            # connection is "up" but the default TCP window hasn't elapsed so
            # the connection remains alive. Without the timeout, this could take
            # 20+ seconds to return which is unacceptable during a failover event.
            remote_imported = await self.middleware.call(
                'failover.call_remote', 'zfs.pool.query_imported_fast', [], {'timeout': 5}
            )
            if len(remote_imported) <= 1:
                # getting here means we dont have a pool and neither does remote node
                return 'ERROR'
            else:
                # Other node has the pool (excluding boot pool)
                return 'BACKUP'
        except Exception as e:
            # Anything other than ClientException is unexpected and should be logged
            if not isinstance(e, CallError):
                self.logger.warning('Failed checking failover status', exc_info=True)
            return 'UNKNOWN'

    @private
    async def status_refresh(self):
        await self.middleware.call('cache.pop', 'failover_status')
        # Kick a new status so it may be ready on next user call
        await self.middleware.call('failover.status')
        await self.middleware.call('failover.disabled.reasons')

    @accepts()
    @returns(Bool())
    def in_progress(self):
        """
        Returns True if there is an ongoing failover event.
        """
        event = self.middleware.call_sync(
            'core.get_jobs', [
                ('method', 'in', [
                    'failover.events.vrrp_master',
                    'failover.events.vrrp_backup'
                ]),
                ('state', '=', 'RUNNING'),
            ]
        )
        return bool(event)

    @no_auth_required
    @throttle(seconds=2, condition=throttle_condition)
    @accepts()
    @returns(List('ips', items=[Str('ip')]))
    @pass_app(rest=True)
    async def get_ips(self, app):
        """Get a list of IPs for which the webUI can be accessed."""
        return await self.middleware.call('system.general.get_ui_urls')

    @accepts()
    @returns(Bool())
    async def become_passive(self):
        """
        Restart the keepavlived service which will cause any VIP addresses
        on this controller to be migrated to the other controller. This will
        cause a failover event if run on the master controller. If this is
        run on the passive controller it will do 1 of 2 things:

        1: if there are no VIP(s) on the passive controller, then this will
            do nothing.
        2: if there are VIP(s) on the passive controller, then the VIP(s)
            will be migrated to the active controller. A failover event
            will be triggered but it will do nothing since the active will
            already have the zpool(s) imported.
        """
        return await self.middleware.call('service.restart', 'keepalived', {'ha_propagate': False})

    @accepts()
    @returns(Bool())
    async def force_master(self):
        """
        Force this controller to become MASTER, if it's not already.
        """
        if not await self.middleware.call('system.is_enterprise'):
            return False

        if await self.middleware.call('failover.status') == 'MASTER':
            return False

        crit_ints = [i for i in await self.middleware.call('interface.query') if i.get('failover_critical', False)]
        if crit_ints:
            await self.middleware.call('failover.events.event', crit_ints[0]['name'], 'forcetakeover')
            return True
        else:
            # if there are no interfaces marked critical for failover and this method was
            # still called, then we can at least start fenced to reserve the disks
            rc = await self.middleware.call('failover.fenced.start', True)
            return not rc if rc != 6 else bool(rc)  # 6 means already running

        return False

    @accepts(Dict(
        'options',
        Bool('reboot', default=False),
    ))
    @returns()
    def sync_to_peer(self, options):
        """
        Sync database and files to the other controller.

        `reboot` as true will reboot the other controller after syncing.
        """
        standby = ' standby controller.'
        self.logger.debug('Syncing database to' + standby)
        self.middleware.call_sync('failover.send_database')

        self.logger.debug('Syncing cached encryption keys to' + standby)
        self.middleware.call_sync('failover.sync_keys_to_remote_node')

        self.logger.debug('Syncing zpool cachefile, license, pwenc and authorized_keys files to' + standby)
        self.send_small_file('/data/license')
        self.send_small_file('/data/pwenc_secret')
        self.send_small_file('/root/.ssh/authorized_keys')
        self.send_small_file(ZPOOL_CACHE_FILE, ZPOOL_CACHE_FILE_OVERWRITE)
        self.middleware.call_sync('failover.call_remote', 'failover.zpool.cachefile.setup', ['SYNC'])

        self.middleware.call_sync(
            'failover.call_remote', 'core.call_hook', ['config.on_upload', [FREENAS_DATABASE]],
        )

        # need to make sure the license information is updated on the standby node since
        # it's cached in memory
        _prev = self.middleware.call_sync('system.product_type')
        self.middleware.call_sync(
            'failover.call_remote', 'core.call_hook', ['system.post_license_update', [_prev]]
        )

        if options['reboot']:
            self.middleware.call_sync('failover.call_remote', 'system.reboot', [{'delay': 2}])

    @accepts()
    @returns()
    def sync_from_peer(self):
        """
        Sync database and files from the other controller.
        """
        self.middleware.call_sync('failover.call_remote', 'failover.sync_to_peer')

    @private
    async def send_database(self):
        await self.middleware.run_in_executor(DatastoreService.thread_pool, self._send_database)

    def _send_database(self):
        # We are in the `DatastoreService` thread so until the end of this method an item that we put into `SQL_QUEUE`
        # will be the last one and no one else is able to write neither to the database nor to the journal.

        # Journal thread will see that this is special value and will clear journal.
        SQL_QUEUE.put(None)

        token = self.middleware.call_sync('failover.call_remote', 'auth.generate_token')
        self.middleware.call_sync('failover.sendfile', token, FREENAS_DATABASE, FREENAS_DATABASE + '.sync')
        self.middleware.call_sync('failover.call_remote', 'failover.receive_database')

    @private
    def receive_database(self):
        os.rename(FREENAS_DATABASE + '.sync', FREENAS_DATABASE)
        self.middleware.call_sync('datastore.setup')

    @private
    def send_small_file(self, path, dest=None):
        if dest is None:
            dest = path
        if not os.path.exists(path):
            return
        mode = os.stat(path).st_mode
        with open(path, 'rb') as f:
            first = True
            while True:
                read = f.read(1024 * 1024 * 10)
                if not read:
                    break
                self.middleware.call_sync('failover.call_remote', 'filesystem.file_receive', [
                    dest, base64.b64encode(read).decode(), {'mode': mode, 'append': not first}
                ])
                first = False

    @private
    async def mismatch_disks(self):
        """
        On HA systems, da#'s can be different
        between controllers. This isn't common
        but does occurr. An example being when
        a customer powers off passive storage controller
        for maintenance and also powers off an expansion shelf.
        The active controller will reassign da#'s appropriately
        depending on which shelf was powered off. When passive
        storage controller comes back online, the da#'s will be
        different than what's on the active controller because the
        kernel reassigned those da#'s. This function now grabs
        the serial numbers of each disk and calculates the difference
        between the controllers. Instead of returning da#'s to alerts,
        this returns serial numbers.
        This accounts for 2 scenarios:
         1. the quantity of disks are different between
            controllers
         2. the quantity of disks are the same between
            controllers but serials do not match
        """
        local_boot_disks = await self.middleware.call('boot.get_disks')
        remote_boot_disks = await self.middleware.call('failover.call_remote', 'boot.get_disks')
        local_disks = set(
            v['ident']
            for k, v in (await self.middleware.call('device.get_info', 'DISK')).items()
            if k not in local_boot_disks
        )
        remote_disks = set(
            v['ident']
            for k, v in (await self.middleware.call('failover.call_remote', 'device.get_info', ['DISK'])).items()
            if k not in remote_boot_disks
        )
        return {
            'missing_local': sorted(remote_disks - local_disks),
            'missing_remote': sorted(local_disks - remote_disks),
        }

    @accepts(Dict(
        'options',
        List(
            'pools', items=[
                Dict(
                    'pool_keys',
                    Str('name', required=True),
                    Str('passphrase', required=True)
                )
            ],
        ),
        List(
            'datasets', items=[
                Dict(
                    'dataset_keys',
                    Str('name', required=True),
                    Str('passphrase', required=True),
                )
            ],
        ),
    ))
    @returns(Bool())
    async def unlock(self, options):
        """
        Unlock datasets in HA, syncing passphrase between controllers and forcing this controller
        to be MASTER importing the pools.
        """
        if options['pools'] or options['datasets']:
            await self.middleware.call(
                'failover.update_encryption_keys', {
                    'pools': options['pools'],
                    'datasets': options['datasets'],
                },
            )

        return await self.middleware.call('failover.force_master')

    @private
    @accepts(
        Str('pool_name'),
    )
    @returns(Dict(
        List('unlocked', items=[Str('dataset')], required=True),
        Dict(
            'failed',
            required=True,
            additional_attrs=True,
            example={'vol1/enc': {'error': 'Invalid Key', 'skipped': []}},
        ),
    ))
    @job(lock=lambda args: f'failover_dataset_unlock_{args[0]}')
    async def unlock_zfs_datasets(self, job, pool_name):
        # Unnlock all (if any) zfs datasets for `pool_name`
        # that we have keys for in the cache or the database.
        zfs_keys = (await self.encryption_keys())['zfs']
        unlock_job = await self.middleware.call(
            'pool.dataset.unlock', pool_name, {
                'recursive': True,
                'datasets': [{'name': name, 'passphrase': passphrase} for name, passphrase in zfs_keys.items()],
                # Do not waste time handling attachments, failover process will restart services and regenerate configs
                # for us
                'toggle_attachments': False,
            }
        )
        return await job.wrap(unlock_job)

    @private
    @accepts()
    async def encryption_keys(self):
        # TODO: remove GELI key since it's
        # not supported in SCALE
        return await self.middleware.call(
            'cache.get_or_put', 'failover_encryption_keys', 0, lambda: {'geli': {}, 'zfs': {}}
        )

    @private
    @accepts(
        Dict(
            'update_encryption_keys',
            Bool('sync_keys', default=True),
            List(
                'pools', items=[
                    Dict(
                        'pool_geli_keys',
                        Str('name', required=True),
                        Str('passphrase', required=True),
                    )
                ],
            ),
            List(
                'datasets', items=[
                    Dict(
                        'dataset_keys',
                        Str('name', required=True),
                        Str('passphrase', required=True),
                    )
                ],
            ),
        )
    )
    async def update_encryption_keys(self, options):
        # TODO: remove `pools` key and `geli` logic
        # since GELI is not supported in SCALE
        if not options['pools'] and not options['datasets']:
            raise CallError('Please specify pools/datasets to update')

        async with ENCRYPTION_CACHE_LOCK:
            keys = await self.encryption_keys()
            for pool in options['pools']:
                keys['geli'][pool['name']] = pool['passphrase']
            for dataset in options['datasets']:
                keys['zfs'][dataset['name']] = dataset['passphrase']
            await self.middleware.call('cache.put', 'failover_encryption_keys', keys)
            if options['sync_keys']:
                await self.sync_keys_to_remote_node(lock=False)

    @private
    @accepts(
        Dict(
            'remove_encryption_keys',
            Bool('sync_keys', default=True),
            List('pools', items=[Str('pool')]),
            List('datasets', items=[Str('dataset')]),
        )
    )
    async def remove_encryption_keys(self, options):
        # TODO: remove `pools` key and `geli` logic
        # since GELI is not supported in SCALE
        if not options['pools'] and not options['datasets']:
            raise CallError('Please specify pools/datasets to remove')

        async with ENCRYPTION_CACHE_LOCK:
            keys = await self.encryption_keys()
            for pool in options['pools']:
                keys['geli'].pop(pool, None)
            for dataset in options['datasets']:
                keys['zfs'] = {
                    k: v for k, v in keys['zfs'].items() if k != dataset and not k.startswith(f'{dataset}/')
                }
            await self.middleware.call('cache.put', 'failover_encryption_keys', keys)
            if options['sync_keys']:
                await self.sync_keys_to_remote_node(lock=False)

    @private
    async def is_single_master_node(self):
        return await self.middleware.call('failover.status') in ('MASTER', 'SINGLE')

    @accepts(
        Str('action', enum=['ENABLE', 'DISABLE']),
        Dict(
            'options',
            Bool('active'),
        ),
    )
    @returns()
    async def control(self, action, options):
        if not options:
            # The node making the call is the one we want to make MASTER by default
            node = await self._master_node((await self.middleware.call('failover.node')))
        else:
            node = await self._master_node(options.get('active'))

        failover = await self.middleware.call('datastore.config', 'system.failover')
        if action == 'ENABLE':
            if failover['disabled'] is False:
                # Already enabled
                return False
            update = {
                'disabled': False,
                'master_node': node,
            }
        elif action == 'DISABLE':
            if failover['disabled'] is True:
                # Already disabled
                return False
            update = {
                'disabled': True,
                'master_node': node,
            }

        await self.middleware.call('datastore.update', 'system.failover', failover['id'], update)

    @private
    def upgrade_version(self):
        return 1

    @accepts(Dict(
        'failover_upgrade',
        Str('train', empty=False),
    ))
    @returns(Bool())
    @job(lock='failover_upgrade', pipes=['input'], check_pipes=False)
    def upgrade(self, job, options):
        """
        Upgrades both controllers.

        Files will be downloaded to the Active Controller and then transferred to the Standby
        Controller.

        Upgrade process will start concurrently on both nodes.

        Once both upgrades are applied, the Standby Controller will reboot. This job will wait for
        that job to complete before finalizing.
        """

        if self.middleware.call_sync('failover.status') != 'MASTER':
            raise CallError('Upgrade can only run on Active Controller.')

        try:
            job.check_pipe('input')
        except ValueError:
            updatefile = False
        else:
            updatefile = True

        train = options.get('train')
        if train:
            self.middleware.call_sync('update.set_train', train)

        local_path = self.middleware.call_sync('update.get_update_location')

        if updatefile:
            # means manual update file was provided so write it
            # to local storage
            updatefile_name = 'updatefile.tar'
            job.set_progress(None, 'Uploading update file')
            updatefile_localpath = os.path.join(local_path, updatefile_name)
            os.makedirs(local_path, exist_ok=True)
            with open(updatefile_localpath, 'wb') as f:
                shutil.copyfileobj(job.pipes.input.r, f, 1048576)

        try:
            if not self.middleware.call_sync('failover.call_remote', 'system.ready'):
                raise CallError('Standby Controller is not ready.')

            if not updatefile:
                # means no update file was provided so go out to
                # the interwebz and download it
                def download_callback(j):
                    job.set_progress(
                        None, j['progress']['description'] or 'Downloading upgrade files'
                    )

                djob = self.middleware.call_sync('update.download', job_on_progress_cb=download_callback)
                djob.wait_sync(raise_error=True)
                if not djob.result:
                    raise CallError('No updates available.')

            self.middleware.call_sync('keyvalue.set', 'HA_UPGRADE', True)

            self.middleware.call_sync('failover.call_remote', 'update.destroy_upload_location')
            remote_path = self.middleware.call_sync(
                'failover.call_remote', 'update.create_upload_location'
            ) or '/var/tmp/firmware'

            if updatefile:
                # means update file was provided to us so send it to the standby
                job.set_progress(None, 'Sending files to Standby Controller')
                token = self.middleware.call_sync('failover.call_remote', 'auth.generate_token')
                for f in os.listdir(local_path):
                    self.middleware.call_sync(
                        'failover.sendfile',
                        token,
                        os.path.join(local_path, f),
                        os.path.join(remote_path, f)
                    )

            local_version = self.middleware.call_sync('system.version')
            remote_version = self.middleware.call_sync('failover.call_remote', 'system.version')

            update_remote_descr = update_local_descr = 'Starting upgrade'

            def callback(j, controller):
                nonlocal update_local_descr, update_remote_descr
                if j['state'] != 'RUNNING':
                    return
                if controller == 'LOCAL':
                    update_local_descr = f'{int(j["progress"]["percent"])}%: {j["progress"]["description"]}'
                else:
                    update_remote_descr = f'{int(j["progress"]["percent"])}%: {j["progress"]["description"]}'
                job.set_progress(
                    None,
                    f'Active Controller: {update_local_descr}\n' + f'Standby Controller: {update_remote_descr}'
                )

            if updatefile:
                update_method = 'update.manual'
                update_remote_args = [os.path.join(remote_path, updatefile_name)]
                update_local_args = [updatefile_localpath]
            else:
                update_method = 'update.update'
                update_remote_args = []
                update_local_args = []

            if local_version == remote_version:
                # start the upgrade on the remote (standby) controller
                rjob = self.middleware.call_sync(
                    'failover.call_remote', update_method, update_remote_args, {
                        'job_return': True,
                        'callback': partial(callback, controller='REMOTE')
                    }
                )
            else:
                rjob = None

            # upgrade the local (active) controller
            ljob = self.middleware.call_sync(
                update_method, *update_local_args,
                job_on_progress_cb=partial(callback, controller='LOCAL')
            )
            ljob.wait_sync(raise_error=True)

            remote_boot_id = self.middleware.call_sync('failover.call_remote', 'system.boot_id')

            # check the remote (standby) controller upgrade job
            if rjob:
                rjob.result()

            self.middleware.call_sync(
                'failover.call_remote', 'system.reboot',
                [{'delay': 5}],
                {'job': True}
            )
        except Exception:
            raise

        # The upgrade procedure will upgrade both systems simultaneously
        # as well as activate the new BEs. The standby controller will be
        # automatically rebooted into the new BE to "finalize" the upgrade.
        # However, we will re-activate the old BE on the active controller
        # to give the end-user a chance to "test" the new upgrade so in the
        # rare case something horrendous occurs on the new version they can
        # simply "reboot" (to cause a failover) and faill back to the
        # controller running the old version of the software.
        # The procedure is supposed to look like this:
        #   1. upgrade both controllers
        #   2. standby controller activates new BE and reboots
        #   3. end-user verifies the standby controller upgraded without issues
        #   4. end-user then failovers (reboots) to the newly upgraded node
        #   5. end-user verifies that the new software functions as expected
        #   6. end-user is then presented with a webUI option to "apply pending upgrade"
        #   7. end-user chooses that webUI option
        #   8. after webUI option is chosen, standby node is rebooted to "finalize" the
        #       upgrade procedure
        local_bootenv = self.middleware.call_sync('bootenv.query', [('active', 'rin', 'N')])
        if not local_bootenv:
            raise CallError('Could not find current boot environment.')
        self.middleware.call_sync('bootenv.activate', local_bootenv[0]['id'])

        # SCALE is using systemd and at the time of writing this, the
        # DefaultTimeoutStopSec setting hasn't been changed and so
        # defaults to 90 seconds. This means when the system is sent the
        # shutdown signal, all the associated user-space programs are
        # asked to be shutdown. If any of those take longer than 90
        # seconds to respond to SIGTERM then the program is sent SIGKILL.
        # Finally, if after 90 seconds the standby controller is still
        # responding to remote requests then play it safe and assume the
        # reboot failed (this should be rare but my future self will
        # appreciate the fact I wrote this out because of the inevitable
        # complexities of gluster/k8s/vms etc etc for which I predict
        # will exhibit this behavior :P )
        job.set_progress(None, 'Waiting on the Standby Controller to reboot.')
        try:
            retry_time = time.monotonic()
            shutdown_timeout = 90  # seconds
            while time.monotonic() - retry_time < shutdown_timeout:
                self.middleware.call_sync(
                    'failover.call_remote', 'core.ping', [], {'timeout': 5}
                )
                time.sleep(5)
        except CallError:
            pass
        else:
            raise CallError(
                f'Timed out waiting {shutdown_timeout} seconds for the standby controller to reboot',
                errno.ETIMEDOUT
            )

        if not self.upgrade_waitstandby():
            raise CallError(
                'Timed out waiting for the standby controller to upgrade.',
                errno.ETIMEDOUT
            )

        # we captured the `remote_boot_id` up above earlier in the upgrade process.
        # This variable represents a 1-time unique boot id. It's supposed to be different
        # every time the system boots up. If this check is True, then it's safe to say
        # that the remote system never rebooted, therefore, never completing the upgrade
        # process....which isn't good.
        if remote_boot_id == self.middleware.call_sync('failover.call_remote', 'system.boot_id'):
            raise CallError('Standby Controller failed to reboot.')

        return True

    @private
    def upgrade_waitstandby(self, seconds=900):
        """
        We will wait up to 15 minutes by default for the Standby Controller to reboot.
        This values come from observation from support of how long a M-series can take.
        """
        retry_time = time.monotonic()
        while time.monotonic() - retry_time < seconds:
            try:
                if not self.middleware.call_sync('failover.call_remote', 'system.ready'):
                    time.sleep(5)
                    continue
                return True
            except CallError as e:
                if e.errno in (errno.ECONNREFUSED, errno.ECONNRESET):
                    time.sleep(5)
                    continue
                raise
        return False

    @accepts()
    @returns(Bool())
    def upgrade_pending(self):
        """
        Verify if HA upgrade is pending.

        `upgrade_finish` needs to be called to finish
        the HA upgrade process if this method returns true.
        """

        if self.middleware.call_sync('failover.status') != 'MASTER':
            raise CallError('Upgrade can only be run from the Active Controller.')

        if self.middleware.call_sync('keyvalue.get', 'HA_UPGRADE', False) is not True:
            return False

        try:
            assert self.middleware.call_sync('failover.call_remote', 'core.ping') == 'pong'
        except Exception:
            return False

        local_version = self.middleware.call_sync('system.version')
        remote_version = self.middleware.call_sync('failover.call_remote', 'system.version')

        if local_version == remote_version:
            self.middleware.call_sync('keyvalue.set', 'HA_UPGRADE', False)
            return False

        local_bootenv = self.middleware.call_sync(
            'bootenv.query', [('active', 'rin', 'N')])

        remote_bootenv = self.middleware.call_sync(
            'failover.call_remote', 'bootenv.query', [[('active', '=', 'NR')]])

        if not local_bootenv or not remote_bootenv:
            raise CallError('Unable to determine installed version of software')

        tn_version = re.compile(r'\d*\.?\d+')
        loc_findall = tn_version.findall(local_bootenv[0]['id'])
        rem_findall = tn_version.findall(remote_bootenv[0]['id'])

        loc_vers = tuple(float(i) for i in loc_findall)
        rem_vers = tuple(float(i) for i in rem_findall)

        if loc_vers > rem_vers:
            return True

        return False

    @accepts()
    @returns(Bool())
    @job(lock='failover_upgrade_finish')
    def upgrade_finish(self, job):
        """
        Perform the last stage of an HA upgrade.

        This will activate the new boot environment on the
        Standby Controller and reboot it.
        """

        if self.middleware.call_sync('failover.status') != 'MASTER':
            raise CallError('Upgrade can only run on Active Controller.')

        job.set_progress(None, 'Ensuring the Standby Controller is booted')
        if not self.upgrade_waitstandby():
            raise CallError('Timed out waiting for the Standby Controller to boot.')

        job.set_progress(None, 'Activating new boot environment')
        local_bootenv = self.middleware.call_sync('bootenv.query', [('active', 'rin', 'N')])
        if not local_bootenv:
            raise CallError('Could not find current boot environment.')
        self.middleware.call_sync('failover.call_remote', 'bootenv.activate', [local_bootenv[0]['id']])

        job.set_progress(None, 'Rebooting Standby Controller')
        self.middleware.call_sync('failover.call_remote', 'system.reboot', [{'delay': 10}])
        self.middleware.call_sync('keyvalue.set', 'HA_UPGRADE', False)
        return True

    @private
    async def sync_keys_from_remote_node(self):
        """
        Sync ZFS encryption keys from the active node.
        """
        if not await self.middleware.call('failover.licensed'):
            return

        # only sync keys if we're the BACKUP node
        if (await self.middleware.call('failover.status')) != 'BACKUP':
            return

        # make sure we can contact the MASTER node
        try:
            assert (await self.middleware.call('failover.call_remote', 'core.ping')) == 'pong'
        except Exception:
            self.logger.error(
                'Failed to contact active controller when syncing encryption keys', exc_info=True
            )
            return

        try:
            await self.middleware.call('failover.call_remote', 'failover.sync_keys_to_remote_node')
        except Exception:
            self.logger.error(
                'Failed to sync keys from active controller when syncing encryption keys', exc_info=True
            )

    @private
    async def sync_keys_to_remote_node(self, lock=True):
        """
        Sync ZFS encryption keys to the standby node.
        """
        if not await self.middleware.call('failover.licensed'):
            return

        # only sync keys if we're the MASTER node
        if (await self.middleware.call('failover.status')) != 'MASTER':
            return

        # make sure we can contact the BACKUP node
        try:
            assert (await self.middleware.call('failover.call_remote', 'core.ping')) == 'pong'
        except Exception:
            self.logger.error(
                'Failed to contact standby controller when syncing encryption keys', exc_info=True
            )
            return

        async with ENCRYPTION_CACHE_LOCK if lock else asyncnullcontext():
            try:
                keys = await self.encryption_keys()
                await self.middleware.call(
                    'failover.call_remote', 'cache.put', ['failover_encryption_keys', keys]
                )
            except Exception as e:
                await self.middleware.call('alert.oneshot_create', 'FailoverKeysSyncFailed', None)
                self.logger.error('Failed to sync keys with standby controller: %s', str(e), exc_info=True)
            else:
                await self.middleware.call('alert.oneshot_delete', 'FailoverKeysSyncFailed', None)
            try:
                kmip_keys = await self.middleware.call('kmip.kmip_memory_keys')
                await self.middleware.call(
                    'failover.call_remote', 'kmip.update_memory_keys', [kmip_keys]
                )
            except Exception as e:
                await self.middleware.call(
                    'alert.oneshot_create', 'FailoverKMIPKeysSyncFailed', {'error': str(e)}
                )
                self.logger.error(
                    'Failed to sync KMIP keys with standby controller: %s', str(e), exc_info=True
                )
            else:
                await self.middleware.call('alert.oneshot_delete', 'FailoverKMIPKeysSyncFailed', None)


async def ha_permission(middleware, app):
    # Skip if session was already authenticated
    if app is not None and app.authenticated is True:
        return

    # We only care for remote connections (IPv4), in the interlink
    try:
        sock = app.request.transport.get_extra_info('socket')
    except AttributeError:
        # app.request or app.request.transport can be None
        return

    if sock.family != socket.AF_INET:
        return

    remote_addr, remote_port = app.request.transport.get_extra_info('peername')

    if remote_port <= 1024 and remote_addr in ('169.254.10.1', '169.254.10.2'):
        AuthService.session_manager.login(app, TruenasNodeSessionManagerCredentials())


async def interface_pre_sync_hook(middleware):
    await middleware.call('failover.internal_interface.pre_sync')


async def hook_license_update(middleware, *args, **kwargs):
    FailoverService.HA_MODE = None
    FailoverService.HA_LICENSED = None

    if not await middleware.call('failover.licensed'):
        return

    # setup the local heartbeat interface
    heartbeat = True
    try:
        await middleware.call('failover.ensure_remote_client')
    except Exception:
        middleware.logger.warning('Failed to ensure remote client on active')
        heartbeat = False

    if heartbeat:
        # setup the remote controller
        try:
            await middleware.call('failover.send_small_file', '/data/license')
        except Exception:
            middleware.logger.warning('Failed to sync db to standby')

        try:
            await middleware.call('failover.call_remote', 'failover.ensure_remote_client')
        except Exception:
            middleware.logger.warning('Failed to ensure remote client on standby')

        try:
            await middleware.call('failover.call_remote', 'etc.generate', ['rc'])
        except Exception:
            middleware.logger.warning('etc.generate failed on standby')

    await middleware.call('failover.status_refresh')


async def hook_post_rollback_setup_ha(middleware, *args, **kwargs):
    """
    This hook needs to be run after a NIC rollback operation and before
    an `interfaces.sync` operation on a TrueNAS HA system
    """
    if not await middleware.call('failover.licensed'):
        return

    try:
        await middleware.call('failover.call_remote', 'core.ping')
    except Exception:
        middleware.logger.debug('[HA] Failed to contact standby controller', exc_info=True)
        return

    await middleware.call('failover.send_database')

    middleware.logger.debug('[HA] Successfully sent database to standby controller')


async def hook_setup_ha(middleware, *args, **kwargs):
    if not await middleware.call('failover.licensed'):
        return

    if not await middleware.call('interface.query', [('failover_virtual_aliases', '!=', [])]):
        return

    if not await middleware.call('pool.query'):
        return

    # If we have reached this stage make sure status is up to date
    await middleware.call('failover.status_refresh')

    try:
        ha_configured = await middleware.call(
            'failover.call_remote', 'failover.status'
        ) != 'SINGLE'
    except Exception:
        ha_configured = False

    if ha_configured:
        # If HA is already configured and failover has been disabled,
        # and we have gotten to this point, then this means a few things could be happening.
        #    1. a new interface is being added
        #    2. an alias is being added to an already configured interface
        #    3. an interface is being modified (changing vhid/ip etc)
        #    4. an interface is being deleted

        # In the event #2 happens listed above, there is a race condition that
        # must be accounted for. When an alias is added to an already configured interface,
        # a CARP event will be triggered and the interface will go from MASTER to INIT->BACKUP->MASTER which
        # generates a devd event that is processed by the failover.event plugin.
        # It takes a few seconds for the kernel to transition the CARP interface from BACKUP->MASTER.
        # However, we refresh the failover.status while this interface is transitioning.
        # This means that failover.status returns 'ERROR'.
        # To work around this we check 2 things:
        #    1. if failover.status == 'MASTER' then we continue
        #    or
        #    2. the node in the chassis is marked as the master_node in the webUI
        #      (because failover has been disabled in the webUI)
        cur_status = await middleware.call('failover.status')
        config = await middleware.call('failover.config')
        if cur_status == 'MASTER' or (config['master'] and config['disabled']):

            # In the event HA is configured and the end-user deletes
            # an interface, we need to sync the database over to the
            # standby node before we call `interface.sync`
            middleware.logger.debug('[HA] Sending database to standby node')
            await middleware.call('failover.send_database')

            # Need to send the zpool cachefile to the other node so it matches
            # when a failover event occurs
            middleware.logger.debug('[HA] Sending zpool cachefile to standby node')
            await middleware.call('failover.send_small_file', ZPOOL_CACHE_FILE, ZPOOL_CACHE_FILE_OVERWRITE)
            await middleware.call('failover.call_remote', 'failover.zpool.cachefile.setup', ['SYNC'])

            middleware.logger.debug('[HA] Configuring network on standby node')
            await middleware.call('failover.call_remote', 'interface.sync')

        return

    # when HA is initially setup, we don't synchronize service states to the
    # standby controller. Minimally, however, it's nice to synchronize ssh
    # (if appropriate, of course)
    filters = [('srv_service', '=', 'ssh')]
    ssh_enabled = remote_ssh_started = False
    ssh = await middleware.call('datastore.query', 'services.services', filters)
    if ssh:
        if ssh[0]['srv_enable']:
            ssh_enabled = True
        if await middleware.call('failover.call_remote', 'service.started', ['ssh']):
            remote_ssh_started = True

    middleware.logger.debug('[HA] Setting up')

    middleware.logger.debug('[HA] Synchronizing database and files')
    await middleware.call('failover.sync_to_peer')

    middleware.logger.debug('[HA] Configuring network on standby node')
    await middleware.call('failover.call_remote', 'interface.sync')

    if ssh_enabled and not remote_ssh_started:
        middleware.logger.debug('[HA] Starting SSH on standby node')
        await middleware.call('failover.call_remote', 'service.start', ['ssh'])

    middleware.logger.debug('[HA] Resfreshing failover status')
    await middleware.call('failover.status_refresh')

    middleware.logger.info('[HA] Setup complete')

    middleware.send_event('failover.setup', 'ADDED', fields={})


async def hook_pool_export(middleware, pool=None, *args, **kwargs):
    await middleware.call('enclosure.sync_zpool', pool)
    await middleware.call('failover.remove_encryption_keys', {'pools': [pool]})


async def hook_pool_post_import(middleware, pool):
    if pool and pool['encrypt'] == 2 and pool['passphrase']:
        await middleware.call(
            'failover.update_encryption_keys', {
                'pools': [{'name': pool['name'], 'passphrase': pool['passphrase']}]
            }
        )


async def hook_pool_dataset_unlock(middleware, datasets):
    datasets = [
        {'name': ds['name'], 'passphrase': ds['encryption_key']}
        for ds in datasets if ds['key_format'].upper() == 'PASSPHRASE'
    ]
    if datasets:
        await middleware.call('failover.update_encryption_keys', {'datasets': datasets})


async def hook_pool_dataset_post_create(middleware, dataset_data):
    if dataset_data['encrypted']:
        if str(dataset_data['key_format']).upper() == 'PASSPHRASE':
            await middleware.call(
                'failover.update_encryption_keys', {
                    'datasets': [{'name': dataset_data['name'], 'passphrase': dataset_data['encryption_key']}]
                }
            )
        else:
            kmip = await middleware.call('kmip.config')
            if kmip['enabled'] and kmip['manage_zfs_keys']:
                await middleware.call('failover.sync_keys_to_remote_node')


async def hook_pool_dataset_post_delete_lock(middleware, dataset):
    await middleware.call('failover.remove_encryption_keys', {'datasets': [dataset]})


async def hook_pool_dataset_change_key(middleware, dataset_data):
    if dataset_data['key_format'] == 'PASSPHRASE' or dataset_data['old_key_format'] == 'PASSPHRASE':
        if dataset_data['key_format'] == 'PASSPHRASE':
            await middleware.call(
                'failover.update_encryption_keys', {
                    'datasets': [{'name': dataset_data['name'], 'passphrase': dataset_data['encryption_key']}]
                }
            )
        else:
            await middleware.call('failover.remove_encryption_keys', {'datasets': [dataset_data['name']]})
    else:
        kmip = await middleware.call('kmip.config')
        if kmip['enabled'] and kmip['manage_zfs_keys']:
            await middleware.call('failover.sync_keys_to_remote_node')


async def hook_pool_dataset_inherit_parent_encryption_root(middleware, dataset):
    await middleware.call('failover.remove_encryption_keys', {'datasets': [dataset]})


async def hook_kmip_sync(middleware, *args, **kwargs):
    await middleware.call('failover.sync_keys_to_remote_node')


async def service_remote(middleware, service, verb, options):
    """
    Most of service actions need to be replicated to the standby node so we don't lose
    too much time during failover regenerating things (e.g. users database)

    This is the middleware side of what legacy UI did on service changes.
    """
    if not options['ha_propagate']:
        return
    # Skip if service is blacklisted or we are not MASTER
    if service in (
        'system',
        'webshell',
        'smartd',
        'nfs',
    ) or await middleware.call('failover.status') != 'MASTER':
        return
    # Nginx should never be stopped on standby node
    if service == 'nginx' and verb == 'stop':
        return
    try:
        await middleware.call('failover.call_remote', 'core.bulk', [
            f'service.{verb}', [[service, options]]
        ])
    except Exception as e:
        if not (isinstance(e, CallError) and e.errno in (errno.ECONNREFUSED, errno.ECONNRESET)):
            middleware.logger.warning(f'Failed to run {verb}({service})', exc_info=True)


async def ready_system_sync_keys(middleware):
    await middleware.call('failover.sync_keys_from_remote_node')


async def _event_system(middleware, event_type, args):
    if args['id'] == 'ready':
        # called when system is ready to issue an event in case HA upgrade is pending.
        if await middleware.call('failover.status') in ('MASTER', 'SINGLE'):
            return

        if await middleware.call('keyvalue.get', 'HA_UPGRADE', False):
            middleware.send_event('failover.upgrade_pending', 'ADDED', id='BACKUP', fields={'pending': True})
    elif args['id'] == 'shutdown':
        await middleware.call('failover.fenced.stop', True)


def remote_status_event(middleware, *args, **kwargs):
    middleware.call_sync('failover.status_refresh')


async def setup(middleware):
    middleware.event_register('failover.setup', 'Sent when failover is being setup.')
    middleware.event_register('failover.status', 'Sent when failover status changes.', no_auth_required=True)
    middleware.event_register('failover.upgrade_pending', textwrap.dedent('''\
        Sent when system is ready and HA upgrade is pending.

        It is expected the client will react by issuing `upgrade_finish` call
        at user will.'''))
    middleware.event_subscribe('system', _event_system)
    middleware.register_hook('core.on_connect', ha_permission, sync=True)
    middleware.register_hook('interface.pre_sync', interface_pre_sync_hook, sync=True)
    middleware.register_hook('interface.post_sync', hook_setup_ha, sync=True)
    middleware.register_hook('interface.post_rollback', hook_post_rollback_setup_ha, sync=True)
    middleware.register_hook('pool.post_create_or_update', hook_setup_ha, sync=True)
    middleware.register_hook('pool.post_export', hook_pool_export, sync=True)
    middleware.register_hook('pool.post_import', hook_setup_ha, sync=True)
    middleware.register_hook('pool.post_import', hook_pool_post_import, sync=True)
    middleware.register_hook('dataset.post_create', hook_pool_dataset_post_create, sync=True)
    middleware.register_hook('dataset.post_delete', hook_pool_dataset_post_delete_lock, sync=True)
    middleware.register_hook('dataset.post_lock', hook_pool_dataset_post_delete_lock, sync=True)
    middleware.register_hook('dataset.post_unlock', hook_pool_dataset_unlock, sync=True)
    middleware.register_hook('dataset.change_key', hook_pool_dataset_change_key, sync=True)
    middleware.register_hook(
        'dataset.inherit_parent_encryption_root', hook_pool_dataset_inherit_parent_encryption_root, sync=True
    )
    middleware.register_hook('kmip.sed_keys_sync', hook_kmip_sync, sync=True)
    middleware.register_hook('kmip.zfs_keys_sync', hook_kmip_sync, sync=True)
    middleware.register_hook('system.post_license_update', hook_license_update, sync=False)
    middleware.register_hook('service.pre_action', service_remote, sync=False)

    # Register callbacks to properly refresh HA status and send events on changes
    await middleware.call('failover.remote_subscribe', 'system', remote_status_event)
    await middleware.call('failover.remote_on_connect', remote_status_event)
    await middleware.call('failover.remote_on_disconnect', remote_status_event)

    if await middleware.call('system.ready'):
        asyncio.ensure_future(ready_system_sync_keys(middleware))
