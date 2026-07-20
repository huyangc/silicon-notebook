import type { KnowhowHealthFilter } from "./knowhow-model";

export type KnowhowJumpTarget = { tableId: string; rowId: string | null };

export type KnowhowNavigation = {
  isOpen: boolean;
  healthFilter: KnowhowHealthFilter;
  jumpTarget: KnowhowJumpTarget | null;
};

export const CLOSED_KNOWHOW_NAVIGATION: KnowhowNavigation = {
  isOpen: false,
  healthFilter: "all",
  jumpTarget: null,
};

export function openKnowhowNavigation(options: {
  healthFilter?: KnowhowHealthFilter;
  jumpTarget?: KnowhowJumpTarget | null;
} = {}): KnowhowNavigation {
  return {
    isOpen: true,
    healthFilter: options.healthFilter ?? "all",
    jumpTarget: options.jumpTarget ?? null,
  };
}

export function closeKnowhowNavigation(_current?: KnowhowNavigation): KnowhowNavigation {
  return CLOSED_KNOWHOW_NAVIGATION;
}
