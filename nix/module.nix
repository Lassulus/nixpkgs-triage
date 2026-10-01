{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.nixpkgs-triage;
  stateDir = "/var/lib/nixpkgs-triage";
  host = if lib.hasInfix ":" cfg.address then "[${cfg.address}]" else cfg.address;
in
{
  options.services.nixpkgs-triage = {
    enable = lib.mkEnableOption "the nixpkgs-triage web dashboard with its background PR sync";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The nixpkgs-triage package.";
    };

    address = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      example = "::";
      description = "Address the dashboard listens on. It has no authentication; put a reverse proxy in front.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "Port the dashboard listens on.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open {option}`port` in the firewall.";
    };

    syncInterval = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 300;
      description = ''
        Seconds between the end of one `triage update` and the start of the next; 0 disables the sync loop.
        The first run is a full sync of all open PRs (about 40 minutes), later runs are incremental.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/nixpkgs-triage";
      description = ''
        systemd EnvironmentFile providing `GITHUB_TOKEN=…` for the sync (a token without scopes is enough
        to read public PRs). Keep it out of the Nix store.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.syncInterval == 0 || cfg.environmentFile != null;
        message = "services.nixpkgs-triage.environmentFile must provide GITHUB_TOKEN when syncInterval > 0.";
      }
    ];

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    systemd.services.nixpkgs-triage = {
      description = "nixpkgs-triage web dashboard";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      environment = {
        TRIAGE_DB = "${stateDir}/triage.db";
        TRIAGE_JOBS_DIR = "${stateDir}/jobs";
      };
      serviceConfig = {
        ExecStart = lib.escapeShellArgs [
          (lib.getExe cfg.package)
          "serve"
          "--listen"
          "${host}:${toString cfg.port}"
          "--sync-every"
          (toString cfg.syncInterval)
        ];
        EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;
        DynamicUser = true;
        StateDirectory = "nixpkgs-triage";
        WorkingDirectory = stateDir;
        Restart = "on-failure";
        RestartSec = 10;

        CapabilityBoundingSet = "";
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        PrivateDevices = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" ];
        UMask = "0077";
      };
    };
  };
}
