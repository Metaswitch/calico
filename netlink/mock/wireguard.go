package mock

import (
	"github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"
	"github.com/projectcalico/felix/ip"
	netlinkshim "github.com/projectcalico/felix/netlink"
	"github.com/projectcalico/libcalico-go/lib/set"
	"github.com/sirupsen/logrus"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"
)

// ----- Mock dataplane management functions for test code -----

func (d *MockNetlinkDataplane) NewMockWireguard() (netlinkshim.Wireguard, error) {
	d.NumNewNetlinkCalls++
	if d.PersistentlyFailToConnect || d.shouldFail(FailNextNewWireguard) {
		return nil, SimulatedError
	}
	if d.shouldFail(FailNextNewWireguardNotSupported) {
		return nil, NotSupportedError
	}
	Expect(d.WireguardOpen).To(BeFalse())
	d.WireguardOpen = true
	return d, nil
}

// ----- Wireguard API -----

func (d *MockNetlinkDataplane) Close() error {
	d.mutex.Lock()
	defer d.mutex.Unlock()

	Expect(d.WireguardOpen).To(BeTrue())
	d.WireguardOpen = false
	if d.shouldFail(FailNextWireguardClose) {
		return SimulatedError
	}

	return nil
}

func (d *MockNetlinkDataplane) DeviceByName(name string) (*wgtypes.Device, error) {
	d.mutex.Lock()
	defer d.mutex.Unlock()
	defer ginkgo.GinkgoRecover()

	Expect(d.WireguardOpen).To(BeTrue())
	if d.shouldFail(FailNextWireguardDeviceByName) {
		return nil, SimulatedError
	}
	link, ok := d.NameToLink[name]
	if !ok {
		return nil, NotFoundError
	}
	if link.Type() != "wireguard" {
		return nil, FileDoesNotExistError
	}

	device := &wgtypes.Device{
		Name:         name,
		Type:         wgtypes.LinuxKernel,
		PrivateKey:   link.WireguardPrivateKey,
		PublicKey:    link.WireguardPublicKey,
		ListenPort:   link.WireguardListenPort,
		FirewallMark: link.WireguardFirewallMark,
	}
	for _, peer := range link.WireguardPeers {
		device.Peers = append(device.Peers, peer)
	}

	return device, nil
}

func (d *MockNetlinkDataplane) ConfigureDevice(name string, cfg wgtypes.Config) error {
	d.mutex.Lock()
	defer d.mutex.Unlock()
	defer ginkgo.GinkgoRecover()

	Expect(d.WireguardOpen).To(BeTrue())
	if d.shouldFail(FailNextWireguardConfigureDevice) {
		return SimulatedError
	}
	link, ok := d.NameToLink[name]
	if !ok {
		return NotFoundError
	}

	if cfg.FirewallMark != nil {
		link.WireguardFirewallMark = *cfg.FirewallMark
	}
	if cfg.ListenPort != nil {
		link.WireguardListenPort = *cfg.ListenPort
	}
	if cfg.PrivateKey != nil {
		link.WireguardPrivateKey = *cfg.PrivateKey
		link.WireguardPublicKey = link.WireguardPrivateKey.PublicKey()
	}
	if cfg.ReplacePeers || len(cfg.Peers) > 0 {
		logrus.Debug("Update peers for wireguard link")
		existing := link.WireguardPeers
		if cfg.ReplacePeers || link.WireguardPeers == nil {
			logrus.Debug("Reset internal peers map")
			link.WireguardPeers = map[wgtypes.Key]wgtypes.Peer{}
		}
		for _, peerCfg := range cfg.Peers {
			Expect(peerCfg.PublicKey).NotTo(Equal(wgtypes.Key{}))
			if peerCfg.UpdateOnly {
				_, ok := existing[peerCfg.PublicKey]
				Expect(ok).To(BeTrue())
			}
			if peerCfg.Remove {
				_, ok := existing[peerCfg.PublicKey]
				Expect(ok).To(BeTrue())
				delete(existing, peerCfg.PublicKey)
				continue
			}

			// Get the current peer settings so we can apply the deltas.
			peer := link.WireguardPeers[peerCfg.PublicKey]

			// Store the public key (this may be zero if the peer ff not exist).
			peer.PublicKey = peerCfg.PublicKey

			// Apply updates.
			if peerCfg.Endpoint != nil {
				peer.Endpoint = peerCfg.Endpoint
			}
			if peerCfg.PersistentKeepaliveInterval != nil {
				peer.PersistentKeepaliveInterval = *peerCfg.PersistentKeepaliveInterval
			}

			// Construct the set of allowed IPs and then transfer to the slice for storage.
			allowedIPs := set.New()
			if !peerCfg.ReplaceAllowedIPs {
				for _, ipnet := range peer.AllowedIPs {
					allowedIPs.Add(ip.CIDRFromIPNet(&ipnet))
				}
			}
			if len(peerCfg.AllowedIPs) > 0 {
				for _, ipnet := range peerCfg.AllowedIPs {
					allowedIPs.Add(ip.CIDRFromIPNet(&ipnet))
				}
			}
			peer.AllowedIPs = nil
			allowedIPs.Iter(func(item interface{}) error {
				peer.AllowedIPs = append(peer.AllowedIPs, item.(ip.CIDR).ToIPNet())
				return nil
			})

			// Store the peer.
			link.WireguardPeers[peerCfg.PublicKey] = peer
		}
	}

	return nil
}
