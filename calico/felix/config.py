# -*- coding: utf-8 -*-
# Copyright (c) 2014, 2015 Metaswitch Networks
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
felix.config
~~~~~~~~~~~~

Configuration management for Felix.

On instantiation, this module reads environment variables and builds a
singleton configuration object. Other modules should just import the
Config object and use the fields within it.
"""
import os

import ConfigParser
import logging
import socket

from calico import common

# Logger
log = logging.getLogger(__name__)

# Convert log level names into python log levels.
LOGLEVELS = {"none":      None,
             "debug":     logging.DEBUG,
             "info":      logging.INFO,
             "warn":      logging.WARNING,
             "warning":   logging.WARNING,
             "err":       logging.ERROR,
             "error":     logging.ERROR,
             "crit":      logging.CRITICAL,
             "critical":  logging.CRITICAL}


class ConfigException(Exception):
    def __init__(self, message, source):
        super(ConfigException, self).__init__(message)
        self.message = message
        self.source = source

    def __str__(self):
        return "%s (data source : %s)" % (self.message, self.source)

class Config(object):
    def __init__(self):
        """
        Create a config.
        :raises EtcdException
        """
        self.ETCD_ADDR = self.getenv("ETCDADDR", "localhost:4001")
        fields = self.ETCD_ADDR.split(":")
        if len(fields) != 2:
            raise ConfigException("Invalid format for EtcdAddr (%s) - must be "
                                  "hostname:port" %
                                  (self.ETCD_ADDR), "environment")

        self.validate_addr("EtcdAddr", fields[0])

        try:
            int(fields[1])
        except ValueError:
            raise ConfigException("Invalid port in EtcdAddr (%s)" %
                                  (self.ETCD_ADDR), "environment")

        self.HOSTNAME = self.getenv("HOSTNAME", socket.gethostname())

        self.STARTUP_CLEANUP_DELAY = 30
        self.METADATA_IP = "127.0.0.1"
        self.METADATA_PORT = "8775"
        self.RESYNC_INT_SEC = 1800
        self.IFACE_PREFIX = None
        self.LOGFILE = "/var/log/calico/felix.log"
        self.LOGLEVFILE = "INFO"
        self.LOGLEVSYS = "ERROR"
        self.LOGLEVSCR = "ERROR"

        self.LOGLEVFILE = LOGLEVELS.get(self.LOGLEVFILE.lower(), logging.DEBUG)
        self.LOGLEVSYS = LOGLEVELS.get(self.LOGLEVSYS.lower(), logging.DEBUG)
        self.LOGLEVSCR = LOGLEVELS.get(self.LOGLEVSCR.lower(), logging.DEBUG)

    def update_config(self, cfg_dict):
        self.STARTUP_CLEANUP_DELAY = int(cfg_dict.pop("StartupCleanupDelay",
                                                      "30"))
        self.METADATA_IP = cfg_dict.pop("MetadataAddr", "127.0.0.1")
        self.METADATA_PORT = cfg_dict.pop("MetadataPort", "8775")
        self.RESYNC_INT_SEC = int(cfg_dict.pop("ResyncIntervalSecs", "1800"))
        self.IFACE_PREFIX = cfg_dict.pop("InterfacePrefix", None)
        self.LOGFILE = cfg_dict.pop("LogFilePath", "/var/log/calico/felix.log")
        self.LOGLEVFILE = cfg_dict.pop("LogSeverityFile", "INFO")
        self.LOGLEVSYS = cfg_dict.pop("LogSeveritySys", "ERROR")
        self.LOGLEVSCR = cfg_dict.pop("LogSeverityScreen", "ERROR")

        self.LOGLEVFILE = LOGLEVELS.get(self.LOGLEVFILE.lower(), logging.DEBUG)
        self.LOGLEVSYS = LOGLEVELS.get(self.LOGLEVSYS.lower(), logging.DEBUG)
        self.LOGLEVSCR = LOGLEVELS.get(self.LOGLEVSCR.lower(), logging.DEBUG)

        self.validate_cfg()

    def getenv(self, name, default):
        env_var = "FELIX_%s" % name
        log.debug("Looking for environment variable override %s", env_var)
        if env_var in os.environ:
            value = os.environ[env_var]
            log.info("Environment variable %s=%r overrides default %s",
                     env_var, value, default)
            return value

        return default

    def validate_cfg(self):
        #*********************************************************************#
        #* Firewall that the config is not invalid.                          *#
        #*********************************************************************#
        if self.METADATA_IP.lower() == "none":
            # Metadata is not required.
            self.METADATA_IP = None
            self.METADATA_PORT = None
        else:
            # Metadata must be supplied as IP or address, but we store as IP
            self.METADATA_IP = self.validate_addr("MetadataAddr",
                                                  self.METADATA_IP)

            if not common.validate_port(self.METADATA_PORT):
                raise ConfigException("Invalid MetadataPort value : %s" %
                                      self.METADATA_PORT,
                                      "etcd:/calico/config/MetadataPort")


        if self.IFACE_PREFIX is None:
            raise ConfigException("Missing InterfacePrefix value",
                                  "etcd:/calico/config/InterfacePrefix")

        # Log file may be "None" (the literal string, either provided or as
        # default). In this case no log file should be written.
        if self.LOGFILE.lower() == "none":
            # Metadata is not required.
            self.LOGFILE = None

    def validate_addr(self, name, addr):
        """
        Validate an address, returning the IP address it resolves to. If the
        address cannot be resolved then an exception is returned.

        Parameters :
        - name of the field, for use in logging
        - address to resolve
        """
        try:
            stripped_addr = addr.strip()
            if not stripped_addr:
                raise ConfigException("Blank %s value" % name,
                                      "environment")

            return socket.gethostbyname(addr)
        except socket.gaierror:
            raise ConfigException("Invalid or unresolvable %s value : %s" %
                                  (name, addr),
                                  "environment")
