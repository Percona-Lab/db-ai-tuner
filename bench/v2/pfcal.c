// pfcal: what does one page fault cost? (v2 calibration, 2026-11-28)
//   first-touch : first access to fresh anonymous memory (zero-fill minor fault)
//   resident    : access to an already-mapped page that is not in the CPU caches (no fault)
//   swapped     : access to a page pushed out with MADV_PAGEOUT (served by whatever swap is configured: zram,
//                 zswap, disk); the page content comes from a real data file so the compression ratio is realistic
// Pages are touched in random order, one access per page; latencies in ns, percentiles over all pages.
// build: gcc -O2 -o pfcal pfcal.c        usage: pfcal <size MB> <content file> [rounds]
// Configure the swap device and vm.page-cluster=0 (no swap read-ahead) before running.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <fcntl.h>
#include <unistd.h>
#ifndef MADV_PAGEOUT
#define MADV_PAGEOUT 21
#endif
#define P 4096
static inline long long ns(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000000000LL + t.tv_nsec; }
static int cmp(const void *a, const void *b) { long long x = *(const long long *)a, y = *(const long long *)b; return (x > y) - (x < y); }
static long vm_swap_kb(void) {
    FILE *f = fopen("/proc/self/status", "r"); char l[256]; long v = -1;
    while (f && fgets(l, sizeof l, f)) if (!strncmp(l, "VmSwap:", 7)) v = atol(l + 7);
    if (f) fclose(f); return v;
}
static void report(const char *what, long long *t, size_t n) {
    qsort(t, n, sizeof *t, cmp); long double s = 0; for (size_t i = 0; i < n; i++) s += t[i];
    printf("{\"what\":\"%s\",\"pages\":%zu,\"mean_ns\":%.0Lf,\"p50_ns\":%lld,\"p90_ns\":%lld,\"p99_ns\":%lld,\"p999_ns\":%lld}\n",
           what, n, s / n, t[n / 2], t[n * 9 / 10], t[n * 99 / 100], t[n * 999 / 1000]);
}
int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <size MB> <content file> [rounds]\n", argv[0]); return 1; }
    size_t n = (size_t)atol(argv[1]) * 256; int rounds = argc > 3 ? atoi(argv[3]) : 3;
    long long *t = malloc(n * sizeof *t); size_t *ord = malloc(n * sizeof *ord);
    srand(42); for (size_t i = 0; i < n; i++) ord[i] = i;
    for (size_t i = n - 1; i > 0; i--) { size_t j = (size_t)rand() % (i + 1); size_t x = ord[i]; ord[i] = ord[j]; ord[j] = x; }
    // content: real pages from a data file (all-zero pages skipped), cycled
    int fd = open(argv[2], O_RDONLY); if (fd < 0) { perror("content"); return 1; }
    size_t cn = 0, cap = 65536; char *content = malloc(cap * P), zero[P] = {0}, buf[P];
    while (cn < cap && read(fd, buf, P) == P) if (memcmp(buf, zero, P)) memcpy(content + cn++ * P, buf, P);
    close(fd); if (!cn) { fprintf(stderr, "no content pages\n"); return 1; }
    volatile char sink = 0;
    for (int r = 0; r < rounds; r++) {
        char *m = mmap(NULL, n * P, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (m == MAP_FAILED) { perror("mmap"); return 1; }
        madvise(m, n * P, MADV_NOHUGEPAGE);
        for (size_t k = 0; k < n; k++) { size_t i = ord[k]; long long a = ns(); m[i * P] = 1; t[k] = ns() - a; }
        report("first-touch", t, n);
        for (size_t i = 0; i < n; i++) memcpy(m + i * P, content + (i % cn) * P, P);
        char *flush = malloc(64 << 20); memset(flush, 1, 64 << 20); free(flush);   // push the data out of the CPU caches
        for (size_t k = 0; k < n; k++) { size_t i = ord[k]; long long a = ns(); sink += m[i * P + 100]; t[k] = ns() - a; }
        report("resident", t, n);
        if (madvise(m, n * P, MADV_PAGEOUT)) { perror("MADV_PAGEOUT"); return 1; }
        // a page written to a disk swap device can stay in the swap cache while memory is plentiful, and a later fault
        // is then a minor fault from RAM; once its write has finished, a second MADV_PAGEOUT reclaims that clean copy
        sleep(5); madvise(m, n * P, MADV_PAGEOUT); sleep(1);
        long sw = vm_swap_kb();
        printf("{\"what\":\"pageout\",\"swapped_mb\":%ld,\"of_mb\":%zu}\n", sw / 1024, n / 256);
        for (size_t k = 0; k < n; k++) { size_t i = ord[k]; long long a = ns(); sink += m[i * P + 100]; t[k] = ns() - a; }
        report("swapped", t, n);
        munmap(m, n * P);
        fflush(stdout);
    }
    return sink == 42;
}
