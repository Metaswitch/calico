// Copyright (c) 2021 Tigera, Inc. All rights reserved.
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

// +build !cgo

package libbpf

const MapTypeProgrArray = 3

type Obj struct {
}

type TCOpts struct {
}

type Map struct {
}

func (m *Map) Name() string {
	panic("LIBBPF syscall stub")
}

func (m *Map) Type() int {
	panic("LIBBPF syscall stub")
}

func (m *Map) SetPinPath(path string) error {
	panic("LIBBPF syscall stub")
}

func OpenObject(filename string) (*Obj, error) {
	panic("LIBBPF syscall stub")
}

func (o *Obj) Load() error {
	panic("LIBBPF syscall stub")
}

func (o *Obj) FirstMap() (*Map, error) {
	panic("LIBBPF syscall stub")
}

func (m *Map) NextMap(obj *Obj) (*Map, error) {
	panic("LIBBPF syscall stub")
}

func (o *Obj) AttachClassifier(secName, ifName, hook string) (*TCOpts, error) {
	panic("LIBBPF syscall stub")
}

func CreateQDisc(ifName string) error {
	panic("LIBBPF syscall stub")
}

func RemoveQDisc(ifName string) error {
	panic("LIBBPF syscall stub")
}

func (o *Obj) UpdateJumpMap(mapName, progName string, mapIndex int) error {
	panic("LIBBPF syscall stub")
}

func (o *Obj) Close() error {
	panic("LIBBPF syscall stub")
}

func GetProgID(ifaceName, hook string, opts *TCOpts) (int, error) {
	panic("LIBBPF syscall stub")
}
