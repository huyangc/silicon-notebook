export const SOURCE_UPLOAD_MAX_MB_DEFAULT = 50;
export const SOURCE_UPLOAD_MAX_MB_MIN = 1;
export const SOURCE_UPLOAD_MAX_MB_MAX = 1024;
export const SOURCE_UPLOAD_MAX_FILES_PER_BATCH = 20;

const BYTES_PER_MIB = 1024 * 1024;

// The backend accepts at most 40 scalar fields of at most 4 KiB plus multipart
// boundaries and headers. One MiB is a named, conservative transport allowance
// above the file bytes; it is not a second user-visible file-size setting.
export const SOURCE_UPLOAD_MULTIPART_OVERHEAD_BYTES = BYTES_PER_MIB;

export function sourceUploadMaxMbFromEnvironment(value) {
  const normalized = value?.trim() || String(SOURCE_UPLOAD_MAX_MB_DEFAULT);
  if (!/^[0-9]+$/.test(normalized)) {
    throw new TypeError("SOURCE_UPLOAD_MAX_MB must be a whole number from 1 through 1024");
  }
  const parsed = Number(normalized);
  if (
    !Number.isSafeInteger(parsed)
    || parsed < SOURCE_UPLOAD_MAX_MB_MIN
    || parsed > SOURCE_UPLOAD_MAX_MB_MAX
  ) {
    throw new TypeError("SOURCE_UPLOAD_MAX_MB must be a whole number from 1 through 1024");
  }
  return parsed;
}

/** Whole-request ceiling required by Next's external-rewrite body clone.
 *
 * SOURCE_UPLOAD_MAX_MB is per file while one multipart request may contain the
 * fixed batch maximum. This transport value is therefore derived from those
 * existing rails and adds no competing per-file limit; the backend remains the
 * authoritative streaming validator.
 */
export function sourceUploadProxyBodyMaxBytes(value) {
  return (
    sourceUploadMaxMbFromEnvironment(value)
    * BYTES_PER_MIB
    * SOURCE_UPLOAD_MAX_FILES_PER_BATCH
    + SOURCE_UPLOAD_MULTIPART_OVERHEAD_BYTES
  );
}
