package wireguard

type Config struct {
	// Wireguard configuration
	Enabled             bool
	ListeningPort       int
	FirewallMark        int
	RoutingRulePriority int
	RoutingTableIndex   int
	InterfaceName       string
	MTU                 int
}
