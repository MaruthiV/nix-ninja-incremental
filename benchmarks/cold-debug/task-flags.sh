: "${NIX_BUILD_TOP:?expected a Nix build}"
: "${NIX_CFLAGS_COMPILE:=}"
for nni_flags in "${!NIX_CFLAGS_COMPILE@}"; do
  printf -v "$nni_flags" '%s -fdebug-prefix-map=%s=/build' "${!nni_flags}" "$NIX_BUILD_TOP"
  export "$nni_flags"
done
