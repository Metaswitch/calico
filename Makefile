include ../metadata.mk

# This Makefile builds Felix and releases it in various forms:
#
#				   Go install
#					|
#		 +-------+		v
#		 | Felix |   +---------------------+
#		 |  Go   |   | calico/go-build     |
#		 |  code |   +---------------------+
#		 +-------+	  /
#			\	 /
#			 \      /
#			 go build
#			     |
#			     v
#		       +------------------+
#		       | bin/calico-felix |
#		       +------------------+
#			       /
#		      .-------'
#		    /
#		   /
#	    docker build
#		  |
#		  v
#	   +--------------+
#	   | calico/felix |
#	   +--------------+
#
###############################################################################
PACKAGE_NAME = github.com/alauda/felix

# Name of the images.
# e.g., <registry>/<name>:<tag>
FELIX_IMAGE    ?=felix
BUILD_IMAGES   ?=$(FELIX_IMAGE)

LIBBPF_A=bpf-gpl/include/libbpf/src/$(ARCH)/libbpf.a
LIBBPF_CONTAINER_PATH=/go/src/github.com/alauda/felix/bpf-gpl/include/libbpf/src
BPFGPL_CONTAINER_PATH=/go/src/github.com/alauda/felix/bpf-gpl

# List of Go files that are generated by the build process.  Builds should
# depend on these, clean removes them.
GENERATED_FILES=proto/felixbackend.pb.go bpf/asm/opcode_string.go

# All Felix go files.
SRC_FILES:=$(shell find . $(foreach dir,$(NON_FELIX_DIRS) fv,-path ./$(dir) -prune -o) -type f -name '*.go' -print) $(GENERATED_FILES)
# Add Felix cgo files
SRC_FILES += $(shell find bpf/ -name '*.[ch]')
FV_SRC_FILES:=$(shell find fv -type f -name '*.go' -print)
EXTRA_DOCKER_ARGS+=--init -v $(CURDIR)/../pod2daemon:/go/src/github.com/projectcalico/calico/pod2daemon:rw

###############################################################################
# Download and include ../lib.Makefile
#   Additions to EXTRA_DOCKER_ARGS need to happen before the include since
#   that variable is evaluated when we declare DOCKER_RUN and siblings.
###############################################################################
include ../lib.Makefile

ifeq ($(ARCH),arm64)
# Required for eBPF support in ARM64.
# We need to force ARM64 build image to be used in a crosscompilation run.
CALICO_BUILD:=$(CALICO_BUILD)-$(ARCH)
DOCKER_GO_BUILD:=$(DOCKER_RUN) $(CALICO_BUILD)
endif

FV_ETCDIMAGE?=$(ETCD_IMAGE)
FV_TYPHAIMAGE?=felix-test/typha:latest-$(BUILDARCH)
FV_K8SIMAGE=calico/go-build:$(GO_BUILD_VER)
FV_FELIXIMAGE?=$(FELIX_IMAGE):latest-$(ARCH)

# Total number of batches to split the tests into.  In CI we set this to say 5 batches,
# and run a single batch on each test VM.
FV_NUM_BATCHES?=1
# Space-delimited list of FV batches to run in parallel.  Defaults to running all batches
# in parallel on this host.  The CI system runs a subset of batches in each parallel job.
#
# To run multiple batches in parallel in order to speed up local runs (on a powerful
# developer machine), set FV_NUM_BATCHES to say 3, then this value will be automatically
# calculated.  Note: the tests tend to flake more when run in parallel even though they
# were designed to be isolated; if you hit a failure, try running the tests sequentially
# (with FV_NUM_BATCHES=1) to check that it's not a flake.
FV_BATCHES_TO_RUN?=$(shell seq $(FV_NUM_BATCHES))
FV_SLOW_SPEC_THRESH=90

