#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030b0000
#include <Python.h>
#include <pythread.h>
#include <ctype.h>

#include "sqlite3.c"

typedef struct {
  sqlite3 *connection;
  PyThread_type_lock lifetime;
  KoraFd *main;
  KoraOpen open;
  PyObject *progress;
  PyObject *trace;
  PyObject *authorizer;
} NativeDb;

static const char *dbCapsuleName = "kora.sqlite.connection";

static const char *errorClass(int code) {
  switch (code & 255) {
    case SQLITE_CONSTRAINT: return "IntegrityError";
    case SQLITE_MISMATCH: return "IntegrityError";
    case SQLITE_TOOBIG: return "DataError";
    case SQLITE_RANGE: return "ProgrammingError";
    case SQLITE_MISUSE: return "ProgrammingError";
    case SQLITE_INTERNAL: return "InternalError";
    case SQLITE_CORRUPT:
    case SQLITE_NOTADB: return "DatabaseError";
    default: return "OperationalError";
  }
}

static PyObject *dbError(int code, const char *message) {
  if (PyErr_Occurred()) return NULL;
  if (code == SQLITE_NOMEM) return PyErr_NoMemory();
  PyObject *stock = PyImport_ImportModule("sqlite3");
  if (!stock) return NULL;
  PyObject *type = PyObject_GetAttrString(stock, errorClass(code));
  Py_DECREF(stock);
  if (!type) return NULL;
  PyObject *error = PyObject_CallFunction(type, "s", message);
  if (error) {
    PyObject *value = PyLong_FromLong(code);
    if (value && PyObject_SetAttrString(error, "sqlite_errorcode", value) == 0) {
      PyErr_SetObject(type, error);
    }
    Py_XDECREF(value);
    Py_DECREF(error);
  }
  Py_DECREF(type);
  return NULL;
}

static int closeDatabase(NativeDb *db) {
  int result = SQLITE_OK;
  Py_BEGIN_ALLOW_THREADS
  PyThread_acquire_lock(db->lifetime, WAIT_LOCK);
  if (db->connection) {
    result = sqlite3_close(db->connection);
    if (result == SQLITE_OK) db->connection = NULL;
  }
  PyThread_release_lock(db->lifetime);
  koraSettleFd(db->main);
  Py_END_ALLOW_THREADS
  if (result == SQLITE_OK) {
    Py_CLEAR(db->open.journal);
    Py_CLEAR(db->progress);
    Py_CLEAR(db->trace);
    Py_CLEAR(db->authorizer);
  }
  return result;
}

static void destroyDatabase(PyObject *capsule) {
  NativeDb *db = PyCapsule_GetPointer(capsule, dbCapsuleName);
  if (!db || closeDatabase(db) != SQLITE_OK) return;
  PyThread_free_lock(db->lifetime);
  koraReleaseFd(db->main);
  free(db);
}

static PyObject *allocateDatabase(PyObject *module, PyObject *unused) {
  NativeDb *db = calloc(1, sizeof(*db));
  if (!db) return PyErr_NoMemory();
  db->main = koraNewFd();
  db->lifetime = PyThread_allocate_lock();
  PyObject *capsule = db->main && db->lifetime
    ? PyCapsule_New(db, dbCapsuleName, destroyDatabase) : PyErr_NoMemory();
  if (!capsule) {
    if (db->lifetime) PyThread_free_lock(db->lifetime);
    free(db->main);
    free(db);
  }
  return capsule;
}

static int approveMain(void *context, int descriptor, int empty) {
  PyGILState_STATE gil = PyGILState_Ensure();
  PyObject *result = PyObject_CallFunction(
    context, empty < 0 ? "i" : "ii", descriptor, empty);
  int approved = result == Py_True;
  Py_XDECREF(result);
  PyGILState_Release(gil);
  return approved;
}

