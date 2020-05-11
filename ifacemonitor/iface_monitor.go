// Copyright (c) 2020 Tigera, Inc. All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package ifacemonitor

import (
	"regexp"
	"syscall"
	"time"

	log "github.com/sirupsen/logrus"
	"github.com/vishvananda/netlink"

	"github.com/projectcalico/libcalico-go/lib/set"

	"github.com/projectcalico/felix/ip"
)

type netlinkStub interface {
	Subscribe(
		linkUpdates chan netlink.LinkUpdate,
		addrUpdates chan netlink.AddrUpdate,
	) error
	LinkList() ([]netlink.Link, error)
	AddrList(link netlink.Link, family int) ([]netlink.Addr, error)
}

type State string

const (
	StateUnknown = ""
	StateUp      = "up"
	StateDown    = "down"
)

type InterfaceStateCallback func(ifaceName string, ifaceState State, ifIndex int)
type AddrStateCallback func(ifaceName string, addrs set.Set)

type Config struct {
	// List of interface names that dataplane receives no callbacks from them.
	InterfaceExcludes []*regexp.Regexp
}
type InterfaceMonitor struct {
	Config

	netlinkStub   netlinkStub
	resyncC       <-chan time.Time
	upIfaces      map[string]int // Map from interface name to index.
	StateCallback InterfaceStateCallback
	AddrCallback  AddrStateCallback
	ifaceName     map[int]string
	ifaceAddrs    map[int]set.Set
}

func New(config Config) *InterfaceMonitor {
	// Interface monitor using the real netlink, and resyncing every 10 seconds.
	resyncTicker := time.NewTicker(10 * time.Second)
	return NewWithStubs(config, &netlinkReal{}, resyncTicker.C)
}

func NewWithStubs(config Config, netlinkStub netlinkStub, resyncC <-chan time.Time) *InterfaceMonitor {
	return &InterfaceMonitor{
		Config:      config,
		netlinkStub: netlinkStub,
		resyncC:     resyncC,
		upIfaces:    map[string]int{},
		ifaceName:   map[int]string{},
		ifaceAddrs:  map[int]set.Set{},
	}
}

func IsInterfacePresent(name string) bool {
	link, _ := netlink.LinkByName(name)
	return link != nil
}

func (m *InterfaceMonitor) MonitorInterfaces() {
	log.Info("Interface monitoring thread started.")

	updates := make(chan netlink.LinkUpdate, 10)
	addrUpdates := make(chan netlink.AddrUpdate, 10)
	if err := m.netlinkStub.Subscribe(updates, addrUpdates); err != nil {
		log.WithError(err).Panic("Failed to subscribe to netlink stub")
	}
	filteredUpdates := make(chan netlink.LinkUpdate, 10)
	filteredAddrUpdates := make(chan netlink.AddrUpdate, 10)
	go filterUpdates(filteredAddrUpdates, addrUpdates, filteredUpdates, updates)
	log.Info("Subscribed to netlink updates.")

	// Start of day, do a resync to notify all our existing interfaces.  We also do periodic
	// resyncs because it's not clear what the ordering guarantees are for our netlink
	// subscription vs a list operation as used by resync().
	err := m.resync()
	if err != nil {
		log.WithError(err).Panic("Failed to read link states from netlink.")
	}

readLoop:
	for {
		log.WithFields(log.Fields{
			"updates":     filteredUpdates,
			"addrUpdates": filteredAddrUpdates,
			"resyncC":     m.resyncC,
		}).Debug("About to select on possible triggers")
		select {
		case update, ok := <-filteredUpdates:
			log.WithField("update", update).Debug("Link update")
			if !ok {
				log.Warn("Failed to read a link update")
				break readLoop
			}
			m.handleNetlinkUpdate(update)
		case addrUpdate, ok := <-filteredAddrUpdates:
			log.WithField("addrUpdate", addrUpdate).Debug("Address update")
			if !ok {
				log.Warn("Failed to read an address update")
				break readLoop
			}
			m.handleNetlinkAddrUpdate(addrUpdate)
		case <-m.resyncC:
			log.Debug("Resync trigger")
			err := m.resync()
			if err != nil {
				log.WithError(err).Panic("Failed to read link states from netlink.")
			}
		}
	}
	log.Panic("Failed to read events from Netlink.")
}

const flapDampingDelay = 100 * time.Millisecond

