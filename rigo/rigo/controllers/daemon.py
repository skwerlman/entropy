# -*- coding: utf-8 -*-
"""
Copyright (C) 2012 Fabio Erculiani

Authors:
  Fabio Erculiani

This program is free software; you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation; version 3.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details.

You should have received a copy of the GNU General Public License along with
this program; if not, write to the Free Software Foundation, Inc.,
51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
"""

import time
from threading import Lock, Semaphore

import dbus

from gi.repository import Gtk, GLib, GObject

from rigo.enums import AppActions, RigoViewStates, \
    LocalActivityStates
from rigo.models.application import Application
from rigo.ui.gtk3.widgets.notifications import NotificationBox, \
    PleaseWaitNotificationBox, LicensesNotificationBox, \
    OrphanedAppsNotificationBox

from rigo.utils import prepare_markup

from RigoDaemon.enums import ActivityStates as DaemonActivityStates, \
    AppActions as DaemonAppActions, \
    AppTransactionOutcome as DaemonAppTransactionOutcome, \
    AppTransactionStates as DaemonAppTransactionStates
from RigoDaemon.config import DbusConfig as DaemonDbusConfig

from entropy.const import const_debug_write, \
    const_debug_enabled
from entropy.misc import ParallelTask

from entropy.i18n import _, ngettext
from entropy.output import darkgreen, brown, darkred, red, blue

import entropy.tools

