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
from itertools import chain

from calico.felix import futils
from calico.felix.futils import IPV4, IPV6, FailedSystemCall
from calico.felix.actor import actor_message, Actor
from calico.felix.refcount import ReferenceManager, RefCountedActor

_log = logging.getLogger(__name__)

FELIX_PFX = "felix-"
IPSET_PREFIX = { IPV4: FELIX_PFX+"v4-", IPV6: FELIX_PFX+"v6-" }
IPSET_TMP_PREFIX = { IPV4: FELIX_PFX+"tmp-v4-", IPV6: FELIX_PFX+"tmp-v6-" }


class IpsetManager(ReferenceManager):
    def __init__(self, ip_type):
        """
        Manages all the ipsets for tags for either IPv4 or IPv6.

        :param ip_type: IP type (IPV4 or IPV6)
        """
        super(IpsetManager, self).__init__(qualifier=ip_type)

        self.ip_type = ip_type

        # State.
        # Tag IDs indexed by profile IDs
        self.tags_by_prof_id = {}
        # Endpoint dicts indexed by EndpointId objects
        self.endpoints_by_ep_id = {}

        # Main index.  Since an IP address can be assigned to multiple
        # endpoints, we need to track which endpoints reference an IP.  When
        # we find the set of endpoints with an IP is empty, we remove the
        # ip from the tag.
        # ip_owners_by_tag[tag][ip][profile_id] = set([combined_id,
        #                                              combined_id2, ...])
        # Here "combined_id" is an EndpointId object.
        self.ip_owners_by_tag = defaultdict(
            lambda: defaultdict(lambda: defaultdict(set)))

        # Set of EndpointId objects referenced by profile IDs.
        self.endpoint_ids_by_profile_id = defaultdict(set)

        # Set of tag IDs that may be out of sync. Accumulated by the
        # index-update functions. We apply the updates in _finish_msg_batch().
        # May include non-live tag IDs.
        self._dirty_tags = set()

    def _create(self, tag_id):
        active_ipset = TagIpset(futils.uniquely_shorten(tag_id, 16),
                                   self.ip_type)
        return active_ipset

    def _on_object_started(self, tag_id, active_ipset):
        _log.debug("TagIpset actor for %s started", tag_id)
        # Fill the ipset in with its members, this will trigger its first
        # programming, after which it will call us back to tell us it is ready.
        # We can't use self._dirty_tags to defer this in case the set becomes
        # unreferenced before _finish_msg_batch() is called.
        self._update_active_ipset(tag_id)

    def _update_active_ipset(self, tag_id):
        """
        Replaces the members of the identified TagIpset with the
        current set.

        :param tag_id: The ID of the tag, must be an active tag.
        """
        assert self._is_starting_or_live(tag_id)
        active_ipset = self.objects_by_id[tag_id]
        members = self.ip_owners_by_tag.get(tag_id, {}).keys()
        active_ipset.replace_members(set(members), async=True)

    def _update_dirty_active_ipsets(self):
        """
        Updates the members of any live ActiveIpsets that are marked dirty.

        Clears the set of dirty tags as a side-effect.
        """
        for tag_id in self._dirty_tags:
            if self._is_starting_or_live(tag_id):
                self._update_active_ipset(tag_id)
            self._maybe_yield()
        self._dirty_tags.clear()

    @property
    def nets_key(self):
        nets = "ipv4_nets" if self.ip_type == IPV4 else "ipv6_nets"
        return nets

    @actor_message()
    def apply_snapshot(self, tags_by_prof_id, endpoints_by_id):
        """
        Apply a snapshot read from etcd, replacing existing state.

        :param tags_by_prof_id: A dict mapping security profile ID to a list of
            profile tags.
        :param endpoints_by_id: A dict mapping EndpointId objects to endpoint
            data dicts.
        """
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
        for endpoint_id in missing_endpoints:
            self.on_endpoint_update(endpoint_id, None)
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
        live_ipsets = self.objects_by_id.itervalues()
        # stopping_objects_by_id is a dict of sets of TagIpset objects,
        # chain them together.
        stopping_ipsets = chain.from_iterable(
            self.stopping_objects_by_id.itervalues())
        for ipset in chain(live_ipsets, stopping_ipsets):
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
        Called when the tag list of the given profile has changed or been
        deleted.

        Updates the indices and notifies any live TagIpset objects of any
        any changes that affect them.

        :param str profile_id: Profile ID affected.
        :param list[str]|NoneType tags: List of tags for the given profile or
            None if deleted.
        """
        _log.info("Tags for profile %s updated", profile_id)

        # General approach is to default to the empty list if the new/old
        # tag list is missing; then add/delete falls out: all the tags will
        # end up in added_tags/removed_tags.
        old_tags = set(self.tags_by_prof_id.get(profile_id, []))
        new_tags = set(tags or [])
        # Find the endpoints that use these tags and work out what tags have
        # been added/removed.
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
                    self._remove_mapping(tag_id, profile_id, endpoint_id, ip)
            for tag_id in added_tags:
                for ip in ip_addrs:
                    self._add_mapping(tag_id, profile_id, endpoint_id, ip)

        if tags is None:
            _log.info("Tags for profile %s deleted", profile_id)
            self.tags_by_prof_id.pop(profile_id, None)
        else:
            self.tags_by_prof_id[profile_id] = tags

    def _extract_ips(self, endpoint):
        """
        Extract IP address list from an endpoint dict.

        :param dict endpoint: Endpoint dictionary
        :returns: List of IP addresses of the correct IP version.
        """
        if endpoint is None:
            return set()
        return set(map(futils.net_to_ip,
                       endpoint.get(self.nets_key, [])))

    @actor_message()
    def on_endpoint_update(self, endpoint_id, endpoint):
        """
        Update tag memberships and indices with the new endpoint dict.

        :param EndpointId endpoint_id: ID of the endpoint.
        :param dict|NoneType endpoint: Either a dict containing endpoint
            information or None to indicate deletion.

        """

        # Endpoint updates are the most complex to handle because they may
        # change the profile IDs (and hence the set of tags) as well as the
        # ip addresses attached to the interface.  In addition, the endpoint
        # may or may not have existed before.
        #
        # General approach: force all the possibilities through the same
        # update loops by defaulting values.  For example, if there was no
        # previous endpoint then we default old_tags to the empty set.  Then,
        # when we calculate removed_tags, we'll get the empty set and the
        # removal loop will be skipped.
        old_endpoint = self.endpoints_by_ep_id.pop(endpoint_id, {})
        old_prof_ids = set(old_endpoint.get("profile_ids", []))
        old_tags = set()
        for profile_id in old_prof_ids:
            for tag in self.tags_by_prof_id.get(profile_id, []):
                old_tags.add((profile_id, tag))

        if endpoint is None:
            _log.debug("Deletion, setting new_tags to empty.")
            new_prof_ids = set()
        else:
            _log.debug("Add/update, setting new_tags to indexed value.")
            new_prof_ids = set(endpoint.get("profile_ids", []))
            self.endpoints_by_ep_id[endpoint_id] = endpoint
        new_tags = set()
        for profile_id in new_prof_ids:
            for tag in self.tags_by_prof_id.get(profile_id, []):
                new_tags.add((profile_id, tag))

        if new_prof_ids != old_prof_ids:
            # Profile ID changed, or an add/delete.  the _xxx_profile_index
            # methods ignore profile_id == None so we'll do the right thing.
            _log.debug("Profile IDs changed from %s to %s",
                       old_prof_ids, new_prof_ids)
            self._remove_profile_index(old_prof_ids, endpoint_id)
            self._add_profile_index(new_prof_ids, endpoint_id)

        # Since we've defaulted new/old_tags to set() if needed, we can
        # use set operations to calculate the tag changes.
        added_tags = new_tags - old_tags
        unchanged_tags = new_tags & old_tags
        removed_tags = old_tags - new_tags

        # _extract_ips() will default old/new_ips to set() if there are no IPs.
        old_ips = self._extract_ips(old_endpoint)
        new_ips = self._extract_ips(endpoint)

        # Add *new* IPs to new tags.  On a deletion, added_tags will be empty.
        # Do this first to avoid marking ipsets as dirty if an endpoint moves
        # from one profile to another but keeps the same tag.
        for profile_id, tag in added_tags:
            for ip in new_ips:
                self._add_mapping(tag, profile_id,  endpoint_id, ip)
        # Change IPs in unchanged tags.
        added_ips = new_ips - old_ips
        removed_ips = old_ips - new_ips
        for profile_id, tag in unchanged_tags:
            for ip in removed_ips:
                self._remove_mapping(tag, profile_id,  endpoint_id, ip)
            for ip in added_ips:
                self._add_mapping(tag, profile_id,  endpoint_id, ip)
        # Remove *all* *old* IPs from removed tags.  For a deletion, only this
        # loop will fire.
        for profile_id, tag in removed_tags:
            for ip in old_ips:
                self._remove_mapping(tag, profile_id, endpoint_id, ip)

    def _add_mapping(self, tag_id, profile_id, endpoint_id, ip_address):
        """
        Adds the given tag->IP->profile->endpoint mapping to the index.
        Marks the tag as dirty if the update resulted in the IP being
        newly added.

        :param str tag_id: Tag ID
        :param str profile_id: Profile ID
        :param EndpointId endpoint_id: ID of the endpoint
        :param str ip_address: IP address to add
        """
        ip_added = not bool(self.ip_owners_by_tag[tag_id][ip_address])
        ep_ids = self.ip_owners_by_tag[tag_id][ip_address][profile_id]
        ep_ids.add(endpoint_id)
        if ip_added:
            self._dirty_tags.add(tag_id)

    def _remove_mapping(self, tag_id, profile_id, endpoint_id, ip_address):
        """
        Removes the tag->IP->profile->endpoint mapping from index.
        Marks the tag as dirty if the update resulted in the IP being
        removed.

        :param str tag_id: Tag ID
        :param str profile_id: Profile ID
        :param EndpointId endpoint_id: ID of the endpoint
        :param str ip_address: IP address to remove
        """
        ep_ids = self.ip_owners_by_tag[tag_id][ip_address][profile_id]
        ep_ids.discard(endpoint_id)
        if not ep_ids:
            del self.ip_owners_by_tag[tag_id][ip_address][profile_id]
            if not self.ip_owners_by_tag[tag_id][ip_address]:
                del self.ip_owners_by_tag[tag_id][ip_address]
                self._dirty_tags.add(tag_id)
            if not self.ip_owners_by_tag[tag_id]:
                del self.ip_owners_by_tag[tag_id]

    def _add_profile_index(self, prof_ids, endpoint_id):
        """
        Notes in the index that an endpoint uses the given profiles.

        :param list[str] prof_ids: List of profile IDs
        :param EndpointId endpoint_id: ID of the endpoint
        """
        for prof_id in prof_ids:
            self.endpoint_ids_by_profile_id[prof_id].add(endpoint_id)

    def _remove_profile_index(self, prof_ids, endpoint_id):
        """
        Notes in the index that an endpoint no longer uses any of the
        given profiles.

        :param list[str] prof_ids: List of profile IDs
        :param EndpointId endpoint_id: ID of the endpoint
        """
        for prof_id in prof_ids:
            endpoints = self.endpoint_ids_by_profile_id[prof_id]
            endpoints.discard(endpoint_id)
            if not endpoints:
                _log.debug("No more endpoints use profile %s", prof_id)
                del self.endpoint_ids_by_profile_id[prof_id]

    def _finish_msg_batch(self, batch, results):
        """
        Called after a batch of messages is finished, processes any
        pending TagIpset member updates.

        Doing that here allows us to lots of updates into one replace
        operation.  It also avoid wasted effort if tags are flapping.
        """
        super(IpsetManager, self)._finish_msg_batch(batch, results)
        _log.info("Finishing batch, sending updates to any dirty tags..")
        self._update_dirty_active_ipsets()
        _log.info("Finished sending updates to dirty tags.")


class IpsetActor(Actor):
    def __init__(self, ipset, qualifier=None):
        """
        Actor managing a single ipset.
        """
        super(IpsetActor, self).__init__(qualifier=qualifier)

        self._ipset = ipset
        # Members - which entries should be in the ipset.
        self.members = set()
        # Members which really are in the ipset.
        self.programmed_members = None

        self.stopped = False

    def owned_ipset_names(self):
        """
        This method is safe to call from another greenlet; it only accesses
        immutable state.

        :return: set of name of ipsets that this Actor owns and manages.  the
                 sets may or may not be present.
        """
        return set([self._ipset.set_name, self._ipset.temp_set_name])

    @actor_message()
    def replace_members(self, members):
        """
        Replace the members of this ipset with the supplied set.

        :param set[str]|list[str] members: IP address strings.
        """
        _log.info("Replacing members of ipset %s", self.name)
        self.members.clear()
        self.members.update(members)

    def _finish_msg_batch(self, batch, results):
        if not self.stopped and self.members != self.programmed_members:
            self._sync_to_ipset()

    def _sync_to_ipset(self):
        _log.info("Rewriting %s with %d members.", self, len(self.members))
        _log.debug("Setting ipset %s to %s", self, self.members)
        # Defer to our helper.
        self._ipset.replace_members(self.members)
        # We have got the set into the correct state.
        self.programmed_members = self.members.copy()

    def __str__(self):
        return (
            self.__class__.__name__ + "<queue_len=%s,live=%s,msg=%s,"
                                      "name=%s>" %
            (
                self._event_queue.qsize(),
                bool(self.greenlet),
                self._current_msg,
                self.name,
            )
        )


class TagIpset(IpsetActor, RefCountedActor):

    def __init__(self, tag, ip_type):
        """
        Refrence-counted Actor managing a single tag's ipset.

        :param str tag: Name of tag that this ipset represents.
        :param ip_type: IPV4 or IPV6
        """

        self.tag = tag
        name = tag_to_ipset_name(ip_type, tag)
        tmpname = tag_to_ipset_name(ip_type, tag, tmp=True)
        family = "inet" if ip_type == IPV4 else "inet6"
        # Helper class, used to do atomic rewrites of ipsets.
        ipset = Ipset(name, tmpname, family, "hash:ip")
        super(TagIpset, self).__init__(ipset, qualifier=tag)

        # Notified ready?
        self.notified_ready = False

    @actor_message()
    def on_unreferenced(self):
        # Mark the object as stopped so that we don't accidentally recreate
        # the ipset in _finish_msg_batch.
        self.stopped = True
        try:
            self._ipset.delete()
        finally:
            self._notify_cleanup_complete()

    def _finish_msg_batch(self, batch, results):
        super(TagIpset, self)._finish_msg_batch(batch, results)
        if not self.notified_ready:
            # We have created the set, so we are now ready.
            self.notified_ready = True
            self._notify_ready()

    def __str__(self):
        return (
            self.__class__.__name__ + "<queue_len=%s,live=%s,msg=%s,"
                                      "name=%s,id=%s>" %
            (
                self._event_queue.qsize(),
                bool(self.greenlet),
                self._current_msg,
                self.name,
                self._id,
            )
        )


class Ipset(object):
    """
    (Synchronous) wrapper around an ipset, supporting atomic rewrites.
    """
    def __init__(self, ipset_name, temp_ipset_name, ip_family,
                 ipset_type="hash:ip"):
        self.set_name = ipset_name
        self.temp_set_name = temp_ipset_name
        self.type = ipset_type
        self.family = ip_family

    def ensure_exists(self):
        """
        Creates the ipset iif it does not exist.

        Leaves the set and its contents untouched if it already exists.
        """
        input_lines = [self._create_cmd(self.set_name)]
        self._exec_and_commit(input_lines)

    def replace_members(self, members):
        """
        Atomically rewrites the ipset with the new members.

        Creates the set if it does not exist.
        """
        # We use ipset restore, which processes a batch of ipset updates.
        # The only operation that we're sure is atomic is swapping two ipsets
        # so we build up the complete set of members in a temporary ipset,
        # swap it into place and then delete the old ipset.
        input_lines = [
            # Ensure both the main set and the temporary set exist.
            self._create_cmd(self.set_name),
            self._create_cmd(self.temp_set_name),
            # Flush the temporary set.  This is a no-op unless we had a
            # left-over temporary set before.
            "flush %s" % self.temp_set_name,
        ]
        # Add all the members to the temporary set,
        input_lines += ["add %s %s" % (self.temp_set_name, m)
                        for m in members]
        # Then, atomically swap the temporary set into place.
        input_lines.append("swap %s %s" % (self.set_name, self.temp_set_name))
        # Finally, delete the temporary set (which was the old active set).
        input_lines.append("destroy %s" % self.temp_set_name)
        # COMMIT tells ipset restore to actually execute the changes.
        self._exec_and_commit(input_lines)

    def _exec_and_commit(self, input_lines):
        input_lines.append("COMMIT")
        input_str = "\n".join(input_lines) + "\n"
        futils.check_call(["ipset", "restore"], input_str=input_str)

    def _create_cmd(self, name):
        return ("create %s %s family %s --exist" %
                (name, self.type, self.family))

    def delete(self):
        # Destroy the ipsets - ignoring any errors.
        _log.debug("Delete ipsets %s and %s if they exist",
                   self.set_name, self.temp_set_name)
        futils.call_silent(["ipset", "destroy", self.set_name])
        futils.call_silent(["ipset", "destroy", self.temp_set_name])


HOSTS_IPSET_V4 = Ipset(FELIX_PFX + "calico-hosts-4",
                       FELIX_PFX + "calico-hosts-4-tmp",
                       "inet")
HOSTS_IPSET_V6 = Ipset(FELIX_PFX + "calico-hosts-6",
                       FELIX_PFX + "calico-hosts-6-tmp",
                       "inet6")


def tag_to_ipset_name(ip_type, tag, tmp=False):
    """
    Turn a (possibly shortened) tag ID into an ipset name.

    :param str ip_type: IP type (IPV4 or IPV6)
    :param str tag: Tag ID
    :param bool tmp: Is this the tmp ipset, or the permanent one?
    """
    if not tmp:
        name = IPSET_PREFIX[ip_type] + tag
    else:
        name = IPSET_TMP_PREFIX[ip_type] + tag
    return name


def list_ipset_names():
    """
    List all names of ipsets. Note that this is *not* the same as the ipset
    list command which lists contents too (hence the name change).

    :returns: List of names of ipsets.
    """
    data = futils.check_call(["ipset", "list"]).stdout
    lines = data.split("\n")

    names = []

    for line in lines:
        words = line.split()
        if len(words) > 1 and words[0] == "Name:":
            names.append(words[1])

    return names
