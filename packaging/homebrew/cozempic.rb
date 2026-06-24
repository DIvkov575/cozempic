class Cozempic < Formula
  include Language::Python::Virtualenv

  desc "Context cleaning CLI for Claude Code — prune bloat, protect agent teams"
  homepage "https://github.com/Ruya-AI/cozempic"
  url "https://files.pythonhosted.org/packages/79/24/d79e5068040aa824fea252f11bce7be4c1b6fbe0f676262babb20d4c313d/cozempic-1.8.36.tar.gz"
  sha256 "8e6a4aab802259c4c4cbd48ebefd8325221d49673ed836e50edf2aa2ff6fb566"
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
