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

package tc

const (
	MarkCalico                       = 0xc0000000
	MarkCalicoMask                   = 0xf0000000
	MarkSeen                         = MarkCalico | 0x1000000
	MarkSeenMask                     = MarkCalicoMask | MarkSeen
	MarkSeenBypass                   = MarkSeen | 0x2000000
	MarkSeenBypassMask               = MarkSeenMask | MarkSeenBypass
	MarkSeenBypassForward            = MarkSeenBypass | 0x300000
	MarkSeenBypassForwardSourceFixup = MarkSeenBypass | 0x500000
	MarkSeenBypassSkipRPF            = MarkSeenBypass | 0x400000
	MarkSeenBypassSkipRPFMask        = MarkSeenBypassMask | 0xf00000
	MarkSeenNATOutgoing              = MarkSeen | 0x800000
	MarkSeenNATOutgoingMask          = MarkSeenMask | MarkSeenNATOutgoing
)
