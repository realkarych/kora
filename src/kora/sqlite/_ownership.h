#ifndef KORA_SQLITE_OWNERSHIP_H
#define KORA_SQLITE_OWNERSHIP_H

#include <stdatomic.h>
#include <stdlib.h>
#include <sys/types.h>

typedef enum {
  KORA_FD_ABSENT,
  KORA_FD_LIVE,
  KORA_FD_CACHED,
  KORA_FD_RETAINED,
  KORA_FD_RETIRED,
  KORA_FD_UNRESOLVED
} KoraFdState;

typedef struct {
  _Atomic KoraFdState state;
  atomic_uint references;
  atomic_int unresolvedSidecar;
  int descriptor;
  dev_t device;
  ino_t inode;
} KoraFd;

typedef struct {
  KoraFd **owner;
  int (*admit)(void *, int, int);
  void *context;
} KoraOpen;

static _Thread_local KoraOpen *koraOpen;
static atomic_int koraUnresolvedIdentity;

static KoraFd *koraRetainFd(KoraFd *owner) {
  atomic_fetch_add(&owner->references, 1);
  return owner;
}

static void koraReleaseFd(KoraFd *owner) {
  if (atomic_fetch_sub(&owner->references, 1) == 1 &&
      (owner->state == KORA_FD_ABSENT || owner->state == KORA_FD_RETIRED) &&
      !owner->unresolvedSidecar) {
    free(owner);
  }
}

static void koraParkFd(KoraFd *owner) {
  if (owner->state != KORA_FD_UNRESOLVED) owner->state = KORA_FD_RETAINED;
}

#endif
