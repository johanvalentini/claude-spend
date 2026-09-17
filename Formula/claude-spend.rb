# Homebrew formula for claude-spend.
#
# This file is the source of truth; scripts/release.sh copies it into the tap repo
# (github.com/johanvalentini/homebrew-claude-spend, Formula/claude-spend.rb) on each release.
# Do not edit url/sha256/resources by hand: `./scripts/release.sh X.Y.Z` regenerates them
# (see .claude/skills/release/SKILL.md).
class ClaudeSpend < Formula
  include Language::Python::Virtualenv

  desc "Local OTLP collector and TUI for Claude Code token and cost usage"
  homepage "https://github.com/johanvalentini/claude-spend"
  url "https://github.com/johanvalentini/claude-spend/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "83431fe6ef09c09fd28cd47763e5fbd6e89a0229d6d35647675c564715fb5475"
  license "MIT"

  depends_on :macos
  depends_on "python@3.14"

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/06/ff/7841249c247aa650a76b9ee4bbaeae59370dc8bfd2f6c01f3630c35eb134/markdown_it_py-4.2.0.tar.gz"
    sha256 "04a21681d6fbb623de53f6f364d352309d4094dd4194040a10fd51833e418d49"
  end

  resource "mdit-py-plugins" do
    url "https://files.pythonhosted.org/packages/59/fc/f8d0863f8862f25602c0404d75568e89fb6b4109804645e5cdfb1be5cf56/mdit_py_plugins-0.6.1.tar.gz"
    sha256 "a2bca0f039f39dbd35fb74ae1b5f998608c437463371f0ff7f49a19a17a114d0"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/d6/54/cfe61301667036ec958cb99bd3efefba235e65cdeb9c84d24a8293ba1d90/mdurl-0.1.2.tar.gz"
    sha256 "bb413d29f5eea38f31dd4754dd7377d4465116fb207585f97bf925588687c1ba"
  end

  resource "platformdirs" do
    url "https://files.pythonhosted.org/packages/69/b7/802a56eca9f2fac455b8bab5375a2647b0f0e14a2cd63ef077de3c4a7658/platformdirs-4.11.7.tar.gz"
    sha256 "4f41487eeeeeb07f3a6625e61d9bc0ae6809f92d3386dbd74392fbb76108104d"
  end

  resource "plotext" do
    url "https://files.pythonhosted.org/packages/c9/d7/f75f397af966fe252d0d34ffd3cae765317fce2134f925f95e7d6725d1ce/plotext-5.3.2.tar.gz"
    sha256 "52d1e932e67c177bf357a3f0fe6ce14d1a96f7f7d5679d7b455b929df517068e"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/49/2e/ced460408999b33da6b31b0021b0f37d329e202d4169aeb164493778f25b/pygments-2.21.0.tar.gz"
    sha256 "610ca751c9bc2492b38eb9a38a7fbc93edbbb2d7182edaf34e66ae493dee5c8c"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/c0/8f/0722ca900cc807c13a6a0c696dacf35430f72e0ec571c4275d2371fca3e9/rich-15.0.0.tar.gz"
    sha256 "edd07a4824c6b40189fb7ac9bc4c52536e9780fbbfbddf6f1e2502c31b068c36"
  end

  resource "textual" do
    url "https://files.pythonhosted.org/packages/00/21/39a76b01bd5eea82a04baaca7580e105d8c59450df03998345bb2cfb307b/textual-8.2.8.tar.gz"
    sha256 "3f106a9fbc73e39dd266c9712432087de78a6d644084c7c241d6a25c3169115b"
  end

  resource "textual-plotext" do
    url "https://files.pythonhosted.org/packages/9a/b0/e4e0f38df057db778252db0dd2c08522d7222b8537b6a0181d797b9044bd/textual_plotext-1.0.1.tar.gz"
    sha256 "836f53a3316756609e194129a35c2875638e7958c261f541e0a794f7c98011be"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/f6/cc/6253133b5bb138fc3306cebfbda2c520f545d36b5be2c7255cc528bb45d6/typing_extensions-4.16.0.tar.gz"
    sha256 "dc983d19a509c94dba722ee6abd33940f7c05a89e243c47e907eb4db6f1a43e5"
  end

  def install
    virtualenv_install_with_resources
  end

  service do
    run [opt_bin/"claude-spend", "collector"]
    environment_variables CLAUDE_SPEND_PORT: "4318"
    keep_alive true
    log_path var/"log/claude-spend-collector.log"
    error_log_path var/"log/claude-spend-collector.log"
    process_type :background
  end

  def caveats
    <<~EOS
      Start the collector (launchd, keeps running across reboots):
        brew services start claude-spend

      Tell Claude Code to export telemetry to it (edits ~/.claude/settings.json):
        claude-spend setup

      Watch:   claude-spend tui
      Report:  claude-spend report
      Undo:    claude-spend setup --purge && brew services stop claude-spend
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/claude-spend --version")
    settings = testpath/"settings.json"
    system bin/"claude-spend", "setup", "--settings", settings, "--port", "4999"
    assert_match "http://127.0.0.1:4999", settings.read
  end
end
