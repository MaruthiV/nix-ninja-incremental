#include <stdio.h>
#include <string.h>
#include "zstd.h"

int main(void) {
    const char input[] = "incremental planning should preserve the build result";
    unsigned char compressed[ZSTD_COMPRESSBOUND(sizeof input)];
    char restored[sizeof input];
    size_t size = ZSTD_compress(compressed, sizeof compressed, input, sizeof input, 3);
    if (ZSTD_isError(size)) {
        return 1;
    }
    size_t restored_size = ZSTD_decompress(restored, sizeof restored, compressed, size);
    if (ZSTD_isError(restored_size) || restored_size != sizeof input) {
        return 2;
    }
    if (memcmp(input, restored, sizeof input) != 0) {
        return 3;
    }
    puts("zstd roundtrip ok");
    return 0;
}
