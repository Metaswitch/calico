// Copyright (c) 2016-2017 Tigera, Inc. All rights reserved.

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

package main

import (
	"flag"
	"time"

	. "github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"
	"k8s.io/client-go/kubernetes"
)

var _ = Describe("calc-graph test", func() {

	var (
		clientset *kubernetes.Clientset
		nsPrefix  string
	)

	BeforeEach(func() {
		clientset = initialize(flag.Arg(0))
	})

	It("should run test 1", func() {
		Expect(calcGraph1(clientset)).To(BeNil())
		nsPrefix = "ns-"
	})

	It("should process 1000 pods", func() {
		Expect(create1000Pods(clientset)).To(BeNil())
		nsPrefix = "test"
	})

	AfterEach(func() {
		time.Sleep(20 * time.Second)
		cleanupAll(clientset, nsPrefix)
	})
})
