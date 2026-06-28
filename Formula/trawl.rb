class Trawl < Formula
  desc "Curated, terminal-native torrent finder over aria2"
  homepage "https://github.com/araidz/Trawl"
  url "https://github.com/araidz/Trawl/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "85d8493e9b55d6ba62245413544052047cb758f95b823ff639890f0484aab9b0"
  license "MIT"

  depends_on "aria2"
  depends_on "python@3.14"

  def install
    libexec.install "trawl"
    (bin/"trawl").write <<~SH
      #!/bin/sh
      export PYTHONPATH="#{libexec}:$PYTHONPATH"
      exec "#{Formula["python@3.14"].opt_bin}/python3.14" -m trawl "$@"
    SH
  end

  test do
    assert_match "terminal torrent finder", shell_output("#{bin}/trawl --help")
  end
end