# Linker flags for building Felix.
#
# We use -X to insert the version information into the placeholder variables
# in the buildinfo package.
#
# We use -B to insert a build ID note into the executable, without which, the
# RPM build tools complain.
LDFLAGS=-ldflags "\
	-X $(PACKAGE_NAME)/buildinfo.GitVersion=$(GIT_DESCRIPTION) \
	-X $(PACKAGE_NAME)/buildinfo.BuildDate=$(DATE) \
	-X $(PACKAGE_NAME)/buildinfo.GitRevision=$(GIT_COMMIT) \
	-B 0x$(BUILD_ID)"

.PHONY: clean
clean:
	rm -rf bin \
	       docker-image/bin \
	       dist \
	       build \
	       fv/fv.test \
	       go/docs/calc.pdf \
	       release-notes-*
	find . -name "junit.xml" -type f -delete
	find . -name "*.coverprofile" -type f -delete
	find . -name "coverage.xml" -type f -delete
	find . -name ".coverage" -type f -delete
	find . -name "*.pyc" -type f -delete
	# Clean .created files which indicate images / releases have been built.
	find . -name '.*.created*' -type f -delete
	$(DOCKER_GO_BUILD) make -C bpf-apache clean
	$(DOCKER_GO_BUILD) make -C bpf-gpl clean
	$(DOCKER_GO_BUILD) make -C $(LIBBPF_CONTAINER_PATH) clean

.PHONY: clean-generated
# Delete (checked-in) generated files.  Intentionally not part of the main clean target since these files are
# checked in and deleting them makes the repo "dirty" as far as git is concerned.  It also breaks downstream
# builds that import these files since they don't know to rebuild felix's generated files.
clean-generated:
	rm -rf $(GENERATED_FILES)

###############################################################################
# Building the binary
###############################################################################
BUILD_TARGETS:=bin/calico-felix

ifeq ($(ARCH), $(filter $(ARCH),amd64 arm64))
# Currently CGO can be enabled in ARM64 and AMD64 builds.
CGO_ENABLED=1
CGO_LDFLAGS="-L$(LIBBPF_CONTAINER_PATH)/$(ARCH) -lbpf -lelf -lz"
CGO_CFLAGS="-I$(LIBBPF_CONTAINER_PATH) -I$(BPFGPL_CONTAINER_PATH)"
FV_RACE_DETECTOR_ENABLED?=true
BUILD_TARGETS+=build-bpf
else
CGO_ENABLED=0
CGO_LDFLAGS=""
# Race detection requires CGO enabled.
FV_RACE_DETECTOR_ENABLED?=false
endif

ifeq ($(ARCH), amd64)
BUILD_TARGETS+=bin/calico-felix.exe
GOEXPERIMENT=boringcrypto
TAGS=fipsstrict
endif

build: $(BUILD_TARGETS)
build-all: $(addprefix sub-build-,$(VALIDARCHES))
sub-build-%:
	$(MAKE) build ARCH=$*

bin/calico-felix: bin/calico-felix-$(ARCH)
	ln -f bin/calico-felix-$(ARCH) bin/calico-felix

libbpf: $(LIBBPF_A)
$(LIBBPF_A): $(shell find bpf-gpl/include/libbpf -type f ! -name "*.a")
	mkdir -p bpf-gpl/include/libbpf/src/$(ARCH)
	$(MAKE) register ARCH=$(ARCH)
	$(DOCKER_GO_BUILD) sh -c "make -j 16 -C bpf-gpl/include/libbpf/src BUILD_STATIC_ONLY=1 OBJDIR=$(ARCH)"

DOCKER_GO_BUILD_CGO=$(DOCKER_RUN) $(TARGET_PLATFORM) -e CGO_ENABLED=$(CGO_ENABLED) -e CGO_LDFLAGS=$(CGO_LDFLAGS) -e CGO_CFLAGS=$(CGO_CFLAGS) $(CALICO_BUILD)
DOCKER_GO_BUILD_CGO_WINDOWS=$(DOCKER_RUN) $(TARGET_PLATFORM) -e CGO_ENABLED=$(CGO_ENABLED) $(CALICO_BUILD)