// filterUpdates filters out updates that occur when IPs are quickly removed and re-added.
// Some DHCP clients flap the IP during an IP renewal, for example.
//
// Algorithm:
// * Maintain a queue of link and address updates per interface.
// * When we see a potential flap (i.e. an IP deletion), defer processing the queue for a while.
// * If the flap resolves itself (i.e. the IP is added back), suppress the IP deletion.
func filterUpdates(addrOutC chan<- netlink.AddrUpdate, addrInC <-chan netlink.AddrUpdate,
	linkOutC chan<- netlink.LinkUpdate, linkInC <-chan netlink.LinkUpdate) {

	log.Debug("filterUpdates: starting")
	var timerC <-chan time.Time

	type timestampedUpd struct {
		ReadyAt time.Time
		Update  interface{} // AddrUpdate or LinkUpdate
	}

	updatesByIfaceIdx := map[int][]timestampedUpd{}

	for {
		select {
		case linkUpd := <-linkInC:
			idx := linkUpd.Index
			if len(updatesByIfaceIdx[int(idx)]) == 0 {
				log.Debug("filterUpdates: link change with empty queue, short circuit.")
				linkOutC <- linkUpd
				continue
			}
			updatesByIfaceIdx[int(idx)] = append(updatesByIfaceIdx[int(idx)],
				timestampedUpd{
					ReadyAt: time.Now().Add(flapDampingDelay),
					Update:  linkUpd,
				})
		case addrUpd := <-addrInC:
			log.WithField("update", addrUpd).Debug("filterUpdates: got new update")
			idx := addrUpd.LinkIndex
			oldUpds := updatesByIfaceIdx[idx]

			var readyToSendTime time.Time
			if addrUpd.NewAddr {
				if len(oldUpds) == 0 {
					// This is an add for a new IP and there's nothing else in the queue for this interface.
					// Short circuit.
					log.Debug("filterUpdates: add with empty queue, short circuit.")
					addrOutC <- addrUpd
					continue
				}

				// Else, there's something else in the queue, need to process the queue...
				log.Debug("filterUpdates: add with non-empty queue.")
				readyToSendTime = time.Now()
			} else {
				log.Debug("filterUpdates: delete.")
				readyToSendTime = time.Now().Add(flapDampingDelay)
			}
			upds := oldUpds[:0]
			for _, upd := range oldUpds {
				log.WithField("previous", upd).Debug("filterUpdates: examining previous update.")
				if oldAddrUpd, ok := upd.Update.(netlink.AddrUpdate); ok {
					if ip.IPNetsEqual(&oldAddrUpd.LinkAddress, &addrUpd.LinkAddress) {
						// New update for the same IP, suppress the old update
						log.WithField("address", oldAddrUpd.LinkAddress.String()).Debug(
							"Received update for same IP within a short time, squashed the update.")
						// To prevent continuous flapping from delaying route updates forever, take the timestamp of the
						// first update.
						readyToSendTime = upd.ReadyAt
						continue
					}
				}
				upds = append(upds, upd)
			}
			upds = append(upds, timestampedUpd{ReadyAt: readyToSendTime, Update: addrUpd})
			updatesByIfaceIdx[idx] = upds
		case <-timerC:
			log.Debug("filterUpdates: timer popped.")
		}
		var nextUpdTime time.Time
		for idx, upds := range updatesByIfaceIdx {
			log.WithField("ifaceIdx", idx).Debug("filterUpdates: examining updates for interface.")
			for len(upds) > 0 {
				firstUpd := upds[0]
				if time.Since(firstUpd.ReadyAt) >= 0 {
					// Either update is old enough to prevent flapping or it's an address being added.
					// Ready to send...
					log.WithField("update", firstUpd).Debug("filterUpdates: update ready to send.")
					switch u := firstUpd.Update.(type) {
					case netlink.AddrUpdate:
						addrOutC <- u
					case netlink.LinkUpdate:
						linkOutC <- u
					}
					upds = upds[1:]
				} else {
					// Update is too new, figure out when it'll be safe to send it.
					log.WithField("update", firstUpd).Debug("filterUpdates: update not ready.")
					if nextUpdTime.IsZero() || firstUpd.ReadyAt.Before(nextUpdTime) {
						nextUpdTime = firstUpd.ReadyAt
					}
					break
				}
			}
			if len(upds) == 0 {
				log.WithField("ifaceIdx", idx).Debug("filterUpdates: no more updates for interface.")
				delete(updatesByIfaceIdx, idx)
			} else {
				log.WithField("ifaceIdx", idx).WithField("num", len(upds)).Debug(
					"filterUpdates: still updates for interface.")
				updatesByIfaceIdx[idx] = upds
			}
		}
		if !nextUpdTime.IsZero() {
			// Need to schedule a retry.
			delay := time.Until(nextUpdTime)
			if delay <= 0 {
				delay = 1
			}
			log.WithField("delay", delay).Debug("filterUpdates: calculated delay.")
			timerC = time.After(delay)
		} else {
			log.Debug("filterUpdates: no more updates to send, disabling timer.")
			timerC = nil
		}
	}
}

