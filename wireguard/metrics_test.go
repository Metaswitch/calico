package wireguard_test

import (
	"bytes"
	"net"
	"text/template"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/common/expfmt"

	. "github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"

	"github.com/projectcalico/felix/ip"
	"github.com/projectcalico/felix/netlinkshim"
	"github.com/projectcalico/felix/wireguard"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"
)

var _ netlinkshim.Wireguard = (*wireguardDevicesOnly)(nil)

type wireguardDevicesOnly struct {
	name               string
	listenPort, fwMark int
	privateKey         wgtypes.Key
	peers              []*wgtypes.Peer
}

func newMockPeeredWireguardDevice(privateKey wgtypes.Key, peers []*wgtypes.Peer) *wireguardDevicesOnly {
	return &wireguardDevicesOnly{
		name:       "wireguard.cali",
		listenPort: 51820,
		fwMark:     0x1000000001,
		privateKey: privateKey,
		peers:      peers,
	}
}

type mockPeerInfo struct {
	privKey wgtypes.Key
	peer *wgtypes.Peer
}

func mustPrivateKey() wgtypes.Key {
	pk, err := wgtypes.GeneratePrivateKey()
	if err != nil {
		panic(err)
	}
	return pk
}


func mustNewMockPeer(ipAddr string, port int) *mockPeerInfo {
	privKey := mustPrivateKey()
	peer := &wgtypes.Peer{
		PublicKey: privKey.PublicKey(),
		Endpoint: &net.UDPAddr{IP: ip.FromString(ipAddr).AsNetIP(), Port: port},
		ProtocolVersion: 4,
	}

	return &mockPeerInfo{privKey, peer}
}

func (w *wireguardDevicesOnly) Close() error {
	return nil
}
func (w *wireguardDevicesOnly) DeviceByName(name string) (*wgtypes.Device, error) {
	dev := &wgtypes.Device{
		Name:         name,
		Type:         wgtypes.LinuxKernel,
		PrivateKey:   w.privateKey,
		PublicKey:    w.privateKey.PublicKey(),
		ListenPort:   w.listenPort,
		FirewallMark: w.fwMark,
	}

	for _, peer := range w.peers {
		dev.Peers = append(dev.Peers, *peer)
	}

	return dev, nil
}
func (w *wireguardDevicesOnly) Devices() ([]*wgtypes.Device, error) {
	dev, _ := w.DeviceByName(w.name)
	return []*wgtypes.Device{dev}, nil
}
func (w *wireguardDevicesOnly) ConfigureDevice(_ string, _ wgtypes.Config) error {
	return nil
}
func (w *wireguardDevicesOnly) generatePeerTraffic(rx, tx int64) time.Time {
	ts := time.Now()
	for _, peer := range w.peers {
		peer.ReceiveBytes += rx
		peer.TransmitBytes += tx
		peer.LastHandshakeTime = time.Now()
	}
	return ts
}


var _ = Describe("wireguard metrics", func() {

	var wgStats *wireguard.Metrics
	var wgClient *wireguardDevicesOnly
	var mockPeers []*mockPeerInfo
	const (
		hostname = "l0c4lh057"
	)

	newWireguardDevicesOnly := func() (netlinkshim.Wireguard, error) {
		return wgClient, nil
	}

	BeforeEach(func() {
		mockPeers = []*mockPeerInfo{
			mustNewMockPeer("10.0.0.1", 1001),
			mustNewMockPeer("10.0.0.2", 1002),
		}
		wgClient = newMockPeeredWireguardDevice(mockPeers[0].privKey, []*wgtypes.Peer{
			mockPeers[1].peer,
		})
		wgStats = wireguard.NewWireguardMetricsWithShims(
			hostname,
			newWireguardDevicesOnly,
		)
	})

	It("should be yield metrics", func() {

		By("checking if it's constructable")
		Expect(wgStats).ToNot(BeNil())

		By("registering it in a prometheus.Registry")
		registry := prometheus.NewRegistry()
		registry.MustRegister(wgStats)

		By("producing metrics")
		wgClient.generatePeerTraffic(1, 1)
		_, err := registry.Gather()
		Expect(err).ToNot(HaveOccurred())

		<-time.After(5 * time.Second)
		ts := wgClient.generatePeerTraffic(1024, 1024)
		mfs, err := registry.Gather()
		Expect(err).ToNot(HaveOccurred())
		Expect(mfs).To(HaveLen(4))

		By("comparing text output")
		buf := &bytes.Buffer{}
		for _, mf := range mfs {
			_, err := expfmt.MetricFamilyToText(buf, mf)
			Expect(err).ToNot(HaveOccurred())
		}

		data := map[string]interface{}{
			"pubkey": mockPeers[0].peer.PublicKey.String(),
			"peerkey": mockPeers[1].peer.PublicKey.String(),
			"endpoint": mockPeers[1].peer.Endpoint.String(),
			"hostname": hostname,
			"iface": wgClient.name,
			"listenport": wgClient.listenPort,
			"ts": float64(ts.Unix()),
		}

		tmpl := template.Must(
			template.New("").Parse(`# HELP wireguard_bytes_rcvd wireguard interface total incoming bytes to peer
# TYPE wireguard_bytes_rcvd counter
wireguard_bytes_rcvd{hostname="{{.hostname}}",peer_endpoint="{{.endpoint}}",peer_key="{{.peerkey}}",public_key="{{.pubkey}}"} 1024
# HELP wireguard_bytes_sent wireguard interface total outgoing bytes to peer
# TYPE wireguard_bytes_sent counter
wireguard_bytes_sent{hostname="{{.hostname}}",peer_endpoint="{{.endpoint}}",peer_key="{{.peerkey}}",public_key="{{.pubkey}}"} 1024
# HELP wireguard_latest_handshake_seconds wireguard interface latest handshake unix timestamp in seconds to a peer
# TYPE wireguard_latest_handshake_seconds gauge
wireguard_latest_handshake_seconds{hostname="{{.hostname}}",peer_endpoint="{{.endpoint}}",peer_key="{{.peerkey}}",public_key="{{.pubkey}}"} {{.ts}}
# HELP wireguard_meta wireguard interface and runtime metadata
# TYPE wireguard_meta gauge
wireguard_meta{hostname="{{.hostname}}",iface="{{.iface}}",listen_port="{{.listenport}}",public_key="{{.pubkey}}"} 1
`))
		buf2 := &bytes.Buffer{}
		err = tmpl.Execute(buf2, data)
		Expect(err).ToNot(HaveOccurred())

		Expect(buf.String()).To(Equal(buf2.String()))
	})

	AfterEach(func() {
		wgClient = nil
	})
})
