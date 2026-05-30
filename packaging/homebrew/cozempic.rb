class Cozempic < Formula
  include Language::Python::Virtualenv

  desc "Context cleaning CLI for Claude Code — prune bloat, protect agent teams"
  homepage "https://github.com/Ruya-AI/cozempic"
  url "https://files.pythonhosted.org/packages/ba/a2/f6533485453ab563bb0b7020bed564113ca5d788b3488fc913d7b5aff9fc/cozempic-1.8.18.tar.gz"
  sha256 "da2c37cb85c1652b16af27f3593fc4890b1e8d878c2df0788da27dd195da1cb1"
  license "MIT"

  depends_on "python@3.12"

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