class RigoServiceController(GObject.Object):

    """
    This is the Rigo Application frontend to RigoDaemon.
    Handles privileged requests on our behalf.
    """

    NOTIFICATION_CONTEXT_ID = "RigoServiceControllerContextId"

    class ServiceNotificationBox(NotificationBox):

        def __init__(self, message, message_type):
            NotificationBox.__init__(
                self, message,
                tooltip=_("Good luck!"),
                message_type=message_type,
                context_id=RigoServiceController.NOTIFICATION_CONTEXT_ID)

    class SharedLocker(object):

        """
        SharedLocker ensures that Entropy Resources
        lock and unlock operations are called once,
        avoiding reentrancy, which is a property of
        lock_resources() and unlock_resources(), even
        during concurrent access.
        """

        def __init__(self, entropy_client, locked):
            self._entropy = entropy_client
            self._locking_mutex = Lock()
            self._locked = locked

        def lock(self):
            with self._locking_mutex:
                lock = False
                if not self._locked:
                    lock = True
                    self._locked = True
            if lock:
                self._entropy.lock_resources(
                    blocking=True, shared=True)

        def unlock(self):
            with self._locking_mutex:
                unlock = False
                if self._locked:
                    unlock = True
                    self._locked = False
            if unlock:
                self._entropy.unlock_resources()

    __gsignals__ = {
        # we request to lock the whole UI wrt repo
        # interaction
        "start-working" : (GObject.SignalFlags.RUN_LAST,
                           None,
                           (GObject.TYPE_PYOBJECT,
                            GObject.TYPE_PYOBJECT),
                           ),
        # Repositories have been updated
        "repositories-updated" : (GObject.SignalFlags.RUN_LAST,
                                  None,
                                  (GObject.TYPE_PYOBJECT,
                                   GObject.TYPE_PYOBJECT,),
                                  ),
        # Application actions have been completed
        "applications-managed" : (GObject.SignalFlags.RUN_LAST,
                                  None,
                                  (GObject.TYPE_PYOBJECT,
                                   GObject.TYPE_PYOBJECT,),
                                  ),
        # Application has been processed
        "application-processed" : (GObject.SignalFlags.RUN_LAST,
                                   None,
                                   (GObject.TYPE_PYOBJECT,
                                    GObject.TYPE_PYOBJECT,
                                    GObject.TYPE_PYOBJECT,),
                                   ),
        # Application is being processed
        "application-processing" : (GObject.SignalFlags.RUN_LAST,
                                    None,
                                    (GObject.TYPE_PYOBJECT,
                                     GObject.TYPE_PYOBJECT,),
                                    ),
        "application-abort" : (GObject.SignalFlags.RUN_LAST,
                               None,
                               (GObject.TYPE_PYOBJECT,
                                GObject.TYPE_PYOBJECT,),
                               ),
    }

    DBUS_INTERFACE = DaemonDbusConfig.BUS_NAME
    DBUS_PATH = DaemonDbusConfig.OBJECT_PATH

    _OUTPUT_SIGNAL = "output"
    _REPOSITORIES_UPDATED_SIGNAL = "repositories_updated"
    _TRANSFER_OUTPUT_SIGNAL = "transfer_output"
    _PING_SIGNAL = "ping"
    _RESOURCES_UNLOCK_REQUEST_SIGNAL = "resources_unlock_request"
    _RESOURCES_LOCK_REQUEST_SIGNAL = "resources_lock_request"
    _ACTIVITY_STARTED_SIGNAL = "activity_started"
    _ACTIVITY_PROGRESS_SIGNAL = "activity_progress"
    _ACTIVITY_COMPLETED_SIGNAL = "activity_completed"
    _PROCESSING_APPLICATION_SIGNAL = "processing_application"
    _APPLICATION_PROCESSING_UPDATE = "application_processing_update"
    _APPLICATION_PROCESSED_SIGNAL = "application_processed"
    _APPLICATIONS_MANAGED_SIGNAL = "applications_managed"
    _UNSUPPORTED_APPLICATIONS_SIGNAL = "unsupported_applications"
    _RESTARTING_UPGRADE_SIGNAL = "restarting_system_upgrade"
    _SUPPORTED_APIS = [0]

    def __init__(self, rigo_app, activity_rwsem,
                 entropy_client, entropy_ws):
        GObject.Object.__init__(self)
        self._rigo = rigo_app
        self._activity_rwsem = activity_rwsem
        self._nc = None
        self._bottom_nc = None
        self._wc = None
        self._avc = None
        self._apc = None
        self._terminal = None
        self._entropy = entropy_client
        self._entropy_ws = entropy_ws
        self.__dbus_main_loop = None
        self.__system_bus = None
        self.__entropy_bus = None
        self.__entropy_bus_mutex = Lock()
        self._registered_signals = {}
        self._registered_signals_mutex = Lock()

        self._local_transactions = {}
        self._local_activity = LocalActivityStates.READY
        self._local_activity_mutex = Lock()
        self._reset_daemon_transaction_state()

        self._please_wait_box = None
        self._please_wait_mutex = Lock()

        self._application_request_serializer = Lock()
        # this controls the the busy()/unbusy()
        # atomicity.
        self._application_request_mutex = Lock()

        # threads doing repo activities must coordinate
        # with this
        self._update_repositories_mutex = Lock()

    def _reset_daemon_transaction_state(self):
        """
        Reset local daemon transaction state bits.
        """
        self._daemon_activity_progress = 0
        self._daemon_processing_application_state = None
        self._daemon_transaction_app = None
        self._daemon_transaction_app_state = None
        self._daemon_transaction_app_progress = -1

    def set_applications_controller(self, avc):
        """
        Bind ApplicationsViewController object to this class.
        """
        self._avc = avc

    def set_application_controller(self, apc):
        """
        Bind ApplicationViewController object to this class.
        """
        self._apc = apc

    def set_terminal(self, terminal):
        """
        Bind a TerminalWidget to this object, in order to be used with
        events coming from dbus.
        """
        self._terminal = terminal

    def set_work_controller(self, wc):
        """
        Bind a WorkViewController to this object in order to be used to
        set progress status.
        """
        self._wc = wc

    def set_notification_controller(self, nc):
        """
        Bind a NotificationViewController to this object.
        """
        self._nc = nc

    def set_bottom_notification_controller(self, bottom_nc):
        """
        Bind a BottomNotificationViewController to this object.
        """
        self._bottom_nc = bottom_nc

    def setup(self, shared_locked):
        """
        Execute object setup once initialization phase is complete.
        This phase is comprehensive of all the set_* method calls.
        """
        if self._apc is not None:
            # connect application request events
            self._apc.connect(
                "application-request-action",
                self._on_application_request_action)

        # since we handle the lock/unlock of entropy
        # resources here, we need to know what's the
        # initial state
        self._shared_locker = self.SharedLocker(
            self._entropy, shared_locked)

    def service_available(self):
        """
        Return whether the RigoDaemon dbus service is
        available.
        """
        try:
            self._entropy_bus
            return True
        except dbus.exceptions.DBusException:
            return False

    def busy(self, local_activity):
        """
        Become busy, switch to some local activity.
        If an activity is already taking place,
        LocalActivityStates.BusyError is raised.
        If the active activity equals the requested one,
        LocalActivityStates.SameError is raised.
        """
        with self._local_activity_mutex:
            if self._local_activity == local_activity:
                raise LocalActivityStates.SameError()
            if self._local_activity != LocalActivityStates.READY:
                raise LocalActivityStates.BusyError()
            GLib.idle_add(self._bottom_nc.set_activity,
                          local_activity)
            self._local_activity = local_activity

    def unbusy(self, current_activity):
        """
        Exit from busy state, switch to local activity called "READY".
        If we're already out of any activity, raise
        LocalActivityStates.AlreadyReadyError()
        """
        with self._local_activity_mutex:
            if self._local_activity == LocalActivityStates.READY:
                raise LocalActivityStates.AlreadyReadyError()
            if self._local_activity != current_activity:
                raise LocalActivityStates.UnbusyFromDifferentActivity()
            GLib.idle_add(self._bottom_nc.set_activity,
                          LocalActivityStates.READY)
            self._local_activity = LocalActivityStates.READY

    def local_activity(self):
        """
        Return the current local activity (enum from LocalActivityStates)
        """
        return self._local_activity

    def local_transactions(self):
        """
        Return the current local transaction state mapping.
        """
        return self._local_transactions

    def supported_apis(self):
        """
        Return a list of supported RigoDaemon APIs.
        """
        return RigoServiceController._SUPPORTED_APIS

    @property
    def repositories_lock(self):
        """
        Return the Repositories Update Mutex object.
        This lock protects repositories access during their
        physical update.
        """
        return self._update_repositories_mutex

    @property
    def _dbus_main_loop(self):
        if self.__dbus_main_loop is None:
            from dbus.mainloop.glib import DBusGMainLoop
            self.__dbus_main_loop = DBusGMainLoop(set_as_default=True)
        return self.__dbus_main_loop

    @property
    def _system_bus(self):
        if self.__system_bus is None:
            self.__system_bus = dbus.SystemBus(
                mainloop=self._dbus_main_loop)
        return self.__system_bus

    @property
    def _entropy_bus(self):
        with self.__entropy_bus_mutex:
            if self.__entropy_bus is None:
                self.__entropy_bus = self._system_bus.get_object(
                    self.DBUS_INTERFACE, self.DBUS_PATH
                    )

                # ping/pong signaling, used to let
                # RigoDaemon release exclusive locks
                # when no client is connected
                self.__entropy_bus.connect_to_signal(
                    self._PING_SIGNAL, self._ping_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # Entropy stdout/stderr messages
                self.__entropy_bus.connect_to_signal(
                    self._OUTPUT_SIGNAL, self._output_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # Entropy UrlFetchers messages
                self.__entropy_bus.connect_to_signal(
                    self._TRANSFER_OUTPUT_SIGNAL,
                    self._transfer_output_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon Entropy Resources unlock requests
                self.__entropy_bus.connect_to_signal(
                    self._RESOURCES_UNLOCK_REQUEST_SIGNAL,
                    self._resources_unlock_request_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon Entropy Resources lock requests
                self.__entropy_bus.connect_to_signal(
                    self._RESOURCES_LOCK_REQUEST_SIGNAL,
                    self._resources_lock_request_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon is telling us that a new activity
                # has just begun
                self.__entropy_bus.connect_to_signal(
                    self._ACTIVITY_STARTED_SIGNAL,
                    self._activity_started_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon is telling us the activity
                # progress
                self.__entropy_bus.connect_to_signal(
                    self._ACTIVITY_PROGRESS_SIGNAL,
                    self._activity_progress_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon is telling us that an activity
                # has been completed
                self.__entropy_bus.connect_to_signal(
                    self._ACTIVITY_COMPLETED_SIGNAL,
                    self._activity_completed_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon tells us that a queue action
                # is being processed as we cycle (lol)
                self.__entropy_bus.connect_to_signal(
                    self._PROCESSING_APPLICATION_SIGNAL,
                    self._processing_application_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon tells us about an Application
                # processing status update
                self.__entropy_bus.connect_to_signal(
                    self._APPLICATION_PROCESSING_UPDATE,
                    self._application_processing_update_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon tells us that a queued app action
                # is now complete
                self.__entropy_bus.connect_to_signal(
                    self._APPLICATION_PROCESSED_SIGNAL,
                    self._application_processed_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon tells us that there are unsupported
                # applications currently installed
                self.__entropy_bus.connect_to_signal(
                    self._UNSUPPORTED_APPLICATIONS_SIGNAL,
                    self._unsupported_applications_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # RigoDaemon tells us that the currently scheduled
                # System Upgrade is being restarted due to further
                # updates being available
                self.__entropy_bus.connect_to_signal(
                    self._RESTARTING_UPGRADE_SIGNAL,
                    self._restarting_system_upgrade_signal,
                    dbus_interface=self.DBUS_INTERFACE)

            return self.__entropy_bus

    ### GOBJECT EVENTS

    def _on_application_request_action(self, apc, app, app_action):
        """
        This event comes from ApplicationViewController notifying
        that user would like to schedule the given action for App.
        "app" is an Application object, "app_action" is an AppActions
        enum value.
        """
        const_debug_write(
            __name__,
            "_on_application_request_action: "
            "%s -> %s" % (app, app_action))
        self.application_request(app, app_action)

    ### DBUS SIGNALS

    def _processing_application_signal(self, package_id, repository_id,
                                       daemon_action, daemon_tx_state):
        const_debug_write(
            __name__,
            "_processing_application_signal: received for "
            "%d, %s, action: %s, tx state: %s" % (
                package_id, repository_id, daemon_action,
                daemon_tx_state))

        def _rate_limited_set_application(app):
            _sleep_secs = 1.0
            if self._wc is not None:
                last_t = getattr(self, "_rate_lim_set_app", 0.0)
                cur_t = time.time()
                if (abs(cur_t - last_t) < _sleep_secs):
                    # yeah, we're nazi, we sleep in the mainloop
                    time.sleep(_sleep_secs)
                setattr(self, "_rate_lim_set_app", cur_t)
                self._wc.set_application(app, daemon_action)

        # circular dep trick
        app = None
        def _redraw_callback(*args):
            if self._wc is not None:
                GLib.idle_add(
                    _rate_limited_set_application, app)

        app = Application(
            self._entropy, self._entropy_ws,
            (package_id, repository_id),
            redraw_callback=_redraw_callback)

        self._daemon_processing_application_state = daemon_tx_state
        _rate_limited_set_application(app)
        self._daemon_transaction_app = app
        self._daemon_transaction_app_state = None
        self._daemon_transaction_app_progress = 0

        self.emit("application-processing", app, daemon_action)

    def _application_processing_update_signal(
        self, package_id, repository_id, app_transaction_state,
        progress):
        const_debug_write(
            __name__,
            "_application_processing_update_signal: received for "
            "%i, %s, transaction_state: %s, progress: %i" % (
                package_id, repository_id,
                app_transaction_state, progress))

        app = Application(
            self._entropy, self._entropy_ws,
            (package_id, repository_id))
        self._daemon_transaction_app = app
        self._daemon_transaction_app_progress = progress
        self._daemon_transaction_app_state = app_transaction_state

    def _application_processed_signal(self, package_id, repository_id,
                                      daemon_action, app_outcome):
        const_debug_write(
            __name__,
            "_application_processed_signal: received for "
            "%i, %s, action: %s, outcome: %s" % (
                package_id, repository_id, daemon_action, app_outcome))

        self._daemon_transaction_app = None
        self._daemon_transaction_app_progress = -1
        self._daemon_transaction_app_state = None
        app = Application(
            self._entropy, self._entropy_ws,
            (package_id, repository_id),
            redraw_callback=None)

        self.emit("application-processed", app, daemon_action,
                  app_outcome)

        if app_outcome != DaemonAppTransactionOutcome.SUCCESS:
            msg = prepare_markup(_("An <b>unknown error</b> occurred"))
            if app_outcome == DaemonAppTransactionOutcome.DOWNLOAD_ERROR:
                msg = prepare_markup(_("<b>%s</b> download failed")) % (
                    app.name,)
            elif app_outcome == DaemonAppTransactionOutcome.INSTALL_ERROR:
                msg = prepare_markup(_("<b>%s</b> install failed")) % (
                    app.name,)
            elif app_outcome == DaemonAppTransactionOutcome.REMOVE_ERROR:
                msg = prepare_markup(_("<b>%s</b> removal failed")) % (
                    app.name,)
            elif app_outcome == \
                    DaemonAppTransactionOutcome.PERMISSION_DENIED:
                msg = prepare_markup(_("<b>%s</b>, not authorized")) % (
                    app.name,)
            elif app_outcome == DaemonAppTransactionOutcome.INTERNAL_ERROR:
                msg = prepare_markup(_("<b>%s</b>, internal error")) % (
                    app.name,)
            elif app_outcome == \
                DaemonAppTransactionOutcome.DEPENDENCIES_NOT_FOUND_ERROR:
                msg = prepare_markup(
                    _("<b>%s</b> dependencies not found")) % (
                        app.name,)
            elif app_outcome == \
                DaemonAppTransactionOutcome.DEPENDENCIES_COLLISION_ERROR:
                msg = prepare_markup(
                    _("<b>%s</b> dependencies collision error")) % (
                        app.name,)
            elif app_outcome == \
                DaemonAppTransactionOutcome.DEPENDENCIES_NOT_REMOVABLE_ERROR:
                msg = prepare_markup(
                    _("<b>%s</b> dependencies not removable error")) % (
                        app.name,)

            box = NotificationBox(
                msg,
                tooltip=_("An error occurred"),
                message_type=Gtk.MessageType.ERROR,
                context_id="ApplicationProcessedSignalError")
            def _show_me(*args):
                self._bottom_nc.emit("show-work-view")
            box.add_destroy_button(_("Ok, thanks"))
            box.add_button(_("Show me"), _show_me)
            self._nc.append(box)

    def _applications_managed_signal(self, success, local_activity):
        """
        Signal coming from RigoDaemon notifying us that the
        MANAGING_APPLICATIONS is over.
        """
        with self._registered_signals_mutex:
            our_signals = self._registered_signals.get(
                self._APPLICATIONS_MANAGED_SIGNAL)
            if our_signals is None:
                # not generated by us
                return
            if our_signals:
                sig_match = our_signals.pop(0)
                sig_match.remove()
            else:
                # somebody already consumed this signal
                if const_debug_enabled():
                    const_debug_write(
                        __name__,
                        "_applications_managed_signal: "
                        "already consumed")
                return

        with self._application_request_mutex:
            # should be safe to block in here, because
            # the other thread can only block here when
            # we're not in busy state

            # This way repository in-RAM caches are reset
            # otherwise installed repository metadata becomes
            # inconsistent
            self._release_local_resources(clear_avc=False)

            # reset progress bar, we're done with it
            if self._wc is not None:
                self._wc.reset_progress()

            # we don't expect to fail here, it would
            # mean programming error.
            self.unbusy(local_activity)

            # 2 -- ACTIVITY CRIT :: OFF
            self._activity_rwsem.writer_release()

            self.emit("applications-managed", success, local_activity)

            const_debug_write(
                __name__,
                "_applications_managed_signal: applications-managed")

    def _restarting_system_upgrade_signal(self, updates_amount):
        """
        System Upgrade Activity is being restarted due to further updates
        being available. This happens when RigoDaemon processed critical
        updates during the previous activity execution.
        """
        if self._nc is not None:
            msg = "%s. %s" % (
                _("<b>System Upgrade</b> Activity is being <i>restarted</i>"),
                ngettext("There is <b>%i</b> more update",
                         "There are <b>%i</b> more updates",
                         int(updates_amount)) % (updates_amount,),)
            box = self.ServiceNotificationBox(
                prepare_markup(msg), Gtk.MessageType.INFO)
            self._nc.append(box, timeout=20)

    def _unsupported_applications_signal(self, manual_package_ids,
                                         package_ids):
        const_debug_write(
            __name__,
            "_unsupported_applications_signal: manual: "
            "%s, normal: %s" % (
                manual_package_ids, package_ids))

        self._entropy.rwsem().reader_acquire()
        try:
            repository_id = self._entropy.installed_repository(
                ).repository_id()
        finally:
            self._entropy.rwsem().reader_release()

        if manual_package_ids or package_ids:
            manual_apps = []
            apps = []
            list_objs = [(manual_package_ids, manual_apps),
                         (package_ids, apps)]
            for source_list, app_list in list_objs:
                for package_id in source_list:
                    app = Application(
                        self._entropy, self._entropy_ws,
                        (package_id, repository_id))
                    app_list.append(app)

            if self._nc is not None:
                box = OrphanedAppsNotificationBox(
                    self._apc, self, self._entropy, self._entropy_ws,
                    manual_apps, apps)
                self._nc.append(box)

    def _repositories_updated_signal(self, result, message):
        """
        Signal coming from RigoDaemon notifying us that repositories have
        been updated.
        """
        with self._registered_signals_mutex:
            our_signals = self._registered_signals.get(
                self._REPOSITORIES_UPDATED_SIGNAL)
            if our_signals is None:
                # not generated by us
                return
            if our_signals:
                sig_match = our_signals.pop(0)
                sig_match.remove()
            else:
                # somebody already consumed this signal
                if const_debug_enabled():
                    const_debug_write(
                        __name__,
                        "_repositories_updated_signal: "
                        "already consumed")
                return

        # reset progress bar, we're done with it
        if self._wc is not None:
            self._wc.reset_progress()

        local_activity = LocalActivityStates.UPDATING_REPOSITORIES
        # we don't expect to fail here, it would
        # mean programming error.
        self.unbusy(local_activity)

        # 1 -- ACTIVITY CRIT :: OFF
        self._activity_rwsem.writer_release()
        self.repositories_lock.release()

        self.emit("repositories-updated",
                  result, message)

        const_debug_write(
            __name__,
            "_repositories_updated_signal: repositories-updated")

    def _output_signal(self, text, header, footer, back, importance, level,
               count_c, count_t, percent, raw):
        """
        Entropy Client output() method from RigoDaemon comes here.
        Will be redirected to a virtual terminal here in Rigo.
        This is called in the Gtk.MainLoop.
        """
        if count_c == 0 and count_t == 0:
            count = None
        else:
            count = (count_c, count_t)

        if self._terminal is None:
            self._entropy.output(text, header=header, footer=footer,
                                 back=back, importance=importance,
                                 level=level, count=count,
                                 percent=percent)
            return

        if raw:
            self._terminal.feed_child(text.replace("\n", "\r\n"))
            return

        color_func = darkgreen
        if level == "warning":
            color_func = brown
        elif level == "error":
            color_func = darkred

        count_str = ""
        if count:
            if len(count) > 1:
                if percent:
                    fraction = float(count[0])/count[1]
                    percent_str = str(round(fraction*100, 1))
                    count_str = " ("+percent_str+"%) "
                else:
                    count_str = " (%s/%s) " % (red(str(count[0])),
                        blue(str(count[1])),)

        # reset cursor
        self._terminal.feed_child(chr(27) + '[2K')
        if back:
            msg = "\r" + color_func(">>") + " " + header + count_str + text \
                + footer
        else:
            msg = "\r" + color_func(">>") + " " + header + count_str + text \
                + footer + "\r\n"

        self._terminal.feed_child(msg)

    def _transfer_output_signal(self, average, downloaded_size, total_size,
                                data_transfer_bytes, time_remaining_secs):
        """
        Entropy UrlFetchers update() method (via transfer_output()) from
        RigoDaemon comes here. Will be redirected to WorkAreaController
        Progress Bar if available.
        """
        if self._wc is None:
            return

        fraction = float(average) / 100
        human_dt = entropy.tools.bytes_into_human(data_transfer_bytes)
        total = round(total_size, 1)

        if total > 1:
            text = "%s/%s kB @ %s/sec, %s" % (
                round(float(downloaded_size)/1024, 1),
                total,
                human_dt, time_remaining_secs)
        else:
            text = None

        self._wc.set_progress(fraction, text=text)

    def _ping_signal(self):
        """
        Need to call pong() as soon as possible to hold all Entropy
        Resources allocated by RigoDaemon.
        """
        dbus.Interface(
            self._entropy_bus,
            dbus_interface=self.DBUS_INTERFACE).pong()

    def _resources_lock_request_signal(self, activity):
        """
        RigoDaemon is asking us to acquire a shared Entropy Resources
        lock. First we check if we have released it.
        """
        const_debug_write(
            __name__,
            "_resources_lock_request_signal: "
            "called, with remote activity: %s" % (activity,))

        def _resources_lock():
            const_debug_write(
                __name__,
                "_resources_lock_request_signal._resources_lock: "
                "enter (sleep)")

            self._shared_locker.lock()
            clear_avc = True
            if activity in (
                DaemonActivityStates.MANAGING_APPLICATIONS,
                DaemonActivityStates.UPGRADING_SYSTEM,):
                clear_avc = False
            self._release_local_resources(clear_avc=clear_avc)

            const_debug_write(
                __name__,
                "_resources_lock_request_signal._resources_lock: "
                "regained shared lock")

        task = ParallelTask(_resources_lock)
        task.name = "ResourceLockAfterRelease"
        task.daemon = True
        task.start()

    def _resources_unlock_request_signal(self, activity):
        """
        RigoDaemon is asking us to release our Entropy Resources Lock.
        An ActivityStates value is provided in order to let us decide
        if we can acknowledge the request.
        """
        const_debug_write(
            __name__,
            "_resources_unlock_request_signal: "
            "called, with remote activity: %s" % (activity,))

        if activity == DaemonActivityStates.UPDATING_REPOSITORIES:

            # did we ask that or is it another client?
            local_activity = self.local_activity()
            if local_activity == LocalActivityStates.READY:

                def _update_repositories():
                    self._release_local_resources()
                    accepted = self._update_repositories(
                        [], False, master=False)
                    if accepted:
                        const_debug_write(
                            __name__,
                            "_resources_unlock_request_signal: "
                            "_update_repositories accepted, unlocking")
                        self._shared_locker.unlock()

                # another client, bend over XD
                # LocalActivityStates value will be atomically
                # switched in the above thread.
                task = ParallelTask(_update_repositories)
                task.daemon = True
                task.name = "UpdateRepositoriesExternal"
                task.start()

                const_debug_write(
                    __name__,
                    "_resources_unlock_request_signal: "
                    "somebody called repo update, starting here too")

            elif local_activity == \
                    LocalActivityStates.UPDATING_REPOSITORIES:

                def _unlocker():
                    self._release_local_resources() # CANBLOCK
                    self._shared_locker.unlock()
                task = ParallelTask(_unlocker)
                task.daemon = True
                task.name = "UpdateRepositoriesInternal"
                task.start()

                const_debug_write(
                    __name__,
                    "_resources_unlock_request_signal: "
                    "it's been us calling repositories update")
                # it's been us calling it, ignore request
                return

            else:
                const_debug_write(
                    __name__,
                    "_resources_unlock_request_signal: "
                    "not accepting RigoDaemon resources unlock request, "
                    "local activity: %s" % (local_activity,))

        elif activity == DaemonActivityStates.MANAGING_APPLICATIONS:

            local_activity = self.local_activity()
            if local_activity == LocalActivityStates.READY:

                def _application_request():
                    self._release_local_resources(clear_avc=False)
                    accepted = self._application_request(
                        None, None, master=False)
                    if accepted:
                        const_debug_write(
                            __name__,
                            "_resources_unlock_request_signal: "
                            "_application_request accepted, unlocking")
                        self._shared_locker.unlock()

                # another client, bend over XD
                # LocalActivityStates value will be atomically
                # switched in the above thread.
                task = ParallelTask(_application_request)
                task.daemon = True
                task.name = "ApplicationRequestExternal"
                task.start()

                const_debug_write(
                    __name__,
                    "_resources_unlock_request_signal: "
                    "somebody called app request, starting here too")

            elif local_activity == \
                    LocalActivityStates.MANAGING_APPLICATIONS:
                self._release_local_resources(clear_avc=False)
                self._shared_locker.unlock()

                const_debug_write(
                    __name__,
                    "_resources_unlock_request_signal: "
                    "it's been us calling manage apps")
                # it's been us calling it, ignore request
                return

        elif activity == DaemonActivityStates.UPGRADING_SYSTEM:

            local_activity = self.local_activity()
            if local_activity == LocalActivityStates.READY:

                def _upgrade_system():
                    accepted = self._upgrade_system(
                        False, master=False)
                    if accepted:
                        const_debug_write(
                            __name__,
                            "_resources_unlock_request_signal: "
                            "_upgrade_system accepted, unlocking")
                        self._shared_locker.unlock()

                # another client, bend over XD
                # LocalActivityStates value will be atomically
                # switched in the above thread.
                task = ParallelTask(_upgrade_system)
                task.daemon = True
                task.name = "UpgradeSystemExternal"
                task.start()

                const_debug_write(
                    __name__,
                    "_resources_unlock_request_signal: "
                    "somebody called sys upgrade, starting here too")

            elif local_activity == \
                    LocalActivityStates.UPGRADING_SYSTEM:
                self._shared_locker.unlock()

                const_debug_write(
                    __name__,
                    "_resources_unlock_request_signal: "
                    "it's been us calling system upgrade")
                # it's been us calling it, ignore request
                return

            else:
                const_debug_write(
                    __name__,
                    "_resources_unlock_request_signal 2: "
                    "not accepting RigoDaemon resources unlock request, "
                    "local activity: %s" % (local_activity,))


    def _activity_started_signal(self, activity):
        """
        RigoDaemon is telling us that the scheduled activity,
        either by us or by another Rigo, has just begun and
        that it, RigoDaemon, has now exclusive access to
        Entropy Resources.
        """
        const_debug_write(
            __name__,
            "_activity_started_signal: "
            "called, with remote activity: %s" % (activity,))

        self._reset_daemon_transaction_state()
        # reset please wait notification then
        self._please_wait(None)

    def _activity_progress_signal(self, activity, progress):
        """
        RigoDaemon is telling us the currently running activity
        progress.
        """
        const_debug_write(
            __name__,
            "_activity_progress_signal: "
            "called, with remote activity: %s, val: %i" % (
                activity, progress,))
        # update progress bar if it's not used for pushing
        # download state
        if self._daemon_processing_application_state == \
                DaemonAppTransactionStates.MANAGE:
            if self._wc is not None:
                prog = float(progress) / 100
                prog_txt = "%d %%" % (progress,)
                self._wc.set_progress(prog, text=prog_txt)

        self._daemon_activity_progress = progress

    def _activity_completed_signal(self, activity, success):
        """
        RigoDaemon is telling us that the scheduled activity,
        has been completed.
        """
        const_debug_write(
            __name__,
            "_activity_completed_signal: "
            "called, with remote activity: %s, success: %s" % (
                activity, success,))
        self._reset_daemon_transaction_state()

    ### GP PUBLIC METHODS

    def get_transaction_state(self):
        """
        Return current RigoDaemon Application transaction
        state information, if available.
        """
        app = self._daemon_transaction_app
        state = self._daemon_transaction_app_state
        progress = self._daemon_transaction_app_progress
        if app is None:
            state = None
            progress = -1
        if state is None:
            app = None
            progress = -1
        return app, state, progress

    def application_request(self, app, app_action, simulate=False):
        """
        Start Application Action (install/remove).
        """
        task = ParallelTask(self._application_request,
                            app, app_action, simulate=simulate)
        task.name = "ApplicationRequest{%s, %s}" % (
            app, app_action,)
        task.daemon = True
        task.start()

    def upgrade_system(self, simulate=False):
        """
        Start a System Upgrade.
        """
        task = ParallelTask(self._upgrade_system,
                            simulate=simulate)
        task.name = "UpgradeSystem{simulate=%s}" % (
            simulate,)
        task.daemon = True
        task.start()

    def update_repositories(self, repositories, force):
        """
        Start Entropy Repositories Update
        """
        task = ParallelTask(self._update_repositories,
                            repositories, force)
        task.name = "UpdateRepositoriesThread"
        task.daemon = True
        task.start()

    def activity(self):
        """
        Return RigoDaemon activity states (any of RigoDaemon.ActivityStates
        values).
        """
        return dbus.Interface(
            self._entropy_bus,
            dbus_interface=self.DBUS_INTERFACE).activity()

    def action_queue_length(self):
        """
        Return the current size of the RigoDaemon Application Action Queue.
        """
        return dbus.Interface(
            self._entropy_bus,
            dbus_interface=self.DBUS_INTERFACE).action_queue_length()

    def action(self, app):
        """
        Return Application transaction state (RigoDaemon.AppAction enum
        value).
        """
        package_id, repository_id = app.get_details().pkg
        return dbus.Interface(
            self._entropy_bus,
            dbus_interface=self.DBUS_INTERFACE).action(
            package_id, repository_id)

    def exclusive(self):
        """
        Return whether RigoDaemon is running in with
        Entropy Resources acquired in exclusive mode.
        """
        return dbus.Interface(
            self._entropy_bus,
            dbus_interface=self.DBUS_INTERFACE).exclusive()

    def api(self):
        """
        Return RigoDaemon API version
        """
        return dbus.Interface(
            self._entropy_bus,
            dbus_interface=self.DBUS_INTERFACE).api()

    def _release_local_resources(self, clear_avc=True):
        """
        Release all the local resources (like repositories)
        that shall be used by RigoDaemon.
        For example, leaving EntropyRepository objects open
        would cause sqlite3 to deadlock.
        """
        self._entropy.rwsem().writer_acquire()
        try:
            if clear_avc:
                self._avc.clear_safe()
            self._entropy.close_repositories()
        finally:
            self._entropy.rwsem().writer_release()

    def _please_wait(self, show):
        """
        Show a Please Wait NotificationBox if show is not None,
        otherwise hide, if there.
        "show" contains the NotificationBox message.
        """
        msg = _("Waiting for <b>RigoDaemon</b>, please wait...")
        with self._please_wait_mutex:

            if show and self._please_wait_box:
                return

            if not show and not self._please_wait_box:
                return

            if not show and self._please_wait_box:
                # remove from NotificationController
                # if there
                box = self._please_wait_box
                self._please_wait_box = None
                GLib.idle_add(self._nc.remove, box)
                return

            if show and not self._please_wait_box:
                # create a new Please Wait Notification Box
                sem = Semaphore(0)

                def _make():
                    box = PleaseWaitNotificationBox(
                        msg,
                        RigoServiceController.NOTIFICATION_CONTEXT_ID)
                    self._please_wait_box = box
                    sem.release()
                    self._nc.append(box)

                GLib.idle_add(_make)
                sem.acquire()

    def _update_repositories(self, repositories, force,
                             master=True):
        """
        Ask RigoDaemon to update repositories once we're
        100% sure that the UI is locked down.
        """
        # 1 -- ACTIVITY CRIT :: ON
        self._activity_rwsem.writer_acquire() # CANBLOCK

        local_activity = LocalActivityStates.UPDATING_REPOSITORIES
        try:
            self.busy(local_activity)
            # will be unlocked when we get the signal back
        except LocalActivityStates.BusyError:
            const_debug_write(__name__, "_update_repositories: "
                              "LocalActivityStates.BusyError!")
            # 1 -- ACTIVITY CRIT :: OFF
            self._activity_rwsem.writer_release()
            return False
        except LocalActivityStates.SameError:
            const_debug_write(__name__, "_update_repositories: "
                              "LocalActivityStates.SameError!")
            # 1 -- ACTIVITY CRIT :: OFF
            self._activity_rwsem.writer_release()
            return False

        self._please_wait(True)
        accepted = self._update_repositories_unlocked(
            repositories, force, master)

        if not accepted:
            self.unbusy(local_activity)
            # 1 -- ACTIVITY CRIT :: OFF
            self._activity_rwsem.writer_release()

            def _notify():
                box = self.ServiceNotificationBox(
                    prepare_markup(
                        _("Another activity is currently in progress")),
                    Gtk.MessageType.ERROR)
                box.add_destroy_button(_("K thanks"))
                self._nc.append(box)
            GLib.idle_add(_notify)

            # unhide please wait notification
            self._please_wait(False)

            return False

        return True

    def _update_repositories_unlocked(self, repositories, force,
                                      master):
        """
        Internal method handling the actual Repositories Update
        execution.
        """
        if self._wc is not None:
            GLib.idle_add(self._wc.activate_progress_bar)
            GLib.idle_add(self._wc.deactivate_app_box)

        GLib.idle_add(self.emit, "start-working",
                      RigoViewStates.WORK_VIEW_STATE, True)

        const_debug_write(__name__, "RigoServiceController: "
                          "_update_repositories_unlocked: "
                          "start-working")

        while not self._rigo.is_ui_locked():
            const_debug_write(__name__, "RigoServiceController: "
                              "_update_repositories_unlocked: "
                              "waiting Rigo UI lock")
            time.sleep(0.5)

        const_debug_write(__name__, "RigoServiceController: "
                          "_update_repositories_unlocked: "
                          "rigo UI now locked!")

        signal_sem = Semaphore(1)

        def _repositories_updated_signal(result, message):
            if not signal_sem.acquire(False):
                # already called, no need to call again
                return
            # this is done in order to have it called
            # only once by two different code paths
            self._repositories_updated_signal(
                result, message)

        with self._registered_signals_mutex:
            # connect our signal
            sig_match = self._entropy_bus.connect_to_signal(
                self._REPOSITORIES_UPDATED_SIGNAL,
                _repositories_updated_signal,
                dbus_interface=self.DBUS_INTERFACE)

            # and register it as a signal generated by us
            obj = self._registered_signals.setdefault(
                self._REPOSITORIES_UPDATED_SIGNAL, [])
            obj.append(sig_match)

        # Clear all the NotificationBoxes from upper area
        # we don't want people to click on them during the
        # the repo update. Kill the completely.
        if self._nc is not None:
            self._nc.clear_safe(managed=False)

        if self._terminal is not None:
            self._terminal.reset()

        self.repositories_lock.acquire()
        # not allowing other threads to mess with repos
        # will be released on repo updated signal

        accepted = True
        if master:
            accepted = dbus.Interface(
                self._entropy_bus,
                dbus_interface=self.DBUS_INTERFACE
                ).update_repositories(repositories, force)
        else:
            # check if we need to cope with races
            self._update_repositories_signal_check(
                sig_match, signal_sem)

        return accepted

    def _update_repositories_signal_check(self, sig_match, signal_sem):
        """
        Called via _update_repositories_unlocked() in order to handle
        the possible race between RigoDaemon signal and the fact that
        we just lost it.
        This is only called in slave mode. When we didn't spawn the
        repositories update directly.
        """
        activity = self.activity()
        if activity == DaemonActivityStates.UPDATING_REPOSITORIES:
            return

        # lost the signal or not, we're going to force
        # the callback.
        if not signal_sem.acquire(False):
            # already called, no need to call again
            const_debug_write(
                __name__,
                "_update_repositories_signal_check: abort")
            return

        const_debug_write(
            __name__,
            "_update_repositories_signal_check: accepting")
        # Run in the main loop, to avoid calling a signal
        # callback in random threads.
        GLib.idle_add(self._repositories_updated_signal,
                      0, "", activity)

    def _ask_blocking_question(self, ask_meta, message, message_type):
        """
        Ask a task blocking question to User and waits for the
        answer.
        """
        box = self.ServiceNotificationBox(
            prepare_markup(message), message_type)

        def _say_yes(widget):
            ask_meta['res'] = True
            self._nc.remove(box)
            ask_meta['sem'].release()

        def _say_no(widget):
            ask_meta['res'] = False
            self._nc.remove(box)
            ask_meta['sem'].release()

        box.add_button(_("Yes, thanks"), _say_yes)
        box.add_button(_("No, sorry"), _say_no)
        self._nc.append(box)

    def _notify_blocking_message(self, sem, message, message_type):
        """
        Notify a task blocking information to User and wait for the
        acknowledgement.
        """
        box = self.ServiceNotificationBox(
            prepare_markup(message), message_type)

        def _say_kay(widget):
            self._nc.remove(box)
            if sem is not None:
                sem.release()

        box.add_button(_("Ok then"), _say_kay)
        self._nc.append(box)

    def _notify_blocking_licenses(self, ask_meta, app, license_map):
        """
        Notify licenses that have to be accepted for Application and
        block until User answers.
        """
        box = LicensesNotificationBox(app, self._entropy, license_map)

        def _license_accepted(widget, forever):
            ask_meta['forever'] = forever
            ask_meta['res'] = True
            self._nc.remove(box)
            ask_meta['sem'].release()

        def _license_declined(widget):
            ask_meta['res'] = False
            self._nc.remove(box)
            ask_meta['sem'].release()

        box.connect("accepted", _license_accepted)
        box.connect("declined", _license_declined)
        self._nc.append(box)

    def _application_request_removal_checks(self, app):
        """
        Examine Application Removal Request on behalf of
        _application_request_checks().
        """
        removable = app.is_removable()
        if not removable:
            msg = _("<b>%s</b>\nis part of the Base"
                    " System and <b>cannot</b> be removed")
            msg = msg % (app.get_markup(),)
            message_type = Gtk.MessageType.ERROR

            GLib.idle_add(
                self._notify_blocking_message,
                None, msg, message_type)

            return False

        return True

    def _accept_licenses(self, license_list):
        """
        Accept the given list of license ids.
        """
        dbus.Interface(
            self._entropy_bus,
            dbus_interface=self.DBUS_INTERFACE).accept_licenses(
            license_list)

    def _application_request_install_checks(self, app):
        """
        Examine Application Install Request on behalf of
        _application_request_checks().
        """
        installable = True
        try:
            installable = app.is_installable()
        except Application.AcceptLicenseError as err:
            # can be installed, but licenses have to be accepted
            license_map = err.get()
            const_debug_write(
                __name__,
                "_application_request_install_checks: "
                "need to accept licenses: %s" % (license_map,))
            ask_meta = {
                'sem': Semaphore(0),
                'forever': False,
                'res': None,
            }
            GLib.idle_add(self._notify_blocking_licenses,
                          ask_meta, app, license_map)
            ask_meta['sem'].acquire()

            const_debug_write(
                __name__,
                "_application_request_install_checks: "
                "unblock, accepted:: %s, forever: %s" % (
                    ask_meta['res'], ask_meta['forever'],))

            if not ask_meta['res']:
                return False
            if ask_meta['forever']:
                self._accept_licenses(license_map.keys())
            return True

        if not installable:
            msg = prepare_markup(
                _("<b>%s</b>\ncannot be installed at this time"
                    " due to <b>missing/masked</b> dependencies or"
                    " dependency <b>conflict</b>"))
            msg = msg % (app.get_markup(),)
            message_type = Gtk.MessageType.ERROR

            GLib.idle_add(
                self._notify_blocking_message,
                None, msg, message_type)

            return False

        conflicting_apps = app.get_install_conflicts()
        if conflicting_apps:
            msg = prepare_markup(
                _("Installing <b>%s</b> would cause the removal"
                  " of the following Applications: %s"))
            msg = msg % (
                app.name,
                ", ".join(
                    ["<b>" + x.name + "</b>" for x in conflicting_apps]),)
            message_type = Gtk.MessageType.WARNING

            ask_meta = {
                'res': None,
                'sem': Semaphore(0),
            }
            GLib.idle_add(self._ask_blocking_question, ask_meta,
                          msg, message_type)
            ask_meta['sem'].acquire() # CANBLOCK
            if not ask_meta['res']:
                return False

        return True

    def _application_request_checks(self, app, daemon_action):
        """
        Examine Application Request before sending it to RigoDaemon.
        Specifically, check for things like system apps removal asking
        User confirmation.
        """
        if daemon_action == DaemonAppActions.REMOVE:
            accepted = self._application_request_removal_checks(app)
        else:
            accepted = self._application_request_install_checks(app)
        if not accepted:
            def _emit():
                self.emit("application-abort", app, daemon_action)
            GLib.idle_add(_emit)
        return accepted

    def _application_request_unlocked(self, app, daemon_action,
                                      master, busied, simulate):
        """
        Internal method handling the actual Application Request
        execution.
        """
        if app is not None:
            package_id, repository_id = app.get_details().pkg
        else:
            package_id, repository_id = None, None

        if busied:
            if self._wc is not None:
                GLib.idle_add(self._wc.activate_progress_bar)
                # this will be back active once we have something
                # to show
                GLib.idle_add(self._wc.deactivate_app_box)

            # Clear all the NotificationBoxes from upper area
            # we don't want people to click on them during the
            # the repo update. Kill the completely.
            if self._nc is not None:
                self._nc.clear_safe(managed=False)

            # emit, but we don't really need to switch to
            # the work view nor locking down the UI
            GLib.idle_add(self.emit, "start-working", None, False)

            const_debug_write(__name__, "RigoServiceController: "
                              "_application_request_unlocked: "
                              "start-working")
            # don't check if UI is locked though

            signal_sem = Semaphore(1)

            def _applications_managed_signal(success):
                if not signal_sem.acquire(False):
                    # already called, no need to call again
                    return
                # this is done in order to have it called
                # only once by two different code paths
                self._applications_managed_signal(
                    success, LocalActivityStates.MANAGING_APPLICATIONS)

            with self._registered_signals_mutex:
                # connect our signal
                sig_match = self._entropy_bus.connect_to_signal(
                    self._APPLICATIONS_MANAGED_SIGNAL,
                    _applications_managed_signal,
                    dbus_interface=self.DBUS_INTERFACE)

                # and register it as a signal generated by us
                obj = self._registered_signals.setdefault(
                    self._APPLICATIONS_MANAGED_SIGNAL, [])
                obj.append(sig_match)

        const_debug_write(
            __name__,
            "_application_request_unlocked, about to 'schedule'")

        accepted = True
        if master:
            accepted = dbus.Interface(
                self._entropy_bus,
                dbus_interface=self.DBUS_INTERFACE
                ).enqueue_application_action(
                    package_id, repository_id, daemon_action,
                    simulate)
            const_debug_write(
                __name__,
                "service enqueue_application_action, got: %s, type: %s" % (
                    accepted, type(accepted),))

            def _notify():
                queue_len = self.action_queue_length()
                msg = prepare_markup(_("<b>%s</b> action enqueued") % (
                        app.name,))
                if queue_len > 0:
                    msg += prepare_markup(ngettext(
                        ", <b>%i</b> Application enqueued so far...",
                        ", <b>%i</b> Applications enqueued so far...",
                        queue_len)) % (queue_len,)
                box = self.ServiceNotificationBox(
                    msg, Gtk.MessageType.INFO)
                self._nc.append(box, timeout=10)
            if accepted:
                GLib.idle_add(_notify)

        else:
            self._applications_managed_signal_check(
                sig_match, signal_sem,
                DaemonActivityStates.MANAGING_APPLICATIONS,
                LocalActivityStates.MANAGING_APPLICATIONS)

        return accepted

    def _applications_managed_signal_check(self, sig_match, signal_sem,
                                           daemon_activity,
                                           local_activity):
        """
        Called via _application_request_unlocked() in order to handle
        the possible race between RigoDaemon signal and the fact that
        we just lost it.
        This is only called in slave mode. When we didn't spawn the
        repositories update directly.
        """
        activity = self.activity()
        if activity == daemon_activity:
            return

        # lost the signal or not, we're going to force
        # the callback.
        if not signal_sem.acquire(False):
            # already called, no need to call again
            const_debug_write(
                __name__,
                "_applications_managed_signal_check: abort")
            return

        const_debug_write(
            __name__,
            "_applications_managed_signal_check: accepting")
        # Run in the main loop, to avoid calling a signal
        # callback in random threads.
        GLib.idle_add(self._applications_managed_signal,
                      True, local_activity)

    def _application_request(self, app, app_action, simulate=False,
                             master=True):
        """
        Forward Application Request (install or remove) to RigoDaemon.
        Make sure there isn't any other ongoing activity.
        """
        # Need to serialize access to this method because
        # we're going to acquire several resources in a non-atomic
        # way wrt access to this method.
        with self._application_request_serializer:

            with self._application_request_mutex:
                busied = True
                # since we need to writer_acquire(), which is blocking
                # better try to allocate the local activity first
                local_activity = LocalActivityStates.MANAGING_APPLICATIONS
                try:
                    self.busy(local_activity)
                except LocalActivityStates.BusyError:
                    const_debug_write(__name__, "_application_request: "
                                      "LocalActivityStates.BusyError!")
                    # doing other stuff, cannot go ahead
                    return False
                except LocalActivityStates.SameError:
                    const_debug_write(__name__, "_application_request: "
                                      "LocalActivityStates.SameError, "
                                      "no need to acquire writer")
                    # we're already doing this activity, do not acquire
                    # activity_rwsem
                    busied = False

                if busied:
                    # 2 -- ACTIVITY CRIT :: ON
                    const_debug_write(__name__, "_application_request: "
                                      "about to acquire writer end of "
                                      "activity rwsem")
                    self._activity_rwsem.writer_acquire() # CANBLOCK

                def _unbusy():
                    if busied:
                        self.unbusy(local_activity)
                        # 2 -- ACTIVITY CRIT :: OFF
                        self._activity_rwsem.writer_release()

                # clean terminal, make sure no crap is left there
                if self._terminal is not None:
                    self._terminal.reset()

            daemon_action = None
            if app_action == AppActions.INSTALL:
                daemon_action = DaemonAppActions.INSTALL
            elif app_action == AppActions.REMOVE:
                daemon_action = DaemonAppActions.REMOVE

            accepted = True
            do_notify = True
            if master:
                accepted = self._application_request_checks(
                    app, daemon_action)
                if not accepted:
                    do_notify = False
                const_debug_write(
                    __name__,
                    "_application_request, checks result: %s" % (
                        accepted,))

            if accepted:
                self._please_wait(True)
                accepted = self._application_request_unlocked(
                    app, daemon_action, master,
                    busied, simulate)

            if not accepted:
                with self._application_request_mutex:
                    _unbusy()

                def _notify():
                    box = self.ServiceNotificationBox(
                        prepare_markup(
                            _("Another activity is currently in progress")
                        ),
                        Gtk.MessageType.ERROR)
                    box.add_destroy_button(_("K thanks"))
                    self._nc.append(box)
                if do_notify:
                    GLib.idle_add(_notify)

            # unhide please wait notification
            self._please_wait(False)

            return accepted

    def _upgrade_system_license_check(self):
        """
        Examine Applications that are going to be upgraded looking for
        licenses to read and accept.
        """
        self._entropy.rwsem().reader_acquire()
        try:
            update, remove, fine, spm_fine = \
                self._entropy.calculate_updates()
            if not update:
                return True
            licenses = self._entropy.get_licenses_to_accept(update)
            if not licenses:
                return True
        finally:
            self._entropy.rwsem().reader_release()

        license_map = {}
        for lic_id, pkg_matches in licenses.items():
            obj = license_map.setdefault(lic_id, [])
            for pkg_match in pkg_matches:
                app = Application(
                    self._entropy, self._entropy_ws,
                    pkg_match)
                obj.append(app)

        const_debug_write(
            __name__,
            "_system_upgrade_license_checks: "
            "need to accept licenses: %s" % (license_map,))
        ask_meta = {
            'sem': Semaphore(0),
            'forever': False,
            'res': None,
        }
        GLib.idle_add(self._notify_blocking_licenses,
                      ask_meta, None, license_map)
        ask_meta['sem'].acquire()

        const_debug_write(
            __name__,
            "_system_upgrade_license_checks: "
            "unblock, accepted:: %s, forever: %s" % (
                ask_meta['res'], ask_meta['forever'],))

        if not ask_meta['res']:
            return False
        if ask_meta['forever']:
            self._accept_licenses(license_map.keys())
        return True

    def _upgrade_system_checks(self):
        """
        Examine System Upgrade Request before sending it to RigoDaemon.
        """
        # add license check
        accepted = self._upgrade_system_license_check()
        if not accepted:
            return False

        return True

    def _upgrade_system_unlocked(self, master, simulate):
        """
        Internal method handling the actual System Upgrade
        execution.
        """
        if self._wc is not None:
            GLib.idle_add(self._wc.activate_progress_bar)
            # this will be back active once we have something
            # to show
            GLib.idle_add(self._wc.deactivate_app_box)

        # Clear all the NotificationBoxes from upper area
        # we don't want people to click on them during the
        # the repo update. Kill the completely.
        if self._nc is not None:
            self._nc.clear_safe(managed=False)

        # emit, but we don't really need to switch to
        # the work view nor locking down the UI
        GLib.idle_add(self.emit, "start-working", None, False)

        const_debug_write(__name__, "RigoServiceController: "
                          "_upgrade_system_unlocked: "
                          "start-working")
        # don't check if UI is locked though

        signal_sem = Semaphore(1)

        def _applications_managed_signal(success):
            if not signal_sem.acquire(False):
                # already called, no need to call again
                return
            # this is done in order to have it called
            # only once by two different code paths
            self._applications_managed_signal(
                success, LocalActivityStates.UPGRADING_SYSTEM)

        with self._registered_signals_mutex:
            # connect our signal
            sig_match = self._entropy_bus.connect_to_signal(
                self._APPLICATIONS_MANAGED_SIGNAL,
                _applications_managed_signal,
                dbus_interface=self.DBUS_INTERFACE)

            # and register it as a signal generated by us
            obj = self._registered_signals.setdefault(
                self._APPLICATIONS_MANAGED_SIGNAL, [])
            obj.append(sig_match)

        const_debug_write(
            __name__,
            "_upgrade_system_unlocked, about to 'schedule'")

        accepted = True
        if master:
            accepted = dbus.Interface(
                self._entropy_bus,
                dbus_interface=self.DBUS_INTERFACE
                ).upgrade_system(simulate)
            const_debug_write(
                __name__,
                "service upgrade_system, accepted: %s" % (
                    accepted,))

            def _notify():
                msg = prepare_markup(
                    _("<b>System Upgrade</b> has begun, "
                      "now go make some coffee"))
                box = self.ServiceNotificationBox(
                    msg, Gtk.MessageType.INFO)
                self._nc.append(box, timeout=10)
            if accepted:
                GLib.idle_add(_notify)

        else:
            self._applications_managed_signal_check(
                sig_match, signal_sem,
                DaemonActivityStates.UPGRADING_SYSTEM,
                LocalActivityStates.UPGRADING_SYSTEM)

        return accepted

    def _upgrade_system(self, simulate, master=True):
        """
        Forward a System Upgrade Request to RigoDaemon.
        """
        # This code has a lot of similarities wtih the application
        # request one.
        with self._application_request_mutex:
            # since we need to writer_acquire(), which is blocking
            # better try to allocate the local activity first
            local_activity = LocalActivityStates.UPGRADING_SYSTEM
            try:
                self.busy(local_activity)
            except LocalActivityStates.BusyError:
                const_debug_write(__name__, "_upgrade_system: "
                                  "LocalActivityStates.BusyError!")
                # doing other stuff, cannot go ahead
                return False
            except LocalActivityStates.SameError:
                const_debug_write(__name__, "_upgrade_system: "
                                  "LocalActivityStates.SameError, "
                                  "aborting")
                return False

            # 3 -- ACTIVITY CRIT :: ON
            const_debug_write(__name__, "_upgrade_system: "
                              "about to acquire writer end of "
                              "activity rwsem")
            self._activity_rwsem.writer_acquire() # CANBLOCK

            def _unbusy():
                self.unbusy(local_activity)
                # 3 -- ACTIVITY CRIT :: OFF
                self._activity_rwsem.writer_release()

            # clean terminal, make sure no crap is left there
            if self._terminal is not None:
                self._terminal.reset()

        do_notify = True
        accepted = True
        if master:
            accepted = self._upgrade_system_checks()
            if not accepted:
                do_notify = False
            const_debug_write(
                __name__,
                "_upgrade_system, checks result: %s" % (
                    accepted,))

        if accepted:
            self._please_wait(True)
            accepted = self._upgrade_system_unlocked(
                master, simulate)

        if not accepted:
            with self._application_request_mutex:
                _unbusy()

            def _notify():
                box = self.ServiceNotificationBox(
                    prepare_markup(
                        _("Another activity is currently in progress")
                    ),
                    Gtk.MessageType.ERROR)
                box.add_destroy_button(_("K thanks"))
                self._nc.append(box)
            if do_notify:
                GLib.idle_add(_notify)

        # unhide please wait notification
        self._please_wait(False)

        return accepted