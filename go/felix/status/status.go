// Copyright (c) 2016 Tigera, Inc. All rights reserved.
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

package status

import (
	log "github.com/Sirupsen/logrus"
	"github.com/projectcalico/calico/go/datastructures/set"
	"github.com/projectcalico/calico/go/felix/proto"
	"github.com/tigera/libcalico-go/lib/backend/api"
	"github.com/tigera/libcalico-go/lib/backend/model"
	"github.com/tigera/libcalico-go/lib/errors"
	"time"
	"github.com/projectcalico/calico/go/felix/jitter"
)

type EndpointStatusReporter struct {
	hostname           string
	endpointUpdates    <-chan interface{}
	inSync             <-chan bool
	datastore          api.Client
	epStatusIDToStatus map[model.Key]string
	dirtyStatIDs       set.Set
	reportingDelay     time.Duration
	resyncInterval     time.Duration
}

func NewEndpointStatusReporter(hostname string,
	endpointUpdates <-chan interface{},
	inSync <-chan bool,
	datastore api.Client,
	reportingDelay time.Duration,
	resyncInterval time.Duration) *EndpointStatusReporter {
	return &EndpointStatusReporter{
		hostname:           hostname,
		endpointUpdates:    endpointUpdates,
		datastore:          datastore,
		inSync:             inSync,
		epStatusIDToStatus: make(map[model.Key]string),
		dirtyStatIDs:       set.New(),
		reportingDelay:     reportingDelay,
		resyncInterval:     resyncInterval,
	}
}

func (esr *EndpointStatusReporter) Start() {
	go esr.loopHandlingEndpointStatusUpdates()
}

func (esr *EndpointStatusReporter) loopHandlingEndpointStatusUpdates() {
	log.Infof("Starting endpoint status reporter loop with resync "+
		"interval %v, report rate limit: 1/%v", esr.resyncInterval,
		esr.reportingDelay)
	datamodelInSync := false
	resyncRequested := false
	updatesAllowed := true

	// BUG(smc) Should jitter the tickers.
	resyncSchedulingTicker := jitter.NewTicker(esr.resyncInterval, esr.resyncInterval/10)
	updateRateLimitTicker := jitter.NewTicker(esr.reportingDelay, esr.reportingDelay/10)
	for {
		select {
		case <-resyncSchedulingTicker.C:
			log.Debug("Endpoint status resync tick: scheduling cleanup")
			resyncRequested = true
		case <-updateRateLimitTicker.C:
			if !updatesAllowed {
				log.Debugf("Update tick: uncorking updates")
				updatesAllowed = true
			}
		case <-esr.inSync:
			log.Debug("Datamodel in sync, enabling status resync")
			datamodelInSync = true
		case msg := <-esr.endpointUpdates:
			var statID model.Key
			var status string
			switch msg := msg.(type) {
			case *proto.WorkloadEndpointStatusUpdate:
				statID = model.WorkloadEndpointStatusKey{
					Hostname:       esr.hostname,
					OrchestratorID: msg.Id.OrchestratorId,
					WorkloadID:     msg.Id.WorkloadId,
					EndpointID:     msg.Id.EndpointId,
				}
				status = msg.Status.Status
			case *proto.WorkloadEndpointStatusRemove:
				statID = model.WorkloadEndpointStatusKey{
					Hostname:       esr.hostname,
					OrchestratorID: msg.Id.OrchestratorId,
					WorkloadID:     msg.Id.WorkloadId,
					EndpointID:     msg.Id.EndpointId,
				}
			case *proto.HostEndpointStatusUpdate:
				statID = model.HostEndpointStatusKey{
					Hostname:   esr.hostname,
					EndpointID: msg.Id.EndpointId,
				}
				status = msg.Status.Status
			case *proto.HostEndpointStatusRemove:
				statID = model.HostEndpointStatusKey{
					Hostname:   esr.hostname,
					EndpointID: msg.Id.EndpointId,
				}
			default:
				log.Fatalf("Unexpected message: %#v", msg)
			}
			if esr.epStatusIDToStatus[statID] != status {
				if status != "" {
					esr.epStatusIDToStatus[statID] = status
				} else {
					delete(esr.epStatusIDToStatus, statID)
				}
				esr.dirtyStatIDs.Add(statID)
			}
		}

		if datamodelInSync && resyncRequested {
			// TODO: load data from datamodel, mark missing/extra/incorrect keys dirty.
			log.Debug("Doing endpoint status resync")
			esr.attemptResync()
			resyncRequested = false
		}

		if updatesAllowed && esr.dirtyStatIDs.Len() > 0 {
			// Not throttled and there's an update pending.
			var statID model.Key
			esr.dirtyStatIDs.Iter(func(item interface{}) error {
				statID = item.(model.Key)
				return set.StopIteration
			})

			err := esr.writeEndpointStatus(statID,
				esr.epStatusIDToStatus[statID])
			if err == nil {
				log.Debugf(
					"Write successful, discarding %v from dirty set",
					statID)
				esr.dirtyStatIDs.Discard(statID)
			}
			// Cork updates until the next timer pop.
			updatesAllowed = false
		}
	}
}

func (esr *EndpointStatusReporter) attemptResync() {
	wlListOpts := model.WorkloadEndpointStatusListOptions{
		Hostname: esr.hostname,
	}
	kvs, err := esr.datastore.List(wlListOpts)
	if err != nil {
		log.Errorf("Failed to load workload endpoint statuses from datastore: %v",
			err)
		return
	}
	for _, kv := range kvs {
		if kv.Value == nil {
			// Parse error, needs refresh.
			esr.dirtyStatIDs.Add(kv.Key)
		} else {
			status := kv.Value.(model.WorkloadEndpointStatus).Status
			if status != esr.epStatusIDToStatus[kv.Key] {
				log.Debugf("Found out-of sync endpoint status: %v", kv.Key)
				esr.dirtyStatIDs.Add(kv.Key)
			}
		}
	}

	hostListOpts := model.HostEndpointStatusListOptions{
		Hostname: esr.hostname,
	}
	kvs, err = esr.datastore.List(hostListOpts)
	if err != nil {
		log.Errorf("Failed to load workload endpoint statuses from datastore: %v",
			err)
		return
	}
	for _, kv := range kvs {
		if kv.Value == nil {
			// Parse error, needs refresh.
			esr.dirtyStatIDs.Add(kv.Key)
		} else {
			status := kv.Value.(model.HostEndpointStatus).Status
			if status != esr.epStatusIDToStatus[kv.Key] {
				log.Debugf("Found out-of sync endpoint status: %v", kv.Key)
				esr.dirtyStatIDs.Add(kv.Key)
			}
		}
	}
}

func (esr *EndpointStatusReporter) writeEndpointStatus(epID model.Key, status string) (err error) {
	kv := model.KVPair{Key: epID}
	if status != "" {
		log.Debugf("Writing endpoint status for %v: %v", epID, status)
		switch epID.(type) {
		case model.HostEndpointStatusKey:
			kv.Value = model.HostEndpointStatus{status}
		case model.WorkloadEndpointStatusKey:
			kv.Value = model.WorkloadEndpointStatus{status}
		}
		_, err = esr.datastore.Apply(&kv)
	} else {
		log.Debugf("Deleting endpoint status for %v", epID)
		err = esr.datastore.Delete(&kv)
		if _, ok := err.(errors.ErrorResourceDoesNotExist); ok {
			// Ignore non-existent resource.
			err = nil
		}
	}
	return
}
