# SMART Sniffer Agent — Cross-compilation Makefile
#
# Usage:
#   make              — build for the current platform
#   make all          — build for all supported platforms
#   make checksums    — generate SHA256 checksums for all binaries
#   make release      — build all + generate checksums (used by CI)
#   make clean        — remove build artifacts
#   make linux-amd64  — build a specific target

BINARY   := smartha-agent
# VERSION is derived from git by default so self-builders get an accurate
# version string embedded in their binary. CI overrides this explicitly on
# the command line (e.g. `make release VERSION=0.5.1`). Fallback to "dev"
# when git is unavailable or the tree is not a git checkout.
VERSION  ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS  := -ldflags "-s -w -X main.version=$(VERSION)"
OUTDIR   := build

# Linux builds are statically linked (CGO_ENABLED=0) to avoid glibc version
# dependencies on older distros (Debian 9, RHEL 7) and to support musl-based
# distros (Alpine). The agent does no stdlib name resolution or net.Dial
# calls, so the pure-Go resolver fallback has no functional impact. See
# docs/internal/plans/plan-v0.5.1-consolidated-changes.md §Change 1.
LINUX_ENV := CGO_ENABLED=0

# Default: build for the host platform.
.PHONY: build
build:
	go build $(LDFLAGS) -o $(OUTDIR)/$(BINARY) .

# All supported cross-compilation targets.
.PHONY: all
all: linux-amd64 linux-arm64 linux-arm darwin-amd64 darwin-arm64 windows-amd64

.PHONY: linux-amd64
linux-amd64:
	$(LINUX_ENV) GOOS=linux GOARCH=amd64 go build $(LDFLAGS) -o $(OUTDIR)/$(BINARY)-linux-amd64 .

.PHONY: linux-arm64
linux-arm64:
	$(LINUX_ENV) GOOS=linux GOARCH=arm64 go build $(LDFLAGS) -o $(OUTDIR)/$(BINARY)-linux-arm64 .

.PHONY: linux-arm
linux-arm:
	$(LINUX_ENV) GOOS=linux GOARCH=arm GOARM=7 go build $(LDFLAGS) -o $(OUTDIR)/$(BINARY)-linux-arm .

.PHONY: darwin-amd64
darwin-amd64:
	GOOS=darwin GOARCH=amd64 go build $(LDFLAGS) -o $(OUTDIR)/$(BINARY)-darwin-amd64 .

.PHONY: darwin-arm64
darwin-arm64:
	GOOS=darwin GOARCH=arm64 go build $(LDFLAGS) -o $(OUTDIR)/$(BINARY)-darwin-arm64 .

.PHONY: windows-amd64
windows-amd64:
	GOOS=windows GOARCH=amd64 go build $(LDFLAGS) -o $(OUTDIR)/$(BINARY)-windows-amd64.exe .

# Generate SHA256 checksums for all binaries in the build directory.
.PHONY: checksums
checksums:
	@echo "Generating checksums..."
	@cd $(OUTDIR) && sha256sum $(BINARY)-* > checksums.txt 2>/dev/null || \
		cd $(OUTDIR) && shasum -a 256 $(BINARY)-* > checksums.txt
	@echo "Checksums written to $(OUTDIR)/checksums.txt"
	@cat $(OUTDIR)/checksums.txt

# Build all targets and generate checksums (CI entrypoint).
.PHONY: release
release: all checksums

.PHONY: clean
clean:
	rm -rf $(OUTDIR)
