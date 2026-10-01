# The module in a VM: the dashboard serves, and its sync loop starts `triage update` inside the
# service sandbox. There is no network, so that sync retries network errors, which must not take
# the server down.
{ pkgs, module }:

pkgs.testers.runNixOSTest {
  name = "nixpkgs-triage";
  nodes.machine = {
    imports = [ module ];
    services.nixpkgs-triage = {
      enable = true;
      syncInterval = 5;
      # Test-only token; a real one belongs in a secret file outside the store.
      environmentFile = pkgs.writeText "nixpkgs-triage-env" "GITHUB_TOKEN=dummy";
    };
  };
  testScript = ''
    import json

    machine.wait_for_unit("nixpkgs-triage.service")
    machine.wait_for_open_port(8080)
    assert "nixpkgs-triage" in machine.succeed("curl -sf http://127.0.0.1:8080/")
    machine.succeed("curl -sf http://127.0.0.1:8080/app.js")

    status = json.loads(machine.succeed("curl -sf http://127.0.0.1:8080/api/status"))
    assert status["total_open"] == 0, status
    page = json.loads(machine.succeed("curl -sf 'http://127.0.0.1:8080/api/prs?sort=newest'"))
    assert page["prs"] == [] and page["next"] is None, page

    # The sync child started, took the sync lock and reached the GitHub client; the server keeps going.
    machine.wait_until_succeeds(
      "journalctl -u nixpkgs-triage.service | grep -q 'sync: .*network error'", timeout=60
    )
    machine.succeed("test -f /var/lib/nixpkgs-triage/triage.db")
    machine.succeed("test -f /var/lib/nixpkgs-triage/triage.db.sync.lock")
    machine.succeed("systemctl is-active nixpkgs-triage.service")
  '';
}
