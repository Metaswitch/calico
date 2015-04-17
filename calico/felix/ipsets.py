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
felix.ipsets
~~~~~~~~~~~~

IP sets management functions.
"""
from collections import defaultdict

import logging
import os
import tempfile

from calico.felix import futils
from calico.felix.futils import IPV4, IPV6, FailedSystemCall
from calico.felix.actor import actor_message
from calico.felix.refcount import ReferenceManager, RefCountedActor

_log = logging.getLogger(__name__)

FELIX_PFX = "felix-"
IPSET_PREFIX = { IPV4: FELIX_PFX+"v4-", IPV6: FELIX_PFX+"v6-" }
IPSET_TMP_PREFIX = { IPV4: FELIX_PFX+"tmp-v4-", IPV6: FELIX_PFX+"tmp-v6-" }


def tag_to_ipset_name(ip_type, tag, tmp=False):
    """
    Turn a tag ID in all its glory into an ipset name.
    """
    if not tmp:
        name = IPSET_PREFIX[ip_type] + tag
    else:
        name = IPSET_TMP_PREFIX[ip_type] + tag
    return name


class IpsetManager(ReferenceManager):
    def __init__(self, ip_type):
        """
        Manages all the ipsets for tags for either IPv4 or IPv6.

        :param ip_type: IP type (IPV4 or IPV6)
        """
        super(IpsetManager, self).__init__(qualifier=ip_type)

        self.ip_type = ip_type

        # State.
        self.tags_by_prof_id = {}
        self.endpoints_by_ep_id = {}

        # Main index self.ip_owners_by_tag[tag][ip] == set([endpoint_id])
        self.ip_owners_by_tag = defaultdict(lambda: defaultdict(set))
        # And the actual ip memberships
        self.ips_in_tag = defaultdict(set)

        self.endpoint_ids_by_profile_id = defaultdict(set)

    def _create(self, tag_id):
        # Create the ActiveIpset, and put a message on the queue that will
        # trigger it to update the ipset as soon as it starts. Note that we do
        # this now so that it is sure to be processed with the first batch even
        # if other messages are arriving.
        active_ipset = ActiveIpset(futils.uniquely_shorten(tag_id, 16),
                                   self.ip_type)
        members = self.ips_in_tag.get(tag_id, set())
        active_ipset.replace_members(members, async=True)
        return active_ipset

    def _on_object_started(self, tag_id, ipset):
        _log.debug("ActiveIpset actor for %s started", tag_id)

    @property
    def nets_key(self):
        nets = "ipv4_nets" if self.ip_type == IPV4 else "ipv6_nets"
        return nets

    @actor_message()
    def apply_snapshot(self, tags_by_prof_id, endpoints_by_id):
        _log.info("Applying tags snapshot. %s tags, %s endpoints",
                  len(tags_by_prof_id), len(endpoints_by_id))
        missing_profile_ids = set(self.tags_by_prof_id.keys())
        for profile_id, tags in tags_by_prof_id.iteritems():
            assert tags is not None
            self.on_tags_update(profile_id, tags)
            missing_profile_ids.discard(profile_id)
            self._maybe_yield()
        for profile_id in missing_profile_ids:
            self.on_tags_update(profile_id, None)
            self._maybe_yield()
        del missing_profile_ids
        missing_endpoints = set(self.endpoints_by_ep_id.keys())
        for endpoint_id, endpoint in endpoints_by_id.iteritems():
            assert endpoint is not None
            self.on_endpoint_update(endpoint_id, endpoint)
            missing_endpoints.discard(endpoint_id)
            self._maybe_yield()
        for ep_id in missing_endpoints:
            self.on_endpoint_update(ep_id, None)
            self._maybe_yield()
        _log.info("Tags snapshot applied: %s tags, %s endpoints",
                  len(tags_by_prof_id), len(endpoints_by_id))

    @actor_message()
    def cleanup(self):
        """
        Clean up left-over ipsets that existed at start-of-day.
        """
        _log.info("Cleaning up left-over ipsets.")
        all_ipsets = list_ipset_names()
        # only clean up our own rubbish.
        pfx = IPSET_PREFIX[self.ip_type]
        tmppfx = IPSET_TMP_PREFIX[self.ip_type]
        felix_ipsets = set([n for n in all_ipsets if n.startswith(pfx) or
                                                     n.startswith(tmppfx)])
        whitelist = set()
        for ipset in self.objects_by_id.values():
            # Ask the ipset for all the names it may use and whitelist.
            whitelist.update(ipset.owned_ipset_names())
        _log.debug("Whitelisted ipsets: %s", whitelist)
        ipsets_to_delete = felix_ipsets - whitelist
        _log.debug("Deleting ipsets: %s", ipsets_to_delete)
        # Delete the ipsets before we return.  We can't queue these up since
        # that could conflict if someone increffed one of the ones we're about
        # to delete.
        for ipset_name in ipsets_to_delete:
            try:
                futils.check_call(["ipset", "destroy", ipset_name])
            except FailedSystemCall:
                _log.exception("Failed to clean up dead ipset %s, will "
                               "retry on next cleanup.", ipset_name)

    @actor_message()
    def on_tags_update(self, profile_id, tags):
        """
        Called when the given tag list has changed or been deleted.
        :param list[str]|NoneType tags: List of tags for the given profile or
            None if deleted.
        """
        _log.info("Tags for profile %s updated", profile_id)

        # General approach is to default to the empty list if the new/old
        # tag list is missing; then add/delete falls out: all the tags will
        # end up in either added_tags or removed_tags.
        old_tags = set(self.tags_by_prof_id.get(profile_id, []))
        new_tags = set(tags or [])

        endpoint_ids = self.endpoint_ids_by_profile_id.get(profile_id, set())
        added_tags = new_tags - old_tags
        removed_tags = old_tags - new_tags

        _log.debug("Endpoint IDs with this profile: %s", endpoint_ids)
        _log.debug("Profile %s added tags: %s", profile_id, added_tags)
        _log.debug("Profile %s removed tags: %s", profile_id, removed_tags)

        for endpoint_id in endpoint_ids:
            endpoint = self.endpoints_by_ep_id.get(endpoint_id, {})
            ip_addrs = self._extract_ips(endpoint)
            for tag_id in removed_tags:
                for ip in ip_addrs:
                    self._remove_mapping(tag_id, endpoint_id, ip)
            for tag_id in added_tags:
                for ip in ip_addrs:
                    self._add_mapping(tag_id, endpoint_id, ip)

        if tags is None:
            _log.info("Tags for profile %s deleted", profile_id)
            self.tags_by_prof_id.pop(profile_id, None)
        else:
            self.tags_by_prof_id[profile_id] = tags

    def _extract_ips(self, endpoint):
        if endpoint is None:
            return set()
        return set(map(futils.net_to_ip,
                       endpoint.get(self.nets_key, [])))


    @actor_message()
    def on_endpoint_update(self, endpoint_id, endpoint):
        """
        Update tag memberships and indexes with the new endpoint dict.

        :param str endpoint_id: ID of the endpoint.
        :param dict|NoneType endpoint: Either a dict containing endpoint
            information or None to indicate deletion.

        """

        # Endpoint updates are the most complex to handle because they may
        # change the profile ID (and hence the set of tags) as well as the
        # ip addresses attached to the interface.  In addition, the endpoint
        # may or may not have existed before.
        #
        # General approach: force all the possibilities through the same
        # update loops by defaulting values.  For example, if there was no
        # previous endpoint then we default old_tags to the empty set.  Then,
        # when we calculate removed_tags, we'll get the empty set and the
        # removal loop will be skipped.
        old_endpoint = self.endpoints_by_ep_id.get(endpoint_id, {})
        old_prof_id = old_endpoint.get("profile_id")
        if old_prof_id:
            old_tags = set(self.tags_by_prof_id.get(old_prof_id, []))
        else:
            old_tags = set()

        if endpoint is None:
            _log.debug("Deletion, setting new_tags to empty.")
            new_prof_id = None
            new_tags = set()
        else:
            _log.debug("Add/update, setting new_tags to indexed value.")
            new_prof_id = endpoint.get("profile_id")
            new_tags = set(self.tags_by_prof_id.get(new_prof_id, []))

        if new_prof_id != old_prof_id:
            # Profile ID changed, or an add/delete.  the _xxx_profile_index
            # methods ignore profile_id == None so we'll do the right thing.
            _log.debug("Profile ID changed from %s to %s")
            self._remove_profile_index(old_prof_id, endpoint_id)
            self._add_profile_index(new_prof_id, endpoint_id)

        # Since we've defaulted new/old_tags to set() if needed, we can
        # use set operations to calculate the tag changes.
        added_tags = new_tags - old_tags
        unchanged_tags = new_tags & old_tags
        removed_tags = old_tags - new_tags

        # _extract_ips() will default old/new_ips to set() if there are no IPs.
        old_ips = self._extract_ips(old_endpoint)
        new_ips = self._extract_ips(endpoint)

        # Remove *all* *old* IPs from removed tags.  For a deletion only this
        # loop will fire, removed_tags will be all tags and old_ips will be
        # all the old IPs.
        for tag in removed_tags:
            for ip in old_ips:
                self._remove_mapping(tag, endpoint_id, ip)
        # Change IPs in unchanged tags.
        added_ips = new_ips - old_ips
        removed_ips = old_ips - new_ips
        for tag in unchanged_tags:
            for ip in removed_ips:
                self._remove_mapping(tag, endpoint_id, ip)
            for ip in added_ips:
                self._add_mapping(tag, endpoint_id, ip)
        # Add *new* IPs to new tags.
        for tag in added_tags:
            for ip in new_ips:
                self._add_mapping(tag, endpoint_id, ip)

        _log.info("Endpoint update complete")

    def _add_mapping(self, tag_id, endpoint_id, ip_address):
        """
        Adds the given tag->endpoint->IP mapping to the index and updates
        the ActiveIpset if present.

        :return: True if the IP wasn't already in that tag.
        """
        ep_ids = self.ip_owners_by_tag[tag_id][ip_address]
        ip_added = not bool(ep_ids)
        ep_ids.add(endpoint_id)
        self.ips_in_tag[tag_id].add(ip_address)
        if ip_added and self._is_starting_or_live(tag_id):
            _log.debug("Adding %s to active tag %s", ip_address, tag_id)
            self.objects_by_id[tag_id].add_member(ip_address, async=True)
        return ip_added

    def _remove_mapping(self, tag_id, endpoint_id, ip_address):
        """
        Removes the tag->endpoint->IP mapping from indexes and updates
        any ActiveIpset if the IP is no longer present in the tag.

        :return: True if the update resulted in removing that IP from the tag.
        """
        ep_ids = self.ip_owners_by_tag[tag_id][ip_address]
        ep_ids.discard(endpoint_id)
        ip_removed = False
        if not ep_ids and ip_address in self.ips_in_tag:
            ips_in_tag = self.ips_in_tag[tag_id]
            ips_in_tag.discard(ip_address)
            if not ips_in_tag:
                del self.ips_in_tag[tag_id]
            del self.ip_owners_by_tag[tag_id][ip_address]
            ip_removed = True
        if ip_removed and self._is_starting_or_live(tag_id):
            _log.debug("Removing %s from active tag %s", ip_address, tag_id)
            self.objects_by_id[tag_id].remove_member(ip_address, async=True)
        return ip_removed

    def _add_profile_index(self, prof_id, endpoint_id):
        if prof_id is None:
            return
        self.endpoint_ids_by_profile_id[prof_id].add(endpoint_id)

    def _remove_profile_index(self, prof_id, endpoint_id):
        if prof_id is None:
            return
        endpoints = self.endpoint_ids_by_profile_id[prof_id]
        endpoints.discard(endpoint_id)
        if not endpoints:
            _log.debug("No more endpoints use profile %s", prof_id)
            del self.endpoint_ids_by_profile_id[prof_id]


class ActiveIpset(RefCountedActor):

    def __init__(self, tag, ip_type):
        """
        Actor managing a single ipset.

        :param str tag: Name of tag that this ipset represents.
        :param ip_type: IPV4 or IPV6
        """
        super(ActiveIpset, self).__init__(qualifier=tag)

        self.tag = tag
        self.ip_type = ip_type
        self.name = tag_to_ipset_name(ip_type, tag)
        self.tmpname = tag_to_ipset_name(ip_type, tag, tmp=True)
        self.family = "inet" if ip_type == IPV4 else "inet6"

        # Members - which entries should be in the ipset.
        self.members = set()

        # Members which really are in the ipset.
        self.programmed_members = None

        # Do the sets exist?
        self.set_exists = ipset_exists(self.name)
        self.tmpset_exists = ipset_exists(self.tmpname)

        # Notified ready?
        self.notified_ready = False

    def owned_ipset_names(self):
        """
        This method is safe to call from another greenlet; it only accesses
        immutable state.

        :return: set of name of ipsets that this Actor owns and manages.  the
                 sets may or may not be present.
        """
        return set([self.name, self.tmpname])

    @actor_message()
    def replace_members(self, members):
        _log.info("Replacing members of ipset %s", self.name)
        assert isinstance(members, set), "Expected members to be a set"
        self.members = members

    @actor_message()
    def add_member(self, member):
        _log.info("Adding member %s to ipset %s", member, self.name)
        if member not in self.members:
            self.members.add(member)

    @actor_message()
    def remove_member(self, member):
        _log.info("Removing member %s from ipset %s", member, self.name)
        try:
            self.members.remove(member)
        except KeyError:
            _log.info("%s was not in ipset %s", member, self.name)

    @actor_message()
    def on_unreferenced(self):
        try:
            if self.set_exists:
                futils.check_call(["ipset", "destroy", self.name])
            if self.tmpset_exists:
                futils.check_call(["ipset", "destroy", self.tmpname])
        finally:
            self._notify_cleanup_complete()

    def _finish_msg_batch(self, batch, results):
        # No need to combine members of the batch (although we could). None of
        # the add_members / remove_members / replace_members calls actually
        # does any work, just updating state. The _finish_msg_batch call will
        # then program the real changes.
        if self.members != self.programmed_members:
            self._sync_to_ipset()

        if not self.notified_ready:
            # We have created the set, so we are now ready.
            self.notified_ready = True
            self._notify_ready()

    def _sync_to_ipset(self):
        _log.debug("Setting ipset %s to %s", self.name, self.members)
        fd, filename = tempfile.mkstemp(text=True)
        f = os.fdopen(fd, "w")

        if not self.set_exists:
            # ipset does not exist, so just create it and put the data in it.
            set_name = self.name
            create = True
            swap = False
        elif not self.tmpset_exists:
            # Set exists, but tmpset does not
            set_name = self.tmpname
            create = True
            swap = True
        else:
            # Both set and tmpset exist
            set_name = self.tmpname
            create = False
            swap = True

        if create:
            f.write("create %s hash:ip family %s\n" % (set_name, self.family))
        else:
            f.write("flush %s\n" % (set_name))

        for member in self.members:
            f.write("add %s %s\n" % (set_name, member))

        if swap:
            f.write("swap %s %s\n" % (self.name, self.tmpname))
            f.write("destroy %s\n" % (self.tmpname))

        f.close()

        # Load that data.
        futils.check_call(["ipset", "restore", "-file", filename])

        # By the time we get here, the set exists, and the tmpset does not if
        # we just destroyed it after a swap (it might still exist if it did and
        # the main set did not when we started, unlikely though that seems!).
        self.set_exists = True
        if swap:
            self.tmpset_exists = False

        # Tidy up the tmp file.
        os.remove(filename)

        # We have got the set into the correct state.
        self.programmed_members = self.members.copy()


def ipset_exists(name):
    """
    Check if a set of the correct name exists.
    """
    return futils.call_silent(["ipset", "list", name]) == 0


def list_ipset_names():
    """
    List all names of ipsets. Note that this is *not* the same as the ipset
    list command which lists contents too (hence the name change).
    """
    data = futils.check_call(["ipset", "list"]).stdout
    lines = data.split("\n")

    names = []

    for line in lines:
        words = line.split()
        if len(words) > 1 and words[0] == "Name:":
            names.append(words[1])

    return names