static PyObject *openDatabaseOwner(PyObject *module, PyObject *args) {
  PyObject *capsule, *admit, *journal;
  const char *path;
  int readOnly, timeout, directory, prepare, reuseOnly;
  unsigned long long pool;
  if (!PyArg_ParseTuple(args, "OspiOOipKp", &capsule, &path, &readOnly, &timeout,
                        &admit, &journal, &directory, &prepare, &pool, &reuseOnly)) {
    return NULL;
  }
  NativeDb *db = PyCapsule_GetPointer(capsule, dbCapsuleName);
  if (!db) return NULL;
  if (db->connection || db->main->state != KORA_FD_ABSENT || koraOpen ||
      path[0] != '/' || !PyCallable_Check(admit) ||
      (journal != Py_None && !PyCallable_Check(journal))) {
    return dbError(SQLITE_MISUSE, "SQLite main admission is unavailable");
  }
  int flags = (readOnly ? SQLITE_OPEN_READONLY : SQLITE_OPEN_READWRITE) |
              SQLITE_OPEN_FULLMUTEX | SQLITE_OPEN_EXRESCODE;
  if (journal != Py_None) flags |= SQLITE_OPEN_NOFOLLOW;
  db->open = (KoraOpen){&db->main, approveMain, admit,
                       journal == Py_None ? NULL : Py_NewRef(journal), directory,
                       prepare ? path : NULL, pool, reuseOnly, 0};
  int result;
  Py_BEGIN_ALLOW_THREADS
  koraOpen = &db->open;
  result = sqlite3_open_v2(path, &db->connection, flags, NULL);
  koraOpen = NULL;
  db->open.preparePath = NULL;
  if (result == SQLITE_OK) result = sqlite3_busy_timeout(db->connection, timeout);
  if (result == SQLITE_OK) {
    unixEnterMutex();
    db->main->pool = pool;
    unixLeaveMutex();
  }
  Py_END_ALLOW_THREADS
  if (result != SQLITE_OK) {
    closeDatabase(db);
    return dbError(result, reuseOnly && !db->open.reused && result == SQLITE_BUSY
      ? "SQLite native admission is full." : sqlite3_errstr(result));
  }
  Py_RETURN_NONE;
}

static PyObject *reusedMain(PyObject *module, PyObject *capsule) {
  NativeDb *db = PyCapsule_GetPointer(capsule, dbCapsuleName);
  return db ? PyBool_FromLong(db->open.reused) : NULL;
}

static PyObject *closeDatabaseOwner(PyObject *module, PyObject *capsule) {
  NativeDb *db = PyCapsule_GetPointer(capsule, dbCapsuleName);
  if (!db) return NULL;
  int result = closeDatabase(db);
  if (result != SQLITE_OK) return dbError(result, sqlite3_errstr(result));
  Py_RETURN_NONE;
}

static PyObject *databaseSettled(PyObject *module, PyObject *capsule) {
  NativeDb *db = PyCapsule_GetPointer(capsule, dbCapsuleName);
  if (!db) return NULL;
  int settled;
  Py_BEGIN_ALLOW_THREADS
  settled = koraSettleFd(db->main);
  Py_END_ALLOW_THREADS
  return PyBool_FromLong(settled && !db->connection);
}

static PyObject *relatedRetirement(PyObject *module, PyObject *args) {
  PyObject *source, *target;
  if (!PyArg_ParseTuple(args, "OO", &source, &target)) return NULL;
  NativeDb *released = PyCapsule_GetPointer(source, dbCapsuleName);
  NativeDb *waiting = PyCapsule_GetPointer(target, dbCapsuleName);
  if (!released || !waiting) return NULL;
  int related;
  Py_BEGIN_ALLOW_THREADS
  related = koraRelated(released->main, waiting->main);
  Py_END_ALLOW_THREADS
  return PyBool_FromLong(related);
}

static const char *statementCapsuleName = "kora.sqlite.statement";
static const char *blobCapsuleName = "kora.sqlite.blob";
static const char *closedCapsuleName = "kora.sqlite.closed";

static PyObject *newChild(void *resource, const char *name,
                         PyCapsule_Destructor destroy, PyObject *database) {
  PyObject *capsule = PyCapsule_New(resource, name, NULL);
  if (capsule && (PyCapsule_SetContext(capsule, database) != 0 ||
                  PyCapsule_SetDestructor(capsule, destroy) != 0)) {
    Py_DECREF(capsule);
    return NULL;
  }
  if (capsule) Py_INCREF(database);
  return capsule;
}

