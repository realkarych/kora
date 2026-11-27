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

typedef struct KoraFd KoraFd;

struct KoraFd {
  _Atomic KoraFdState state;
  atomic_uint references;
  atomic_int lockPin;
  KoraFd *next;
  KoraFd *main;
  KoraFd *lastUser;
  int descriptor;
  int role;
  unsigned long long pool;
  dev_t device;
  ino_t inode;
  dev_t sidecarDevice;
  ino_t sidecarInode;
};

typedef struct {
  KoraFd **owner;
  int (*admit)(void *, int, int);
  void *context;
  PyObject *journal;
  int directory;
  const char *preparePath;
  unsigned long long pool;
  int reuseOnly;
  int reused;
} KoraOpen;

static _Thread_local KoraOpen *koraOpen;
static atomic_int koraUnresolvedIdentity;

static KoraFd *koraNewFd(void) {
  KoraFd *owner = calloc(1, sizeof(*owner));
  if (owner) {
    atomic_init(&owner->references, 1);
    atomic_init(&owner->state, KORA_FD_ABSENT);
    atomic_init(&owner->lockPin, 0);
  }
  return owner;
}

static KoraFd *koraRetainFd(KoraFd *owner) {
  atomic_fetch_add(&owner->references, 1);
  return owner;
}

static void koraReleaseFd(KoraFd *owner) {
  if (atomic_fetch_sub(&owner->references, 1) == 1) free(owner);
}

static void koraParkFd(KoraFd *owner) {
  if (owner->state == KORA_FD_LIVE || owner->state == KORA_FD_CACHED) {
    owner->state = KORA_FD_RETAINED;
  }
}

#endif
