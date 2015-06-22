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
calico.monotonic
~~~~~~~~~~~~~~~~

Monotonic clock functions.

monotonic_time() should be used for timing and calculating timer pops
in preference to time.time() which can be non-monotonic or jump
wildly, especially in a VM.
"""
import logging

_log = logging.getLogger(__name__)

__all__ = ["monotonic_time"]

import ctypes
import os


CLOCK_MONOTONIC_RAW = 4 # see <linux/time.h>


class timespec(ctypes.Structure):
    _fields_ = [
        ('tv_sec', ctypes.c_long),
        ('tv_nsec', ctypes.c_long)
    ]


librt = ctypes.CDLL('librt.so.1', use_errno=True)
clock_gettime = librt.clock_gettime
clock_gettime.argtypes = [ctypes.c_int, ctypes.POINTER(timespec)]


def _raw_monotonic_time():
    """
    :returns: a time in seconds from an unspecified epoch (which may vary
        between processes).  Guaranteed to be monotonic within the life of
        a process.
    """
    t = timespec()
    if clock_gettime(CLOCK_MONOTONIC_RAW , ctypes.pointer(t)) != 0:
        errno_ = ctypes.get_errno()
        raise OSError(errno_, os.strerror(errno_))
    monotime = t.tv_sec + t.tv_nsec * 1e-9
    return monotime


# Make our epoch friendly.
_epoch = _raw_monotonic_time()
def monotonic_time():
    return _raw_monotonic_time() - _epoch