static int closeChild(PyObject *capsule, const char *name, int (*close)(void *)) {
  if (PyCapsule_IsValid(capsule, closedCapsuleName)) return SQLITE_OK;
  void *resource = PyCapsule_GetPointer(capsule, name);
  if (!resource) return SQLITE_MISUSE;
  PyObject *database = PyCapsule_GetContext(capsule);
  if (PyCapsule_SetDestructor(capsule, NULL) != 0 ||
      PyCapsule_SetName(capsule, closedCapsuleName) != 0) return SQLITE_MISUSE;
  int result;
  Py_BEGIN_ALLOW_THREADS
  result = close(resource);
  Py_END_ALLOW_THREADS
  Py_DECREF(database);
  return result;
}

static int finalizeNative(void *statement) {
  sqlite3_finalize(statement);
  return SQLITE_OK;
}

static void destroyStatement(PyObject *capsule) {
  closeChild(capsule, statementCapsuleName, finalizeNative);
}

static int bindValue(sqlite3_stmt *statement, int index, PyObject *value) {
  if (value == Py_None) return sqlite3_bind_null(statement, index);
  if (PyLong_Check(value)) {
    long long number = PyLong_AsLongLong(value);
    return PyErr_Occurred() ? SQLITE_RANGE : sqlite3_bind_int64(statement, index, number);
  }
  if (PyFloat_Check(value)) {
    return sqlite3_bind_double(statement, index, PyFloat_AsDouble(value));
  }
  if (PyUnicode_Check(value)) {
    Py_ssize_t length;
    const char *text = PyUnicode_AsUTF8AndSize(value, &length);
    return text ? sqlite3_bind_text64(statement, index, text, length, SQLITE_TRANSIENT,
                                      SQLITE_UTF8) : SQLITE_MISMATCH;
  }
  if (PyObject_CheckBuffer(value)) {
    Py_buffer view;
    if (PyObject_GetBuffer(value, &view, PyBUF_SIMPLE) != 0) return SQLITE_MISMATCH;
    int result = sqlite3_bind_blob64(statement, index, view.buf, view.len, SQLITE_TRANSIENT);
    PyBuffer_Release(&view);
    return result;
  }
  return SQLITE_MISMATCH;
}

static int bindParameters(sqlite3_stmt *statement, PyObject *parameters) {
  int count = sqlite3_bind_parameter_count(statement), named = PyDict_Check(parameters);
  if (!named && PySequence_Size(parameters) != count) {
    return SQLITE_RANGE;
  }
  for (int index = 1; index <= count; index++) {
    const char *name = sqlite3_bind_parameter_name(statement, index);
    PyObject *value = named ? (name ? PyMapping_GetItemString(parameters, name + 1) : NULL)
                            : PySequence_GetItem(parameters, index - 1);
    if (!value) return SQLITE_RANGE;
    int result = bindValue(statement, index, value);
    Py_DECREF(value);
    if (result != SQLITE_OK) return result;
  }
  return SQLITE_OK;
}

static PyObject *prepareStatement(PyObject *module, PyObject *args) {
  PyObject *database, *parameters;
  const char *sql;
  if (!PyArg_ParseTuple(args, "OsO", &database, &sql, &parameters)) return NULL;
  NativeDb *db = PyCapsule_GetPointer(database, dbCapsuleName);
  if (!db) return NULL;
  if (!db->connection) return dbError(SQLITE_MISUSE, "Cannot operate on a closed database.");
  sqlite3_stmt *statement = NULL;
  const char *tail = NULL;
  int result;
  Py_BEGIN_ALLOW_THREADS
  result = sqlite3_prepare_v2(db->connection, sql, -1, &statement, &tail);
  Py_END_ALLOW_THREADS
  if (result == SQLITE_OK) {
    while (isspace((unsigned char)*tail)) tail++;
    if (!*tail) {
      if (!statement) Py_RETURN_NONE;
      result = bindParameters(statement, parameters);
      if (result != SQLITE_OK) {
        sqlite3_finalize(statement);
        return dbError(result, "Invalid SQLite binding");
      }
      PyObject *capsule = newChild(statement, statementCapsuleName, destroyStatement, database);
      if (!capsule) sqlite3_finalize(statement);
      return capsule;
    }
    result = SQLITE_MISUSE;
  }
  sqlite3_finalize(statement);
  return dbError(result, result == SQLITE_MISUSE
    ? sqlite3_errstr(result) : sqlite3_errmsg(db->connection));
}

