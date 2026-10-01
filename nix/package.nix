{
  lib,
  stdenvNoCC,
  python3,
}:

stdenvNoCC.mkDerivation {
  pname = "nixpkgs-triage";
  version = "0-unstable";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../triage
      ../nixpkgs_triage
      ../categories.toml
      ../prompts
    ];
  };

  # patchShebangs points `#!/usr/bin/env python3` at this interpreter.
  buildInputs = [ python3 ];

  # The code finds categories.toml, prompts/ and static/ next to itself, so it stays one tree.
  # The database and job directories default to that tree too, which is read-only in the store:
  # set TRIAGE_DB and TRIAGE_JOBS_DIR (the NixOS module does).
  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/nixpkgs-triage $out/bin
    cp -r triage nixpkgs_triage categories.toml prompts $out/share/nixpkgs-triage/
    patchShebangs $out/share/nixpkgs-triage/triage
    ${python3.interpreter} -m compileall -q $out/share/nixpkgs-triage/nixpkgs_triage
    ln -s $out/share/nixpkgs-triage/triage $out/bin/triage
    runHook postInstall
  '';

  meta = {
    description = "Triage workflow for open NixOS/nixpkgs pull requests: sync, categories, checks, web dashboard";
    mainProgram = "triage";
    platforms = lib.platforms.unix;
  };
}