bin/calico-felix-$(ARCH): $(LIBBPF_A) $(SRC_FILES)
	@echo Building felix for $(ARCH) on $(BUILDARCH)
	mkdir -p bin
	if [ "$(SEMAPHORE)" != "true" -o ! -e $@ ] ; then \
	  $(DOCKER_GO_BUILD_CGO) sh -c '$(GIT_CONFIG_SSH) \
	     go build -v -o $@ -v -buildvcs=false $(BUILD_FLAGS) $(LDFLAGS) "$(PACKAGE_NAME)/cmd/calico-felix"'; \
	fi

bin/calico-felix-race-$(ARCH): $(LIBBPF_A) $(SRC_FILES) ../go.mod
	@echo Building felix with race detector enabled for $(ARCH) on $(BUILDARCH)
	mkdir -p bin
	if [ "$(SEMAPHORE)" != "true" -o ! -e $@ ] ; then \
	  $(DOCKER_GO_BUILD_CGO) sh -c '$(GIT_CONFIG_SSH) \
	     go build -v -race -o $@ -v -buildvcs=false $(BUILD_FLAGS) $(LDFLAGS) "$(PACKAGE_NAME)/cmd/calico-felix"'; \
	fi

# Generate the protobuf bindings for go. The proto/felixbackend.pb.go file is included in SRC_FILES
protobuf proto/felixbackend.pb.go: proto/felixbackend.proto
	docker run --rm --user $(LOCAL_USER_ID):$(LOCAL_GROUP_ID) \
		  -v $(CURDIR):/code -v $(CURDIR)/proto:/src:rw \
		      $(PROTOC_CONTAINER) \
		      --gogofaster_out=plugins=grpc:. \
		      felixbackend.proto
	# Make sure the generated code won't cause a static-checks failure.
	$(MAKE) fix

# We pre-build lots of different variants of the TC programs, defer to the script.
BPF_GPL_O_FILES:=$(addprefix bpf-gpl/,$(shell bpf-gpl/list-objs))

