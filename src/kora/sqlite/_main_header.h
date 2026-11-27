#ifndef KORA_SQLITE_MAIN_HEADER_H
#define KORA_SQLITE_MAIN_HEADER_H

#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

typedef enum {
  KORA_MAIN_UNCERTAIN,
  KORA_MAIN_EMPTY,
  KORA_MAIN_ORDINARY,
  KORA_MAIN_METADATA_TEST,
  KORA_MAIN_JOURNAL_TEST
} KoraMainKind;

static KoraMainKind koraMainHeader(int descriptor, const struct stat *identity) {
  unsigned char header[100];
  if (!S_ISREG(identity->st_mode)) return KORA_MAIN_UNCERTAIN;
  ssize_t size = pread(descriptor, header, sizeof(header), 0);
  if (size == 0 && identity->st_size == 0) return KORA_MAIN_EMPTY;
  if (size != sizeof(header) || memcmp(header, "SQLite format 3", 16)) {
    return KORA_MAIN_UNCERTAIN;
  }
  if (memcmp(header + 72, (unsigned char[20]){0}, 20)) return KORA_MAIN_UNCERTAIN;
  unsigned page = (unsigned)header[16] * 256 + header[17];
  if (page == 1) page = 65536;
  if (page < 512 || page > 65536 || (page & (page - 1)) != 0 ||
      identity->st_size < page || page - header[20] < 480 ||
      header[18] < 1 || header[18] > 2 || header[19] < 1 || header[19] > 2 ||
      header[21] != 64 || header[22] != 32 || header[23] != 32) {
    return KORA_MAIN_UNCERTAIN;
  }
  if (!memcmp(header + 68, "KMT1", 4)) return KORA_MAIN_METADATA_TEST;
  if (!memcmp(header + 68, "KJT1", 4)) return KORA_MAIN_JOURNAL_TEST;
  return KORA_MAIN_ORDINARY;
}

#endif
