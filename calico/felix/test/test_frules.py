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
felix.test.test_frules
~~~~~~~~~~~~~~~~~~~~~~~~~

Tests of iptables rules generation function.
"""
import logging
from calico.felix import frules
from calico.felix.frules import (
    profile_to_chain_name,  rules_to_chain_rewrite_lines, UnsupportedICMPType,
    _rule_to_iptables_fragment
)
from calico.felix.test.base import BaseTestCase

_log = logging.getLogger(__name__)

DEFAULT_MARK = ('--append chain-foo --match comment '
                '--comment "Mark as not matched" --jump MARK --set-mark 1')
RULES_TESTS = [
    ([{"src_net": "10.0.0.0/8"},], 4,
     ["--append chain-foo --source 10.0.0.0/8 --jump RETURN",
      DEFAULT_MARK]),

    ([{"protocol": "icmp",
       "src_net": "10.0.0.0/8",
       "icmp_type": 7,
       "icmp_code": 123},], 4,
     ["--append chain-foo --protocol icmp --source 10.0.0.0/8 "
      "--match icmp --icmp-type 7/123 "
      "--jump RETURN",
      DEFAULT_MARK]),

    ([{"protocol": "icmp",
       "src_net": "10.0.0.0/8",
       "icmp_type": 7},], 4,
     ["--append chain-foo --protocol icmp --source 10.0.0.0/8 "
      "--match icmp --icmp-type 7 "
      "--jump RETURN",
      DEFAULT_MARK]),

    ([{"protocol": "icmpv6",
       "src_net": "1234::beef",
       "icmp_type": 7},], 6,
     ["--append chain-foo --protocol icmpv6 --source 1234::beef "
      "--match icmp6 --icmpv6-type 7 "
      "--jump RETURN",
      DEFAULT_MARK]),

    ([{"protocol": "tcp",
       "src_tag": "tag-foo",
       "src_ports": [10, "11:12"]}], 4,
     ["--append chain-foo --protocol tcp "
      "--match set --match-set ipset-foo src "
      "--match multiport --source-ports 10,11:12 --jump RETURN",
      DEFAULT_MARK]),

    ([{"protocol": "tcp",
       "src_ports": [10]}], 4,
     ["--append chain-foo --protocol tcp "
      "--match tcp --source-port 10 --jump RETURN",
      DEFAULT_MARK]),

    ([{"protocol": "tcp",
       "src_ports": [10, "0:12"]}], 4,
     ["--append chain-foo --protocol tcp "
      "--match tcp --source-port 0 --jump RETURN",
      "--append chain-foo --protocol tcp "
      "--match multiport --source-ports 10,1:12 --jump RETURN",
      DEFAULT_MARK]),

    ([{"protocol": "tcp",
       "src_ports": [1, "2:3", 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]}], 4,
     ["--append chain-foo --protocol tcp "
      "--match multiport --source-ports 1,2:3,4,5,6,7,8,9,10,11,12,13,14,15 "
      "--jump RETURN",
      "--append chain-foo --protocol tcp "
      "--match multiport --source-ports 16,17 "
      "--jump RETURN",
      DEFAULT_MARK]),
]

IP_SET_MAPPING = {
    "tag-foo": "ipset-foo",
    "tag-bar": "ipset-bar",
}


class TestRules(BaseTestCase):

    def test_profile_to_chain_name(self):
        self.assertEqual(profile_to_chain_name("inbound", "prof1"),
                         "felix-p-prof1-i")
        self.assertEqual(profile_to_chain_name("outbound", "prof1"),
                         "felix-p-prof1-o")

    def test_split_port_lists(self):
        self.assertEqual(
            frules._split_port_lists([1, 2, 3, 4, 5, 6, 7, 8, 9,
                                      10, 11, 12, 13, 14, 15]),
            [['1', '2', '3', '4', '5', '6', '7', '8', '9',
              '10', '11', '12', '13', '14', '15']]
        )
        self.assertEqual(
            frules._split_port_lists([1, 2, 3, 4, 5, 6, 7, 8, 9,
                                      10, 11, 12, 13, 14, 15, 16]),
            [['1', '2', '3', '4', '5', '6', '7', '8', '9',
              '10', '11', '12', '13', '14', '15'],
             ['16']]
        )
        self.assertEqual(
            frules._split_port_lists([1, "2:3", 4, 5, 6, 7, 8, 9,
                                      10, 11, 12, 13, 14, 15, 16, 17]),
            [['1', '2:3', '4', '5', '6', '7', '8', '9',
              '10', '11', '12', '13', '14', '15'],
             ['16', '17']]
        )
        self.assertEqual(
            frules._split_port_lists([1, "0:3", 4, 5, 6, 7, 8, 9,
                                      10, 11, 12, 13, 14, 15, 16, 17]),
            [['0'],
             ['1', '1:3', '4', '5', '6', '7', '8', '9',
              '10', '11', '12', '13', '14', '15'],
             ['16', '17']]
        )
        self.assertEqual(
            frules._split_port_lists([0, "0:3"]),
            [['0'], ['1:3']]
        )

    def test_filter_zero_ports(self):
        self.assertEqual(frules._filter_zero_ports(["0"]), (True, []))
        self.assertEqual(frules._filter_zero_ports(["0:0"]), (True, []))
        self.assertEqual(frules._filter_zero_ports(["0:1"]), (True, ["1"]))
        self.assertEqual(frules._filter_zero_ports(["1:0"]), (True, ["1"]))
        self.assertEqual(frules._filter_zero_ports(["0:65535"]),
                         (True, ["1:65535"]))
        self.assertEqual(frules._filter_zero_ports(["65535:0"]),
                         (True, ["65535:1"]))

        self.assertEqual(frules._filter_zero_ports([]), (False, []))
        self.assertEqual(frules._filter_zero_ports([1]), (False, ["1"]))
        self.assertEqual(frules._filter_zero_ports(["1:65535"]),
                         (False, ["1:65535"]))

    def test_rules_generation(self):
        for rules, ip_version, expected_output in RULES_TESTS:
            fragments = rules_to_chain_rewrite_lines(
                "chain-foo",
                rules,
                ip_version,
                IP_SET_MAPPING,
                on_allow="RETURN",
            )
            self.assertEqual(fragments, expected_output)

    def test_bad_icmp_type(self):
        with self.assertRaises(UnsupportedICMPType):
            _rule_to_iptables_fragment("foo", {"icmp_type": 255}, 4, {})