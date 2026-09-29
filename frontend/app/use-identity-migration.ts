import { useRef, useState } from "react";

import { migrateToLegacyAccount } from "./auth.ts";
import { beginSessionHandoff, endSessionHandoff, getToken, setToken } from "./auth-session.ts";
import type { IdentityMigrationOutcome } from "./identity-migration-form";

type MigrationDeps = {
  migrate?: typeof migrateToLegacyAccount;
  reload?: () => void;
};

/**
 * 迁移旧账号的操作 owner：single-flight 归这里，关掉表单或菜单都不释放；
 * 成功只在发起时的 token 仍是当前 token 时才装新 token 并整页重载
 * （迁移后会话属于另一个 user_id，不让自动账号的界面状态混进旧账号）。
 * 在途期间登记会话交接：服务端已吊销旧会话时，后台请求的 401 不得抢先登出；
 * 失败或结果被丢弃时撤销登记，旧会话之后的 401 照常清理。
 */
export function useIdentityMigration({
  migrate = migrateToLegacyAccount,
  reload = () => window.location.reload(),
}: MigrationDeps = {}) {
  const inFlightRef = useRef(false);
  const [inFlight, setInFlight] = useState(false);

  async function run(loginName: string, password: string): Promise<IdentityMigrationOutcome> {
    if (inFlightRef.current) return "in-flight";
    inFlightRef.current = true;
    setInFlight(true);
    const sentToken = getToken();
    const handoff = beginSessionHandoff(sentToken);
    let applied = false;
    try {
      const result = await migrate(loginName, password);
      if (!sentToken || getToken() !== sentToken) return "discarded";
      setToken(result.token);
      applied = true;
      reload();
      return "applied";
    } finally {
      if (!applied) {
        endSessionHandoff(handoff);
        inFlightRef.current = false;
        setInFlight(false);
      }
    }
  }

  return { inFlight, run };
}
