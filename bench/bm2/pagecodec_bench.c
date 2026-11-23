// pagecodec_bench: time lz4 / zstd compress + decompress of 4 KiB pieces of an InnoDB .ibd file, one core.
// The unit is what zram / zswap compress (one kernel page). All-zero pieces are skipped.
// build: gcc -O2 -o pagecodec_bench pagecodec_bench.c -llz4 -lzstd
// usage: pagecodec_bench <file.ibd> [max pages]   -> per-codec ns per page and ratio
#include <lz4.h>
#include <zstd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define P 4096
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e9 + t.tv_nsec; }

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s file [max pages]\n", argv[0]); return 1; }
    long maxp = argc > 2 ? atol(argv[2]) : 50000;
    FILE *f = fopen(argv[1], "rb"); if (!f) { perror("open"); return 1; }
    char *pages = malloc((size_t)maxp * P), zero[P] = {0}, buf[P];
    long n = 0;
    while (n < maxp && fread(buf, 1, P, f) == P) if (memcmp(buf, zero, P)) memcpy(pages + (size_t)n++ * P, buf, P);
    fclose(f);
    int cap = LZ4_compressBound(P) > (int)ZSTD_compressBound(P) ? LZ4_compressBound(P) : (int)ZSTD_compressBound(P);
    char *out = malloc((size_t)n * cap); int *len = malloc(n * sizeof(int)); char dec[P];
    printf("%ld pages of %s\n", n, argv[1]);
    for (int rep = 0; rep < 2; rep++) {   // the first pass warms caches and the CPU clock; the second is reported
        // lz4 (default = the kernel's LZ4_compress_default path)
        double t0 = now(); long tot = 0;
        for (long i = 0; i < n; i++) { len[i] = LZ4_compress_default(pages + i * P, out + i * cap, P, cap); tot += len[i] < P ? len[i] : P; }
        double t1 = now();
        for (long i = 0; i < n; i++) if (LZ4_decompress_safe(out + i * cap, dec, len[i], P) != P) { fprintf(stderr, "lz4 decode error\n"); return 1; }
        double t2 = now();
        if (rep) printf("lz4        compress %6.2f us/page  decompress %5.2f us/page  ratio %.2f\n", (t1 - t0) / n / 1e3, (t2 - t1) / n / 1e3, (double)n * P / tot);
        for (int lvl = 1; lvl <= 3; lvl += 2) {   // 3 = the kernel's default zstd level for zram and zswap
            ZSTD_CCtx *cc = ZSTD_createCCtx(); ZSTD_DCtx *dc = ZSTD_createDCtx();
            t0 = now(); tot = 0;
            for (long i = 0; i < n; i++) { len[i] = (int)ZSTD_compressCCtx(cc, out + i * cap, cap, pages + i * P, P, lvl); tot += len[i] < P ? len[i] : P; }
            t1 = now();
            for (long i = 0; i < n; i++) if (ZSTD_decompressDCtx(dc, dec, P, out + i * cap, len[i]) != P) { fprintf(stderr, "zstd decode error\n"); return 1; }
            t2 = now();
            if (rep) printf("zstd -%d    compress %6.2f us/page  decompress %5.2f us/page  ratio %.2f\n", lvl, (t1 - t0) / n / 1e3, (t2 - t1) / n / 1e3, (double)n * P / tot);
            ZSTD_freeCCtx(cc); ZSTD_freeDCtx(dc);
        }
    }
    return 0;
}
