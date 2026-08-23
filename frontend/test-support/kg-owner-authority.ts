import { sameKgIdentity, sameKgOwner, type KgWorkspaceOwner } from "../app/kg-workspace-model";
import type { KgOwnerAuthority } from "../app/use-kg-owner";

// A hand-driven stand-in for `use-kg-owner.ts`'s shared gate, so each KG domain
// owner (`use-kg-knowledge` / `use-kg-schema` / `use-kg-graph`) can be rendered
// and exercised on its own — without `Home`, and without the other two domains.
//
// It reimplements nothing interesting: `owns` / `ownsIdentity` delegate to the
// production `sameKgOwner` / `sameKgIdentity` predicates, so a test that proves
// "a late visible commit was refused" is proving it against the same comparison
// the real gate uses. What the harness adds is the ability to *move* the owner
// (`rotate` = same actor + notebook, new generation — what opening another
// notebook and coming back produces; `hide` = no owner at all).
export type TestKgAuthorityHarness = {
  authority: KgOwnerAuthority;
  /** Install a live owner and return it (generation increases each call). */
  establish: (actorId: string, notebookId: string) => KgWorkspaceOwner;
  /** Same actor + notebook, brand-new generation. */
  rotate: () => KgWorkspaceOwner;
  /** No owner at all — the "owner hidden" branch of every returned view. */
  hide: () => void;
};

export function createTestKgAuthority(): TestKgAuthorityHarness {
  let owner: KgWorkspaceOwner | null = null;
  let generation = 0;
  const tokens = new Map<string, object>();

  const authority: KgOwnerAuthority = {
    ownerVersion: 0,
    currentOwner: () => owner,
    owns: (candidate) => sameKgOwner(owner, candidate),
    ownsIdentity: (candidate) => sameKgIdentity(owner, candidate),
    beginOperation: (kind) => {
      const token = {};
      tokens.set(kind, token);
      return token;
    },
    ownsOperation: (kind, token) => tokens.get(kind) === token,
  };

  const install = (actorId: string, notebookId: string): KgWorkspaceOwner => {
    generation += 1;
    owner = { actorId, notebookId, generation, viewGeneration: generation };
    return owner;
  };

  return {
    authority,
    establish: install,
    rotate: () => {
      if (!owner) throw new Error("rotate() before establish()");
      return install(owner.actorId, owner.notebookId);
    },
    hide: () => { owner = null; },
  };
}