static PyObject *columnValue(sqlite3_stmt *statement, int index) {
  switch (sqlite3_column_type(statement, index)) {
    case SQLITE_INTEGER: return PyLong_FromLongLong(sqlite3_column_int64(statement, index));
    case SQLITE_FLOAT: return PyFloat_FromDouble(sqlite3_column_double(statement, index));
    case SQLITE_TEXT: {
      const unsigned char *text = sqlite3_column_text(statement, index);
      if (!text) return PyErr_NoMemory();
      PyObject *value = PyUnicode_DecodeUTF8((const char *)text,
        sqlite3_column_bytes(statement, index), "strict");
      if (!value && PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
        PyErr_Clear();
        return dbError(SQLITE_ERROR, "Could not decode SQLite text as UTF-8");
      }
      return value;
    }
    case SQLITE_BLOB: {
      const void *data = sqlite3_column_blob(statement, index);
      return PyBytes_FromStringAndSize(data, sqlite3_column_bytes(statement, index));
    }
    default: Py_RETURN_NONE;
  }
}

static PyObject *statementValues(PyObject *capsule, int columns) {
  sqlite3_stmt *statement = PyCapsule_GetPointer(capsule, statementCapsuleName);
  if (!statement) return NULL;
  if (!columns) {
    int result;
    Py_BEGIN_ALLOW_THREADS
    result = sqlite3_step(statement);
    Py_END_ALLOW_THREADS
    if (result == SQLITE_DONE) Py_RETURN_NONE;
    if (result != SQLITE_ROW) return dbError(result, sqlite3_errmsg(sqlite3_db_handle(statement)));
  }
  int count = sqlite3_column_count(statement);
  if (columns && !count) Py_RETURN_NONE;
  PyObject *row = PyTuple_New(count);
  if (!row) return NULL;
  for (int index = 0; index < count; index++) {
    PyObject *value = columns ? PyUnicode_FromString(sqlite3_column_name(statement, index))
                              : columnValue(statement, index);
    if (!value || PyTuple_SetItem(row, index, value) != 0) {
      Py_DECREF(row);
      return NULL;
    }
  }
  return row;
}

static PyObject *stepStatement(PyObject *module, PyObject *capsule) {
  return statementValues(capsule, 0);
}

static PyObject *statementColumns(PyObject *module, PyObject *capsule) {
  return statementValues(capsule, 1);
}

static PyObject *closeStatement(PyObject *module, PyObject *capsule) {
  int result = closeChild(capsule, statementCapsuleName, finalizeNative);
  if (result != SQLITE_OK) return dbError(result, sqlite3_errstr(result));
  Py_RETURN_NONE;
}

static NativeDb *connectedDatabase(PyObject *capsule) {
  NativeDb *db = PyCapsule_GetPointer(capsule, dbCapsuleName);
  if (db && !db->connection) {
    dbError(SQLITE_MISUSE, "Cannot operate on a closed database.");
    return NULL;
  }
  return db;
}

static PyObject *databaseState(PyObject *module, PyObject *capsule) {
  NativeDb *db = connectedDatabase(capsule);
  if (!db) return NULL;
  return Py_BuildValue("iLL", sqlite3_get_autocommit(db->connection),
    (long long)sqlite3_changes64(db->connection),
    (long long)sqlite3_last_insert_rowid(db->connection));
}

static PyObject *interruptDatabase(PyObject *module, PyObject *capsule) {
  NativeDb *db = PyCapsule_GetPointer(capsule, dbCapsuleName);
  if (!db) return NULL;
  Py_BEGIN_ALLOW_THREADS
  PyThread_acquire_lock(db->lifetime, WAIT_LOCK);
  if (db->connection) sqlite3_interrupt(db->connection);
  PyThread_release_lock(db->lifetime);
  Py_END_ALLOW_THREADS
  Py_RETURN_NONE;
}

static int callbackResult(PyObject *result, int failure) {
  long value = result ? PyLong_AsLong(result) : failure;
  Py_XDECREF(result);
  if (PyErr_Occurred() || value < INT_MIN || value > INT_MAX) {
    PyErr_Clear();
    return failure;
  }
  return (int)value;
}

