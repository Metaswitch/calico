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
felix.test.test_fiptables
~~~~~~~~~~~~~~~~~~~~~~~~~

Tests of iptables handling function.
"""

import logging
from calico.felix import fiptables
from calico.felix.test.base import BaseTestCase

_log = logging.getLogger(__name__)


PARSE_SAVE_TESTS = [
("""# Generated by ip6tables-save v1.4.21 on Mon Mar 30 12:59:13 2015
*filter
:INPUT DROP [0:0]
:OUTPUT DROP [0:0]
:FORWARD DROP [0:0]
:fx-foo - [0:0]
:ufw6-user-output - [0:0]
-A INPUT -j ufw6-before-logging-input
-A FORWARD -j ufw6-after-logging-forward
-A FORWARD -j ufw6-reject-forward
-A OUTPUT -j ufw6-track-output
*raw
:foo
""",
{
    "filter": set(["INPUT", "OUTPUT", "FORWARD", "fx-foo",
                   "ufw6-user-output"]),
    "raw": set(["foo"]),
}),
]


class TestIptablesUpdater(BaseTestCase):

    def test_parse_save(self):
        for inp, exp in PARSE_SAVE_TESTS:
            output = fiptables.parse_ipt_save(inp)
            self.assertEqual(exp, output, "Expected\n\n%s\n\nTo parse as: %s\n"
                                          "but got: %s" % (inp, exp, output))