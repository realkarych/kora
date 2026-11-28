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

static int koraOpenMain(const char *path, int flags, mode_t mode) {
  int fd = koraOpenFile(path, flags, mode);
  if (fd < 0 && errno == ENOENT && koraOpen->preparePath && (flags & O_RDWR)) {
    fd = koraOpenFile(koraOpen->preparePath, flags | O_CREAT | O_EXCL, 0600);
    if (fd < 0 && errno == EEXIST) fd = koraOpenFile(path, flags, mode);
  }
  return fd;
}

static const char *koraJournalName(const KoraOpen *scope) {
  if (scope->marker == KORA_MARKER_METADATA) return "main.db-journal";
  return scope->authorityJournal ? "authority.sqlite3-journal" : "control.sqlite-journal";
}

static int koraOpenJournalFile(const KoraOpen *scope, const char *path, int flags, mode_t mode) {
  if (!scope->authorityJournal && !scope->marker) return koraOpenFile(path, flags, mode);
  int fd;
  do {
    fd = openat(scope->directory, koraJournalName(scope),
                flags | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, mode);
  } while (fd < 0 && errno == EINTR);
  return fd;
}

static int koraOpenJournal(const KoraOpen *scope, const char *path, int flags, mode_t mode, int *created) {
  if (!(flags & O_CREAT)) return koraOpenJournalFile(scope, path, flags, mode);
  int fd = koraOpenJournalFile(scope, path, flags | O_EXCL, mode);
  if (fd >= 0) *created = 1;
  return fd < 0 && errno == EEXIST && !(flags & O_EXCL)
    ? koraOpenJournalFile(scope, path, flags & ~O_CREAT, mode) : fd;
}

static int koraSameFile(const KoraFd *left, const KoraFd *right) {
  return left->device == right->device && left->inode == right->inode;
}

static int koraRelated(KoraFd *released, KoraFd *waiting) {
  unixEnterMutex();
  int found = koraSameFile(released, waiting);
  for (KoraFd *fd = koraFiles; fd && !found; fd = fd->next) {
    if (fd == waiting || fd->main == waiting) {
      found = koraSameFile(released, fd) ||
        (fd->device == released->sidecarDevice && fd->inode == released->sidecarInode);
    }
  }
  unixLeaveMutex();
  return found;
}

static int koraTryClose(KoraFd **link) {
  KoraFd *owner = *link;
  if (owner->state != KORA_FD_RETAINED) return 0;
  unixInodeInfo *main = inodeList;
  while (main && (main->fileId.dev != owner->device ||
                  main->fileId.ino != (u64)owner->inode)) main = main->pNext;
  if (main) sqlite3_mutex_enter(main->pLockMutex);
  int pinned = 0;
  for (KoraFd *other = koraFiles; other && !pinned; other = other->next) {
    pinned = other != owner && other->lockPin && koraSameFile(owner, other);
  }
  if (!pinned && (!main || (!main->nLock && (!main->koraUnlockUnknown || owner->lockPin)))) {
    owner->state = osClose(owner->descriptor) == 0
      ? KORA_FD_RETIRED : KORA_FD_UNRESOLVED;
    owner->descriptor = -1;
    if (main && owner->lockPin && owner->state == KORA_FD_RETIRED) main->koraUnlockUnknown = 0;
  }
  if (main) sqlite3_mutex_leave(main->pLockMutex);
  if (owner->state != KORA_FD_RETIRED) return 0;
  *link = owner->next;
  if (owner->main) koraReleaseFd(owner->main);
  if (owner->lastUser) koraReleaseFd(owner->lastUser);
  owner->main = NULL;
  owner->lastUser = NULL;
  koraReleaseFd(owner);
  return 1;
}

static int koraConflict(KoraFd *owner, int precheck) {
  if (koraUnresolvedIdentity) return 1;
  for (KoraFd **link = &koraFiles; *link;) {
    KoraFd *other = *link;
    if (other != owner && koraSameFile(owner, other)) {
      if (!other->main && koraTryClose(link)) continue;
      if (other->state == KORA_FD_RETAINED || other->state == KORA_FD_UNRESOLVED) return 1;
      if (!precheck && (other->lockPin || owner->role == KORA_SHM ||
          owner->role != other->role ||
          (owner->main && !koraSameFile(owner->main, other->main)))) return 1;
    }
    link = &other->next;
  }
  return 0;
}

static int koraBlockedIdentity(const struct stat *hint) {
  KoraFd key = {.device = hint->st_dev, .inode = hint->st_ino};
  return koraConflict(&key, 1);
}

static int koraBlockedPath(const char *path) {
  struct stat hint;
  if (osStat(path, &hint) != 0) return 0;
  unixEnterMutex();
  int blocked = koraBlockedIdentity(&hint);
  unixLeaveMutex();
  return blocked;
}