static int runProgress(void *application) {
  PyGILState_STATE gil = PyGILState_Ensure();
  int result = callbackResult(PyObject_CallObject(application, NULL), 1);
  PyGILState_Release(gil);
  return result;
}

static int runTrace(unsigned flag, void *application, void *statement, void *sql) {
  PyGILState_STATE gil = PyGILState_Ensure();
  char *expanded = sqlite3_expanded_sql(statement);
  PyObject *result = PyObject_CallFunction(application, "s", expanded ? expanded : sql);
  sqlite3_free(expanded);
  Py_XDECREF(result);
  if (PyErr_Occurred()) PyErr_Clear();
  PyGILState_Release(gil);
  return 0;
}

static int runAuthorizer(void *application, int action, const char *first,
                         const char *second, const char *schema, const char *source) {
  PyGILState_STATE gil = PyGILState_Ensure();
  int result = callbackResult(PyObject_CallFunction(application, "izzzz", action,
    first, second, schema, source), SQLITE_DENY);
  PyGILState_Release(gil);
  return result;
}

static PyObject *configureCallback(PyObject *module, PyObject *args) {
  PyObject *capsule, *callable;
  int kind, interval;
  if (!PyArg_ParseTuple(args, "OiOi", &capsule, &kind, &callable, &interval)) return NULL;
  NativeDb *db = connectedDatabase(capsule);
  if (!db) return NULL;
  if (callable != Py_None && !PyCallable_Check(callable)) {
    return dbError(SQLITE_MISUSE, "Invalid SQLite callback");
  }
  void *context = callable == Py_None ? NULL : callable;
  int result = SQLITE_OK;
  PyObject **owned;
  switch (kind) {
    case 0:
      owned = &db->progress;
      sqlite3_progress_handler(db->connection, interval, context ? runProgress : NULL, context);
      break;
    case 1:
      owned = &db->trace;
      result = sqlite3_trace_v2(db->connection, context ? SQLITE_TRACE_STMT : 0,
                               context ? runTrace : NULL, context);
      break;
    case 2:
      owned = &db->authorizer;
      result = sqlite3_set_authorizer(db->connection, context ? runAuthorizer : NULL, context);
      break;
    default: return dbError(SQLITE_MISUSE, "Unknown SQLite callback");
  }
  if (result != SQLITE_OK) return dbError(result, sqlite3_errstr(result));
  PyObject *previous = *owned;
  *owned = Py_NewRef(callable);
  Py_XDECREF(previous);
  Py_RETURN_NONE;
}

static PyObject *fromSqliteValue(sqlite3_value *value) {
  switch (sqlite3_value_type(value)) {
    case SQLITE_INTEGER: return PyLong_FromLongLong(sqlite3_value_int64(value));
    case SQLITE_FLOAT: return PyFloat_FromDouble(sqlite3_value_double(value));
    case SQLITE_BLOB: return PyBytes_FromStringAndSize(sqlite3_value_blob(value),
                                                       sqlite3_value_bytes(value));
    case SQLITE_TEXT: {
      const unsigned char *text = sqlite3_value_text(value);
      return text ? PyUnicode_DecodeUTF8((const char *)text,
        sqlite3_value_bytes(value), "strict") : PyErr_NoMemory();
    }
    default: Py_RETURN_NONE;
  }
}

static void runFunction(sqlite3_context *context, int count, sqlite3_value **input) {
  PyGILState_STATE gil = PyGILState_Ensure();
  PyObject *arguments = PyTuple_New(count), *result = NULL;
  if (arguments) {
    int index;
    for (index = 0; index < count; index++) {
      PyObject *value = fromSqliteValue(input[index]);
      if (!value || PyTuple_SetItem(arguments, index, value) != 0) break;
    }
    if (index == count) result = PyObject_CallObject(sqlite3_user_data(context), arguments);
    Py_DECREF(arguments);
  }
  if (result == Py_None) {
    sqlite3_result_null(context);
  } else if (result && PyLong_Check(result)) {
    sqlite3_result_int64(context, PyLong_AsLongLong(result));
  } else {
    sqlite3_result_error(context, "SQLite function failed", -1);
  }
  Py_XDECREF(result);
  if (PyErr_Occurred()) {
    PyErr_Clear();
    sqlite3_result_error(context, "SQLite function failed", -1);
  }
  PyGILState_Release(gil);
}