func (m *InterfaceMonitor) isExcludedInterface(ifName string) bool {
	for _, nameExp := range m.InterfaceExcludes {
		if nameExp.Match([]byte(ifName)) {
			return true
		}
	}
	return false
}

func (m *InterfaceMonitor) handleNetlinkUpdate(update netlink.LinkUpdate) {
	attrs := update.Attrs()
	linkAttrs := update.Link.Attrs()
	if attrs == nil || linkAttrs == nil {
		// Defensive, some sort of interface that the netlink lib doesn't understand?
		log.WithField("update", update).Warn("Missing attributes on netlink update.")
		return
	}

	msgType := update.Header.Type
	ifaceExists := msgType == syscall.RTM_NEWLINK // Alternative is an RTM_DELLINK
	m.storeAndNotifyLink(ifaceExists, update.Link)
}

func (m *InterfaceMonitor) handleNetlinkAddrUpdate(update netlink.AddrUpdate) {
	ifIndex := update.LinkIndex
	if ifName, known := m.ifaceName[ifIndex]; known {
		if m.isExcludedInterface(ifName) {
			return
		}
	}

	addr := update.LinkAddress.IP.String()
	exists := update.NewAddr
	log.WithFields(log.Fields{
		"addr":    addr,
		"ifIndex": ifIndex,
		"exists":  exists,
	}).Info("Netlink address update.")

	// notifyIfaceAddrs needs m.ifaceName[ifIndex] - because we can only notify when we know the
	// interface name - so check that we have that.
	if _, known := m.ifaceName[ifIndex]; !known {
		// We think this interface does not exist - indicates a race between the link and
		// address update channels.  Addresses will be notified when we process the link
		// update.
		log.WithField("ifIndex", ifIndex).Debug("Link not notified yet.")
		return
	}
	if _, known := m.ifaceAddrs[ifIndex]; !known {
		// m.ifaceAddrs[ifIndex] has exactly the same lifetime as m.ifaceName[ifIndex], so
		// it should be impossible for m.ifaceAddrs[ifIndex] not to exist if
		// m.ifaceName[ifIndex] does exist.  However we check anyway and warn in case there
		// is some possible scenario...
		log.WithField("ifIndex", ifIndex).Warn("Race for new interface.")
		return
	}

	if exists {
		if !m.ifaceAddrs[ifIndex].Contains(addr) {
			m.ifaceAddrs[ifIndex].Add(addr)
			m.notifyIfaceAddrs(ifIndex)
		}
	} else {
		if m.ifaceAddrs[ifIndex].Contains(addr) {
			m.ifaceAddrs[ifIndex].Discard(addr)
			m.notifyIfaceAddrs(ifIndex)
		}
	}
}

func (m *InterfaceMonitor) notifyIfaceAddrs(ifIndex int) {
	log.WithField("ifIndex", ifIndex).Debug("notifyIfaceAddrs")
	if name, known := m.ifaceName[ifIndex]; known {
		log.WithField("ifIndex", ifIndex).Debug("Known interface")
		addrs := m.ifaceAddrs[ifIndex]
		if addrs != nil {
			// Take a copy, so that the dataplane's set of addresses is independent of
			// ours.
			addrs = addrs.Copy()
		}
		m.AddrCallback(name, addrs)
	}
}

func (m *InterfaceMonitor) storeAndNotifyLink(ifaceExists bool, link netlink.Link) {
	attrs := link.Attrs()
	ifIndex := attrs.Index
	newName := attrs.Name
	log.WithFields(log.Fields{
		"ifaceExists": ifaceExists,
		"link":        link,
	}).Debug("storeAndNotifyLink called")

	oldName := m.ifaceName[ifIndex]
	if oldName != "" && oldName != newName {
		log.WithFields(log.Fields{
			"oldName": oldName,
			"newName": newName,
		}).Info("Interface renamed, simulating deletion of old copy.")
		m.storeAndNotifyLinkInner(false, oldName, link)
	}

	m.storeAndNotifyLinkInner(ifaceExists, newName, link)
}

