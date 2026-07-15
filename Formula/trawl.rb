class Trawl < Formula
  desc "Curated, terminal-native torrent finder over aria2"
  homepage "https://github.com/araidz/Trawl"
  url "https://github.com/araidz/Trawl/archive/refs/tags/v0.2.5.tar.gz"
  sha256 "250cfe440eb54bee6f5c7dd1cbba3dd838a7701fa546679cdd57a700ccc25f3d"
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
