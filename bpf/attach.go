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

package bpf

import (
	"encoding/json"
	"io/ioutil"
	"os"
	"os/exec"
	"path"
	"strings"

	log "github.com/sirupsen/logrus"
)

const (
	BPF_PROG_BINARY_DIR    = "/usr/lib/calico/bpf"
	ATTACHED_PROG_HASH_DIR = "/var/run/calico/bpf"
)

type AttachedProgInfo struct {
	Name string `json:"name"`
	Hash string `json:"hash"`
}

func CheckAttachedProgs(iface, progName string) (bool, string) {
	var progInfo AttachedProgInfo
	var bytesToRead []byte

	binaryName := path.Join(BPF_PROG_BINARY_DIR, progName)
	hashCmd := exec.Command("sha256sum", binaryName)
	outBytes, err := hashCmd.Output()
	if err != nil {
		log.Info(err)
		return false, ""
	}
	calculatedHash := strings.Split(string(outBytes), " ")[0]

	name := iface + "_" + strings.TrimSuffix(progName, path.Ext(progName)) + ".json"
	filename := path.Join(ATTACHED_PROG_HASH_DIR, name)
	if bytesToRead, err = ioutil.ReadFile(filename); err != nil {
		log.Info(err)
		return false, calculatedHash
	}

	if err := json.Unmarshal(bytesToRead, &progInfo); err != nil {
		log.Info(err)
		return false, calculatedHash
	}

	if progInfo.Hash == calculatedHash {
		return true, calculatedHash
	}

	return false, calculatedHash
}

func RememberAttachedProgs(iface, progName, csum string) {
	var progInfo = AttachedProgInfo{
		Name: progName,
		Hash: csum,
	}

	if err := os.MkdirAll(ATTACHED_PROG_HASH_DIR, 0600); err != nil {
		log.Info(err)
		return
	}

	bytesToWrite, err := json.Marshal(progInfo)
	if err != nil {
		log.Info(err)
		return
	}

	name := iface + "_" + strings.TrimSuffix(progName, path.Ext(progName)) + ".json"
	filename := path.Join(ATTACHED_PROG_HASH_DIR, name)
	if err = ioutil.WriteFile(filename, bytesToWrite, 0400); err != nil {
		log.Info(err)
	}
}
