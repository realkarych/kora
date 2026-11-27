#include "_main_header.h"

enum { KORA_SHM = -1 };
static KoraFd *koraFiles;

static int koraOpenFile(const char *path, int flags, mode_t mode) {
  int fd;
  do {
    fd = osOpen(path, flags | O_CLOEXEC | O_NONBLOCK, mode);
  } while (fd < 0 && errno == EINTR);
  return fd;
}

static int koraSameFile(const KoraFd *left, const KoraFd *right) {
  return left->device == right->device && left->inode == right->inode;
}

static void koraSetParent(KoraFd *owner, KoraFd *main) {
  if (main) koraRetainFd(main);
  if (owner->main) koraReleaseFd(owner->main);
  owner->main = main;
}

static int koraTryClose(KoraFd **link) {
  KoraFd *owner = *link;
  if (owner->state != KORA_FD_RETAINED) return 0;
  for (KoraFd *other = koraFiles; other; other = other->next) {
    if (other->role == KORA_SHM && other->state == KORA_FD_LIVE &&
        koraSameFile(owner, other)) return 0;
  }
  unixInodeInfo *main = inodeList;
  while (main && (main->fileId.dev != owner->device ||
                  main->fileId.ino != (u64)owner->inode)) main = main->pNext;
  if (main) sqlite3_mutex_enter(main->pLockMutex);
  if (!main || (!main->nLock && !main->koraUnlockUnknown)) {
    owner->state = osClose(owner->descriptor) == 0
      ? KORA_FD_RETIRED : KORA_FD_UNRESOLVED;
  }
  if (main) sqlite3_mutex_leave(main->pLockMutex);
  if (owner->state != KORA_FD_RETIRED) return 0;
  *link = owner->next;
  koraSetParent(owner, NULL);
  koraReleaseFd(owner);
  return 1;
}

static int koraRetireOwned(KoraFd *main, int closed) {
  int done = 1;
  for (KoraFd **link = &koraFiles; *link;) {
    KoraFd *owner = *link;
    if ((closed && owner == main) ||
        (owner->main == main && owner->state != KORA_FD_LIVE)) {
      if (koraTryClose(link)) continue;
      done = 0;
    }
    link = &owner->next;
  }
  return done;
}

static int koraSettleFd(KoraFd *main) {
  unixEnterMutex();
  int done = koraRetireOwned(main, 1) &&
    (main->state == KORA_FD_RETIRED || main->state == KORA_FD_ABSENT);
  unixLeaveMutex();
  return done;
}

static void koraCloseSidecar(KoraFd *owner, KoraFd *main) {
  if (owner->state == KORA_FD_LIVE) koraSetParent(owner, main);
  koraParkFd(owner);
  for (KoraFd **link = &koraFiles; *link; link = &(*link)->next) {
    if (*link == owner) {
      koraTryClose(link);
      return;
    }
  }
}

static int koraConflict(KoraFd *owner) {
  if (koraUnresolvedIdentity) return 1;
  for (KoraFd **link = &koraFiles; *link;) {
    KoraFd *other = *link;
    if (other != owner && koraSameFile(owner, other)) {
      if (koraTryClose(link)) continue;
      if (other->state == KORA_FD_RETAINED || other->state == KORA_FD_UNRESOLVED ||
          owner->role == KORA_SHM || owner->role != other->role ||
          (owner->main && !koraSameFile(owner->main, other->main))) return 1;
    }
    link = &other->next;
  }
  return 0;
}

static int koraCapture(KoraFd *owner, int fd, int role, KoraFd *main,
                       struct stat *info) {
  if (owner->state == KORA_FD_ABSENT) {
    owner->next = koraFiles;
    koraFiles = koraRetainFd(owner);
    owner->descriptor = fd;
    owner->role = role;
    koraSetParent(owner, main);
  }
  if (osFstat(fd, info) != 0) {
    owner->state = KORA_FD_UNRESOLVED;
    koraUnresolvedIdentity = 1;
    return SQLITE_CANTOPEN;
  }
  owner->device = info->st_dev;
  owner->inode = info->st_ino;
  if (!S_ISREG(info->st_mode) || koraConflict(owner)) {
    owner->state = KORA_FD_RETAINED;
    return SQLITE_CANTOPEN;
  }
  owner->state = KORA_FD_LIVE;
  return SQLITE_OK;
}

static int koraSidecarReady(KoraFd *main) {
  return main && !koraUnresolvedIdentity && koraRetireOwned(main, 0);
}

static int koraSelectSidecar(KoraFd *owner, KoraFd *main, int fd, int role, mode_t mode) {
  struct stat info;
  int result = koraCapture(owner, fd, role, main, &info);
  if (result == SQLITE_OK && mode && info.st_size == 0 &&
      (info.st_mode & 0777) != mode) osFchmod(fd, mode);
  return result;
}

static int koraSelectMain(unixFile *file, int fd) {
  UnixUnusedFd *slot = file->pPreallocatedUnused;
  KoraFd **receipt = koraOpen->owner;
  if (slot->koraOwner) {
    koraReleaseFd(*receipt);
    *receipt = koraRetainFd(slot->koraOwner);
  } else {
    slot->koraOwner = koraRetainFd(*receipt);
  }
  KoraFd *owner = *receipt;
  struct stat info;
  file->h = fd;
  unixEnterMutex();
  int result = koraCapture(owner, fd, SQLITE_OPEN_MAIN_DB, NULL, &info);
  if (result == SQLITE_OK && !koraSidecarReady(owner)) result = SQLITE_CANTOPEN;
#if defined(__APPLE__)
  if (result == SQLITE_OK && info.st_size == 0) {
    struct statfs mount;
    if (fstatfs(fd, &mount) != 0 ||
        !strncmp(mount.f_fstypename, "msdos", 5) ||
        !strncmp(mount.f_fstypename, "exfat", 5)) {
      owner->state = KORA_FD_UNRESOLVED;
      koraUnresolvedIdentity = 1;
      result = SQLITE_CANTOPEN;
    }
  }
#endif
  if (result != SQLITE_OK) koraParkFd(owner);
  unixLeaveMutex();
  if (result != SQLITE_OK) return result;
  KoraMainKind kind = koraMainHeader(fd, &info);
  result = SQLITE_NOTADB;
  if (kind == KORA_MAIN_EMPTY || kind == KORA_MAIN_ORDINARY) {
    int accepted = koraOpen->admit(koraOpen->context, fd, kind == KORA_MAIN_EMPTY);
    unixEnterMutex();
    result = accepted && !koraConflict(owner) ? SQLITE_OK : SQLITE_CANTOPEN;
    if (result != SQLITE_OK) koraParkFd(owner);
    unixLeaveMutex();
  } else {
    koraParkFd(owner);
  }
  return result;
}
