.. # Copyright (c) Metaswitch Networks 2015. All rights reserved.
   #
   #    Licensed under the Apache License, Version 2.0 (the "License"); you may
   #    not use this file except in compliance with the License. You may obtain
   #    a copy of the License at
   #
   #         http://www.apache.org/licenses/LICENSE-2.0
   #
   #    Unless required by applicable law or agreed to in writing, software
   #    distributed under the License is distributed on an "AS IS" BASIS,
   #    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
   #    implied. See the License for the specific language governing
   #    permissions and limitations under the License.

Configuring Calico
==================

This page describes how to configure Calico. We first describe the
configuration of the core Calico components - Felix and ACL Manager -
because these are needed, and configured similarly, regardless of the
surrounding environment (OpenStack, Docker, or whatever). Then,
depending on that surrounding environment, there will be some further
configuration of that environment needed, to tell it to talk to the
Calico components.

Currently we have detailed environment configuration only for OpenStack.
Work on other environments is in progress, and this page will be
extended as that happens.

This page aims to be a complete Calico configuration reference, and
hence to describe all the possible fields, files etc. For a more
task-based approach, when installing Calico with OpenStack on Ubuntu or
Red Hat, please see :doc:`ubuntu-opens-install` or
:doc:`redhat-opens-install`.

Calico components
-----------------

The core Calico component is Felix. (Please see
:doc:`arch-felix-and-acl` for the Calico architecture.)

Felix
^^^^^

Felix runs on each compute host and is configured by the following
environment variables.

+---------------------+----------------------+----------------------------------------------------------------------------------------------------------------------------------------------------------+
| Setting             | Default              | Meaning                                                                                                                                                  |
+=====================+======================+==========================================================================================================================================================+
| FELIX_HOSTNAME      | socket.gethostname() | The hostname Felix reports to the plugin. Should be used if the hostname Felix autodetects is incorrect or does not match what the plugin will expect.   |
+---------------------+----------------------+----------------------------------------------------------------------------------------------------------------------------------------------------------+
| FELIX_ETCDADDR      | localhost:4001       | The address that Felix uses to access Etcd.                                                                                                              |
+---------------------+----------------------+----------------------------------------------------------------------------------------------------------------------------------------------------------+

OpenStack environment configuration
-----------------------------------

When running Calico with OpenStack, you also need to configure various
OpenStack components, as follows.

Nova (/etc/nova/nova.conf)
^^^^^^^^^^^^^^^^^^^^^^^^^^

Calico uses the Nova metadata service to provide metadata to VMs,
without any proxying by Neutron. To make that work:

-  An instance of the Nova metadata API must run on every compute node.

-  ``/etc/nova/nova.conf`` must not set
   ``service_neutron_metadata_proxy`` or ``service_metadata_proxy`` to
   ``True``. (The default ``False`` value is correct for a Calico
   cluster.)

Neutron server (/etc/neutron/neutron.conf)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

In ``/etc/neutron/neutron.conf`` you need the following settings to
configure the Neutron service.

+------------------------------+----------------------------------------+-------------------------------------------+
| Setting                      | Value                                  | Meaning                                   |
+==============================+========================================+===========================================+
| core\_plugin                 | neutron.plugins.ml2.plugin.Ml2Plugin   | Use ML2 plugin                            |
+------------------------------+----------------------------------------+-------------------------------------------+
| api\_workers                 | 0                                      | Don't use worker threads                  |
+------------------------------+----------------------------------------+-------------------------------------------+
| rpc\_workers                 | 0                                      | Don't use worker threads                  |
+------------------------------+----------------------------------------+-------------------------------------------+
| dhcp\_agents\_per\_network   | 9999                                   | Allow unlimited DHCP agents per network   |
+------------------------------+----------------------------------------+-------------------------------------------+

ML2 (.../ml2\_conf.ini)
^^^^^^^^^^^^^^^^^^^^^^^

In ``/etc/neutron/plugins/ml2/ml2_conf.ini`` you need the following
settings to configure the ML2 plugin.

+--------------------------+---------------+-------------------------------------+
| Setting                  | Value         | Meaning                             |
+==========================+===============+=====================================+
| mechanism\_drivers       | calico        | Use Calico                          |
+--------------------------+---------------+-------------------------------------+
| type\_drivers            | local, flat   | Allow 'local' and 'flat' networks   |
+--------------------------+---------------+-------------------------------------+
| tenant\_network\_types   | local, flat   | Allow 'local' and 'flat' networks   |
+--------------------------+---------------+-------------------------------------+

DHCP agent (.../dhcp\_agent.ini)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

In ``/etc/neutron/dhcp_agent.ini`` you need the following settings to
configure the Neutron DHCP agent.

+---------------------+-------------------------+--------------------------------------------------------------------------------------------------------+
| Setting             | Value                   | Meaning                                                                                                |
+=====================+=========================+========================================================================================================+
| interface\_driver   | RoutedInterfaceDriver   | Use Calico's modified DHCP agent support for TAP interfaces that are routed instead of being bridged   |
+---------------------+-------------------------+--------------------------------------------------------------------------------------------------------+
