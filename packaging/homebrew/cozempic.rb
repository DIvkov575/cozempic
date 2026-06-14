class Cozempic < Formula
  include Language::Python::Virtualenv

  desc "Context cleaning CLI for Claude Code — prune bloat, protect agent teams"
  homepage "https://github.com/Ruya-AI/cozempic"
  url "https://files.pythonhosted.org/packages/e7/fa/1ec53017b6540e16017b2cf5e4f31ec6e2c6b26c09b13af3a82650513032/cozempic-1.8.32.tar.gz"
  sha256 "9f199f4e304c95d3bb9540863128b5a2b26449fa34fd65b5af9c760fa068d11c"
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
