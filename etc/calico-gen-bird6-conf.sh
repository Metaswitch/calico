#! /bin/bash

BIRD_CONF=/etc/bird/bird6.conf
BIRD_CONF_TEMPLATE=/usr/share/calico/bird/calico-bird6.conf.template

# Community Strings Could be an argument if we want
# However, we should make that optional, if we do

$advert_community_string=777
$no_advert_community_string=666


# Require 4 arguments.
[ $# -eq 4 ] || cat <<EOF

Usage: $0 <my-ipv4-address> <my-ipv6-address> <rr-ipv6-address> <as-number>

where
  <my-ipv4-address> is the external IPv4 address of the local machine
  <my-ipv6-address> is the external IPv6 address of the local machine
  <rr-ipv6-address> is the IPv6 address of the route reflector that
      the local BIRD6 should peer with
  <as-number> is the BGP AS number that the route relector is using.

Please specify exactly these 4 required arguments.

EOF
[ $# -eq 4 ] || exit -1

# Name the arguments.
my_ipv4_address=$1
my_ipv6_address=$2
rr_ipv6_address=$3
as_number=$4

# Generate BIRD config file.
mkdir -p $(dirname $BIRD_CONF)
sed -e "
s/@MY_IPV4_ADDRESS@/$my_ipv4_address/;
s/@MY_IPV6_ADDRESS@/$my_ipv6_address/;
s/@RR_IPV6_ADDRESS@/$rr_ipv6_address/;
s/@AS_NUMBER@/$as_number/;
s/@ADVERT@/$advert_community_string/;
s/@NO_ADVERT@/$no_advert_community_string/;
" < $BIRD_CONF_TEMPLATE > $BIRD_CONF

echo BIRD6 configuration generated at $BIRD_CONF

if [ -f /etc/redhat-release ]; then
    # On a Red Hat system, we assume that BIRD6 is locally built and
    # installed, as it is not available for RHEL 6.5 in packaged form.
    # Run this now.
    /usr/local/sbin/bird6 -c /etc/bird/bird6.conf
    echo BIRD6 started
else
    # On a Debian/Ubuntu system, BIRD6 is packaged and already running,
    # so just restart it.
    service bird6 restart
    echo BIRD6 restarted
fi