func (m *InterfaceMonitor) storeAndNotifyLinkInner(ifaceExists bool, ifaceName string, link netlink.Link) {
	log.WithFields(log.Fields{
		"ifaceExists": ifaceExists,
		"ifaceName":   ifaceName,
		"link":        link,
	}).Debug("storeAndNotifyLinkInner called")

	// Store or remove mapping between this interface's index and name.
	attrs := link.Attrs()
	ifIndex := attrs.Index
	if ifaceExists {
		m.ifaceName[ifIndex] = ifaceName
	} else {
		if !m.isExcludedInterface(ifaceName) {
			// for excluded interfaces, e.g. kube-ipvs0, we ignore all ip address changes.
			log.Debug("Notify link non-existence to address callback consumers")
			delete(m.ifaceAddrs, ifIndex)
			m.notifyIfaceAddrs(ifIndex)
		}
		delete(m.ifaceName, ifIndex)
	}

	// We need the operstate of the interface; this is carried in the IFF_RUNNING flag.  The
	// IFF_UP flag contains the admin state, which doesn't tell us whether we can program routes
	// etc.
	rawFlags := attrs.RawFlags
	ifaceIsUp := ifaceExists && rawFlags&syscall.IFF_RUNNING != 0
	oldIfIndex, ifaceWasUp := m.upIfaces[ifaceName]
	logCxt := log.WithField("ifaceName", ifaceName)
	if ifaceIsUp && !ifaceWasUp {
		logCxt.Debug("Interface now up")
		m.upIfaces[ifaceName] = ifIndex
		m.StateCallback(ifaceName, StateUp, ifIndex)
	} else if ifaceWasUp && !ifaceIsUp {
		logCxt.Debug("Interface now down")
		delete(m.upIfaces, ifaceName)
		m.StateCallback(ifaceName, StateDown, oldIfIndex)
	} else {
		logCxt.WithField("ifaceIsUp", ifaceIsUp).Debug("Nothing to notify")
	}

	// If the link now exists, get addresses for the link and store and notify those too; then
	// we don't have to worry about a possible race between the link and address update
	// channels.  We deliberately do this regardless of the link state, as in some cases this
	// will allow us to secure a Host Endpoint interface _before_ it comes up, and so eliminate
	// a small window of insecurity.
	if ifaceExists && !m.isExcludedInterface(ifaceName) {
		// Notify address changes for non excluded interfaces.
		newAddrs := set.New()
		for _, family := range [2]int{netlink.FAMILY_V4, netlink.FAMILY_V6} {
			addrs, err := m.netlinkStub.AddrList(link, family)
			if err != nil {
				log.WithError(err).Warn("Netlink addr list operation failed.")
			}
			for _, addr := range addrs {
				newAddrs.Add(addr.IPNet.IP.String())
			}
		}
		if (m.ifaceAddrs[ifIndex] == nil) || !m.ifaceAddrs[ifIndex].Equals(newAddrs) {
			m.ifaceAddrs[ifIndex] = newAddrs

			m.notifyIfaceAddrs(ifIndex)
		}
	}
}

func (m *InterfaceMonitor) resync() error {
	log.Debug("Resyncing interface state.")
	links, err := m.netlinkStub.LinkList()
	if err != nil {
		log.WithError(err).Warn("Netlink list operation failed.")
		return err
	}
	currentIfaces := set.New()
	for _, link := range links {
		attrs := link.Attrs()
		if attrs == nil {
			// Defensive, some sort of interface that the netlink lib doesn't
			// understand?
			log.WithField("link", link).Warn("Missing attributes on netlink update.")
			continue
		}
		currentIfaces.Add(attrs.Name)
		m.storeAndNotifyLink(true, link)
	}
	for name, ifIndex := range m.upIfaces {
		if currentIfaces.Contains(name) {
			continue
		}
		log.WithField("ifaceName", name).Info("Spotted interface removal on resync.")
		m.StateCallback(name, StateDown, ifIndex)
		m.AddrCallback(name, nil)
		delete(m.upIfaces, name)
		delete(m.ifaceAddrs, ifIndex)
		delete(m.ifaceName, ifIndex)
	}
	log.Debug("Resync complete")
	return nil
}
