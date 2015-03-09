# -*- coding: utf-8 -*-
# Copyright (c) Metaswitch Networks 2015. All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
felix.felix
~~~~~~~~~~~

The main logic for Felix.
"""

# Monkey-patch before we do anything else...
from calico.felix.devices import watch_interfaces
from calico.felix.fetcd import watch_etcd
from calico.felix.ipsets import IpsetPool
from gevent import monkey
monkey.patch_all()

import os

import logging
import gevent

from calico import common
from calico.felix.fiptables import IptablesUpdater, DispatchChains, \
    ActiveProfileManager
from calico.felix.frules import install_global_rules
from calico.felix.dbcache import UpdateSequencer

_log = logging.getLogger(__name__)


def _main_greenlet(config):
    """
    The root of our tree of greenlets.  Responsible for restarting
    its children if desired.
    """

    ipset_pool = IpsetPool()
    v4_updater = IptablesUpdater(ip_version=4)
    v6_updater = IptablesUpdater(ip_version=6)
    iptables_updaters = {
        4: v4_updater,
        6: v6_updater,
    }
    profile_manager = ActiveProfileManager(iptables_updaters)
    dispatch_chains = DispatchChains(iptables_updaters)
    update_sequencer = UpdateSequencer(ipset_pool, v4_updater, v6_updater,
                                       dispatch_chains, profile_manager)

    profile_manager.start()
    dispatch_chains.start()
    ipset_pool.start()
    update_sequencer.start()
    v4_updater.start()
    v6_updater.start()
    greenlets = [profile_manager.greenlet,
                 dispatch_chains.greenlet,
                 update_sequencer.greenlet,
                 ipset_pool.greenlet,
                 v4_updater.greenlet,
                 v6_updater.greenlet]

    # Install the global rules before we start polling for updates.
    iface_prefix = "tap"
    install_global_rules(config, iface_prefix, v4_updater, v6_updater)

    # Start polling for updates.
    greenlets.append(gevent.spawn(watch_etcd, update_sequencer))
    greenlets.append(gevent.spawn(watch_interfaces, update_sequencer))

    # Wait for something to fail.
    # TODO: Maybe restart failed greenlets.
    stopped_greenlets_iter = gevent.iwait(greenlets)
    stopped_greenlet = next(stopped_greenlets_iter)
    try:
        stopped_greenlet.get()
    except Exception:
        _log.exception("Greenlet failed: %s", stopped_greenlet)
        raise
    else:
        _log.error("Greenlet %s unexpectedly returned.", stopped_greenlet)
        raise AssertionError("Greenlet unexpectedly returned")


class FakeConfig(object):
    pass


def main():
    try:
        # Initialise the logging with default parameters.
        common.default_logging()

        # TODO: Load config
        config = FakeConfig()
        config.METADATA_IP = "127.0.0.1"
        config.METADATA_PORT = 8080

        # FIXME: old felix used argparse but that's not in Python 2.6.
        _log.info("Starting up")
        gevent.spawn(_main_greenlet, config).join()  # Should never return
    except BaseException:
        # Make absolutely sure that we exit by asking the OS to terminate our
        # process.  We don't want to let a stray background thread keep us
        # alive.
        _log.exception("Felix exiting due to exception")
        os._exit(1)
        raise  # Unreachable but keeps the linter happy about the broad except.
