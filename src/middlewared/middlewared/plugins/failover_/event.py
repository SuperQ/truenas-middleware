import asyncio
import os
import sys
import time
import contextlib
import threading
import logging
import errno
from collections import defaultdict

from middlewared.utils import filter_list
from middlewared.service import Service, job, accepts
from middlewared.schema import Dict, Bool, Int
from middlewared.plugins.failover_.zpool_cachefile import ZPOOL_CACHE_FILE
from middlewared.plugins.failover_.event_exceptions import AllZpoolsFailedToImport, IgnoreFailoverEvent, FencedError
from libzfs import Error as libzfs_errnos


logger = logging.getLogger('failover')


# When we get to the point of transitioning to MASTER or BACKUP
# we wrap the associated methods (`vrrp_master` and `vrrp_backup`)
# in a job (lock) so that we can protect the failover event.
#
# This does a few things:
#
#    1. protects us if we have an interface that has a
#        rapid succession of state changes
#
#    2. if we have a near simultaneous amount of
#        events get triggered for all interfaces
#        --this can happen on external network failure
#        --this happens when one node reboots
#        --this happens when keepalived service is restarted
#
# If any of the above scenarios occur, we want to ensure
# that only one thread is trying to run fenced or import the
# zpools.


class FailoverEventsService(Service):

    class Config:
        private = True
        namespace = 'failover.events'

    # represents if a failover event was successful or not
    FAILOVER_RESULT = None

    # list of critical services that get restarted first
    # before the other services during a failover event
    CRITICAL_SERVICES = ['iscsitarget', 'cifs', 'nfs']

    # option to be given when changing the state of a service
    # during a failover event, we do not want to replicate
    # the state of a service to the other controller since
    # that's being handled by us explicitly
    HA_PROPAGATE = {'ha_propagate': False}

    # This file is managed in unscheduled_reboot_alert.py
    # Ticket 39114
    WATCHDOG_ALERT_FILE = '/data/sentinels/.watchdog-alert'

    # this is the time limit we place on exporting the
    # zpool(s) when becoming the BACKUP node
    ZPOOL_EXPORT_TIMEOUT = 4  # seconds

    async def restart_service(self, service, timeout):
        logger.info('Restarting %s', service)
        return await asyncio.wait_for(
            self.middleware.call('service.restart', service, self.HA_PROPAGATE),
            timeout=timeout,
        )

    @accepts(Dict(
        'restart_services',
        Bool('critical', default=False),
        Int('timeout', default=15),
    ))
    async def restart_services(self, data):
        """
        Concurrently restart services during a failover
        master event.

        `critical` Boolean when True will only restart the
        critical services.
        `timeout` Integer representing the maximum amount
        of time to wait for a given service to (re)start.
        """
        to_restart = await self.middleware.call('datastore.query', 'services_services')
        to_restart = [i['srv_service'] for i in to_restart if i['srv_enable']]
        if data['critical']:
            to_restart = [i for i in to_restart if i in self.CRITICAL_SERVICES]
        else:
            to_restart = [i for i in to_restart if i not in self.CRITICAL_SERVICES]
            # restart any kubernetes applications
            if (await self.middleware.call('kubernetes.config'))['dataset']:
                to_restart.append('kubernetes')

        exceptions = await asyncio.gather(
            *[self.restart_service(svc, data['timeout']) for svc in to_restart],
            return_exceptions=True
        )
        for svc, exc in zip(to_restart, exceptions):
            if isinstance(exc, asyncio.TimeoutError):
                logger.error(
                    'Failed to restart service "%s" after %d seconds',
                    svc, data['timeout']
                )

    async def background(self):
        """
        Some methods can be backgrounded on a failover
        event since they can take quite some time to
        finish. So background them to not hold up the
        entire failover event.
        """
        logger.info('Syncing enclosure')
        asyncio.ensure_future(self.middleware.call('enclosure.sync_zpool'))

    def run_call(self, method, *args):
        try:
            return self.middleware.call_sync(method, *args)
        except IgnoreFailoverEvent:
            # `self.validate()` calls this method
            raise
        except Exception:
            raise

    def event(self, ifname, event):

        refresh = True
        try:
            return self._event(ifname, event)
        except IgnoreFailoverEvent:
            refresh = False
        finally:
            # refreshing the failover status can cause delays in failover
            # there is no reason to refresh it if the event has been ignored
            if refresh:
                self.run_call('failover.status_refresh')

    def _export_zpools(self, volumes):

        # export the zpool(s)
        try:
            for vol in volumes:
                if vol['status'] != 'OFFLINE':
                    self.middleware.call_sync('zfs.pool.export', vol['name'], {'force': True})
                    logger.info('Exported "%s"', vol['name'])
        except Exception as e:
            # catch any exception that could be raised
            # We sleep for 5 seconds here because this is
            # in its own thread. The calling thread waits
            # for self.ZPOOL_EXPORT_TIMEOUT and if this
            # thread is_alive(), then we violently reboot
            # the node
            logger.error('Error exporting "%s" with error %s', vol['name'], e)
            time.sleep(self.ZPOOL_EXPORT_TIMEOUT + 1)

    def generate_failover_data(self):

        # only care about name, guid, and status
        volumes = self.run_call(
            'pool.query', [], {
                'select': ['name', 'guid', 'status']
            }
        )

        failovercfg = self.run_call('failover.config')
        interfaces = self.run_call('interface.query')
        internal_ints = self.run_call('failover.internal_interfaces')

        data = {
            'disabled': failovercfg['disabled'],
            'master': failovercfg['master'],
            'timeout': failovercfg['timeout'],
            'groups': defaultdict(list),
            'volumes': volumes,
            'non_crit_interfaces': [
                i['id'] for i in filter_list(interfaces, [
                    ('failover_critical', '!=', True),
                ])
            ],
            'internal_interfaces': internal_ints,
        }

        for i in filter_list(interfaces, [('failover_critical', '=', True)]):
            data['groups'][i['failover_group']].append(i['id'])

        return data

    def validate(self, ifname, event):
        """
        When a failover event is generated we need to account for a few
        scenarios.

            1. if we are currently processing a failover event and then
                receive another event and the new event is a _different_
                event than the current one, we consider this an unsolvable
                scenario. Any action we take would be assuming we know
                which controller should become MASTER/BACKUP. When this
                occurs, we play it safe and log a warning and raise an
                `IgnoreFailoverEvent` exception.

            2. if we are currently processing a failover event and then
                receive another event and the new event is the _same_
                event as the current one, we log an informational message
                and raise an `IgnoreFailoverEvent` exception.
        """

        # first check if there is an ongoing failover event
        current_events = self.run_call(
            'core.get_jobs', [
                ('OR', [
                    ('method', '=', 'failover.events.vrrp_master'),
                    ('method', '=', 'failover.events.vrrp_backup')
                ]),
                ('state', '=', 'RUNNING'),
            ]
        )
        for i in current_events:
            cur_iface = i['arguments'][1]
            cur_event = i['arguments'][2]
            msg = f'Received "{event}" event for "{ifname}" but a current event of "{cur_event}" is running'
            msg += f' for "{cur_iface}". Ignoring failover event.'
            logger.info(msg) if event == cur_event else logger.warning(msg)
            raise IgnoreFailoverEvent()

    def _event(self, ifname, event):

        # generate data to be used during the failover event
        fobj = self.generate_failover_data()

        if event != 'forcetakeover':
            if fobj['disabled'] and not fobj['master']:
                # if forcetakeover is false, and failover is disabled
                # and we're not set as the master controller, then
                # there is nothing we need to do.
                logger.warning('Failover is disabled but this node is marked as the BACKUP node. Assuming BACKUP.')
                raise IgnoreFailoverEvent()
            elif fobj['disabled']:
                raise IgnoreFailoverEvent()

            # If there is a state change on a non-critical interface then
            # ignore the event and return
            ignore = [i for i in fobj['non_crit_interfaces'] if i in ifname]
            if ignore:
                logger.warning('Ignoring state change on non-critical interface "%s".', ifname)
                raise IgnoreFailoverEvent()

            needs_imported = False
            for pool in self.run_call('pool.query', [('name', 'in', [i['name'] for i in fobj['volumes']])]):
                if pool['status'] == 'OFFLINE':
                    needs_imported = True
                    break

            # means all zpools are already imported
            if event == 'MASTER' and not needs_imported:
                logger.warning('Received a MASTER event but zpools are already imported, ignoring.')
                raise IgnoreFailoverEvent()

        # if we get here then the last verification step that
        # we need to do is ensure there aren't any current ongoing failover events
        self.run_call('failover.events.validate', ifname, event)

        # start the MASTER failover event
        if event in ('MASTER', 'forcetakeover'):
            return self.run_call('failover.events.vrrp_master', fobj, ifname, event)

        # start the BACKUP failover event
        elif event == 'BACKUP':
            return self.run_call('failover.events.vrrp_backup', fobj, ifname, event)

    @job(lock='vrrp_master')
    def vrrp_master(self, job, fobj, ifname, event):

        # vrrp does the "election" for us. If we've gotten this far
        # then the specified timeout for NOT receiving an advertisement
        # has elapsed. Setting the progress to ELECTING is to prevent
        # extensive API breakage with the platform indepedent failover plugin
        # as well as the front-end (webUI) even though the term is misleading
        # in this use case
        job.set_progress(None, description='ELECTING')

        fenced_error = None
        if event == 'forcetakeover':
            # reserve the disks forcefully ignoring if the other node has the disks
            logger.warning('Forcefully taking over as the MASTER node.')

            # need to stop fenced just in case it's running already
            self.run_call('failover.fenced.stop')

            logger.warning('Forcefully starting fenced')
            fenced_error = self.run_call('failover.fenced.start', True)
        else:
            # if we're here then we need to check a couple things before we start fenced
            # and start the process of becoming master
            #
            #   1. if the interface that we've received a MASTER event for is
            #       in a failover group with other interfaces and ANY of the
            #       other members in the failover group are still BACKUP,
            #       then we need to ignore the event.
            #
            #   TODO: Not sure how keepalived and laggs operate so need to test this
            #           (maybe the event only gets triggered if the lagg goes down)
            #
            status = self.run_call(
                'failover.vip.check_failover_group', ifname, fobj['groups']
            )

            # this means that we received a master event and the interface was
            # in a failover group. And in that failover group, there were other
            # interfaces that were still in the BACKUP state which means the
            # other node has them as MASTER so ignore the event.
            if len(status[1]):
                logger.warning(
                    'Received MASTER event for "%s", but other '
                    'interfaces "%r" are still working on the '
                    'MASTER node. Ignoring event.', ifname, status[0],
                )

                job.set_progress(None, description='IGNORED')
                raise IgnoreFailoverEvent()

            logger.warning('Entering MASTER on "%s".', ifname)

            # need to stop fenced just in case it's running already
            self.run_call('failover.fenced.stop')

            logger.warning('Starting fenced')
            fenced_error = self.run_call('failover.fenced.start')

        # starting fenced daemon failed....which is bad
        # emit an error and exit
        if fenced_error != 0:
            if fenced_error == 1:
                logger.error('Failed to register keys on disks, exiting!')
            elif fenced_error == 2:
                logger.error('Fenced is running on the remote node, exiting!')
            elif fenced_error == 3:
                logger.error('10% or more of the disks failed to be reserved, exiting!')
            elif fenced_error == 5:
                logger.error('Fenced encountered an unexpected fatal error, exiting!')
            else:
                logger.error(f'Fenced exited with code "{fenced_error}" which should never happen, exiting!')

            job.set_progress(None, description='ERROR')
            raise FencedError()

        if not fobj['volumes']:
            # means we received a master event but there are no zpools to import
            # (happens when the box is initially licensed for HA and being setup)
            # there is nothing else to do so just log a warning and return early
            logger.warning('No zpools to import, exiting failover event')
            self.FAILOVER_RESULT = 'INFO'
            return self.FAILOVER_RESULT

        # unlock SED disks
        try:
            self.run_call('disk.sed_unlock_all')
        except Exception as e:
            # failing here doesn't mean the zpool won't import
            # we could have failed on only 1 disk so log an
            # error and move on
            logger.error('Failed to unlock SED disk(s) with error: %r', e)

        # setup the zpool cachefile
        self.run_call('failover.zpool.cachefile.setup', 'MASTER')

        # set the progress to IMPORTING
        job.set_progress(None, description='IMPORTING')

        failed = []
        options = {'altroot': '/mnt'}
        import_options = {'missing_log': True}
        any_host = True
        cachefile = ZPOOL_CACHE_FILE
        new_name = None
        for vol in fobj['volumes']:
            logger.info('Importing %r', vol['name'])

            # import the zpool(s)
            try_again = False
            try:
                self.run_call(
                    'zfs.pool.import_pool', vol['guid'], options, any_host, cachefile, new_name, import_options
                )
            except Exception as e:
                error = next((i.name for i in libzfs_errnos if i.value == e.errno), '')
                if error == 'NOENT' or e.errno == errno.ENOENT:
                    # NOENT when cachefile exists and zpool isn't found from contents in cachefile
                    # ENONENT when the cachefile doesn't exist on disk
                    logger.warning('Failed importing %r using cachefile so trying without it.', vol['name'])
                    try_again = True
                else:
                    vol['error'] = str(e)
                    failed.append(vol)
                    continue

            if try_again:
                # means the cachefile is "stale" or invalid which will prevent
                # an import so let's try to import without it
                try:
                    self.run_call(
                        'zfs.pool.import_pool', vol['guid'], options, any_host, None, new_name, import_options
                    )
                except Exception as e:
                    vol['error'] = str(e)
                    failed.append(vol)
                    continue
                try:
                    # make sure the zpool cachefile property is set appropriately
                    self.run_call(
                        'zfs.pool.update', vol['name'], {'properties': {'cachefile': {'value': ZPOOL_CACHE_FILE}}}
                    )
                except Exception:
                    logger.warning('Failed to set cachefile property for %r', vol['name'], exc_info=True)

            logger.info('Successfully imported %r', vol['name'])

            # try to unlock the zfs datasets (if any)
            unlock_job = self.run_call('failover.unlock_zfs_datasets', vol['name'])
            unlock_job.wait_sync()
            if unlock_job.error:
                logger.error(f'Error unlocking ZFS encrypted datasets: {unlock_job.error}')
            elif unlock_job.result['failed']:
                logger.error('Failed to unlock %s ZFS encrypted dataset(s)', ','.join(unlock_job.result['failed']))

        # if we fail to import all zpools then alert the user because nothing
        # is going to work at this point
        if len(failed) == len(fobj['volumes']):
            for i in failed:
                logger.error(
                    'Failed to import volume with name %r with guid %r with error:\n %r',
                    i['name'], i['guid'], i['error'],
                )

            logger.error('All volumes failed to import!')
            job.set_progress(None, description='ERROR')
            raise AllZpoolsFailedToImport()

        # if we fail to import any of the zpools then alert the user but continue the process
        elif len(failed):
            for i in failed:
                logger.error(
                    'Failed to import volume with name %r with guid %r with error:\n %r',
                    i['name'], i['guid'], i['error'],
                )
                logger.error(
                    'However, other zpools imported so the failover process continued.'
                )

        logger.info('Volume imports complete.')

        # need to make sure failover status is updated in the middleware cache
        logger.info('Refreshing failover status')
        self.run_call('failover.status_refresh')

        # this enables all necessary services that have been enabled by the user
        logger.info('Enabling necessary services')
        self.run_call('etc.generate', 'rc')

        logger.info('Configuring system dataset')
        self.run_call('systemdataset.setup')

        # Write the certs to disk based on what is written in db.
        logger.info('Configuring SSL')
        self.run_call('etc.generate', 'ssl')

        # Now we restart the appropriate services to ensure it's using correct certs.
        logger.info('Configuring HTTP')
        self.run_call('service.restart', 'http')

        # now we restart the services, prioritizing the "critical" services
        logger.info('Restarting critical services.')
        self.run_call('failover.events.restart_services', {'critical': True})

        logger.info('Allowing network traffic.')
        fw_accept_job = self.run_call('failover.firewall.accept_all')
        fw_accept_job.wait_sync()
        if fw_accept_job.error:
            logger.error(f'Error allowing network traffic: {fw_accept_job.error}')

        logger.info('Critical portion of failover is now complete')

        # regenerate cron
        logger.info('Regenerating cron')
        self.run_call('etc.generate', 'cron')

        # sync disks is disabled on passive node
        logger.info('Syncing disks')
        self.run_call('disk.sync_all')

        # background any methods that can take awhile to
        # run but shouldn't hold up the entire failover
        # event
        self.run_call('failover.events.background')

        # restart the remaining "non-critical" services
        logger.info('Restarting remaining services')
        self.run_call('failover.events.restart_services', {'critical': False, 'timeout': 60})

        # start any VMs (this will log errors if the vm(s) fail to start)
        self.run_call('vm.start_on_boot')

        self.run_call('truecommand.start_truecommand_service')

        logger.info('Initializing alert system')
        self.run_call('alert.block_failover_alerts')
        self.run_call('alert.initialize', False)

        kmip_config = self.run_call('kmip.config')
        if kmip_config and kmip_config['enabled']:
            logger.info('Syncing encryption keys with KMIP server')

            # Even though we keep keys in sync, it's best that we do this as well
            # to ensure that the system is up to date with the latest keys available
            # from KMIP. If it's unaccessible, the already synced memory keys are used
            # meanwhile.
            self.run_call('kmip.initialize_keys')

        logger.info('Failover event complete.')

        # clear the description and set the result
        job.set_progress(None, description='SUCCESS')

        self.FAILOVER_RESULT = 'SUCCESS'

        return self.FAILOVER_RESULT

    @job(lock='vrrp_backup')
    def vrrp_backup(self, job, fobj, ifname, event):

        # we need to check a couple things before we stop fenced
        # and start the process of becoming backup
        #
        #   1. if the interface that we've received a BACKUP event for is
        #       in a failover group with other interfaces and ANY of the
        #       other members in the failover group are still MASTER,
        #       then we need to ignore the event.
        #
        #   TODO: Not sure how keepalived and laggs operate so need to test this
        #           (maybe the event only gets triggered if the lagg goes down)
        #
        status = self.run_call(
            'failover.vip.check_failover_group', ifname, fobj['groups']
        )

        # this means that we received a backup event and the interface was
        # in a failover group. And in that failover group, there were other
        # interfaces that were still in the MASTER state so ignore the event.
        if len(status[0]):
            logger.warning(
                'Received BACKUP event for "%s", but other '
                'interfaces "%r" are still working. '
                'Ignoring event.', ifname, status[1],
            )

            job.set_progress(None, description='IGNORED')
            raise IgnoreFailoverEvent()

        logger.warning('Entering BACKUP on "%s".', ifname)

        # we need to stop fenced first
        logger.warning('Stopping fenced')
        self.run_call('failover.fenced.stop')

        logger.info('Blocking network traffic.')
        fw_drop_job = self.run_call('failover.firewall.drop_all')
        fw_drop_job.wait_sync()
        if fw_drop_job.error:
            logger.error(f'Error blocking network traffic: {fw_drop_job.error}')

        # restarting keepalived sends a priority 0 advertisement
        # which means any VIP that is on this controller will be
        # migrated to the other controller
        logger.info('Transitioning all VIPs off this node')
        self.run_call('service.restart', 'keepalived')

        # ticket 23361 enabled a feature to send email alerts when an unclean reboot occurrs.
        # TrueNAS HA, by design, has a triggered unclean shutdown.
        # If a controller is demoted to standby, we set a 4 sec countdown using watchdog.
        # If the zpool(s) can't export within that timeframe, we use watchdog to violently reboot the controller.
        # When this occurrs, the customer gets an email about an "Unauthorized system reboot".
        # The idea for creating a new sentinel file for watchdog related panics,
        # is so that we can send an appropriate email alert.
        # So if we panic here, middleware will check for this file and send an appropriate email.
        # ticket 39114
        with contextlib.suppress(Exception):
            with open(self.WATCHDOG_ALERT_FILE, 'wb') as f:
                f.write(int(time.time()).to_bytes(4, sys.byteorder))
                f.flush()  # be sure it goes straight to disk
                os.fsync(f.fileno())  # be EXTRA sure it goes straight to disk

        # setup the zpool cachefile
        self.run_call('failover.zpool.cachefile.setup', 'BACKUP')

        # export zpools in a thread and set a timeout to
        # to `self.ZPOOL_EXPORT_TIMEOUT`.
        # if we can't export the zpool(s) in this timeframe,
        # we send the 'b' character to the /proc/sysrq-trigger
        # to trigger an immediate reboot of the system
        # https://www.kernel.org/doc/html/latest/admin-guide/sysrq.html
        export_thread = threading.Thread(
            target=self._export_zpools,
            name='failover_export_zpools',
            args=(fobj['volumes'], )
        )
        export_thread.start()
        export_thread.join(timeout=self.ZPOOL_EXPORT_TIMEOUT)
        if export_thread.is_alive():
            # have to enable the "magic" sysrq triggers
            with open('/proc/sys/kernel/sysrq', 'w') as f:
                f.write('1')

            # now violently reboot
            with open('/proc/sysrq-trigger', 'w') as f:
                f.write('b')

        # We also remove this file here, because on boot we become BACKUP if the other
        # controller is MASTER. So this means we have no volumes to export which means
        # the `self.ZPOOL_EXPORT_TIMEOUT` is honored.
        with contextlib.suppress(Exception):
            os.unlink(self.WATCHDOG_ALERT_FILE)

        logger.info('Refreshing failover status')
        self.run_call('failover.status_refresh')

        logger.info('Setting up system dataset')
        self.run_call('systemdataset.setup')

        logger.info('Restarting syslog-ng')
        self.run_call('service.restart', 'syslogd', self.HA_PROPAGATE)

        logger.info('Regenerating cron')
        self.run_call('etc.generate', 'cron')

        logger.info('Stopping smartd')
        self.run_call('service.stop', 'smartd', self.HA_PROPAGATE)

        logger.info('Stopping rrdcached')
        self.run_call('service.stop', 'rrdcached', self.HA_PROPAGATE)

        self.run_call('truecommand.stop_truecommand_service')

        # we keep SSH running on both controllers (if it's enabled by user)
        filters = [['srv_service', '=', 'ssh']]
        options = {'get': True}
        if self.run_call('datastore.query', 'services.services', filters, options)['srv_enable']:
            logger.info('Restarting SSH')
            self.run_call('service.restart', 'ssh', self.HA_PROPAGATE)

        # TODO: ALUA on SCALE??
        # do something with iscsi service here

        logger.info('Syncing encryption keys from MASTER node (if any)')
        self.run_call('failover.call_remote', 'failover.sync_keys_to_remote_node')

        logger.info('Successfully became the BACKUP node.')
        self.FAILOVER_RESULT = 'SUCCESS'

        return self.FAILOVER_RESULT


async def vrrp_fifo_hook(middleware, data):

    # `data` is a single line separated by whitespace for a total of 4 words.
    # we ignore the 1st word (vrrp instance or group) and the 4th word (priority)
    # since both of them are static in our use case
    data = data.split()

    ifname = data[1].split('_')[0].strip('"')  # interface
    event = data[2]  # the state that is being transititoned to

    # we only care about MASTER or BACKUP events currently
    if event not in ('MASTER', 'BACKUP'):
        return

    middleware.send_event(
        'failover.vrrp_event',
        'CHANGED',
        fields={
            'ifname': ifname,
            'event': event,
        }
    )

    await middleware.call('failover.events.event', ifname, event)


def setup(middleware):
    middleware.event_register('failover.vrrp_event', 'Sent when a VRRP state changes.')
    middleware.register_hook('vrrp.fifo', vrrp_fifo_hook)
