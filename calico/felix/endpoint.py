# -*- coding: utf-8 -*-
# Copyright (c) 2015 Metaswitch Networks
# All Rights Reserved.
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
felix.endpoint
~~~~~~~~~~~~~

Endpoint management.
"""

import logging
import socket
from subprocess import CalledProcessError
import gevent
from calico.felix import devices, futils
from calico.felix.actor import (Actor, actor_event, wait_and_check)
from calico.felix.refcount import ReferenceManager
from calico.felix.fiptables import DispatchChains
from calico.felix.profilerules import RulesManager
from calico.felix.frules import (CHAIN_TO_PREFIX, profile_to_chain_name,
                                 CHAIN_FROM_PREFIX)
from calico.felix.futils import FailedSystemCall

_log = logging.getLogger(__name__)

OUR_HOSTNAME = socket.gethostname()


class EndpointManager(ReferenceManager):
    def __init__(self, config, iptables_updaters, dispatch_chains,
                 rules_manager):
        super(EndpointManager, self).__init__()

        # Peers/utility classes.
        self.config = config
        self.iptables_updaters = iptables_updaters
        self.dispatch_chains = dispatch_chains
        self.rules_mgr = rules_manager

        # State
        self.endpoints_by_id = {}
        self.endpoint_id_by_iface_name = {}
        self.interfaces = {}

    def _create(self, object_id):
        return LocalEndpoint(self.config,
                             self.iptables_updaters,
                             self.dispatch_chains,
                             self.rules_mgr)

    def _on_object_activated(self, object_id, obj):
        ep = self.endpoints_by_id.get(object_id)
        obj.on_endpoint_update(ep, async=True)
        if ep:
            iface_name = ep["name"]
            obj.on_interface_update(self.interfaces.get(iface_name),
                                    async=True)

    @actor_event
    def apply_snapshot(self, endpoints_by_id):
        missing_endpoints = set(self.endpoints_by_id.keys())
        for endpoint_id, endpoint in endpoints_by_id.iteritems():
            self.on_endpoint_update(endpoint_id, endpoint)
            missing_endpoints.discard(endpoint_id)
            self._maybe_yield()
        for endpoint_id in missing_endpoints:
            self.on_endpoint_update(endpoint_id, None)
            self._maybe_yield()

    @actor_event
    def on_endpoint_update(self, endpoint_id, endpoint):
        if self._is_active(endpoint_id):
            self.objects_by_id[endpoint_id].on_endpoint_update(endpoint)
        if endpoint is None:
            # Deletion.
            _log.info("Endpoint %s deleted", endpoint_id)
            self.endpoints_by_id.pop(endpoint_id, None)
            if self._is_active(endpoint_id):
                self.decref(endpoint_id)
        else:
            self.endpoints_by_id[endpoint_id] = endpoint
        if endpoint and endpoint["host"] == OUR_HOSTNAME:
            _log.debug("Endpoint is local, ensuring it is active.")
            if not self._is_active(endpoint_id):
                # This will trigger _on_object_activated to pass the profile
                # we just saved off to the endpoint.
                self.get_and_incref(endpoint_id, async=False)

    @actor_event
    def on_interface_update(self, name, iface_state):
        if iface_state:
            self.interfaces[name] = iface_state
        else:
            self.interfaces.pop(name, None)
        endpoint_id = self.endpoint_id_by_iface_name.get(name)
        if endpoint_id and self._is_active(endpoint_id):
            ep = self.objects_by_id[endpoint_id]
            ep.on_interface_update(iface_state, async=True)


class LocalEndpoint(Actor):

    def __init__(self, config, iptables_updaters, dispatch_chains, profile_manager):
        super(LocalEndpoint, self).__init__()
        assert isinstance(dispatch_chains, DispatchChains)
        assert isinstance(profile_manager, RulesManager)
        self.config = config
        self.iptables_updaters = iptables_updaters
        self.dispatch_chains = dispatch_chains
        self.profile_mgr = profile_manager

        # Will be filled in as we learn about the OS interface and the
        # endpoint config.
        self.iface_state = None
        self.endpoint = None
        self._iface_name = None
        self._iface_suffix = None
        self._endpoint_id = None

        # Track whether the last attempt to program the dataplane succeeded.
        # We'll force a reprogram next time we get a kick.
        self._failed = False

        self._profile = None

    @actor_event
    def on_endpoint_update(self, endpoint):
        _log.debug("Endpoint updated: %s", endpoint)
        if endpoint and (not self._iface_name or not self._endpoint_id):
            self._iface_name = endpoint["name"]
            self._endpoint_id = endpoint["id"]
            self._suffix = interface_to_suffix(self.config, self._iface_name)
        was_ready = self._ready
        old_profile_id = self.endpoint and self.endpoint["profile_id"]
        new_profile_id = endpoint and endpoint["profile_id"]
        old_profile = self._profile
        if old_profile_id != new_profile_id:
            self._profile = None
            if new_profile_id is not None:
                _log.debug("Acquiring new profile %s", new_profile_id)
                self._profile = self.profile_mgr.get_and_incref(
                    new_profile_id, async=False)
                _log.debug("Acquired new profile.")
        self.endpoint = endpoint
        self._maybe_update(was_ready, async=False)  # Bypasses queue.

        if old_profile_id != new_profile_id and old_profile:
            # Release the old profile now that we no longer reference it.
            _log.debug("Returning old profile %s", old_profile_id)
            self.profile_mgr.decref(old_profile_id, async=False)

        _log.debug("%s finished processing update", self)

    @actor_event
    def on_interface_update(self, iface_state):
        _log.debug("Endpoint received new interface state: %s", iface_state)
        if iface_state and not self._iface_name:
            self._iface_name = iface_state.name
            self._suffix = interface_to_suffix(self.config, self._iface_name)
        was_ready = self._ready
        self.iface_state = iface_state
        self._maybe_update(was_ready, async=False)  # bypasses queue.

    @property
    def _missing_deps(self):
        missing_deps = []
        if not self.endpoint:
            missing_deps.append("endpoint")
        elif self.endpoint.get("state", "active") != "active":
            missing_deps.append("endpoint active")
        if not self.iface_state:
            missing_deps.append("interface")
        elif not self.iface_state.up:
            missing_deps.append("interface up")
        if not self._profile:
            missing_deps.append("profile")
        return missing_deps

    @property
    def _ready(self):
        return not self._missing_deps

    @actor_event
    def _maybe_update(self, was_ready):
        is_ready = self._ready
        if not is_ready :
            _log.debug("%s not ready, waiting on %s", self, self._missing_deps)
        if self._failed or is_ready != was_ready:
            ifce_name = self._iface_name
            if is_ready:
                # We've got all the info and everything is active.
                if self._failed:
                    _log.warn("Retrying programming after a failure")
                self._failed = False  # Ready to try again...
                _log.debug("Waiting for profile chain...")
                self._profile.ensure_chains_programmed(async=False)
                _log.debug("...profile chain ready.")
                ep_id = self.endpoint["id"]
                _log.info("%s became ready to program.", self)
                try:
                    self._update_chains()
                    self.dispatch_chains.on_endpoint_chains_ready(ifce_name,
                                                                  ep_id,
                                                                  async=True)
                    self._configure_interface()
                    # TODO: remove routes to removed IPs.
                except (OSError, FailedSystemCall, CalledProcessError):
                    _log.exception("Failed to program the dataplane for %s",
                                   self)
                    self._failed = True  # Force retry next time.
                    # Schedule a retry.
                    gevent.spawn_later(5, self._maybe_update, False)
                else:
                    _log.info("Programmed %s", self)
            else:
                # We were active but now we're not, withdraw the dispatch rule
                # and our chain.  We must do this to allow iptables to remove
                # the profile chain.
                _log.info("%s became unready.", self)
                self._failed = False  # Don't care any more.
                # Wait for the referring chain to be updated.
                self.dispatch_chains.remove_dispatch_rule(ifce_name,
                                                          async=False)
                try:
                    self._remove_chains()
                except (OSError, FailedSystemCall, CalledProcessError):
                    # Not much we can do, maybe they were deleted under us?
                    _log.exception("Failed to remove chains")
                else:
                    _log.info("Removed chains for %s", self)
                try:
                    self._deconfigure_interface()
                except (OSError, FailedSystemCall, CalledProcessError):
                    # This is likely because the interface was removed.
                    _log.warning("Failed to remove routes", exc_info=True)
                else:
                    _log.info("Removed interface config for %s", self)

    def _update_chains(self):
        futures = []
        for ip_version, updater in self.iptables_updaters.iteritems():
            chains, updates = get_endpoint_rules(
                self._suffix,
                self._iface_name,
                ip_version,
                self.endpoint.get("ipv%s_nets" % ip_version, []),
                self.endpoint["mac"],
                self.endpoint["profile_id"])
            f = updater.apply_updates("filter", chains, updates, async=True)
            futures.append(f)
        wait_and_check(futures)

    def _remove_chains(self):
        if self._endpoint_id:
            to_chain_name, from_chain_name = chain_names(self._suffix)
            futures = []
            for ip_version, updater in self.iptables_updaters.iteritems():
                f = updater.delete_chain("filter", to_chain_name, async=True)
                futures.append(f)
                f = updater.delete_chain("filter", from_chain_name, async=True)
                futures.append(f)
            wait_and_check(futures)

    def _configure_interface(self):
        """
        Applies sysctls and routes to the interface.
        """
        devices.configure_interface(self._iface_name)
        for ip_type in futils.IP_VERSIONS:
            nets_key = "ipv4_nets" if ip_type == futils.IPV4 else "ipv6_nets"
            for ip in self.endpoint.get(nets_key, []):
                # Note: this may fail if the interface has been deleted, we'll
                # catch that in the caller...
                ip = futils.net_to_ip(ip)
                devices.add_route(ip_type, ip, self._iface_name,
                                  self.endpoint["mac"])

    def _deconfigure_interface(self):
        """
        Applies sysctls and routes to the interface.
        """
        # TODO: delete routes...
        pass

    def __str__(self):
        return "Endpoint<id=%s,iface=%s>" % (self._endpoint_id or "unknown",
                                             self._iface_name or "unknown")

def interface_to_suffix(config, iface_name):
    return iface_name.replace(config.IFACE_PREFIX, "", 1)

def chain_names(endpoint_suffix):
    to_chain_name = (CHAIN_TO_PREFIX + endpoint_suffix)
    from_chain_name = (CHAIN_FROM_PREFIX + endpoint_suffix)
    return to_chain_name, from_chain_name


def get_endpoint_rules(suffix, iface, ip_version, local_ips, mac, profile_id):
    to_chain_name, from_chain_name = chain_names(suffix)

    to_chain = ["--flush %s" % to_chain_name]
    if ip_version == 6:
        #  In ipv6 only, there are 6 rules that need to be created first.
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 130
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 131
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 132
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmp router-advertisement
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmp neighbour-solicitation
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmp neighbour-advertisement
        #
        #  These rules are ICMP types 130, 131, 132, 134, 135 and 136, and can
        #  be created on the command line with something like :
        #     ip6tables -A plw -j RETURN --protocol ipv6-icmp --icmpv6-type 130
        for icmp_type in ["130", "131", "132", "134", "135", "136"]:
            to_chain.append("--append %s --jump RETURN "
                            "--protocol ipv6-icmp "
                            "--icmpv6-type %s" % (to_chain_name, icmp_type))
    to_chain.append("--append %s --match conntrack --ctstate INVALID "
                    "--jump DROP" % to_chain_name)

    # FIXME: Do we want conntrack RELATED,ESTABLISHED?
    to_chain.append("--append %s --match conntrack "
                    "--ctstate RELATED,ESTABLISHED --jump RETURN" %
                    to_chain_name)
    profile_in_chain = profile_to_chain_name("inbound", profile_id)
    to_chain.append("--append %s --goto %s" %
                    (to_chain_name, profile_in_chain))

    # Now the chain that manages packets from the interface...
    from_chain = ["--flush %s" % from_chain_name]
    if ip_version == 6:
        # In ipv6 only, allows all ICMP traffic from this endpoint to anywhere.
        from_chain.append("--append %s --protocol ipv6-icmp" % from_chain_name)

    # Conntrack rules.
    from_chain.append("--append %s --match conntrack --ctstate INVALID "
                      "--jump DROP" % from_chain_name)
    # FIXME: Do we want conntrack RELATED,ESTABLISHED?
    from_chain.append("--append %s --match conntrack "
                      "--ctstate RELATED,ESTABLISHED --jump RETURN" %
                      from_chain_name)

    if ip_version == 4:
        from_chain.append("--append %s --protocol udp --sport 68 --dport 67 "
                          "--jump RETURN" % from_chain_name)
    else:
        assert ip_version == 6
        from_chain.append("--append %s --protocol udp --sport 546 --dport 547 "
                          "--jump RETURN" % from_chain_name)

    # Anti-spoofing rules.  Only allow traffic from known (IP, MAC) pairs to
    # get to the profile chain, drop other traffic.
    profile_out_chain = profile_to_chain_name("outbound", profile_id)
    for ip in local_ips:
        if "/" in ip:
            cidr = ip
        else:
            cidr = "%s/32" % ip if ip_version == 4 else "%s/128" % ip
        # Note use of --goto rather than --jump; this means that when the
        # profile chain returns, it will return the chain that called us, not
        # this chain.
        from_chain.append("--append %s --src %s --match mac --mac-source %s "
                          "--goto %s" % (from_chain_name, cidr,
                                         mac.upper(), profile_out_chain))
    from_chain.append("--append %s --jump DROP" % from_chain_name)

    return [to_chain_name, from_chain_name], to_chain + from_chain
