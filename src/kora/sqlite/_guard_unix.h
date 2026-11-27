#include "_main_header.h"

static int koraOpenMain(const char *path, int flags) {
  int descriptor;
  do {
    descriptor = osOpen(path, flags | O_CLOEXEC | O_NONBLOCK, 0);
  } while (descriptor < 0 && errno == EINTR);
  return descriptor;
}

static int koraShmPins(dev_t device, ino_t inode) {
  for (unixInodeInfo *main = inodeList; main; main = main->pNext) {
    unixShmNode *shm = main->pShmNode;
    if (shm && shm->hShm >= 0 &&
        (!shm->koraIdentityKnown ||
         (shm->koraDevice == device && shm->koraInode == inode))) return 1;
  }
  return 0;
}

static void koraSettleFd(KoraFd *owner) {
  unixEnterMutex();
  if (owner->state == KORA_FD_RETAINED &&
      !koraShmPins(owner->device, owner->inode)) {
    unixInodeInfo *main = inodeList;
    while (main && (main->fileId.dev != owner->device ||
                    main->fileId.ino != (u64)owner->inode)) main = main->pNext;
    if (main == NULL) {
      owner->state = osClose(owner->descriptor) == 0
        ? KORA_FD_RETIRED : KORA_FD_UNRESOLVED;
    }
  }
  unixLeaveMutex();
}

static int koraSelectMain(unixFile *file, int descriptor) {
  UnixUnusedFd *slot = file->pPreallocatedUnused;
  KoraFd **receipt = koraOpen->owner;
  if (slot->koraOwner) {
    koraReleaseFd(*receipt);
    *receipt = koraRetainFd(slot->koraOwner);
  } else {
    slot->koraOwner = koraRetainFd(*receipt);
  }
  KoraFd *owner = *receipt;
  file->h = descriptor;
  owner->descriptor = descriptor;
  struct stat info;
  if (osFstat(descriptor, &info) != 0) {
    owner->state = KORA_FD_UNRESOLVED;
    koraUnresolvedIdentity = 1;
    return SQLITE_CANTOPEN;
  }
#if defined(__APPLE__)
  if (info.st_size == 0) {
    struct statfs mount;
    if (fstatfs(descriptor, &mount) != 0 ||
        !strncmp(mount.f_fstypename, "msdos", 5) ||
        !strncmp(mount.f_fstypename, "exfat", 5)) {
      owner->state = KORA_FD_UNRESOLVED;
      koraUnresolvedIdentity = 1;
      return SQLITE_CANTOPEN;
    }
  }
#endif
  owner->device = info.st_dev;
  owner->inode = info.st_ino;
  owner->state = KORA_FD_LIVE;
  unixEnterMutex();
  int pinned = koraUnresolvedIdentity || koraShmPins(info.st_dev, info.st_ino);
  unixLeaveMutex();
  if (pinned) return SQLITE_CANTOPEN;
  KoraMainKind kind = koraMainHeader(descriptor, &info);
  switch (kind) {
    case KORA_MAIN_EMPTY:
    case KORA_MAIN_ORDINARY:
      return koraOpen->admit(koraOpen->context, descriptor,
                            kind == KORA_MAIN_EMPTY) ? SQLITE_OK : SQLITE_CANTOPEN;
    case KORA_MAIN_METADATA_TEST:
    case KORA_MAIN_JOURNAL_TEST:
    case KORA_MAIN_UNCERTAIN:
      return SQLITE_NOTADB;
  }
  return SQLITE_CANTOPEN;
}
