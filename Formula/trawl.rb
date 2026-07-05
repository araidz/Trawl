class Trawl < Formula
  desc "Curated, terminal-native torrent finder over aria2"
  homepage "https://github.com/araidz/Trawl"
  url "https://github.com/araidz/Trawl/archive/refs/tags/v0.2.1.tar.gz"
  sha256 "e9b07828c872cb1138e574fee1877fd3aadc7e0167e25ded68e039c8f28a26a2"
  license "MIT"

  depends_on "aria2"
  depends_on "python@3.14"

  def install
    libexec.install "trawl"
    (bin/"trawl").write <<~SH
      #!/bin/sh
      export PYTHONPATH="#{libexec}:$PYTHONPATH"
      exec "#{formula_opt_bin("python@3.14")}/python3.14" -m trawl "$@"
    SH
  end

  test do
    assert_match "terminal torrent finder", shell_output("#{bin}/trawl --help")
  end
end
