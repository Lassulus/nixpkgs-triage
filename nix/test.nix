# The dashboard serves, and its sync loop runs `triage update` inside the service sandbox.
# The VM has no network, so the sync keeps hitting network errors; the server must stay up.
{ pkgs, module }:

pkgs.testers.runNixOSTest {
  name = "nixpkgs-triage";
  nodes.machine = {
    imports = [ module ];
    services.nixpkgs-triage = {
      enable = true;
      syncInterval = 5;
      environmentFile = pkgs.writeText "nixpkgs-triage-env" "GITHUB_TOKEN=dummy";
    };
  };
  testScript = ''
    machine.wait_for_unit("nixpkgs-triage.service")
    machine.wait_for_open_port(8080)
    assert "0 of 0 open PRs match" in machine.succeed("curl -sf http://127.0.0.1:8080/")
    assert machine.succeed("curl -sf 'http://127.0.0.1:8080/rows?sort=newest'") == ""
    assert "not in the database" in machine.succeed("curl -sf http://127.0.0.1:8080/pr/1")
    machine.wait_until_succeeds(
      "journalctl -u nixpkgs-triage.service | grep -q 'sync: .*network error'", timeout=60
    )
    machine.succeed("test -f /var/lib/nixpkgs-triage/triage.db.sync.lock")
    machine.succeed("systemctl is-active nixpkgs-triage.service")
  '';
}
