---
level: change
audience: admin
title: 前端类型检查改用新命令
---
维护部署插件或自建检查脚本时请注意：`npm run lint` 现在只做代码规范检查，不再做类型检查，也不检查部署插件；类型检查请改用 `npm run typecheck`。
