export type OptionalLoad<T> =
  | { ok: true; value: T }
  | { ok: false };

async function settleOptional<T>(promise: Promise<T>): Promise<OptionalLoad<T>> {
  try {
    return { ok: true, value: await promise };
  } catch {
    return { ok: false };
  }
}

export function startAnalyticsLoads<A, I, O>(loaders: {
  analytics: () => Promise<A>;
  indexStatus: () => Promise<I>;
  contentOverview: () => Promise<O>;
}): {
  analytics: Promise<A>;
  indexStatus: Promise<OptionalLoad<I>>;
  contentOverview: Promise<OptionalLoad<O>>;
} {
  const analytics = loaders.analytics();
  const indexStatus = loaders.indexStatus();
  const contentOverview = loaders.contentOverview();
  return {
    analytics,
    indexStatus: settleOptional(indexStatus),
    contentOverview: settleOptional(contentOverview),
  };
}
