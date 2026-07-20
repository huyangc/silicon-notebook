export type OptionalLoad<T> =
  | { ok: true; value: T }
  | { ok: false };

export type AnalyticsLoadOwner = {
  generation: number;
  notebookId: string;
};

/**
 * Keeps an analytics modal's parallel reads bound to the notebook and modal
 * instance that started them. A new open or close invalidates every older
 * owner, so a late optional result cannot repopulate a dismissed view.
 */
export class AnalyticsLoadScope {
  private generation = 0;

  begin(notebookId: string): AnalyticsLoadOwner {
    this.generation += 1;
    return { generation: this.generation, notebookId };
  }

  cancel(): void {
    this.generation += 1;
  }

  isCurrent(owner: AnalyticsLoadOwner, activeNotebookId: string | null): boolean {
    return owner.generation === this.generation && owner.notebookId === activeNotebookId;
  }
}

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
