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

package proxy_test

import (
	"net"

	. "github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"

	v1 "k8s.io/api/core/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/projectcalico/felix/bpf/conntrack"
	"github.com/projectcalico/felix/bpf/mock"
	"github.com/projectcalico/felix/bpf/nat"
	proxy "github.com/projectcalico/felix/bpf/proxy"
	"github.com/projectcalico/felix/ip"
)

var _ = Describe("BPF service type change", func() {

	clusterIP := net.IPv4(10, 1, 0, 1)
	extIP := net.IPv4(20, 1, 0, 1)
	port := uint16(1234)
	proto := v1.ProtocolTCP
	extIPstr := "20.1.0.1"
	npPort := int32(30333)
	testSvc := &v1.Service{
		TypeMeta:   typeMetaV1("Service"),
		ObjectMeta: objectMeataV1("testService"),
		Spec: v1.ServiceSpec{
			ClusterIP: "10.1.0.1",
			Type:      v1.ServiceTypeClusterIP,
			Selector: map[string]string{
				"app": "test",
			},
			Ports: []v1.ServicePort{
				{
					Protocol: v1.ProtocolTCP,
					Port:     int32(port),
				},
			},
		},
	}

	testSvcEps := &v1.Endpoints{
		TypeMeta:   typeMetaV1("Endpoints"),
		ObjectMeta: objectMeataV1("testService"),
		Subsets: []v1.EndpointSubset{
			{
				Addresses: []v1.EndpointAddress{
					{
						IP: "10.1.2.1",
					},
					{
						IP: "10.1.2.2",
					},
				},
				Ports: []v1.EndpointPort{
					{
						Port: 1234,
						Name: "1234",
					},
				},
			},
		},
	}
	k8s := fake.NewSimpleClientset(testSvc, testSvcEps)

	initIP := net.IPv4(1, 1, 1, 1)

	front := newMockNATMap()
	back := newMockNATBackendMap()
	aff := newMockAffinityMap()
	ct := mock.NewMockMap(conntrack.MapParams)
	p, _ := proxy.StartKubeProxy(k8s, "test-node", front, back, aff, ct, proxy.WithImmediateSync())
	p.OnHostIPsUpdate([]net.IP{initIP})

	key_clusterIP := nat.NewNATKey(clusterIP, port, proxy.ProtoV1ToIntPanic(proto))
	key_extIP := nat.NewNATKey(extIP, port, proxy.ProtoV1ToIntPanic(proto))
	key_extIP_with_src := nat.NewNATKeySrc(extIP, port, proxy.ProtoV1ToIntPanic(proto), ip.MustParseCIDROrIP("30.1.0.1/32").(ip.V4CIDR))
	key_hostIP := nat.NewNATKey(initIP, uint16(npPort), proxy.ProtoV1ToIntPanic(proto))

	AfterEach(func() {
		p.Stop()
	})

	It("should update service after host ip changes", func() {
		By("check if nat map has the cluster IP", func() {

			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				if len(front.m) == 1 && ret1 {
					return true
				}
				return false
			}).Should(BeTrue())
		})
		// cluster IP -> external IP
		By("Update the service type from ClusterIP to ExternalIP", func() {
			setSvcTypeToExternalIP(testSvc, []string{extIPstr}, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_extIP]
				if len(front.m) == 2 && ret1 && ret2 {
					return true
				}
				return false
			}).Should(BeTrue())

		})

		// External IP -> Cluster IP
		By("Update the service type from ExternalIP to ClusterIP", func() {
			setSvcTypeToClusterIP(testSvc, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_extIP]

				if len(front.m) == 1 && ret1 && !ret2 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// Cluster IP -> Load balancer
		By("Update the service type from ClusterIP to LoadBalancer", func() {
			setSvcTypeToLoadBalancer(testSvc, []string{extIPstr}, []string{"30.1.0.1/32"}, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_extIP]
				_, ret3 := front.m[key_extIP_with_src]

				if len(front.m) == 3 && ret1 && ret2 && ret3 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// LoadBalancer -> ClusterIP
		By("Update the service type from LoadBalancer to ClusterIP", func() {
			setSvcTypeToClusterIP(testSvc, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_extIP]
				_, ret3 := front.m[key_extIP_with_src]

				if len(front.m) == 1 && ret1 && !ret2 && !ret3 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// ClusterIP -> NodePort
		By("Update the service type from ClusterIP to NodePort", func() {
			setSvcTypeToNodePort(testSvc, npPort, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_hostIP]
				if ret1 && ret2 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// NodePort -> ClusterIP
		By("Update the service type from NodePort to ClusterIP", func() {
			setSvcTypeToClusterIP(testSvc, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_hostIP]

				if len(front.m) == 1 && ret1 && !ret2 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// ClusterIP -> NodePort
		By("Update the service type from ClusterIP to NodePort", func() {
			setSvcTypeToNodePort(testSvc, npPort, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_hostIP]
				if ret1 && ret2 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// NodePort -> ExternalIP
		By("Update the service type from NodePort to ExternalIP", func() {
			setSvcTypeToExternalIP(testSvc, []string{extIPstr}, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_extIP]
				_, ret3 := front.m[key_hostIP]
				if len(front.m) == 2 && ret1 && ret2 && !ret3 {
					return true
				}
				return false
			}).Should(BeTrue())

		})

		// ExternalIP -> NodePort
		By("Update the service type from ExternalIP to NodePort", func() {
			setSvcTypeToNodePort(testSvc, npPort, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_hostIP]
				_, ret3 := front.m[key_extIP]
				if ret1 && ret2 && !ret3 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// NodePort -> LoadBalancer
		By("Update the service type from NodePort to LoadBalancer", func() {
			setSvcTypeToLoadBalancer(testSvc, []string{extIPstr}, []string{"30.1.0.1"}, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_extIP]
				_, ret3 := front.m[key_extIP_with_src]
				_, ret4 := front.m[key_hostIP]
				if len(front.m) == 3 && ret1 && ret2 && ret3 && !ret4 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// LoadBalancer -> ExternalIP
		By("Update the service type from LoadBalancer to ExternalIP", func() {
			setSvcTypeToExternalIP(testSvc, []string{extIPstr}, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_extIP]
				_, ret3 := front.m[key_extIP_with_src]
				if len(front.m) == 2 && ret1 && ret2 && !ret3 {
					return true
				}
				return false
			}).Should(BeTrue())

		})

		// External IP -> LoadBalancer
		By("Update the service type from ExternalIP to LoadBalancer", func() {
			setSvcTypeToLoadBalancer(testSvc, []string{extIPstr}, []string{"30.1.0.1"}, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_extIP]
				_, ret3 := front.m[key_extIP_with_src]
				if len(front.m) == 3 && ret1 && ret2 && ret3 {
					return true
				}
				return false
			}).Should(BeTrue())
		})

		// LoadBalancer -> NodePort
		By("Update the service type from LoadBalancer to NodePort", func() {
			setSvcTypeToNodePort(testSvc, npPort, k8s)
			Eventually(func() bool {
				front.Lock()
				defer front.Unlock()
				_, ret1 := front.m[key_clusterIP]
				_, ret2 := front.m[key_hostIP]
				_, ret3 := front.m[key_extIP]
				_, ret4 := front.m[key_extIP_with_src]
				if ret1 && ret2 && !ret3 && !ret4 {
					return true
				}
				return false
			}).Should(BeTrue())
		})
	})
})

func setSvcTypeToClusterIP(testSvc *v1.Service, k8s *fake.Clientset) {
	testSvc.Spec.ExternalIPs = []string{}
	testSvc.Spec.LoadBalancerSourceRanges = []string{}
	testSvc.Spec.Type = v1.ServiceTypeClusterIP
	testSvc.Spec.Ports[0].NodePort = 0
	_, err := k8s.CoreV1().Services(v1.NamespaceDefault).Update(testSvc)
	Expect(err).NotTo(HaveOccurred())
}

func setSvcTypeToExternalIP(testSvc *v1.Service, extIP []string, k8s *fake.Clientset) {
	testSvc.Spec.ExternalIPs = extIP
	testSvc.Spec.LoadBalancerSourceRanges = []string{}
	testSvc.Spec.Type = v1.ServiceTypeClusterIP
	testSvc.Spec.Ports[0].NodePort = 0
	_, err := k8s.CoreV1().Services(v1.NamespaceDefault).Update(testSvc)
	Expect(err).NotTo(HaveOccurred())
}

func setSvcTypeToLoadBalancer(testSvc *v1.Service, extIP, srcRange []string, k8s *fake.Clientset) {
	testSvc.Spec.ExternalIPs = extIP
	testSvc.Spec.LoadBalancerSourceRanges = srcRange
	testSvc.Spec.Ports[0].NodePort = 0
	testSvc.Spec.Type = v1.ServiceTypeLoadBalancer
	_, err := k8s.CoreV1().Services(v1.NamespaceDefault).Update(testSvc)
	Expect(err).NotTo(HaveOccurred())
}

func setSvcTypeToNodePort(testSvc *v1.Service, npPort int32, k8s *fake.Clientset) {
	testSvc.Spec.ExternalIPs = []string{}
	testSvc.Spec.LoadBalancerSourceRanges = []string{}
	testSvc.Spec.Ports[0].NodePort = npPort
	testSvc.Spec.Type = v1.ServiceTypeNodePort
	_, err := k8s.CoreV1().Services(v1.NamespaceDefault).Update(testSvc)
	Expect(err).NotTo(HaveOccurred())
}
