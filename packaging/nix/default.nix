{
  lib,
  python3Packages,
  fetchPypi,
}:

python3Packages.buildPythonApplication rec {
  pname = "cozempic";
  version = "1.8.30";
  pyproject = true;

  __structuredAttrs = true;

  src = fetchPypi {
    inherit pname version;
    hash = "sha256-bZreqemPAgugG4Lb0s3JTRdjRWgWr7a6RTDcE5A4vl0=";
  };

  build-system = [ python3Packages.setuptools ];

  pythonImportsCheck = [ "cozempic" ];

  meta = {
    description = "Context cleaning CLI for Claude Code — prune bloat, protect agent teams from compaction";
    homepage = "https://github.com/Ruya-AI/cozempic";
    changelog = "https://github.com/Ruya-AI/cozempic/releases/tag/v${version}";
    license = lib.licenses.mit;
    mainProgram = "cozempic";
    maintainers = with lib.maintainers; [ junaidtitan ];
    platforms = lib.platforms.all;
  };
}