static void releaseCallable(void *application) {
  PyGILState_STATE gil = PyGILState_Ensure();
  Py_DECREF((PyObject *)application);
  PyGILState_Release(gil);
}

static PyObject *registerFunction(PyObject *module, PyObject *args) {
  PyObject *capsule, *callable;
  const char *name;
  int count, deterministic;
  if (!PyArg_ParseTuple(args, "OsiOp", &capsule, &name, &count, &callable,
                        &deterministic)) return NULL;
  NativeDb *db = connectedDatabase(capsule);
  if (!db) return NULL;
  if (!PyCallable_Check(callable)) return dbError(SQLITE_MISUSE, "Invalid SQLite function");
  int flags = SQLITE_UTF8 | (deterministic ? SQLITE_DETERMINISTIC : 0);
  int result = sqlite3_create_function_v2(db->connection, name, count, flags,
     Py_NewRef(callable), runFunction, NULL, NULL, releaseCallable);
  if (result != SQLITE_OK) return dbError(result, sqlite3_errstr(result));
  Py_RETURN_NONE;
}

static int closeNativeBlob(void *blob) {
  return sqlite3_blob_close(blob);
}

static void destroyBlob(PyObject *capsule) {
  closeChild(capsule, blobCapsuleName, closeNativeBlob);
}

static PyObject *openBlob(PyObject *module, PyObject *args) {
  PyObject *database;
  const char *table, *column;
  long long row;
  if (!PyArg_ParseTuple(args, "OssL", &database, &table, &column, &row)) return NULL;
  NativeDb *db = connectedDatabase(database);
  if (!db) return NULL;
  sqlite3_blob *blob;
  int result;
  Py_BEGIN_ALLOW_THREADS
  result = sqlite3_blob_open(db->connection, "main", table, column, row, 0, &blob);
  Py_END_ALLOW_THREADS
  if (result != SQLITE_OK) return dbError(result, sqlite3_errstr(result));
  PyObject *capsule = newChild(blob, blobCapsuleName, destroyBlob, database);
  if (!capsule) sqlite3_blob_close(blob);
  return capsule;
}

static PyObject *blobLength(PyObject *module, PyObject *capsule) {
  sqlite3_blob *blob = PyCapsule_GetPointer(capsule, blobCapsuleName);
  if (!blob) return NULL;
  return PyLong_FromLong(sqlite3_blob_bytes(blob));
}

static PyObject *closeBlob(PyObject *module, PyObject *capsule) {
  int result = closeChild(capsule, blobCapsuleName, closeNativeBlob);
  if (result != SQLITE_OK) return dbError(result, sqlite3_errstr(result));
  Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
  {"allocate", allocateDatabase, METH_NOARGS, NULL},
  {"open", openDatabaseOwner, METH_VARARGS, NULL},
  {"reused_main", reusedMain, METH_O, NULL},
  {"close", closeDatabaseOwner, METH_O, NULL},
  {"settled", databaseSettled, METH_O, NULL},
  {"related", relatedRetirement, METH_VARARGS, NULL},
  {"prepare", prepareStatement, METH_VARARGS, NULL},
  {"step", stepStatement, METH_O, NULL},
  {"columns", statementColumns, METH_O, NULL},
  {"finalize", closeStatement, METH_O, NULL},
  {"state", databaseState, METH_O, NULL},
  {"interrupt", interruptDatabase, METH_O, NULL},
  {"callback", configureCallback, METH_VARARGS, NULL},
  {"function", registerFunction, METH_VARARGS, NULL},
  {"open_blob", openBlob, METH_VARARGS, NULL},
  {"blob_length", blobLength, METH_O, NULL},
  {"close_blob", closeBlob, METH_O, NULL},
  {NULL, NULL, 0, NULL}
};

static struct PyModuleDef native = {
  PyModuleDef_HEAD_INIT, "_native", NULL, -1, methods
};

PyMODINIT_FUNC PyInit__native(void) {
  PyObject *module = PyModule_Create(&native);
  if (module && PyModule_AddStringConstant(module, "sqlite_version", sqlite3_libversion()) == 0) {
    return module;
  }
  Py_XDECREF(module);
  return NULL;
}
