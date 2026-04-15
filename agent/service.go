// service.go — shared service-identity constants.
//
// ServiceName is the canonical identifier used when the agent is registered
// with the Windows Service Control Manager (SCM). It must match the name
// passed to New-Service / sc.exe in install.ps1 so that the binary's
// svc.Run() call and the registered service entry agree.
//
// Declared in its own file (rather than main_windows.go) so the constant is
// visible to non-Windows builds as well. This keeps cross-platform code
// that logs or references the service name from needing build tags.
//
// See docs/internal/plans/plan-v0.5.1-consolidated-changes.md §Change 2.
package main

// ServiceName is the Windows service identifier. Changing this value is a
// breaking change for existing installations — install.ps1 must be updated
// in lockstep, and upgraders will need to uninstall the old service first.
const ServiceName = "SmartHA-Agent"