# There's a one-to-one mapping from UT C files to objects and the same for the apache programs..
BPF_GPL_UT_O_FILES:=$(BPF_GPL_UT_C_FILES:.c=.o) $(addprefix bpf-gpl/,$(shell bpf-gpl/list-ut-objs))
BPF_APACHE_C_FILES:=$(wildcard bpf-apache/*.c)
BPF_APACHE_O_FILES:=$(addprefix bpf-apache/bin/,$(notdir $(BPF_APACHE_C_FILES:.c=.o)))

ALL_BPF_PROGS=$(BPF_GPL_O_FILES) $(BPF_APACHE_O_FILES)


# Mark the BPF programs phony so we'll always defer to their own makefile.  This is OK as long as
# we're only depending on the BPF programs from other phony targets.  (Otherwise, we'd do
# unnecessary rebuilds of anything that depends on the BPF prgrams.)
.PHONY: build-bpf clean-bpf
build-bpf:
	$(MAKE) register ARCH=$(ARCH)
	$(DOCKER_GO_BUILD) sh -c "make -j 16 -C bpf-apache all && \
	                          make -j 16 -C bpf-gpl all ut-objs"
	rm -rf bin/bpf
	mkdir -p bin/bpf
	cp $(ALL_BPF_PROGS) bin/bpf

clean-bpf:
	rm -f bpf-gpl/*.d bpf-apache/*.d
	$(DOCKER_GO_BUILD) sh -c "make -j -C bpf-apache clean && \
	                          make -j -C bpf-gpl clean"

bpf/asm/opcode_string.go: bpf/asm/asm.go
	$(DOCKER_GO_BUILD) go generate ./bpf/asm/

###############################################################################
# Building the image
###############################################################################
# Build the calico/felix docker image, which contains only Felix.
.PHONY: $(FELIX_IMAGE) $(FELIX_IMAGE)-$(ARCH)

# Whether or not to use race-detector in the produced image.
ifeq ($(FV_RACE_DETECTOR_ENABLED),true)
FV_BINARY=bin/calico-felix-race-$(ARCH)
else
FV_BINARY=bin/calico-felix-$(ARCH)
endif

# by default, build the image for the target architecture
.PHONY: image-all
image-all: $(addprefix sub-image-,$(VALIDARCHES))
sub-image-%:
	$(MAKE) image ARCH=$*

# Note: the felix image is really a "test environment" image.  It doesn't include Felix itself and we don't ship it.
image: $(FELIX_IMAGE)
$(FELIX_IMAGE): $(FELIX_IMAGE)-$(ARCH)

# Touchfile created when we make the felix image.
FELIX_CONTAINER_CREATED=.calico_felix.created-$(ARCH)
FELIX_IMAGE_WITH_TAG=$(FELIX_IMAGE):latest-$(ARCH)
FELIX_IMAGE_ID=$(shell docker images -q $(FELIX_IMAGE_WITH_TAG))
$(FELIX_IMAGE)-$(ARCH): $(FELIX_CONTAINER_CREATED)
$(FELIX_CONTAINER_CREATED): docker-image/calico-felix-wrapper \
                            docker-image/felix.cfg \
                            docker-image/Dockerfile* \
                            $(shell test "$(FELIX_IMAGE_ID)" || echo force-rebuild)
	$(MAKE) register
	$(DOCKER_BUILD) -t $(FELIX_IMAGE_WITH_TAG) --file ./docker-image/Dockerfile.$(ARCH) docker-image --load;
	$(MAKE) retag-build-images-with-registries VALIDARCHES=$(ARCH) IMAGETAG=latest
	touch $(FELIX_CONTAINER_CREATED)

###############################################################################
# Unit Tests
###############################################################################

UT_PACKAGES_TO_SKIP?=fv,k8sfv,bpf/ut

.PHONY: ut
ut combined.coverprofile: $(SRC_FILES) $(LIBBPF_A) build-bpf
	@echo Running Go UTs.
	$(DOCKER_GO_BUILD) ./utils/run-coverage -skipPackage $(UT_PACKAGES_TO_SKIP) $(GINKGO_ARGS)

###############################################################################
# FV Tests
###############################################################################
fv/fv.test: $(LIBBPF_A) $(SRC_FILES) $(FV_SRC_FILES)
	# We pre-build the FV test binaries so that we can run them
	# outside a container and allow them to interact with docker.
	$(DOCKER_GO_BUILD_CGO) go test $(BUILD_FLAGS) ./$(shell dirname $@) -c --tags fvtests -o $@

REMOTE_DEPS=build-typha

build-typha:
	make -C ../typha image DEV_REGISTRIES=felix-test

.PHONY: fv fv-prereqs fv-no-build

# Build everything needed to run the FVs but don't actually run them.  The semaphore job runs
# this target to build everything so that it can be cached and shared between downstream jobs.
fv-prereqs: $(REMOTE_DEPS) \
            $(FELIX_CONTAINER_CREATED) \
            $(FV_BINARY) \
            bin/iptables-locker \
            bin/test-workload \
            bin/test-connection \
            bin/calico-bpf \
            bin/pktgen \
            build-bpf \
            fv/fv.test

# runs all of the fv tests
# to run it in parallel, decide how many parallel engines you will run, and in each one call:
#	 $(MAKE) fv FV_BATCHES_TO_RUN="<num>" FV_NUM_BATCHES=<num>
# where
#	 FV_NUM_BATCHES = total parallel batches
#	 FV_BATCHES_TO_RUN = which number this is
# e.g. to run it in 10 parallel runs:
#	 $(MAKE) fv FV_BATCHES_TO_RUN="1" FV_NUM_BATCHES=10     # the first 1/10
#	 $(MAKE) fv FV_BATCHES_TO_RUN="2" FV_NUM_BATCHES=10     # the second 1/10
#	 $(MAKE) fv FV_BATCHES_TO_RUN="3" FV_NUM_BATCHES=10     # the third 1/10
#	 ...
#	 $(MAKE) fv FV_BATCHES_TO_RUN="10" FV_NUM_BATCHES=10    # the tenth 1/10
#	 etc.
fv fv/latency.log fv/data-races.log: fv-prereqs
	$(MAKE) fv-no-prereqs

# The semaphore job runs this target directly to avoid accidental rebuilds.
fv-no-prereqs:
	rm -f fv/data-races.log fv/latency.log
	cd fv && \
	  FV_FELIXIMAGE=$(FV_FELIXIMAGE) \
	  FV_ETCDIMAGE=$(FV_ETCDIMAGE) \
	  FV_TYPHAIMAGE=$(FV_TYPHAIMAGE) \
	  FV_K8SIMAGE=$(FV_K8SIMAGE) \
	  FV_NUM_BATCHES=$(FV_NUM_BATCHES) \
	  FV_BATCHES_TO_RUN="$(FV_BATCHES_TO_RUN)" \
	  FV_FELIX_LOG_LEVEL="$(FV_FELIX_LOG_LEVEL)" \
	  CERTS_PATH=$(CERTS_PATH) \
	  PRIVATE_KEY=`pwd`/private.key \
	  GINKGO_ARGS='$(GINKGO_ARGS)' \
	  GINKGO_FOCUS="$(GINKGO_FOCUS)" \
	  ARCH=$(ARCH) \
	  FELIX_FV_ENABLE_BPF="$(FELIX_FV_ENABLE_BPF)" \
	  FV_RACE_DETECTOR_ENABLED=$(FV_RACE_DETECTOR_ENABLED) \
	  FV_BINARY=$(FV_BINARY) \
	  FELIX_FV_WIREGUARD_AVAILABLE=`./wireguard-available >/dev/null && echo true || echo false` \
	  ./run-batches
	@if [ -e fv/latency.log ]; then \
	   echo; \
	   echo "Latency results:"; \
	   echo; \
	   cat fv/latency.log; \
	fi

fv-bpf:
	$(MAKE) fv FELIX_FV_ENABLE_BPF=true

check-wireguard:
	fv/wireguard-available || ( echo "WireGuard not available."; exit 1 )

fv/win-fv.exe: $(REMOTE_DEPS)
	mkdir -p bin
	$(DOCKER_GO_BUILD_CGO) sh -c '$(GIT_CONFIG_SSH) \
		GOOS=windows CC=x86_64-w64-mingw32-gcc go test --buildmode=exe -mod=mod ./$(shell dirname $@)/winfv -c -o $@'

###############################################################################
# K8SFV Tests
###############################################################################
# Targets for Felix testing with the k8s backend and a k8s API server,
# with k8s model resources being injected by a separate test client.
GET_CONTAINER_IP := docker inspect --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
GRAFANA_VERSION=4.1.2
PROMETHEUS_DATA_DIR := $$HOME/prometheus-data
K8SFV_PROMETHEUS_DATA_DIR := $(PROMETHEUS_DATA_DIR)/k8sfv
$(K8SFV_PROMETHEUS_DATA_DIR):
	mkdir -p $@

# Directories that aren't part of the main Felix program,
# e.g. standalone test programs.
K8SFV_DIR:=k8sfv
NON_FELIX_DIRS:=$(K8SFV_DIR)
# Files for the Felix+k8s backend test program.
K8SFV_GO_FILES:=$(shell find ./$(K8SFV_DIR) -name prometheus -prune -o -type f -name '*.go' -print)

.PHONY: k8sfv-test k8sfv-test-existing-felix
# Run k8sfv test with Felix built from current code.
# control whether or not we use typha with USE_TYPHA=true or USE_TYPHA=false
# e.g.
#       $(MAKE) k8sfv-test JUST_A_MINUTE=true USE_TYPHA=true
#       $(MAKE) k8sfv-test JUST_A_MINUTE=true USE_TYPHA=false
k8sfv-test: $(REMOTE_DEPS) $(FELIX_CONTAINER_CREATED) $(FV_BINARY) bin/k8sfv.test
	FV_ETCDIMAGE=$(FV_ETCDIMAGE) \
	FV_TYPHAIMAGE=$(FV_TYPHAIMAGE) \
	FV_FELIXIMAGE=$(FV_FELIXIMAGE) \
	FV_K8SIMAGE=$(FV_K8SIMAGE) \
	FV_BINARY=$(FV_BINARY) \
	CERTS_PATH=$(CERTS_PATH) \
	PRIVATE_KEY=`pwd`/fv/private.key \
	k8sfv/run-test

bin/k8sfv.test: $(K8SFV_GO_FILES)
	@echo Building $@...
	$(DOCKER_GO_BUILD) \
	    sh -c 'go test -c $(BUILD_FLAGS) -o $@ ./k8sfv'

.PHONY: run-prometheus run-grafana stop-prometheus stop-grafana
run-prometheus: stop-prometheus $(K8SFV_PROMETHEUS_DATA_DIR)
	FELIX_IP=`$(GET_CONTAINER_IP) k8sfv-felix` && \
	sed "s/__FELIX_IP__/$${FELIX_IP}/" < $(K8SFV_DIR)/prometheus/prometheus.yml.in > $(K8SFV_DIR)/prometheus/prometheus.yml
	docker run --detach --name k8sfv-prometheus \
	-v $(CURDIR)/$(K8SFV_DIR)/prometheus/prometheus.yml:/etc/prometheus.yml \
	-v $(K8SFV_PROMETHEUS_DATA_DIR):/prometheus \
	prom/prometheus \
	-config.file=/etc/prometheus.yml \
	-storage.local.path=/prometheus

stop-prometheus:
	@-docker rm -f k8sfv-prometheus
	sleep 2

run-grafana: stop-grafana run-prometheus
	docker run --detach --name k8sfv-grafana -p 3000:3000 \
	-v $(CURDIR)/$(K8SFV_DIR)/grafana:/etc/grafana \
	-v $(CURDIR)/$(K8SFV_DIR)/grafana-dashboards:/etc/grafana-dashboards \
	grafana/grafana:$(GRAFANA_VERSION) --config /etc/grafana/grafana.ini
	# Wait for it to get going.
	sleep 5
	# Configure prometheus data source.
	PROMETHEUS_IP=`$(GET_CONTAINER_IP) k8sfv-prometheus` && \
	sed "s/__PROMETHEUS_IP__/$${PROMETHEUS_IP}/" < $(K8SFV_DIR)/grafana-datasources/my-prom.json.in | \
	curl 'http://admin:admin@127.0.0.1:3000/api/datasources' -X POST \
	    -H 'Content-Type: application/json;charset=UTF-8' --data-binary @-

stop-grafana:
	@-docker rm -f k8sfv-grafana
	sleep 2

bin/calico-bpf: $(SRC_FILES)
	@echo Building calico-bpf...
	mkdir -p bin
	$(DOCKER_GO_BUILD_CGO) sh -c '$(GIT_CONFIG_SSH) \
	    GOEXPERIMENT=$(GOEXPERIMENT) go build -v -o $@ -tags $(TAGS) -v -buildvcs=false $(BUILD_FLAGS) $(LDFLAGS) "$(PACKAGE_NAME)/cmd/calico-bpf"'

bin/pktgen: $(SRC_FILES) $(FV_SRC_FILES)
	@echo Building pktgen...
	mkdir -p bin
	$(DOCKER_GO_BUILD) sh -c '$(GIT_CONFIG_SSH) \
		GOEXPERIMENT=$(GOEXPERIMENT) go build -v -o $@ -tags $(TAGS) -v -buildvcs=false $(BUILD_FLAGS) $(LDFLAGS) "$(PACKAGE_NAME)/fv/pktgen"'

bin/iptables-locker: ../go.mod $(shell find iptables -type f -name '*.go' -print)
	@echo Building iptables-locker...
	$(DOCKER_GO_BUILD) sh -c '$(GIT_CONFIG_SSH) \
	    GOEXPERIMENT=$(GOEXPERIMENT) go build -v -o $@ -tags $(TAGS) -v -buildvcs=false $(BUILD_FLAGS) $(LDFLAGS) "$(PACKAGE_NAME)/fv/iptables-locker"'

bin/test-workload: ../go.mod fv/cgroup/cgroup.go fv/utils/utils.go fv/connectivity/*.go fv/test-workload/*.go
	@echo Building test-workload...
	$(DOCKER_GO_BUILD) sh -c '$(GIT_CONFIG_SSH) \
	    GOEXPERIMENT=$(GOEXPERIMENT) go build -v -o $@ -tags $(TAGS) -v -buildvcs=false $(BUILD_FLAGS) $(LDFLAGS) "$(PACKAGE_NAME)/fv/test-workload"'

bin/test-connection: ../go.mod fv/cgroup/cgroup.go fv/utils/utils.go fv/connectivity/*.go fv/test-connection/*.go
	@echo Building test-connection...
	mkdir -p bin
	$(DOCKER_GO_BUILD) sh -c '$(GIT_CONFIG_SSH) \
	    GOEXPERIMENT=$(GOEXPERIMENT) go build -v -o $@ -tags $(TAGS) -v -buildvcs=false $(BUILD_FLAGS) $(LDFLAGS) "$(PACKAGE_NAME)/fv/test-connection"'
st:
	@echo "No STs available"

###############################################################################
# Generate files
###############################################################################
gen-files: protobuf bpf/asm/opcode_string.go

###############################################################################
# CI/CD
###############################################################################
.PHONY: ci cd

ci: mod-download image-all ut
ifeq (,$(filter fv, $(EXCEPT)))
	@$(MAKE) fv
endif
ifeq (,$(filter k8sfv-test, $(EXCEPT)))
	@$(MAKE) k8sfv-test JUST_A_MINUTE=true USE_TYPHA=true
	@$(MAKE) k8sfv-test JUST_A_MINUTE=true USE_TYPHA=false
endif

## Deploy images to registry
cd: cd-common

###############################################################################
# Release
###############################################################################
## Produces a clean build of release artifacts at the specified version.
release-build: .release-$(VERSION).created
.release-$(VERSION).created:
	$(MAKE) clean build bin/calico-bpf
	touch $@

###############################################################################
# Developer helper scripts (not used by build or test)
###############################################################################
.PHONY: ut-no-cover
ut-no-cover: $(SRC_FILES)
	@echo Running Go UTs without coverage.
	$(DOCKER_GO_BUILD) ginkgo -r -skipPackage $(UT_PACKAGES_TO_SKIP) $(GINKGO_ARGS)

.PHONY: ut-watch
ut-watch: $(SRC_FILES)
	@echo Watching go UTs for changes...
	$(DOCKER_GO_BUILD) ginkgo watch -r -skipPackage $(UT_PACKAGES_TO_SKIP) $(GINKGO_ARGS)

.PHONY: bin/bpf.test
bin/bpf.test: $(GENERATED_FILES) $(shell find bpf/ -name '*.go')
	$(DOCKER_GO_BUILD_CGO) go test $(BUILD_FLAGS) ./bpf/ -c -o $@

.PHONY: bin/bpf_ut.test
bin/bpf_ut.test: $(GENERATED_FILES) $(shell find bpf/ -name '*.go')
	$(DOCKER_GO_BUILD_CGO) go test $(BUILD_FLAGS) ./bpf/ut -c -o $@

# Build debug version of bpf.test for use with the delve debugger.
.PHONY: bin/bpf_debug.test
bin/bpf_debug.test: $(GENERATED_FILES) $(shell find bpf/ -name '*.go')
	$(DOCKER_GO_BUILD_CGO) go test $(BUILD_FLAGS) ./bpf/ut -c -gcflags="-N -l" -o $@

.PHONY: ut-bpf
ut-bpf: $(LIBBPF_A) bin/bpf_ut.test bin/bpf.test build-bpf
	$(DOCKER_RUN) \
		--privileged \
		-e RUN_AS_ROOT=true \
		$(CALICO_BUILD) sh -c ' \
		mount bpffs /sys/fs/bpf -t bpf && \
		cd /go/src/$(PACKAGE_NAME)/bpf/ && \
		BPF_FORCE_REAL_LIB=true ../bin/bpf.test -test.v -test.run "$(FOCUS)"'
	$(DOCKER_RUN) \
		--privileged \
		-e RUN_AS_ROOT=true \
		-v `pwd`:/code \
		-v `pwd`/bpf-gpl/bin:/usr/lib/calico/bpf \
		$(CALICO_BUILD) sh -c ' \
		mount bpffs /sys/fs/bpf -t bpf && \
		cd /go/src/$(PACKAGE_NAME)/bpf/ut && \
		../../bin/bpf_ut.test -test.v -test.run "$(FOCUS)"'

## Launch a browser with Go coverage stats for the whole project.
.PHONY: cover-browser
cover-browser: combined.coverprofile
	go tool cover -html="combined.coverprofile"

.PHONY: cover-report
cover-report: combined.coverprofile
	# Print the coverage.  We use sed to remove the verbose prefix and trim down
	# the whitespace.
	@echo
	@echo ======== All coverage =========
	@echo
	@$(DOCKER_GO_BUILD) sh -c 'go tool cover -func combined.coverprofile | \
				   sed 's=$(PACKAGE_NAME)/==' | \
				   column -t'
	@echo
	@echo ======== Missing coverage only =========
	@echo
	@$(DOCKER_GO_BUILD) sh -c "go tool cover -func combined.coverprofile | \
				   sed 's=$(PACKAGE_NAME)/==' | \
				   column -t | \
				   grep -v '100\.0%'"

bin/calico-felix.transfer-url: bin/calico-felix
	$(DOCKER_GO_BUILD) sh -c 'curl --upload-file bin/calico-felix https://transfer.sh/calico-felix > $@'

# Cross-compile Felix for Windows
bin/calico-felix.exe: $(SRC_FILES)
	@echo Building felix for Windows...
	mkdir -p bin
	$(DOCKER_RUN) $(CALICO_BUILD) sh -c '$(GIT_CONFIG_SSH) \
	   	GOOS=windows go build -buildvcs=false -v -o $@ -v $(LDFLAGS) "$(PACKAGE_NAME)/cmd/calico-felix" && \
		( ldd $@ 2>&1 | grep -q "Not a valid dynamic program\|not a dynamic executable" || \
		( echo "Error: $@ was not statically linked"; false ) )'

.PHONY: patch-script
patch-script: bin/calico-felix.transfer-url
	$(DOCKER_GO_BUILD) bash -c 'utils/make-patch-script.sh $$(cat bin/calico-felix.transfer-url)'

## Generate a diagram of Felix's internal calculation graph.
docs/calc.pdf: docs/calc.dot
	cd docs/ && dot -Tpdf calc.dot -o calc.pdf

## Install or update the tools used by the build
.PHONY: update-tools
update-tools:
	go get -u github.com/onsi/ginkgo/ginkgo

# Override the default golangci-lint target from the library makefile with a version that uses the right CGO
# argument for Felix.
golangci-lint: $(GENERATED_FILES) $(LIBBPF_A)
	$(DOCKER_GO_BUILD_CGO) sh -c '$(GIT_CONFIG_SSH) golangci-lint run $(LINT_ARGS)'