static int koraRetireOwned(KoraFd *main, int closed) {
  int done = 1;
  for (KoraFd **link = &koraFiles; *link;) {
    KoraFd *owner = *link;
    if ((closed && (owner == main || owner->lastUser == main)) ||
        (owner->main == main && (closed || owner->state != KORA_FD_LIVE))) {
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
  if (owner->state == KORA_FD_LIVE && owner->role == KORA_SHM) {
    owner->lastUser = koraRetainFd(main);
    main->sidecarDevice = owner->device;
    main->sidecarInode = owner->inode;
  }
  koraParkFd(owner);
  for (KoraFd **link = &koraFiles; *link; link = &(*link)->next) {
    if (*link == owner) {
      koraTryClose(link);
      return;
    }
  }
}

static int koraKnownIdentity(int fd, struct stat *info) {
  if (osFstat(fd, info) != 0) return 0;
#if defined(__APPLE__)
  if (info->st_size == 0 && S_ISREG(info->st_mode)) {
    struct statfs mount;
    return fstatfs(fd, &mount) == 0 &&
      strncmp(mount.f_fstypename, "msdos", 5) &&
      strncmp(mount.f_fstypename, "exfat", 5);
  }
#endif
  return 1;
}

static void koraPinUnlock(unixFile *file, int failed) {
  file->pInode->koraUnlockUnknown = failed;
  if (file->pPreallocatedUnused && file->pPreallocatedUnused->koraOwner) {
    file->pPreallocatedUnused->koraOwner->lockPin = failed;
  }
}

static int koraCapture(KoraFd *owner, int fd, int role, KoraFd *main,
                       struct stat *info) {
  if (owner->state == KORA_FD_ABSENT) {
    owner->next = koraFiles;
    koraFiles = koraRetainFd(owner);
    owner->descriptor = fd;
    owner->role = role;
    owner->main = main ? koraRetainFd(main) : NULL;
    if (main) owner->custody = main->custody;
  }
  if (!koraKnownIdentity(fd, info)) {
    owner->state = KORA_FD_UNRESOLVED;
    koraUnresolvedIdentity = 1;
    return SQLITE_CANTOPEN;
  }
  owner->device = info->st_dev;
  owner->inode = info->st_ino;
  if (!S_ISREG(info->st_mode) || owner->lockPin || koraConflict(owner, 0)) {
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

static int koraInspectJournal(KoraOpen *scope, int empty) {
  if (!scope->journal) return SQLITE_OK;
  struct stat hint;
  if (fstatat(scope->directory, koraJournalName(scope), &hint, AT_SYMLINK_NOFOLLOW))
    return errno == ENOENT ? SQLITE_OK : SQLITE_CANTOPEN;
  unixEnterMutex();
  int blocked = empty || scope->marker || koraBlockedIdentity(&hint);
  unixLeaveMutex();
  if (blocked) return SQLITE_CANTOPEN;
  KoraFd *main = *scope->owner, *probe = koraNewFd();
  if (!probe) return SQLITE_NOMEM;
  int fd;
  do {
    fd = openat(scope->directory, koraJournalName(scope),
                O_RDWR | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  } while (fd < 0 && errno == EINTR);
  int result = SQLITE_CANTOPEN;
  if (fd >= 0) {
    unixEnterMutex();
    result = koraSelectSidecar(probe, main, fd, SQLITE_OPEN_MAIN_JOURNAL, 0);
    unixLeaveMutex();
    if (result == SQLITE_OK && !scope->admit(scope->journal, fd, -1)) result = SQLITE_CANTOPEN;
    unixEnterMutex();
    koraCloseSidecar(probe, main);
    if (probe->state != KORA_FD_RETIRED) result = SQLITE_CANTOPEN;
    unixLeaveMutex();
  }
  koraReleaseFd(probe);
  return result;
}

static int koraSelectMain(unixFile *file, int fd, const char *path) {
  UnixUnusedFd *slot = file->pPreallocatedUnused;
  KoraFd **receipt = koraOpen->owner;
  if (slot->koraOwner) {
    koraReleaseFd(*receipt);
    *receipt = koraRetainFd(slot->koraOwner);
    koraOpen->reused = 1;
  } else {
    slot->koraOwner = koraRetainFd(*receipt);
  }
  KoraFd *owner = *receipt;
  struct stat info;
  file->h = fd;
  file->koraAuthority = koraOpen;
  unixEnterMutex();
  owner->pool = 0;
  owner->custody = koraOpen->custody;
  int result = koraCapture(owner, fd, SQLITE_OPEN_MAIN_DB, NULL, &info);
  if (result == SQLITE_OK && !koraSidecarReady(owner)) result = SQLITE_CANTOPEN;
  if (result != SQLITE_OK) koraParkFd(owner);
  unixLeaveMutex();
  if (result != SQLITE_OK) return result;
  KoraMainKind kind = koraMainHeader(fd, &info);
  if (kind == KORA_MAIN_EMPTY && koraOpen->preparePath) {
    struct stat named;
    if (osStat(path, &named) || named.st_dev != info.st_dev || named.st_ino != info.st_ino)
      kind = KORA_MAIN_UNCERTAIN;
  }
  result = SQLITE_NOTADB;
  int marker = koraOpen->marker;
  int expected = marker == KORA_MARKER_JOURNAL_REOPEN ? kind == KORA_MAIN_JOURNAL_TEST
    : marker ? kind == KORA_MAIN_EMPTY
    : kind == KORA_MAIN_EMPTY || kind == KORA_MAIN_ORDINARY;
  if (expected) {
    int accepted = koraOpen->admit(koraOpen->context, fd, kind == KORA_MAIN_EMPTY);
    if (accepted) accepted = koraInspectJournal(koraOpen, kind == KORA_MAIN_EMPTY) == SQLITE_OK;
    unixEnterMutex();
    result = accepted && !koraConflict(owner, 0) ? SQLITE_OK : SQLITE_CANTOPEN;
    if (result != SQLITE_OK) koraParkFd(owner);
    unixLeaveMutex();
  } else {
    koraParkFd(owner);
  }
  return result;
}
