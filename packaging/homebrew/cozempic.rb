class Cozempic < Formula
  include Language::Python::Virtualenv

  desc "Context cleaning CLI for Claude Code — prune bloat, protect agent teams"
  homepage "https://github.com/Ruya-AI/cozempic"
  url "https://files.pythonhosted.org/packages/54/0a/dc68db45566f061dbd8a3e77f7b9214017d4f1ab82254d27c23f11086b2b/cozempic-1.8.30.tar.gz"
  sha256 "6d9adea9e98f020ba01b82dbd2cdc94d1763456816afb6ba4530dc139038be5d"
  license "MIT"

  depends_on "python@3.12"
  depends_on "uv" # MCP server launches via `uv run` (#120)

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      Cozempic auto-initializes on first use — no manual setup needed.
      Every Claude Code session is protected automatically after the first
      cozempic command. To opt out:

        export COZEMPIC_NO_GLOBAL_INIT=1
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/cozempic --version")
    assert_match "diagnose", shell_output("#{bin}/cozempic --help")
  end
end
