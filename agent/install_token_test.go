//go:build !windows

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// v1.0 (J13): the token as install.sh and install.ps1 write it is the token
// the agent reads, for every character a user may type but control ones.
var installTokens = []string{
	`plain-token-1234`,
	`with "double" quotes`,
	`back\slash\\twice`,
	`it's a 'single' one`,
	`$HOME and ${PATH} and $(id)`,
	`spaces  inside and trailing `,
	` leading space`,
	`hash # not a comment#`,
	`colon: and key: value`,
	`all "of' \ $x # : together`,
	`unicode-ünïcödé-€`,
}

// shellFunction is one function's text, cut out of install.sh.
func shellFunction(t *testing.T, name string) string {
	t.Helper()
	src := string(mustRead(t, filepath.Join("..", "install.sh")))
	m := regexp.MustCompile(`(?ms)^` + name + `\(\) \{\n.*?^\}\n`).FindString(src)
	if m == "" {
		t.Fatalf("%s not found in install.sh", name)
	}
	return m
}

func runBash(t *testing.T, script string, args ...string) string {
	t.Helper()
	out, err := exec.Command("bash", append([]string{"-c", script, "x"}, args...)...).Output()
	if err != nil {
		t.Fatalf("bash: %v", err)
	}
	return string(out)
}

func agentToken(t *testing.T, line string) string {
	t.Helper()
	cfg, err := loadWith(t, "port: 9099\n"+line+"\n")
	if err != nil {
		t.Fatalf("the agent cannot read %q: %v", line, err)
	}
	return cfg.Token
}

func TestInstallShTokenRoundTrip(t *testing.T) {
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash not installed")
	}
	quote := shellFunction(t, "yaml_single_quote")
	read := shellFunction(t, "read_yaml_scalar")
	dir := t.TempDir()
	for _, tok := range installTokens {
		line := "token: " + runBash(t, quote+`yaml_single_quote "$1"`, tok)
		if got := agentToken(t, line); got != tok {
			t.Errorf("agent read %q from %q, want %q", got, line, tok)
		}
		path := filepath.Join(dir, "config.yaml")
		os.WriteFile(path, []byte("port: 9099\n"+line+"\nscan_interval: 60s\n"), 0o600)
		back := runBash(t, read+`read_yaml_scalar "$1" token`, path)
		if back != tok {
			t.Errorf("install.sh read back %q, want %q", back, tok)
		}
		// Rewritten from what was read back: the same line.
		if again := "token: " + runBash(t, quote+`yaml_single_quote "$1"`, back); again != line {
			t.Errorf("rewrite %q != %q", again, line)
		}
	}
}

// The forms older installers and hand edits left: double-quoted (with the
// escapes YAML needs) and plain. install.sh reads them as the agent does.
func TestInstallShReadsOlderForms(t *testing.T) {
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash not installed")
	}
	read := shellFunction(t, "read_yaml_scalar")
	dir := t.TempDir()
	for _, line := range []string{
		`token: "plain-token-1234"`,
		`token: "with \"double\" quotes"`,
		`token: "back\\slash"`,
		`token: "hash # inside quotes"  # a comment`,
		`token: plain-token-1234`,
		`token: plain-token-1234   # a comment`,
		`token:    'spaced'`,
		`token: ''`,
	} {
		path := filepath.Join(dir, "config.yaml")
		os.WriteFile(path, []byte(line+"\n"), 0o600)
		got := runBash(t, read+`read_yaml_scalar "$1" token`, path)
		if want := agentToken(t, line); got != want {
			t.Errorf("%s: install.sh %q, agent %q", line, got, want)
		}
	}
}

func TestInstallPs1TokenRoundTrip(t *testing.T) {
	pwsh, err := exec.LookPath("pwsh")
	if err != nil {
		t.Skip("pwsh not installed")
	}
	script := filepath.Join(t.TempDir(), "rt.ps1")
	os.WriteFile(script, []byte(strings.ReplaceAll(`
$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$errs)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if (@('ConvertTo-YamlSingleQuoted', 'ConvertFrom-YamlScalarText', 'Read-YamlScalar', 'Test-TokenText') -contains $f.Name) { . ([scriptblock]::Create($f.Extent.Text)) }
}
$tok = [System.IO.File]::ReadAllText($args[1])
$line = "token: " + (ConvertTo-YamlSingleQuoted $tok)
[System.IO.File]::WriteAllText($args[2], "port: 9099BTn$lineBTn")
$back = Read-YamlScalar -Path $args[2] -Key 'token'
[System.IO.File]::WriteAllText($args[3], [string]$back)
`, "BT", "`")), 0o600)
	dir := t.TempDir()
	for _, tok := range installTokens {
		in, cfgPath, back := filepath.Join(dir, "in"), filepath.Join(dir, "config.yaml"), filepath.Join(dir, "back")
		os.WriteFile(in, []byte(tok), 0o600)
		cmd := exec.Command(pwsh, "-NoProfile", "-NonInteractive", "-File", script,
			filepath.Join("..", "install.ps1"), in, cfgPath, back)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("pwsh: %v %s", err, out)
		}
		written := mustRead(t, cfgPath)
		cfg, err := loadWith(t, string(written))
		if err != nil || cfg.Token != tok {
			t.Errorf("agent read %q from %q (%v), want %q", cfg.Token, written, err, tok)
		}
		if got := string(mustRead(t, back)); got != tok {
			t.Errorf("install.ps1 read back %q, want %q", got, tok)
		}
	}
}
