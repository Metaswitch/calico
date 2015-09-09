# -*- coding: utf-8 -*-
# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
felix.fetcd
~~~~~~~~~~~~

Etcd polling functions.
"""
from collections import defaultdict
import functools
import os
import random
from socket import timeout as SocketTimeout
import httplib
import json
import logging
import datetime
from calico.monotonic import monotonic_time

from etcd import EtcdException, EtcdKeyNotFound
import gevent
import sys
from gevent.event import Event
import urllib3.exceptions
from urllib3.exceptions import ReadTimeoutError, ConnectTimeoutError

from calico import common
from calico.common import ValidationFailed, validate_ip_addr, canonicalise_ip
from calico.datamodel_v1 import (VERSION_DIR, READY_KEY, CONFIG_DIR,
                                 RULES_KEY_RE, TAGS_KEY_RE, ENDPOINT_KEY_RE,
                                 dir_for_per_host_config,
                                 PROFILE_DIR, HOST_DIR, EndpointId, POLICY_DIR,
                                 HOST_IP_KEY_RE, IPAM_V4_CIDR_KEY_RE,
                                 key_for_status, key_for_uptime)
from calico.etcdutils import (
    PathDispatcher, EtcdClientOwner, EtcdWatchHelper, ResyncRequired
)
from calico.felix.actor import Actor, actor_message
from calico.felix.futils import intern_dict, intern_list, logging_exceptions

_log = logging.getLogger(__name__)


RETRY_DELAY = 5

# Etcd paths that we care about for use with the PathDispatcher class.
# We use angle-brackets to name parameters that we want to capture.
PER_PROFILE_DIR = PROFILE_DIR + "/<profile_id>"
TAGS_KEY = PER_PROFILE_DIR + "/tags"
RULES_KEY = PER_PROFILE_DIR + "/rules"
PER_HOST_DIR = HOST_DIR + "/<hostname>"
HOST_IP_KEY = PER_HOST_DIR + "/bird_ip"
WORKLOAD_DIR = PER_HOST_DIR + "/workload"
PER_ORCH_DIR = WORKLOAD_DIR + "/<orchestrator>"
PER_WORKLOAD_DIR = PER_ORCH_DIR + "/<workload_id>"
ENDPOINT_DIR = PER_WORKLOAD_DIR + "/endpoint"
PER_ENDPOINT_KEY = ENDPOINT_DIR + "/<endpoint_id>"
CONFIG_PARAM_KEY = CONFIG_DIR + "/<config_param>"
PER_HOST_CONFIG_PARAM_KEY = PER_HOST_DIR + "/config/<config_param>"

IPAM_DIR = VERSION_DIR + "/ipam"
IPAM_V4_DIR = IPAM_DIR + "/v4"
POOL_V4_DIR = IPAM_V4_DIR + "/pool"
CIDR_V4_KEY = POOL_V4_DIR + "/<pool_id>"

RESYNC_KEYS = [
    VERSION_DIR,
    POLICY_DIR,
    PROFILE_DIR,
    CONFIG_DIR,
    HOST_DIR,
    IPAM_DIR,
    IPAM_V4_DIR,
    POOL_V4_DIR,
]


class EtcdAPI(EtcdClientOwner, Actor):
    """
    Our API to etcd

    Since the python-etcd API is blocking, we defer API watches to
    a worker greenlet and communicate with it via Events.

    We use a seconds worker for writing our status back to etcd.  This
    avoids sharing the etcd client between reads and writes, which is
    problematic because we need to handle EtcdClusterIdChanged for polls
    but not for writes.
    """

    def __init__(self, config, hosts_ipset):
        super(EtcdAPI, self).__init__(config)

        # Timestamp storing when the EtcdAPI started. This info is needed
        # in order to report uptime to etcd.
        self._start_time = monotonic_time()

        # Start up the main etcd-watching greenlet.  It will wait for an
        # event from us before doing anything.
        self._watcher = _EtcdWatcher(config, hosts_ipset)
        self._watcher.link(self._on_worker_died)
        self._watcher.start()

        # Start up a greenlet to trigger periodic resyncs.
        self._resync_greenlet = gevent.spawn(self._periodically_resync)
        self._resync_greenlet.link_exception(self._on_worker_died)

        # Start up a reporting greenlet.
        self._status_reporting_greenlet = gevent.spawn(
            self._periodically_report_status
        )
        self._status_reporting_greenlet.link_exception(self._on_worker_died)

    @logging_exceptions
    def _periodically_resync(self):
        """
        Greenlet: if enabled, periodically triggers a resync from etcd.

        :return: Does not return, unless periodic resync disabled.
        """
        _log.info("Started periodic resync thread, waiting for config.")
        self._watcher.configured.wait()
        interval = self.config.RESYNC_INTERVAL
        _log.info("Config loaded, resync interval %s.", interval)
        if interval == 0:
            _log.info("Interval is 0, periodic resync disabled.")
            return
        while True:
            # Jitter by 20% of interval.
            jitter = random.random() * 0.2 * interval
            sleep_time = interval + jitter
            _log.debug("After jitter, next periodic resync will be in %.1f "
                       "seconds.", sleep_time)
            gevent.sleep(sleep_time)
            self.force_resync(reason="periodic resync", async=True)

    @logging_exceptions
    def _periodically_report_status(self):
        """
        Greenlet: periodically writes Felix's status into etcd.

        :return: Does not return, unless reporting disabled.
        """
        _log.info("Started status reporting thread.")
        ttl = self.config.REPORTING_TTL_SECS
        interval = self.config.REPORTING_INTERVAL_SECS
        _log.debug("Reporting interval: %s, TTL: %s", interval, ttl)

        if interval == 0:
            _log.info("Interval is 0, status reporting disabled.")
            return

        while True:
            try:
                self._update_felix_status(ttl)
            except (ReadTimeoutError,
                    SocketTimeout,
                    ConnectTimeoutError,
                    urllib3.exceptions.HTTPError,
                    httplib.HTTPException,
                    EtcdException) as e:
                # Sadly, we can get exceptions from any one of the layers
                # below python-etcd or from python-etcd itself.  Catch them
                # all and keep trying...
                _log.warning("Error when trying to check into etcd (%r), "
                             "retrying after %s seconds.", e, RETRY_DELAY)
                self.reconnect()
                gevent.sleep(RETRY_DELAY)
            else:
                # Jitter by 10% of interval.
                jitter = random.random() * 0.1 * interval
                sleep_time = interval + jitter
                gevent.sleep(sleep_time)

    def _update_felix_status(self, ttl):
        """
        Writes two keys to etcd:

        * uptime in secs
        * felix status in JSON - containing current time in ISO 8601 Zulu
          format

        :param: ttl int: time to live in sec - lifetime of the status report
        """
        time_now = datetime.datetime.utcnow()
        time_formatted = time_now.replace(microsecond=0).isoformat()+'Z'
        uptime = monotonic_time() - self._start_time
        status = {
            "time": time_formatted,
            "uptime": uptime,
        }

        status_value = json.dumps(status)
        uptime_value = str(uptime)

        status_key = key_for_status(self.config.HOSTNAME)
        self.client.set(status_key, status_value)

        uptime_key = key_for_uptime(self.config.HOSTNAME)
        self.client.set(uptime_key, uptime_value, ttl=ttl)

    @actor_message()
    def load_config(self):
        """
        Loads our config from etcd, should only be called once.

        :return: an event which is triggered when the config has been loaded.
        """
        self._watcher.load_config.set()
        return self._watcher.configured

    @actor_message()
    def start_watch(self, splitter):
        """
        Starts watching etcd for changes.  Implicitly loads the config
        if it hasn't been loaded yet.
        """
        self._watcher.load_config.set()
        self._watcher.splitter = splitter
        self._watcher.begin_polling.set()

    @actor_message()
    def force_resync(self, reason="unknown"):
        """
        Force a resync from etcd after the current poll completes.

        :param str reason: Optional reason to log out.
        """
        _log.info("Forcing a resync from etcd.  Reason: %s.", reason)
        self._watcher.resync_after_current_poll()

    def _on_worker_died(self, watch_greenlet):
        """
        Greenlet: spawned by the gevent Hub if the etcd watch loop ever
        stops, kills the process.
        """
        _log.critical("Worker greenlet died: %s; exiting.", watch_greenlet)
        sys.exit(1)


class _EtcdWatcher(gevent.Greenlet):
    """
    Greenlet that watches the etcd data model for changes.

    (1) Waits for the load_config event to be triggered.
    (2) Connects to etcd and waits for the Ready flag to be set,
        indicating the data model is consistent.
    (3) Loads the config from etcd and passes it to the config object.
    (4) Waits for the begin_polling Event to be triggered.
    (5) Loads a complete snapshot from etcd and passes it to the
        UpdateSplitter.
    (6) Watches etcd for changes, sending them incrementally to the
        UpdateSplitter.
    (On etcd error) starts again from step (5)

    This greenlet is expected to be managed by the EtcdAPI Actor.
    """

    def __init__(self, config, hosts_ipset):
        super(_EtcdWatcher, self).__init__(config)
        self._config = config
        self.hosts_ipset = hosts_ipset

        self.poll_helper = EtcdWatchHelper(config, VERSION_DIR)

        # Keep track of the config loaded from etcd so we can spot if it
        # changes.
        self.last_global_config = None
        self.last_host_config = None

        # Events triggered by the EtcdAPI Actor to tell us to load the config
        # and start polling.  These are one-way flags.
        self.load_config = Event()
        self.begin_polling = Event()

        # Event that we trigger once the config is loaded.
        self.configured = Event()

        # Etcd client, initialised lazily.
        self.my_config_dir = dir_for_per_host_config(self._config.HOSTNAME)

        # Polling state initialized at poll start time.
        self.splitter = None

        # Cache of known endpoints, used to resolve deletions of whole
        # directory trees.
        self.endpoint_ids_per_host = defaultdict(set)

        # Next-hop IP addresses of our hosts, if populated in etcd.
        self.ipv4_by_hostname = {}

        # Program the dispatcher with the paths we care about.  Since etcd
        # gives us a single event for a recursive directory deletion, we have
        # to handle deletes for lots of directories that we otherwise wouldn't
        # care about.
        self.dispatcher = PathDispatcher()
        reg = self.dispatcher.register
        # Top-level directories etc.  If these go away, stop polling and
        # resync.
        for key in RESYNC_KEYS:
            reg(key, on_del=self._resync)
        reg(READY_KEY, on_set=self.on_ready_flag_set, on_del=self._resync)
        # Profiles and their contents.
        reg(PER_PROFILE_DIR, on_del=self.on_profile_delete)
        reg(TAGS_KEY, on_set=self.on_tags_set, on_del=self.on_tags_delete)
        reg(RULES_KEY, on_set=self.on_rules_set, on_del=self.on_rules_delete)
        # Hosts, workloads and endpoints.
        reg(PER_HOST_DIR, on_del=self.on_host_delete)
        reg(HOST_IP_KEY,
            on_set=self.on_host_ip_set,
            on_del=self.on_host_ip_delete)
        reg(WORKLOAD_DIR, on_del=self.on_host_delete)
        reg(PER_ORCH_DIR, on_del=self.on_orch_delete)
        reg(PER_WORKLOAD_DIR, on_del=self.on_workload_delete)
        reg(ENDPOINT_DIR, on_del=self.on_workload_delete)
        reg(PER_ENDPOINT_KEY,
            on_set=self.on_endpoint_set, on_del=self.on_endpoint_delete)
        reg(CIDR_V4_KEY,
            on_set=self.on_ipam_v4_pool_set,
            on_del=self.on_ipam_v4_pool_delete)
        # Configuration keys.  If any of these is changed or set a resync is
        # done, including a full reload of configuration. If any field has
        # actually changed (as opposed to being reset to the same value or
        # explicitly set to the default, say), Felix terminates allowing the
        # init daemon to restart it.
        reg(CONFIG_PARAM_KEY,
            on_set=self._resync,
            on_del=self._resync)
        reg(PER_HOST_CONFIG_PARAM_KEY,
            on_set=self._resync,
            on_del=self._resync)

    @property
    def _client(self):
        # We use the EtcdWatchHelper to manage our client.
        return self.poll_helper.client

    def resync_after_current_poll(self):
        # Trigger a resync after this poll.  We set this flag from a different
        # thread than it is read from but that is a safe operation in Python.
        _log.info("Triggering a resync after this poll")
        self.poll_helper.resync_after_current_poll = True

    @logging_exceptions
    def _run(self):
        """
        Greenlet main loop: loads the initial dump from etcd and then
        monitors for changes and feeds them to the splitter.
        """
        self.load_config.wait()
        while True:
            _log.info("Reconnecting and loading snapshot from etcd...")
            self.poll_helper.reconnect(copy_cluster_id=False)
            self._wait_for_ready()

            # Always reload the config.  This lets us detect if the config has
            # changed and restart felix if so.
            self._load_config()

            if not self.configured.is_set():
                # Unblock anyone who's waiting on the config.
                self.configured.set()

            if not self.begin_polling.is_set():
                _log.info("etcd worker about to wait for begin_polling event")
            self.begin_polling.wait()

            try:
                # Load initial dump from etcd.  First just get all the
                # endpoints and profiles by id.  The response contains a
                # generation ID allowing us to then start polling for updates
                # without missing any.
                self._load_initial_dump()
                while True:
                    # Wait for something to change.
                    response = self.poll_helper.wait_for_etcd_event()
                    self.dispatcher.handle_event(response)
            except ResyncRequired:
                _log.info("Polling aborted, doing resync.")

    def _wait_for_ready(self):
        _log.info("Waiting for etcd to be ready...")
        ready = False
        while not ready:
            try:
                db_ready = self._client.read(READY_KEY,
                                             timeout=10).value
            except EtcdKeyNotFound:
                _log.warn("Ready flag not present in etcd; felix will pause "
                          "updates until the orchestrator sets the flag.")
                db_ready = "false"
            except EtcdException as e:
                # Note: we don't log the
                _log.error("Failed to retrieve ready flag from etcd (%r). "
                           "Felix will not receive updates until the "
                           "connection to etcd is restored.", e)
                db_ready = "false"

            if db_ready == "true":
                _log.info("etcd is ready.")
                ready = True
            else:
                _log.info("etcd not ready.  Will retry.")
                gevent.sleep(RETRY_DELAY)
                continue

    def _load_config(self):
        """
        Loads our configuration from etcd.  Does not return
        until the config is successfully loaded.

        The first call to this method populates the config object.

        Subsequent calls check the config hasn't changed and kill
        the process if it has.  This allows us to be restarted by
        the init daemon in order to pick up the new config.
        """
        while True:
            try:
                global_cfg = self._client.read(CONFIG_DIR,
                                               recursive=True)
                global_dict = _build_config_dict(global_cfg)

                try:
                    host_cfg = self._client.read(self.my_config_dir,
                                                 recursive=True)
                    host_dict = _build_config_dict(host_cfg)
                except EtcdKeyNotFound:
                    # It is not an error for there to be no per-host
                    # config; default to empty.
                    _log.info("No configuration overrides for this node")
                    host_dict = {}
            except (EtcdKeyNotFound, EtcdException) as e:
                # Note: we don't log the stack trace because it's too
                # spammy and adds little.
                _log.error("Failed to read config. etcd may be down or "
                           "the data model may not be ready: %r. Will "
                           "retry.", e)
                gevent.sleep(RETRY_DELAY)
            else:
                if self.configured.is_set():
                    # We've already been configured.  We don't yet support
                    # dynamic config update so instead we check if the config
                    # has changed and die if it has.
                    _log.info("Checking configuration for changes...")
                    if (host_dict != self.last_host_config or
                            global_dict != self.last_global_config):
                        _log.warning("Felix configuration has changed, "
                                     "felix must restart.")
                        _log.info("Old host config: %s", self.last_host_config)
                        _log.info("New host config: %s", host_dict)
                        _log.info("Old global config: %s",
                                  self.last_global_config)
                        _log.info("New global config: %s", global_dict)
                        die_and_restart()
                else:
                    # First time loading the config.  Report it to the config
                    # object.  Take copies because report_etcd_config is
                    # destructive.
                    self.last_host_config = host_dict.copy()
                    self.last_global_config = global_dict.copy()
                    self._config.report_etcd_config(host_dict, global_dict)
                return

    def _load_initial_dump(self):
        """
        Loads a snapshot from etcd and passes it to the update splitter.

        :raises ResyncRequired: if the Ready flag is not set in the snapshot.
        """
        initial_dump = self.poll_helper.load_initial_dump()
        _log.info("Loaded snapshot from etcd cluster %s, parsing it...",
                  self._client.expected_cluster_id)
        rules_by_id = {}
        tags_by_id = {}
        endpoints_by_id = {}
        ipv4_pools_by_id = {}
        self.endpoint_ids_per_host.clear()
        self.ipv4_by_hostname.clear()
        still_ready = False
        for child in initial_dump.children:
            profile_id, rules = parse_if_rules(child)
            if profile_id:
                rules_by_id[profile_id] = rules
                continue
            profile_id, tags = parse_if_tags(child)
            if profile_id:
                tags_by_id[profile_id] = tags
                continue
            endpoint_id, endpoint = parse_if_endpoint(self._config, child)
            if endpoint_id and endpoint:
                endpoints_by_id[endpoint_id] = endpoint
                self.endpoint_ids_per_host[endpoint_id.host].add(endpoint_id)
                continue
            pool_id, pool = parse_if_ipam_v4_pool(child)
            if pool_id and pool:
                ipv4_pools_by_id[pool_id] = pool
                continue
            if self._config.IP_IN_IP_ENABLED:
                hostname, ip = parse_if_host_ip(child)
                if hostname and ip:
                    self.ipv4_by_hostname[hostname] = ip
                    continue

            # Double-check the flag hasn't changed since we read it before.
            if child.key == READY_KEY:
                if child.value == "true":
                    still_ready = True
                else:
                    _log.warning("Aborting resync because ready flag was"
                                 "unset since we read it.")
                    raise ResyncRequired()

        if not still_ready:
            _log.warn("Aborting resync; ready flag no longer present.")
            raise ResyncRequired()

        # Actually apply the snapshot. This does not return anything, but
        # just sends the relevant messages to the relevant threads to make
        # all the processing occur.
        _log.info("Snapshot parsed, passing to update splitter")
        self.splitter.apply_snapshot(rules_by_id,
                                     tags_by_id,
                                     endpoints_by_id,
                                     ipv4_pools_by_id,
                                     async=True)
        if self._config.IP_IN_IP_ENABLED:
            # We only support IPv4 for host tracking right now so there's not
            # much point in going via the splitter.
            # FIXME Support IP-in-IP for IPv6.
            _log.info("Sending (%d) host IPs to ipset.",
                      len(self.ipv4_by_hostname))
            self.hosts_ipset.replace_members(self.ipv4_by_hostname.values(),
                                             async=True)

    def _resync(self, response, **kwargs):
        """
        Force a resync.
        :raises ResyncRequired: always.
        """
        raise ResyncRequired()

    def on_ready_flag_set(self, response):
        if response.value != "true":
            raise ResyncRequired()

    def on_endpoint_set(self, response, hostname, orchestrator,
                        workload_id, endpoint_id):
        """Handler for endpoint updates, passes the update to the splitter."""
        combined_id = EndpointId(hostname, orchestrator, workload_id,
                                 endpoint_id)
        _log.debug("Endpoint %s updated", combined_id)
        self.endpoint_ids_per_host[combined_id.host].add(combined_id)
        endpoint = parse_endpoint(self._config, combined_id, response.value)
        self.splitter.on_endpoint_update(combined_id, endpoint, async=True)

    def on_endpoint_delete(self, response, hostname, orchestrator,
                           workload_id, endpoint_id):
        """Handler for endpoint deleted, passes the update to the splitter."""
        combined_id = EndpointId(hostname, orchestrator, workload_id,
                                 endpoint_id)
        _log.debug("Endpoint %s deleted", combined_id)
        self.endpoint_ids_per_host[combined_id.host].discard(combined_id)
        if not self.endpoint_ids_per_host[combined_id.host]:
            del self.endpoint_ids_per_host[combined_id.host]
        self.splitter.on_endpoint_update(combined_id, None, async=True)

    def on_rules_set(self, response, profile_id):
        """Handler for rules updates, passes the update to the splitter."""
        _log.debug("Rules for %s set", profile_id)
        rules = parse_rules(profile_id, response.value)
        profile_id = intern(profile_id.encode("utf8"))
        self.splitter.on_rules_update(profile_id, rules, async=True)

    def on_rules_delete(self, response, profile_id):
        """Handler for rules deletes, passes the update to the splitter."""
        _log.debug("Rules for %s deleted", profile_id)
        self.splitter.on_rules_update(profile_id, None, async=True)

    def on_tags_set(self, response, profile_id):
        """Handler for tags updates, passes the update to the splitter."""
        _log.debug("Tags for %s set", profile_id)
        rules = parse_tags(profile_id, response.value)
        profile_id = intern(profile_id.encode("utf8"))
        self.splitter.on_tags_update(profile_id, rules, async=True)

    def on_tags_delete(self, response, profile_id):
        """Handler for tags deletes, passes the update to the splitter."""
        _log.debug("Tags for %s deleted", profile_id)
        self.splitter.on_tags_update(profile_id, None, async=True)

    def on_profile_delete(self, response, profile_id):
        """
        Handler for a whole profile deletion

        Fakes a tag and rules delete.
        """
        # Fake deletes for the rules and tags.
        _log.debug("Whole profile %s deleted", profile_id)
        self.splitter.on_rules_update(profile_id, None, async=True)
        self.splitter.on_tags_update(profile_id, None, async=True)

    def on_host_delete(self, response, hostname):
        """
        Handler for deletion of a whole host directory.

        Deletes all the contained endpoints.
        """
        ids_on_that_host = self.endpoint_ids_per_host.pop(hostname, set())
        _log.info("Host %s deleted, removing %d endpoints",
                  hostname, len(ids_on_that_host))
        for endpoint_id in ids_on_that_host:
            self.splitter.on_endpoint_update(endpoint_id, None, async=True)
        self.on_host_ip_delete(response, hostname)

    def on_host_ip_set(self, response, hostname):
        if not self._config.IP_IN_IP_ENABLED:
            _log.debug("Ignoring update to %s because IP-in-IP is disabled",
                       response.key)
            return
        ip = parse_host_ip(hostname, response.value)
        if ip:
            self.ipv4_by_hostname[hostname] = ip
        else:
            _log.warning("Invalid IP for hostname %s: %s, treating as "
                         "deletion", hostname, response.value)
            self.ipv4_by_hostname.pop(hostname, None)
        self.hosts_ipset.replace_members(self.ipv4_by_hostname.values(),
                                         async=True)

    def on_host_ip_delete(self, response, hostname):
        if not self._config.IP_IN_IP_ENABLED:
            _log.debug("Ignoring update to %s because IP-in-IP is disabled",
                       response.key)
            return
        if self.ipv4_by_hostname.pop(hostname, None):
            self.hosts_ipset.replace_members(self.ipv4_by_hostname.values(),
                                             async=True)

    def on_ipam_v4_pool_set(self, response, pool_id):
        pool = parse_ipam_pool(pool_id, response.value)
        self.splitter.on_ipam_pool_update(pool_id, pool, async=True)

    def on_ipam_v4_pool_delete(self, response, pool_id):
        self.splitter.on_ipam_pool_update(pool_id, None, async=True)

    def on_orch_delete(self, response, hostname, orchestrator):
        """
        Handler for deletion of a whole host orchestrator directory.

        Deletes all the contained endpoints.
        """
        _log.info("Orchestrator dir %s/%s deleted, removing contained hosts",
                  hostname, orchestrator)
        orchestrator = intern(orchestrator.encode("utf8"))
        for endpoint_id in list(self.endpoint_ids_per_host[hostname]):
            if endpoint_id.orchestrator == orchestrator:
                self.splitter.on_endpoint_update(endpoint_id, None, async=True)
                self.endpoint_ids_per_host[hostname].discard(endpoint_id)
        if not self.endpoint_ids_per_host[hostname]:
            del self.endpoint_ids_per_host[hostname]

    def on_workload_delete(self, response, hostname, orchestrator,
                           workload_id):
        """
        Handler for deletion of a whole workload directory.

        Deletes all the contained endpoints.
        """
        _log.debug("Workload dir %s/%s/%s deleted, removing endpoints",
                   hostname, orchestrator, workload_id)
        orchestrator = intern(orchestrator.encode("utf8"))
        workload_id = intern(workload_id.encode("utf8"))
        for endpoint_id in list(self.endpoint_ids_per_host[hostname]):
            if (endpoint_id.orchestrator == orchestrator and
                    endpoint_id.workload == workload_id):
                self.splitter.on_endpoint_update(endpoint_id, None, async=True)
                self.endpoint_ids_per_host[hostname].discard(endpoint_id)
        if not self.endpoint_ids_per_host[hostname]:
            del self.endpoint_ids_per_host[hostname]


def die_and_restart():
    # Sleep so that we can't die more than 5 times in 10s even if someone is
    # churning the config.  This prevents our upstart/systemd jobs from giving
    # up on us.
    gevent.sleep(2)
    # Use a failure code to tell systemd that we expect to be restarted.  We
    # use os._exit() because it is bullet-proof.
    os._exit(1)


def _build_config_dict(cfg_node):
    """
    Updates the config dict provided from the given etcd node, which
    should point at a config directory.
    """
    config_dict = {}
    for child in cfg_node.children:
        key = child.key.rsplit("/").pop()
        value = str(child.value)
        config_dict[key] = value
    return config_dict


# Intern JSON keys as we load them to reduce occupancy.
FIELDS_TO_INTERN = set([
    # Endpoint dicts.  It doesn't seem worth interning items like the MAC
    # address or TAP name, which are rarely (if ever) shared.
    "profile_id",
    "profile_ids",
    "state",
    "ipv4_gateway",
    "ipv6_gateway",

    # Rules dicts.
    "protocol",
    "src_tag",
    "dst_tag",
    "action",
])
json_decoder = json.JSONDecoder(
    object_hook=functools.partial(intern_dict,
                                  fields_to_intern=FIELDS_TO_INTERN)
)


def parse_if_endpoint(config, etcd_node):
    m = ENDPOINT_KEY_RE.match(etcd_node.key)
    if m:
        # Got an endpoint.
        host = m.group("hostname")
        orch = m.group("orchestrator")
        workload_id = m.group("workload_id")
        endpoint_id = m.group("endpoint_id")
        combined_id = EndpointId(host, orch, workload_id, endpoint_id)
        if etcd_node.action == "delete":
            _log.debug("Found deleted endpoint %s", endpoint_id)
            endpoint = None
        else:
            endpoint = parse_endpoint(config, combined_id, etcd_node.value)
        # EndpointId does the interning for us.
        return combined_id, endpoint
    return None, None


def parse_endpoint(config, combined_id, raw_json):
    endpoint = safe_decode_json(raw_json,
                                log_tag="endpoint %s" % combined_id.endpoint)
    try:
        common.validate_endpoint(config, combined_id, endpoint)
    except ValidationFailed as e:
        _log.warning("Validation failed for endpoint %s, treating as "
                     "missing: %s", combined_id, e.message)
        endpoint = None
    else:
        _log.debug("Validated endpoint : %s", endpoint)
    return endpoint


def parse_if_rules(etcd_node):
    m = RULES_KEY_RE.match(etcd_node.key)
    if m:
        # Got some rules.
        profile_id = m.group("profile_id")
        if etcd_node.action == "delete":
            rules = None
        else:
            rules = parse_rules(profile_id, etcd_node.value)
        return intern(profile_id.encode("utf8")), rules
    return None, None


def parse_rules(profile_id, raw_json):
    rules = safe_decode_json(raw_json, log_tag="rules %s" % profile_id)
    try:
        common.validate_rules(profile_id, rules)
    except ValidationFailed as e:
        _log.exception("Validation failed for profile %s rules: %s; %r",
                       profile_id, rules, e)
        return None
    else:
        return rules


def parse_if_tags(etcd_node):
    m = TAGS_KEY_RE.match(etcd_node.key)
    if m:
        # Got some tags.
        profile_id = m.group("profile_id")
        if etcd_node.action == "delete":
            tags = None
        else:
            tags = parse_tags(profile_id, etcd_node.value)
        return intern(profile_id.encode("utf8")), tags
    return None, None


def parse_tags(profile_id, raw_json):
    tags = safe_decode_json(raw_json, log_tag="tags %s" % profile_id)
    try:
        common.validate_tags(profile_id, tags)
    except ValidationFailed:
        _log.exception("Validation failed for profile %s tags : %s",
                       profile_id, tags)
        return None
    else:
        # The tags aren't in a top-level object so we need to manually
        # intern them here.
        return intern_list(tags)


def parse_if_host_ip(etcd_node):
    m = HOST_IP_KEY_RE.match(etcd_node.key)
    if m:
        # Got some rules.
        hostname = m.group("hostname")
        if etcd_node.action == "delete":
            ip = None
        else:
            ip = parse_host_ip(hostname, etcd_node.value)
        return hostname, ip
    return None, None


def parse_host_ip(hostname, raw_value):
    if raw_value is None or validate_ip_addr(raw_value):
        return canonicalise_ip(raw_value, None)
    else:
        _log.debug("%s has invalid IP: %r", hostname, raw_value)
        return None


def parse_if_ipam_v4_pool(etcd_node):
    m = IPAM_V4_CIDR_KEY_RE.match(etcd_node.key)
    if m:
        # Got some rules.
        pool_id = m.group("encoded_cidr")
        if etcd_node.action == "delete":
            pool = None
        else:
            pool = parse_ipam_pool(pool_id, etcd_node.value)
        return pool_id, pool
    return None, None


def parse_ipam_pool(pool_id, raw_json):
    pool = safe_decode_json(raw_json, log_tag="ipam pool %s" % pool_id)
    try:
        common.validate_ipam_pool(pool_id, pool, 4)
    except ValidationFailed as e:
        _log.exception("Validation failed for ipam pool %s: %s; %r",
                       pool_id, pool, e)
        return None
    else:
        return pool


def safe_decode_json(raw_json, log_tag=None):
    try:
        return json_decoder.decode(raw_json)
    except (TypeError, ValueError):
        _log.warning("Failed to decode JSON for %s: %r.  Returning None.",
                     log_tag, raw_json)
        return None

